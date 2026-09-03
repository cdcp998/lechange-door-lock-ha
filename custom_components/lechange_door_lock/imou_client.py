"""LeChange (Imou) client-side cloud API client.

Implements the protocol captured from the official Android app
(com.mm.android.lc) — see API/report/乐橙登录协议与门锁API分析.md:

  POST /pcs/v1/<api>  (regional gateway from GetToken response)
  headers x-pcs-* (nonce/date/client-ua/MD5/SHA256 dual signature)

Login (before session):
  username header = "account\\" + <phone>
  key1 = md5(md5(password)).lower()
  key2 = sha256(sha256(password)).lower()

After GetToken:
  username header = "uuid\\" + <internal username>
  key1 = md5(token).lower()
  key2 = sha256(token).lower()
  + x-pcs-session-id header

All APIs share the same gateway / signing scheme.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import random
import ssl
import string
import time
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from .const import (
    API_ENTRY_HOST,
    API_PREFIX,
    APIVER,
    APP_ID,
    CA_FILE,
    CONNECT_TIMEOUT,
    MEDIA_APIVER,
    PROJECT,
    PROTO_VER,
    SUCCESS_CODES,
    AUTH_FAIL_CODES,
)

_LOGGER = logging.getLogger(__name__)


class ImouAPIError(Exception):
    """Raised when the cloud API returns a non-success code."""

    def __init__(self, code: int, desc: str = ""):
        self.code = code
        self.desc = desc
        super().__init__(f"Imou API error {code}: {desc}")


def _md5_hex_lower(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest().lower()


def _sha256_hex_lower(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest().lower()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


# 账号 AES-GCM 加密(指南 §4.2 / n40/h.s()):
#   key=SHA256("F9TtRyv7X89nM0vp2EKOjdKLFnjlrN9rENCRYPTKEY")
#   随机 12B nonce 前置 + AES-256-GCM(密文+tag),整体 Base64 —— 已实测服务端可解
_ENCRYPT_KEY = hashlib.sha256(b"F9TtRyv7X89nM0vp2EKOjdKLFnjlrN9rENCRYPTKEY").digest()


def _enc_account(account: str) -> str | None:
    """App 同款账号加密 (isEncrypt=true 时必须);无 pycryptodome 时返回 None."""
    try:
        from Crypto.Cipher import AES  # 可选依赖:仅加密场景需要
    except ImportError:
        return None
    nonce = random.randbytes(12)
    cipher = AES.new(_ENCRYPT_KEY, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(account.encode())
    return _b64(nonce + ct + tag)


def _hmac_sha256_b64(data: str, key: str) -> str:
    return _b64(hmac.new(key.encode(), data.encode(), hashlib.sha256).digest())


# 真机特征池(多品牌混合, 按终端标识确定性派生):
#   - 覆盖三星/小米/OPPO/vivo/荣耀/华为/一加/谷歌 真机型号 + 对应 Build.BRAND;
#   - terminal_id 稳定派生 → 同一安装机型恒定(每请求漂移本身即是特征),
#     不同安装分散到不同机型画像 → 避免服务端按单一 UA 特征聚类标记;
#   - clientOV(Android SDK) 取该机型现实可能的版本集合(One UI/澎湃/HyperOS 升级后混合);
#   - App 版本(clientVersion)与协议相关, 保持单一固定值不参与随机。
_DEVICE_POOL: tuple[tuple[str, str, tuple[int, ...]], ...] = (
    # (terminalModel, terminalBrand, clientOV 候选)
    ("SM-S921B", "samsung", (34, 35)),      # Galaxy S24
    ("SM-S928B", "samsung", (34, 35)),      # Galaxy S24 Ultra
    ("SM-S916B", "samsung", (34, 35)),      # Galaxy S23+
    ("SM-A556B", "samsung", (34, 35)),      # Galaxy A55
    ("SM-F731B", "samsung", (34, 35)),      # Galaxy Z Flip5
    ("23127PN0CC", "Xiaomi", (34, 35)),     # Xiaomi 14(澎湃 OS)
    ("23013RK75C", "Xiaomi", (34, 35)),     # Redmi K70E
    ("2201123G", "Xiaomi", (34,)),          # Xiaomi 12 系(停更 14)
    ("PJZ110", "OnePlus", (34, 35)),        # OnePlus 12(中国版)
    ("PJT110", "OPPO", (34, 35)),           # OPPO Find X7
    ("PHK110", "OPPO", (34, 35)),           # OPPO Find X6 Pro
    ("V2312A", "vivo", (34, 35)),           # vivo S18
    ("V2302A", "iQOO", (34, 35)),           # iQOO 12
    ("BVL-AN10", "HONOR", (34, 35)),        # 荣耀 Magic6
    ("PGT-AN10", "HONOR", (34,)),           # 荣耀 Magic5 Pro
    ("ALN-AL00", "HUAWEI", (34,)),          # HUAWEI Mate 60
    ("MNA-AL00", "HUAWEI", (34,)),          # HUAWEI P60
    ("Pixel 8 Pro", "google", (34, 35)),    # Google Pixel 8 Pro
    ("Pixel 8", "google", (34, 35)),        # Google Pixel 8
)


def _real_android_id(terminal_id: str) -> str:
    """真机安卓 terminalId = Settings.Secure ANDROID_ID(16 hex 小写)。

    DEX s7/k#g(): terminalId 源头 = android_id,
    清单里安卓真机全为 16hex 小写(18ee5ef24011a454 / f27c70bbf54c5c1e)。
    集成侧统一从持久化 terminal_id 确定性派生(同安装恒定, 平滑迁移):
      - UUID/任意串 → sha256 前 16 hex 小写
      - 已是 16hex 小写 → 原样
    """
    t = (terminal_id or "").strip()
    if t and len(t) == 16 and all(c in "0123456789abcdef" for c in t):
        return t
    return hashlib.sha256(("dsh-tid:" + (terminal_id or "")).encode()).hexdigest()[:16]


def _real_ttid(terminal_id: str) -> str:
    """真机 ttid = 首装 UUID.randomUUID() 去$ 持久化(DEVICE_TTID)。

    DEX com/lc/lib/http/util/c#d(): 随机 UUID (8-4-4-4-12, 大写带连字符),
    replaceAll('$', '') 去连字符 → 32 hex 小写存 DEVICE_TTID, 之后恒用;
    淘宝系终端追踪标识, 真机每条请求都带。集成侧派生自 terminal_id(稳定)。
    """
    t = (terminal_id or "").strip()
    digest = hashlib.sha256(("dsh-ttid:" + t).encode()).hexdigest()
    return digest[:8] + "-" + digest[8:12] + "-" + digest[12:16] + "-" + digest[16:20] + "-" + digest[20:32]


def _pick_device_profile(terminal_id: str) -> tuple[str, str, int, str]:
    """terminal_id → (model, brand, ov, darkMode), 稳定确定性(无随机数状态)。"""
    digest = hashlib.sha256(("dsh-ua:" + (terminal_id or "")).encode()).hexdigest()
    model, brand, ovs = _DEVICE_POOL[int(digest[:8], 16) % len(_DEVICE_POOL)]
    ov = ovs[int(digest[8:16], 16) % len(ovs)]
    dark = str(int(digest[16], 16) % 2)
    return model, brand, ov, dark


def _build_client_ua(terminal_id: str = "") -> str:
    """Base64(JSON) user-agent, same shape as the Android client.

    终端标识必须独立于手机 App(避免与用户手机 terminalId 冲突 → 顶号掉线),
    特征值按真机 Android App 对齐(R17/android 类型无真机校验):
      - terminalId: **16 hex 小写**(真机安卓 = Settings.Secure ANDROID_ID,
        DEX s7/k#g() 源头;清单中安卓真机全为 16hex:18ee5ef24011a454),
        由持久化 terminal_id 确定性派生(_real_android_id),同安装恒定;
        旧 UUID 时代安装平滑迁移(同一 terminal_id → 同一 16hex);
      - terminalModel/Brand/clientOV/darkMode: 从 _DEVICE_POOL 真机特征池按
        terminal_id 确定性派生(多品牌混合, 同安装恒定/跨安装分散,
        避免单一 UA 特征被服务端聚类标记);
    PC 特征(此前 PC-Client/HA-Integration-Box)会话待遇差、频繁失效
    (mobile_client.py 结论),故弃用。
    """
    tid = _real_android_id(terminal_id or str(_uuid.uuid4()).upper())
    model, brand, ov, dark = _pick_device_profile(tid)
    data = {
        "clientType": "android",
        "clientVersion": "10.2.2.0831",
        "clientOV": str(ov),
        "clientOS": "android",
        "terminalModel": model,
        "terminalId": tid,
        "appid": APP_ID,
        "project": PROJECT,
        "language": "zh-CN",
        "clientProtocolVersion": PROTO_VER,
        "timezoneOffset": "480",
        "terminalBrand": brand,
        "country": "CN",
        "darkMode": dark,
        "ttid": _real_ttid(terminal_id),
    }
    return _b64(json.dumps(data, separators=(",", ":")).encode())


def _sign_payload(
    method: str,
    uri_path: str,
    body: bytes,
    username: str,
    key1: str,
    key2: str,
    session_id: Optional[str] = None,
    apiver: str = APIVER,
    content_type: str = "application/json; charset=utf-8",
    terminal_id: str = "",
):
    """Build the x-pcs request headers for one request.

    apiver/Content-Type 分域(接口测试报告 R13):
      用户/设备域  apiver=191204, charset=utf-8
      消息域        apiver=V10.2.2, charset=UTF-8(iot.message.* / 服务端生成类)
    terminal_id: 集成固定终端标识(避免与手机 App 同终端而互相顶号)
    """
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=64))
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ua = _build_client_ua(terminal_id)
    body_md5 = _b64(hashlib.md5(body).digest())
    body_sha256 = _b64(hashlib.sha256(body).digest())

    def sign_base(digest_line: str) -> str:
        s = f"{method}\n{uri_path}\n{digest_line}\n{content_type}\n"
        s += f"x-pcs-apiver:{apiver}\nx-pcs-client-ua:{ua}\nx-pcs-date:{date}\n"
        s += f"x-pcs-nonce:{nonce}\n"
        if session_id:
            s += f"x-pcs-session-id:{session_id}\n"
        s += f"x-pcs-username:{username}\n"
        return s

    headers = {
        "x-pcs-username": username,
        "x-pcs-apiver": apiver,
        "x-pcs-nonce": nonce,
        "x-pcs-date": date,
        "x-pcs-client-ua": ua,
        "Content-Type": content_type,
        "Content-MD5": body_md5,
        "x-pcs-signature": _hmac_sha256_b64(sign_base(body_md5), key1),
        "Content-SHA256": body_sha256,
        "x-pcs-signature-sha256": _hmac_sha256_b64(sign_base(body_sha256), key2),
        "timeout": "10000",
        "User-Agent": "okhttp/4.9.2",
        "Accept-Encoding": "gzip",
    }
    if session_id:
        headers["x-pcs-session-id"] = session_id
    return headers


def _build_ssl_contexts() -> list[ssl.SSLContext]:
    """Default trust first, then add the private Dahua root CA as fallback."""
    ctx_default = ssl.create_default_context()
    try:
        ctx_dahua = ssl.create_default_context()
        ctx_dahua.load_verify_locations(cafile=CA_FILE)
        return [ctx_default, ctx_dahua]
    except (OSError, ssl.SSLError) as err:
        _LOGGER.debug("Could not load Dahua CA (%s), using default trust only", err)
        return [ctx_default]


_SSL_CONTEXTS = _build_ssl_contexts()


class ImouClient:
    """Async client for the LeChange/Imou client-side cloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str = "",
        password: str = "",
        session_id: str = "",
        token: str = "",
        internal_username: str = "",
        api_host: str = "",
        on_session_update=None,
        terminal_id: str = "",
    ):
        self._session = session
        self.username = username        # 账号(登录用 "account\" + username)
        self.password = password
        self.session_id = session_id
        self.token = token
        self.internal_username = internal_username
        self.api_host = api_host or API_ENTRY_HOST
        # 独立终端标识(与手机 App 不同 → 不互顶;集成侧由 entry.options 持久化)
        # UUID 格式(App 同款);旧 lechange-hass-* 由 coordinator 一次性迁移。
        self.terminal_id = terminal_id or str(_uuid.uuid4()).upper()

        self._model_cache: dict[str, "ModelInfo"] = {}
        self._on_session_update = on_session_update  # callable(session dict) -> None
        self._login_lock = asyncio.Lock()

    # ------------------------------------------------------------------ auth
    @property
    def logged_in(self) -> bool:
        return bool(self.session_id and self.internal_username)

    @property
    def _key1(self) -> str:
        return _md5_hex_lower(self.token) if self.token else ""

    @property
    def _key2(self) -> str:
        return _sha256_hex_lower(self.token) if self.token else ""

    async def _apply_login_response(self, account: str, data: dict) -> dict:
        """GetToken/GetTokenBySMS 成功后的统一收尾(会话/密钥切换/区域网关)."""
        d = data if isinstance(data, dict) else {}
        session_id = d.get("sessionId") or ""
        if not session_id:
            raise ImouAPIError(-1, "no sessionId in login response")
        self.session_id = session_id
        self.token = d.get("token") or ""
        self.internal_username = d.get("username") or ""
        self.api_host = (d.get("entryUrlV2") or API_ENTRY_HOST).replace(":443", "")
        self._save_session()
        _LOGGER.debug(
            "Login OK: user_id=%s host=%s session=%s",
            d.get("userId"), self.api_host, session_id,
        )
        # Best effort: full user info (mqttAk / iotEntryUrlV2 ...)
        # retry_auth=False: 避免登录成功后重认证死锁,失败仅记录
        try:
            await self.async_post(
                "user.account.Login",
                {"timezoneOffset": 480, "avatarDigestType": "SHA256"},
                retry_auth=False,
            )
        except (ImouAPIError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("user.account.Login extra call failed: %s", err)
        return {
            "session_id": session_id,
            "token": self.token,
            "username": self.internal_username,
            "user_id": d.get("userId"),
            "host": self.api_host,
            "account": account,
        }

    async def async_login(self, username: str, password: str) -> dict:
        """账号密码登录 -> {sessionId, token, username, userId, host}."""
        body = json.dumps(
            {"data": {"gpsInfo": {"latitude": 0, "longitude": 0}}},
            separators=(",", ":"),
        ).encode()
        key1 = _md5_hex_lower(_md5_hex_lower(password))
        key2 = _sha256_hex_lower(_sha256_hex_lower(password))
        headers = _sign_payload(
            "POST", API_PREFIX + "user.account.GetToken", body,
            "account\\" + username, key1, key2, terminal_id=self.terminal_id,
        )
        data = await self._http_post(self.api_host, API_PREFIX + "user.account.GetToken",
                                     body, headers)
        self.username = username
        self.password = password
        # _http_post 已解包 data 层,这里 data 即业务对象 {sessionId, token, ...}
        return await self._apply_login_response(username, data)

    # ------------------------------------------------------- 短信验证码登录
    async def async_send_sms_code(
        self,
        account: str,
        usage: str = "SMSLogin",
        area_code: str = "+86",
        country: str = "CN",
        is_encrypt: bool = False,
    ) -> dict:
        """发送短信验证码 (common.validcode.GetValidCode, 人机验证接口对接指南 §1).

        依据指南:usage 与业务一一对应(登录=Login / 短信登录=SMSLogin /
        生成临时密码=GenerateSnapkey);App 真机 isEncrypt=true(账号 AES-GCM 加密,
        见 n40/h.s()),不想处理加密可先传明文 isEncrypt:false 实测(服务端策略为准)。
        """
        return await self.async_post(
            "common.validcode.GetValidCode",
            {
                "type": "phone",
                "usage": usage,
                "account": account,
                "areaCode": area_code,
                "country": country,
                "isEncrypt": is_encrypt,
                "isUserSelected": False,
                "extraSendOptions": [],
                "accessToken": "",
            },
        )

    async def async_login_sms(self, account: str, valid_code: str, area_code: str = "") -> dict:
        """短信验证码登录 (user.account.GetTokenBySMS, App 源码取证).

        请求: {account, areaCode, validCode};响应与 GetToken 同构
        ({sessionId, token, username, entryUrlV2, newUser}) → 同一套密钥切换。
        实测(2026-09-03):未登录签名密钥不可外部派生(11010),集成短信登录
        依赖 App 获取验证码;本方法保留供上下文合规/后续复测。
        """
        body = json.dumps(
            {"data": {"account": account, "areaCode": area_code, "validCode": valid_code}},
            separators=(",", ":"),
        ).encode()
        key1 = _md5_hex_lower(_md5_hex_lower(valid_code))
        key2 = _sha256_hex_lower(_sha256_hex_lower(valid_code))
        headers = _sign_payload(
            "POST", API_PREFIX + "user.account.GetTokenBySMS", body,
            "account\\" + account, key1, key2, terminal_id=self.terminal_id,
        )
        data = await self._http_post(self.api_host, API_PREFIX + "user.account.GetTokenBySMS",
                                     body, headers)
        self.username = account
        self.password = ""  # 短信登录无密码,自动重登不可用(需重新配置)
        return await self._apply_login_response(account, data)

    async def async_check_valid_code(
        self, account: str, valid_code: str, usage: str = "SMSLogin", type_: str = "phone"
    ) -> dict:
        """校验验证码 → accessToken (common.validcode.CheckValidCode, 指南 §1.2).

        一次校验一用(用于后续单笔业务);返回 data.accessToken。
        """
        return await self.async_post(
            "common.validcode.CheckValidCode",
            {
                "type": type_,
                "usage": usage,
                "account": account,
                "validCode": valid_code,
                "isEncrypt": False,
            },
        )

    async def async_granting_credit(
        self, account: str, sms_code: str, type_: str = "grantingCredit",
        is_encrypt: bool = True,
    ) -> dict:
        """终端授权提交 (user.account.GrantingCredit, 终端绑定逻辑分析.md §一).

        正确链(2026-09-03 分析):
          GetValidCode(usage=GrantingCredit) → 短信码 → **CheckValidCode →
          {accessToken}** → GrantingCredit.validCode = **accessToken**(非短信码!)
        实测:直接拿短信码提交 → 11001 bad request(鉴权未提前校验)。
        本方法自动完成中间步:短信码 → accessToken → 授权提交。
        """
        # 1) 短信码 → accessToken(一次一用)
        checked = await self.async_check_valid_code(
            account, sms_code, usage="GrantingCredit"
        )
        access_token = (checked or {}).get("accessToken")
        if not access_token:
            raise ImouAPIError(-1, "no accessToken from CheckValidCode")
        # 2) 授权提交(账号加密,App 同款)
        enc_account = _enc_account(account) if is_encrypt else account
        if is_encrypt and enc_account is None:
            _LOGGER.warning("未安装 pycryptodome,终端授权改用明文提交(可能被服务端拒绝)")
            enc_account, is_encrypt = account, False
        return await self.async_post(
            "user.account.GrantingCredit",
            {"account": enc_account, "isEncrypt": is_encrypt, "type": type_,
             "validCode": access_token},
        )
        """短信验证码登录 (user.account.GetTokenBySMS, App 源码取证).

        请求: {account, areaCode, validCode};响应与 GetToken 同构
        ({sessionId, token, username, entryUrlV2, newUser}) → 同一套密钥切换。
        """
        body = json.dumps(
            {"data": {"account": account, "areaCode": area_code, "validCode": valid_code}},
            separators=(",", ":"),
        ).encode()
        key1 = _md5_hex_lower(_md5_hex_lower(valid_code))
        key2 = _sha256_hex_lower(_sha256_hex_lower(valid_code))
        headers = _sign_payload(
            "POST", API_PREFIX + "user.account.GetTokenBySMS", body,
            "account\\" + account, key1, key2, terminal_id=self.terminal_id,
        )
        data = await self._http_post(self.api_host, API_PREFIX + "user.account.GetTokenBySMS",
                                     body, headers)
        self.username = account
        self.password = ""  # 短信登录无密码,自动重登不可用(需重新配置)
        return await self._apply_login_response(account, data)

    async def async_ensure_session(self) -> None:
        """Re-login when the stored session is missing/expired."""
        if self.logged_in:
            return
        if not self.username or not self.password:
            raise ImouAPIError(11010, "no stored credentials")
        async with self._login_lock:
            if self.logged_in:
                return
            await self.async_login(self.username, self.password)

    def _save_session(self) -> None:
        """Notify the coordinator to persist the new session in config entry."""
        if self._on_session_update:
            self._on_session_update({
                "session_id": self.session_id,
                "token": self.token,
                "internal_username": self.internal_username,
                "host": self.api_host,
            })

    # ------------------------------------------------------------- transport
    async def _http_post(self, host: str, path: str, body: bytes, headers: dict) -> dict:
        """POST and return parsed JSON (raising ImouAPIError on bad code/status)."""
        last_err: Optional[Exception] = None
        for ctx in _SSL_CONTEXTS:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    resp = await self._session.post(
                        host + path, data=body, headers=headers, ssl=ctx
                    )
                    text = await resp.text()
                if resp.status != 200:
                    raise ImouAPIError(resp.status, text[:200])
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as err:
                    raise ImouAPIError(-1, f"bad json: {text[:200]}") from err
                code = int(data.get("code", -1) or -1)
                if code not in SUCCESS_CODES:
                    raise ImouAPIError(code, str(data.get("desc") or data.get("errorDesc") or ""))
                return data.get("data") or {}
            except (ssl.SSLCertVerificationError, aiohttp.ClientConnectorCertificateError) as err:
                last_err = err
                _LOGGER.debug("TLS handshake failed with %s: %s", host, err)
                continue  # try next trust store
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise ImouAPIError(-2, f"network: {err}") from err

        raise ImouAPIError(-3, f"TLS verification failed for {host}: {last_err}")

    async def async_post(
        self,
        api_name: str,
        payload: dict,
        retry_auth: bool = True,
        apiver: str = APIVER,
        content_type: str = "application/json; charset=utf-8",
    ) -> dict:
        """Signed POST to `/pcs/v1/{api_name}` with session headers.

        apiver/content_type 分域(R13):用户/设备域默认 191204+utf-8;
        消息域(iot.message.*/服务端生成类)调用方传 "V10.2.2"+"charset=UTF-8"。
        """
        await self.async_ensure_session()
        body = json.dumps({"data": payload}, separators=(",", ":")).encode()
        headers = _sign_payload(
            "POST", API_PREFIX + api_name, body,
            "uuid\\" + self.internal_username,
            self._key1, self._key2, self.session_id,
            apiver=apiver, content_type=content_type, terminal_id=self.terminal_id,
        )
        try:
            return await self._http_post(self.api_host, API_PREFIX + api_name, body, headers)
        except ImouAPIError as err:
            if err.code in AUTH_FAIL_CODES and retry_auth and self.username and self.password:
                _LOGGER.warning("Session invalid (%s), re-logging in...", err.code)
                await self.async_login(self.username, self.password)
                return await self.async_post(
                    api_name, payload, retry_auth=False, apiver=apiver, content_type=content_type
                )
            raise

    # 消息域常量(接口测试报告 R13)
    MSG_APIVER = "V10.2.2"
    MSG_CONTENT_TYPE = "application/json; charset=UTF-8"

    async def async_post_msg(self, api_name: str, payload: dict, retry_auth: bool = True) -> dict:
        """消息域 POST(iot.message.* 等):apiver=V10.2.2, charset=UTF-8."""
        return await self.async_post(
            api_name, payload, retry_auth=retry_auth,
            apiver=self.MSG_APIVER, content_type=self.MSG_CONTENT_TYPE,
        )

    @staticmethod
    def _normalize_device(dev: dict) -> dict:
        """Keep only useful fields from a raw device object."""
        channels = []
        stream_entry = dev.get("streamEntryAddrV3") or ""
        for ch in dev.get("channelList") or []:
            ch_stream_url = ""
            try:
                mc = ch.get("mediaConfig")
                if isinstance(mc, str):
                    mc = json.loads(mc)
                if isinstance(mc, dict):
                    ch_stream_url = mc.get("streamUrl") or ""
            except (TypeError, ValueError, json.JSONDecodeError):
                ch_stream_url = ""
            if not stream_entry and ch_stream_url:
                stream_entry = ch_stream_url
            channels.append({
                "channelId": str(ch.get("channelId", "0")),
                "channelName": ch.get("channelName") or f"通道{ch.get('channelId', 0)}",
                "productId": ch.get("productId", ""),
                "status": ch.get("status", ""),
                "functions": ch.get("functions") or [],
                "stream_url": ch_stream_url,
            })
        return {
            "deviceId": dev.get("deviceId", ""),
            "productId": dev.get("productId", ""),
            "name": dev.get("name") or dev.get("deviceModelName") or dev.get("deviceId", ""),
            "model": dev.get("deviceModel") or dev.get("deviceModelName") or "",
            "catalog": dev.get("catalog", ""),
            "subCategory": dev.get("subCategory", ""),
            "status": dev.get("status", ""),
            "lockState": dev.get("lockState", ""),
            "version": dev.get("version", ""),
            "channelNum": dev.get("channelNum", 0),
            "channels": channels,
            "properties_map": dev.get("propertiesMap") or "",
            "stream_entry": stream_entry,
        }

    async def async_get_device_info(
        self, device_id: str, product_id: str, channel_id: str = "0"
    ) -> dict:
        """单设备完整详情 (device.info.BasicInfoGet, 抓包验证)."""
        data = await self.async_post(
            "device.info.BasicInfoGet",
            {"productId": product_id, "deviceId": device_id, "channelId": channel_id},
        )
        return self._normalize_device(data)

    async def async_get_devices(self) -> list[dict]:
        """List all devices (lock devices carry catalog=SmartLock).

        Verified against the regional gateway:
          device.list.DeviceBasicInfoQueryV2 with offset/limit/transferStr.
        Fallback: device.list.BasicList (also requires offset/limit).
        """
        data = {}
        try:
            data = await self.async_post(
                "device.list.DeviceBasicInfoQueryV2",
                {"offset": 1, "limit": 50, "transferStr": "", "groupId": "",
                 "familyId": "", "needNewSecret": True},
            )
        except ImouAPIError as err:
            _LOGGER.warning("DeviceBasicInfoQueryV2 failed (%s), trying BasicList", err)
            data = await self.async_post(
                "device.list.BasicList", {"offset": 1, "limit": 50}
            )
        devices = data.get("deviceList") or []
        return [self._normalize_device(dev) for dev in devices]

    @staticmethod
    def is_lock(device: dict) -> bool:
        """True when the device is a smart door lock."""
        fields = " ".join([
            str(device.get("catalog", "")),
            str(device.get("subCategory", "")),
            str(device.get("model", "")),
        ]).lower()
        if "lock" in fields or "smart" in fields:
            return True
        return any("unlock" in (ch.get("functions") or []) for ch in device.get("channels", []))

    # ------------------------------------------------------------------ model
    async def async_get_model(self, device_id: str, product_id: str) -> "ModelInfo":
        """Fetch (and cache) the model definition: identifier<->ref, types, enums."""
        cache_key = f"{device_id}:{product_id}"
        if cache_key in self._model_cache:
            return self._model_cache[cache_key]
        data = await self.async_post(
            "iot.manager.QueryModelInfo", {"deviceId": device_id, "productId": product_id}
        )
        mj = data.get("modelJson")
        if isinstance(mj, str):
            mj = json.loads(mj)
        info = ModelInfo(mj or {})
        self._model_cache[cache_key] = info
        return info

    # ---------------------------------------------------------- state/control
    async def async_get_properties(
        self, device_id: str, product_id: str, channel_id: str = "0"
    ) -> dict[str, Any]:
        """Read all properties of one channel; returns {identifier: typed value}."""
        model = await self.async_get_model(device_id, product_id)
        data = await self.async_post(
            "iot.control.GetProperties",
            {"deviceId": device_id, "productId": product_id, "channelId": channel_id},
        )
        props = data.get("properties") or data or {}
        return model.decode_properties(props)

    async def async_set_service(
        self,
        device_id: str,
        product_id: str,
        service_name: str,
        input_data: dict,
        channel_id: Optional[str] = None,
        auth_info: Optional[dict] = None,
    ) -> dict:
        """Call iot.control.SetService; returns outputData keyed by identifier.

        线上协议(2026-09-03 抓包/实测):请求体必须用型号 ref 编码 ——
        `service` 字段为 ref 数字(remoteOpenDoor -> "26600"),inputData 键
        亦为 ref,鉴权字段名为 `client`({"authId": ...})。发送 identifier
        字符串(如 serviceName:"remoteOpenDoor")会被服务端以 11001 拒绝。
        """
        model = await self.async_get_model(device_id, product_id)
        payload = {
            "deviceId": device_id,
            "productId": product_id,
            "channelId": channel_id if channel_id not in (None, "") else "0",
            "service": model.service_ref(service_name),
            "inputData": model.encode_service_input(service_name, input_data or {}),
        }
        if auth_info:
            payload["client"] = (
                auth_info if isinstance(auth_info, dict) else {"authId": auth_info}
            )
        data = await self.async_post("iot.control.SetService", payload)
        return model.decode_outputs(data.get("outputData") or {})

    async def async_set_properties(
        self, device_id: str, product_id: str, properties: dict, channel_id: str = "0"
    ) -> dict:
        """Write properties via iot.control.SetProperties (ref-keyed).

        同 SetService:属性键必须用 ref 编码(bool 值转 1/0),identifier 键
        会被服务端拒绝(10003/11001)。
        """
        model = await self.async_get_model(device_id, product_id)
        payload = {
            "deviceId": device_id,
            "productId": product_id,
            "channelId": channel_id,
            "properties": model.encode_properties(properties),
        }
        return await self.async_post("iot.control.SetProperties", payload)

    # ------------------------------------------------------------- media
    async def async_get_transfer_stream_url(
        self,
        device_id: str,
        product_id: str,
        channel_id: str = "0",
        stream_id: str = "1",
    ) -> str:
        """云端取流地址 (things.media.GetRealTransferStreamUrl, apiver=191204).

        ★ 该请求兼具唤醒语义: 休眠设备收到后 ~5s 上线(WI-007)。
        仅主码流 streamId='1' 在中继有数据(子码流返回 SDP 后零包)。
        返回 resource(TCP:11004); TLS 端口 = 该端口 + 500(与 tls_resource 一致)。
        """
        data = await self.async_post(
            "things.media.GetRealTransferStreamUrl",
            {
                "deviceId": device_id,
                "channelId": channel_id,
                "streamId": stream_id,
                "ownerType": "base",
                "type": "RTSV1",
                "encrypt": "3",
                "owner": "",
                "design": "first",
                "skipAuth": "false",
                "assistStream": "false",
                "imageSize": 0,
                "productId": product_id,
                "audioType": 0,
                "timeLimit": False,
                "videoLimit": 0,
            },
            apiver=MEDIA_APIVER,
        )
        url = data.get("resource") or data.get("tls_resource") or ""
        if not url:
            raise ImouAPIError(-1, "no resource in GetRealTransferStreamUrl response")
        return url

    async def async_download_alarm_image(self, url: str) -> bytes:
        """下载告警抓拍图(picUrl OSS 直链, 带签名与过期时间)。"""
        async with asyncio.timeout(20):
            resp = await self._session.get(
                url, headers={"User-Agent": "okhttp/4.9.2"}
            )
            if resp.status != 200:
                raise ImouAPIError(resp.status, f"alarm image download failed: {url[:80]}")
            return await resp.read()

    # ------------------------------------------------------------------ MQTT
    async def async_get_mqtt_credentials(self) -> dict:
        """获取 MQTT 连接凭据 (client_v2/auth/get, apiver 6550).

        data: {clientId, mqttServer{sslAddr:8883,tcpAddr:1883}, username:
               "Authorization: x-pcs-signature"}
        调用方需保证已登录(本方法内部 async_ensure_session)。
        """
        await self.async_ensure_session()
        identifier = "lcbaseapp" + self.terminal_id.replace("-", "")[:16].lower()
        body = json.dumps(
            {"data": {"identifier": identifier}}, separators=(",", ":")
        ).encode()
        headers = _sign_payload(
            "POST", "/pcs/v1/client_v2/auth/get", body,
            "uuid\\" + self.internal_username,
            self._key1, self._key2, self.session_id,
            apiver="6550", terminal_id=self.terminal_id,
        )
        data = await self._http_post(
            self.api_host, "/pcs/v1/client_v2/auth/get", body, headers
        )
        data["identifier"] = identifier
        data["token"] = self.token
        data["uid"] = self.internal_username
        return data

    # ------------------------------------------- 云消息 API(设备休眠也可用)
    # 说明:临时密码相关接口均走「消息域」(apiver=V10.2.2, charset=UTF-8, 测试报告 R13),
    # 且**不使用** iot.control.SetService(CreateDeviceSnapkey)——老接口可能触发
    # 身份验证码/风控(热更分析:SetService 前置 GetValidCode/CheckValidCode)。
    # 按 R14:keyId/tempKey 由客户端生成,经 SmartLockSecretAdd 直接登记(实测 10000)。
    async def async_smart_lock_secret_list(
        self, device_id: str, product_id: str, types: int = 3
    ) -> dict:
        """临时密码分组列表 (iot.message.SmartLockSecretListV2, 抓包验证).

        types=3 → 临时密钥分组;返回 secretGroups[]。
        """
        return await self.async_post_msg(
            "iot.message.SmartLockSecretListV2",
            {"productId": product_id, "deviceId": device_id, "types": types},
        )

    async def async_smart_lock_secret_add(
        self,
        device_id: str,
        product_id: str,
        temp_key: str,
        *,
        name: str = "Home Assistant",
        number: int = -1,
        effect_days: int = 1,
        usage_period: str = "",
        key_id: Optional[int] = None,
    ) -> dict:
        """添加临时密码 (iot.message.SmartLockSecretAdd; 消息域 apiver=V10.2.2).

        R14 合规:keyId 随机、tempKey 8 位、createTime/expiredTime 必须真实 epoch 秒、
        type=3、usagePeriod 周位图(127=整周)。
        """
        import random as _random
        import time as _time

        now = _time.time()
        key_id = key_id if key_id else _random.randint(10000000, 999999999)
        payload = {
            "productId": product_id,
            "deviceId": device_id,
            "keyId": key_id,
            "type": 3,                      # 临时密钥
            "groupId": "",
            "name": name,
            "phone": "",
            "tempKey": temp_key,
            "location": "",
            "isHijackAlarm": 0,
            "attribute": 0,
            "createTime": int(now),
            "number": number,
            "effectTimes": effect_days,
            "expiredTime": int(now) + 86400 * max(effect_days, 1),
            "usagePeriod": usage_period or "127-",
        }
        return await self.async_post_msg("iot.message.SmartLockSecretAdd", payload)

    async def async_smart_lock_secret_delete(
        self,
        device_id: str,
        product_id: str,
        key_id: int,
        extra: Optional[dict] = None,
    ) -> dict:
        """删除临时密码 (iot.message.SmartLockSecretDelete; 消息域).

        R14: 删除需尽量全字段(仅 state=1 的条目真正移除)。
        """
        payload = {"productId": product_id, "deviceId": device_id, "keyId": key_id}
        if extra:
            payload.update(extra)
        return await self.async_post_msg("iot.message.SmartLockSecretDelete", payload)

    async def async_get_alarm_messages(
        self,
        device_id: str,
        product_id: str,
        channel_id: str = "0",
        count: int = 3,
        begin_alarm_id: str = "-1",
        end_alarm_id: int = -1,
    ) -> dict:
        """混合告警 (cloud.message.GetDeviceAlarmMixMessage, 抓包验证 code=10000).

        设备休眠时云侧消息照常返回;返回 data.alarms[](alarmId/labelType/refId/time/message)。
        """
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc) + timedelta(hours=8)  # 设备时区 UTC+8
        end_day = now.strftime("%Y%m%d") + "T235959"
        begin_day = (now - timedelta(weeks=4)).strftime("%Y%m%d") + "T000000"
        payload = {
            "productId": product_id,
            "deviceId": device_id,
            "channelId": channel_id,
            "beginTime": begin_day,
            "endTime": end_day,
            "beginAlarmId": begin_alarm_id,
            "beginAlarmTime": "",
            "endAlarmId": end_alarm_id,
            "count": count,
            "refreshParentSummary": False,
        }
        return await self.async_post("cloud.message.GetDeviceAlarmMixMessage", payload)


class ModelInfo:
    """Parsed model definition (services/properties, identifier<->ref maps)."""

    def __init__(self, raw: dict):
        self.raw = raw
        self._props_by_identifier: dict[str, dict] = {}
        self._props_by_ref: dict[str, dict] = {}
        for p in raw.get("properties") or []:
            ident = p.get("identifier", "")
            if not ident:
                continue
            self._props_by_identifier[ident] = p
            ref = str(p.get("ref", ""))
            if ref:
                self._props_by_ref[ref] = p
        self.services: dict[str, dict] = {}
        for s in raw.get("services") or []:
            if s.get("identifier"):
                self.services[s["identifier"]] = s

    @property
    def dirty(self) -> bool:
        return not self._props_by_identifier

    def _cast(self, p: dict, value: Any) -> Any:
        """Cast a raw property value using its declared dataType."""
        dt = (p.get("dataType") or {}).get("type", "text")
        if isinstance(value, (dict, list)) or value is None:
            return value
        text = str(value)
        if dt == "bool":
            return text in ("1", "true", "True", "TRUE", "on")
        if dt == "int":
            try:
                return int(text)
            except ValueError:
                return text
        if dt in ("enum",):
            try:
                return int(text)
            except ValueError:
                return text
        # text / array / struct: JSON-encoded strings arrive for structured types
        if dt in ("array", "struct") and text[:1] in ("{", "["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return text

    def _decode_spec(self, spec: dict, value: Any) -> Any:
        """Recursively decode a value by one property/field spec (refs -> identifiers).

        Handles both shapes found in device model definitions:
          - a property:  {"dataType": {"type": "struct", "specs": [fields]}}
          - an array item: {"type": "struct", "specs": [fields]}
        """
        dt = spec.get("dataType") or {}
        dtype = dt.get("type") or spec.get("type") or "text"
        specs = dt.get("specs", spec.get("specs"))
        if isinstance(value, str) and dtype in ("array", "struct") and value[:1] in ("{", "["):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return value
        if isinstance(value, dict):
            if isinstance(specs, list):
                ref2spec = {str(f.get("ref")): f for f in specs if f.get("ref")}
                out = {}
                for key, item in value.items():
                    field = ref2spec.get(str(key))
                    if field:
                        out[field["identifier"]] = self._decode_spec(field, item)
                    else:
                        out[str(key)] = item
                return out
            return value
        if isinstance(value, list):
            item_spec = specs.get("item") if isinstance(specs, dict) else None
            if item_spec:
                return [self._decode_spec(item_spec, item) for item in value]
            return value
        return self._cast(spec, value)

    def decode_properties(self, props: dict) -> dict[str, Any]:
        """{ref: value} -> {identifier: typed value} (recursive for struct/array)."""
        out: dict[str, Any] = {}
        for ref, value in (props or {}).items():
            p = self._props_by_ref.get(str(ref))
            if p:
                out[p["identifier"]] = self._decode_spec(p, value)
            else:
                out[str(ref)] = value
        return out

    def decode_outputs(self, outputs: dict) -> dict[str, Any]:
        """{ref: value} -> {identifier: value} for service outputs."""
        out: dict[str, Any] = {}
        svc_outs: dict[str, dict] = {}
        for s in (self.raw.get("services") or []):
            for o in s.get("outputData") or []:
                ref = str(o.get("ref", ""))
                if ref:
                    svc_outs[ref] = o
        for ref, value in (outputs or {}).items():
            o = svc_outs.get(str(ref))
            if o:
                out[o.get("identifier", str(ref))] = self._cast(o, value)
            else:
                out[str(ref)] = value
        return out

    def enum_desc(self, identifier: str, value: Any) -> str:
        """Human description for an enum property value (uses model spec)."""
        p = self._props_by_identifier.get(identifier)
        if not p:
            return ""
        for item in ((p.get("dataType") or {}).get("specs") or {}).get("list", []):
            if str(item.get("value")) == str(value):
                return item.get("desc", "")
        return ""

    # ------------------------------------------------- ref encoding (线上协议)
    def service_ref(self, identifier: str) -> str:
        """identifier -> ref number string (SetService `service` field).

        线上要求 ref 编码;找不到时回退原值(自定义服务/平台服务名)。
        """
        svc = self.services.get(identifier)
        ref = str(svc.get("ref", "")) if svc else ""
        return ref or identifier

    def _input_spec(self, service_identifier: str) -> dict[str, dict]:
        """{identifier: input-prop-spec} for one service."""
        svc = self.services.get(service_identifier) or {}
        return {
            p.get("identifier", ""): p
            for p in svc.get("inputData") or []
            if p.get("identifier")
        }

    def _encode_value(self, p: dict, value: Any) -> Any:
        """Encode one value by its spec: bool->1/0, struct/array 递归 ref 键."""
        dt = (p.get("dataType") or {})
        dtype = dt.get("type", "text")
        if dtype == "bool":
            return 1 if value in (True, 1, "1", "true", "True", "on") else 0
        if dtype == "struct":
            specs = {f.get("identifier", ""): f for f in dt.get("specs") or []
                     if f.get("identifier")}
            if isinstance(value, dict):
                return {
                    str(specs[k].get("ref", k)) if k in specs else str(k): v
                    for k, v in value.items()
                }
            return value
        if dtype == "array":
            item = dt.get("specs")
            if isinstance(item, dict) and isinstance(value, list) and value:
                return [self._encode_value(item, v) for v in value]
            return value
        return value

    def encode_service_input(self, service_identifier: str, input_data: dict) -> dict:
        """{identifier: value} -> {ref: encoded-value} for one service call."""
        spec = self._input_spec(service_identifier)
        out: dict[str, Any] = {}
        for key, value in (input_data or {}).items():
            p = spec.get(key)
            if p:
                out[str(p.get("ref", key))] = self._encode_value(p, value)
            else:
                out[str(key)] = value
        return out

    def encode_properties(self, properties: dict) -> dict:
        """{identifier: value} -> {ref: encoded-value} for SetProperties."""
        out: dict[str, Any] = {}
        for key, value in (properties or {}).items():
            p = self._props_by_identifier.get(key)
            if p:
                out[str(p.get("ref", key))] = self._encode_value(p, value)
            else:
                out[str(key)] = value
        return out
