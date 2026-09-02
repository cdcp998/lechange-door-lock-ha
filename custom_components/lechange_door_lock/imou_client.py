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


def _hmac_sha256_b64(data: str, key: str) -> str:
    return _b64(hmac.new(key.encode(), data.encode(), hashlib.sha256).digest())


def _build_client_ua() -> str:
    """Base64(JSON) user-agent, same shape as the Android client."""
    data = {
        "clientType": "android",
        "clientVersion": "10.2.2.0831",
        "clientOV": "15",
        "clientOS": "android",
        "terminalModel": "HA-Integration",
        "terminalId": "lechange-" + str(int(time.time())),
        "appid": APP_ID,
        "project": PROJECT,
        "language": "zh-CN",
        "clientProtocolVersion": PROTO_VER,
        "timezoneOffset": "480",
        "terminalBrand": "Custom",
        "country": "CN",
        "darkMode": "0",
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
):
    """Build the x-pcs request headers for one request."""
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=64))
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ua = _build_client_ua()
    body_md5 = _b64(hashlib.md5(body).digest())
    body_sha256 = _b64(hashlib.sha256(body).digest())

    def sign_base(digest_line: str) -> str:
        s = f"{method}\n{uri_path}\n{digest_line}\napplication/json; charset=utf-8\n"
        s += f"x-pcs-apiver:{APIVER}\nx-pcs-client-ua:{ua}\nx-pcs-date:{date}\n"
        s += f"x-pcs-nonce:{nonce}\n"
        if session_id:
            s += f"x-pcs-session-id:{session_id}\n"
        s += f"x-pcs-username:{username}\n"
        return s

    headers = {
        "x-pcs-username": username,
        "x-pcs-apiver": APIVER,
        "x-pcs-nonce": nonce,
        "x-pcs-date": date,
        "x-pcs-client-ua": ua,
        "Content-Type": "application/json; charset=utf-8",
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
    ):
        self._session = session
        self.username = username        # 账号(登录用 "account\" + username)
        self.password = password
        self.session_id = session_id
        self.token = token
        self.internal_username = internal_username
        self.api_host = api_host or API_ENTRY_HOST

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
            "account\\" + username, key1, key2,
        )
        data = await self._http_post(self.api_host, API_PREFIX + "user.account.GetToken",
                                     body, headers)
        self.username = username
        self.password = password
        # _http_post 已解包 data 层,这里 data 即业务对象 {sessionId, token, ...}
        return await self._apply_login_response(username, data)

    # ------------------------------------------------------- 短信验证码登录
    async def async_send_sms_code(self, account: str, area_code: str = "") -> dict:
        """发送短信验证码 (common.validcode.GetValidCode, App 源码取证).

        依据: q40/c.java → type=AccountType.type("phone"), usage=Usage.name
              (登录/绑定场景 usage 枚举名,登录取 "ChangeAccount")。
        """
        return await self.async_post(
            "common.validcode.GetValidCode",
            {"account": account, "areaCode": area_code, "type": "phone",
             "usage": "ChangeAccount"},
        )

    async def async_login_sms(self, account: str, valid_code: str, area_code: str = "") -> dict:
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
            "account\\" + account, key1, key2,
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

    async def async_post(self, api_name: str, payload: dict, retry_auth: bool = True) -> dict:
        """Signed POST to `/pcs/v1/{api_name}` with session headers."""
        await self.async_ensure_session()
        body = json.dumps({"data": payload}, separators=(",", ":")).encode()
        headers = _sign_payload(
            "POST", API_PREFIX + api_name, body,
            "uuid\\" + self.internal_username,
            self._key1, self._key2, self.session_id,
        )
        try:
            return await self._http_post(self.api_host, API_PREFIX + api_name, body, headers)
        except ImouAPIError as err:
            if err.code in AUTH_FAIL_CODES and retry_auth and self.username and self.password:
                _LOGGER.warning("Session invalid (%s), re-logging in...", err.code)
                await self.async_login(self.username, self.password)
                return await self.async_post(api_name, payload, retry_auth=False)
            raise

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
        """Call iot.control.SetService; returns outputData keyed by identifier."""
        model = await self.async_get_model(device_id, product_id)
        payload = {
            "deviceId": device_id,
            "productId": product_id,
            "serviceName": service_name,
            "inputData": input_data or {},
        }
        if channel_id not in (None, "", "0"):
            payload["channelId"] = channel_id
        if auth_info:
            payload["authInfo"] = auth_info
        data = await self.async_post("iot.control.SetService", payload)
        return model.decode_outputs(data.get("outputData") or {})

    async def async_set_properties(
        self, device_id: str, product_id: str, properties: dict, channel_id: str = "0"
    ) -> dict:
        """Write properties via iot.control.SetProperties (identifier -> value)."""
        payload = {"deviceId": device_id, "productId": product_id, "properties": properties}
        if channel_id not in (None, ""):
            payload["channelId"] = channel_id
        return await self.async_post("iot.control.SetProperties", payload)

    # ------------------------------------------- 云消息 API(设备休眠也可用)
    async def async_smart_lock_secret_list(
        self, device_id: str, product_id: str, types: int = 3
    ) -> dict:
        """临时密码分组列表 (iot.message.SmartLockSecretListV2, 抓包验证).

        types=3 → 临时密钥分组;返回 secretGroups[]。
        """
        return await self.async_post(
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
        key_id: int = 0,
    ) -> dict:
        """添加临时密码 (iot.message.SmartLockSecretAdd, 抓包验证 code=10000).

        tempKey 由调用方生成;usagePeriod 如 '127-20260903T0000Z-20260904T2359Z'。
        """
        import time as _time

        now = _time.time()
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
        return await self.async_post("iot.message.SmartLockSecretAdd", payload)

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
