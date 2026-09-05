"""Config flow for the LeChange (Imou) door lock integration."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Optional

import aiohttp
import voluptuous as vol
import homeassistant.helpers.config_validation as cv
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
# NoURLAvailableError 定义在 helpers/network(与 get_url 同模块)。
# 旧版 HA 曾在 homeassistant.exceptions 挂别名, 新版(2026.x)已从 exceptions
# 移除 → 从 exceptions 导入会在加载 config_flow 时 ImportError,
# 前端表现为 "Invalid handler specified"。helpers.network 两个时代均可用。
try:
    from homeassistant.helpers.network import NoURLAvailableError, get_url
except ImportError:  # 极旧版本兜底
    from homeassistant.exceptions import NoURLAvailableError  # type: ignore[attr-defined,no-redef]
    from homeassistant.helpers.network import get_url

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_SESSION_ID,
    CONF_TOKEN,
    CONF_INTERNAL_USERNAME,
    CONF_USER_ID,
    CONF_API_HOST,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_PRODUCT_ID,
    CONF_MODEL_NAME,
    CONF_FIRMWARE_VERSION,
    CONF_CHANNEL_JSON,
    CONF_LOCK_STATE,
    CONF_STREAM_ENTRY,
    CONF_RTSP_HOST,
    CONF_RTSP_PORT,
    CONF_RTSP_USERNAME,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_URL,
    CONF_RTSP_SUBTYPE,
    CONF_SECURITY_CODE,
    CONF_DEVICE_PASSWORD,
    CONF_CAMERA_AUTO_IMAGE,
    CONF_SNAPSHOT_MIN_INTERVAL,
    CONF_SNAPSHOT_OSD,
    CONF_SNAPSHOT_OSD_ALPHA,
    CONF_SNAPSHOT_STREAM_ID,
    CONF_CHANNEL_HOSTS,
    CONF_STREAM_PREVIEW_OSD,
    CONF_STREAM_PREVIEW_SECONDS,
    DEFAULT_CAMERA_AUTO_IMAGE,
    DEFAULT_SNAPSHOT_MIN_INTERVAL,
    DEFAULT_SNAPSHOT_OSD,
    DEFAULT_SNAPSHOT_OSD_ALPHA,
    DEFAULT_SNAPSHOT_STREAM_ID,
    DEFAULT_STREAM_PREVIEW_OSD,
    DEFAULT_STREAM_PREVIEW_SECONDS,
    NEED_CREDIT_CODES,
    OEM_AK,
    OEM_SK,
)
from .imou_client import ImouAPIError, ImouClient

_LOGGER = logging.getLogger(__name__)

LOGIN_METHOD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required("login_method", default="password"): vol.In(["password", "sms"]),
    }
)

PASSWORD_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): cv.string})
SMS_CODE_SCHEMA = vol.Schema({vol.Required("valid_code"): cv.string})
CREDIT_CODE_SCHEMA = vol.Schema({vol.Required("valid_code"): cv.string})


async def http_login(username: str, password: str, session_id: str = "",
                     terminal_id: str = "") -> dict:
    """Password login via the client-side API; returns session dict.

    传自有持久 sid(有登录史 → GetToken 直通); 首次为空。
    """
    async with aiohttp.ClientSession() as session:
        client = ImouClient(session, session_id=session_id,
                            terminal_id=terminal_id)
        data = await client.async_login(username, password)
        data["username_input"] = username
        data["password_input"] = password
        data["sid_used"] = client.session_id
        return data


async def http_login_sms(username: str, valid_code: str, session_id: str = "",
                         terminal_id: str = "") -> dict:
    """SMS verification-code login via GetTokenBySMS (default 前缀 AK 身份).

    username 必须传入: GetTokenBySMS body 的 account 字段由
    _enc_account(client.username) 派生 — 漏传会把空串加密当账号,
    服务端 11001 bad request。
    """
    async with aiohttp.ClientSession() as session:
        client = ImouClient(session, username=username, session_id=session_id,
                            terminal_id=terminal_id)
        data = await client.async_get_token_by_sms_ak(valid_code)
        data["username_input"] = username
        data["password_input"] = ""
        data["sid_used"] = client.session_id
        return data


async def http_send_code(username: str, session_id: str = "", usage: str = "SMSLogin",
                         terminal_id: str = "") -> bool:
    """Send SMS code (default 前缀 AK 身份; GT4 通过后调用).

    账号风险态下 usage=Login 的短信被服务端静默丢弃(响应 10000 但不发);
    SMSLogin 通路必须先过 GT4(CheckGeeTest4)。
    """
    async with aiohttp.ClientSession() as session:
        client = ImouClient(session, username=username, session_id=session_id,
                            terminal_id=terminal_id)
        try:
            await client.async_send_sms_code_gt4(usage=usage)
            return True
        except (ImouAPIError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("GetValidCode failed: %s", err)
            return False


async def http_send_credit_code(username: str, session_id: str = "",
                                terminal_id: str = "") -> bool:
    """发送终端授权验证码(12112 链①: usage=GrantingCredit).

    真机协议: 登录前调用, default 前缀 AK 身份 + SK 单哈希,
    account AES-GCM 加密, areaCode 为空串。
    """
    async with aiohttp.ClientSession() as session:
        client = ImouClient(session, username=username, session_id=session_id,
                            terminal_id=terminal_id)
        try:
            await client.async_send_credit_code_gt4(usage="GrantingCredit")
            return True
        except (ImouAPIError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("GetValidCode(GrantingCredit) failed: %s", err)
            return False


async def http_submit_credit(username: str, valid_code: str, session_id: str = "",
                             terminal_id: str = "") -> bool:
    """提交终端授权(12112 链②: GrantingCredit 原码直传).

    失败时抛 ImouAPIError/网络异常, 由调用方区分展示。
    """
    async with aiohttp.ClientSession() as session:
        client = ImouClient(session, username=username, session_id=session_id,
                            terminal_id=terminal_id)
        await client.async_granting_credit_ak(valid_code)
        return True


async def http_list_devices(username: str, password: str, session_data: dict,
                            terminal_id: str = "") -> list[dict]:
    """Login (reuse session) and list devices.

    login_data 里内部账号键为 "username"(_apply_login_response 统一返回形态),
    兼容历史 "internal_username"; 两者皆缺(极端:短信登录后重登失败但本地仍有
    旧会话)时回退 "" —— 客户端签名退化为 logged_in=False → async_ensure_session
    自动重新登录, 不能让 KeyError 冒泡成流程"未知错误"。
    """
    async with aiohttp.ClientSession() as session:
        client = ImouClient(
            session,
            username=username,
            password=password,
            session_id=session_data.get("session_id"),
            token=session_data.get("token"),
            internal_username=(
                session_data.get("internal_username")
                or session_data.get("username")
                or ""
            ),
            api_host=session_data.get("host"),
            terminal_id=terminal_id,
        )
        return await client.async_get_devices()


def _login_error(err: ImouAPIError) -> str:
    """Map login error codes to a config-flow error key."""
    if err.code == -4:
        return "no_devices"
    if err.code in (-2, -3):
        return "network"
    if err.code == 12114:
        return "gt4_required"        # 走本地 GT4 滑块流程(不再指向 App)
    if err.code in (11006, 11007, 11012, 12000, 2033, 2036):
        return "captcha_needed"
    if err.code in (2026, 2032, 2016):
        return "sms_needed"
    if err.code in (2011, 2015, 3036):
        return "invalid_auth"
    return "invalid_auth"


class LeChangeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Account + password -> device selection.

    GT4 本地验证: 密码/短信登录遇 12114(风险态 GT4)时, 流程自动进入
    async_step_gt4 —— 本地起监听器 + 生成滑块页(用户浏览器打开, 手动滑块),
    四元组回传 → CheckGeeTest4(default 前缀 AK 身份) → 自动重发短信/重试登录 → 继续。
    """

    VERSION = 2

    def __init__(self) -> None:
        super().__init__()
        self._login_data: dict = {}
        self._devices: list[dict] = []
        self._username: str = ""
        self._password: str = ""
        self._session_id: str = ""          # 自有持久 sid(有登录史即直通)
        self._gt4_usage: str = "SMSLogin"
        self._gt4_done: asyncio.Event | None = None
        self._gt4_token: str = ""
        self._gt4_error: str = ""
        self._sms_already_sent: bool = False
        self._credit_code_sent: bool = False
        self._reauth_entry = None           # reauth 源 entry(重新授权现有安装)
        # 流程级终端标识: 同一流程所有请求共用(UA terminalId 恒定,
        # 对齐真机整链同 UA 形态; 避免每请求漂移特征)
        self._terminal_id: str = uuid.uuid4().hex

    async def async_step_reauth(self, entry_data) -> FlowResult:
        """重新认证: 运行时登录被终端管理拦截(12112) → 重新授权。

        ★ 复用 coordinator 所用的同一 terminal_id(entry.data 授权时终端 /
        兼容旧 entry.options) — 授权清单按 UA terminalId 记忆, 换终端必须重新
        短信授权;复用后 coordinator 无缝续用, 实体与历史全部保留。
        """
        entry_id = (self.context or {}).get("entry_id")
        self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_failed")
        self._username = str(self._reauth_entry.data.get(CONF_USERNAME, "") or "")
        # 运行时终端标识(与授权链/运行时一致);缺失时走安装级持久化解析
        self._terminal_id = (
            str(self._reauth_entry.data.get("terminal_id") or "")
            or str((self._reauth_entry.options or {}).get("terminal_id") or "")
        )
        if not self._terminal_id:
            from .terminal_store import get_or_create_terminal_id

            self._terminal_id = await get_or_create_terminal_id(
                self.hass, self._username
            )
        # 复用运行时 sid(已具登录史, GetToken/授权链共用)
        self._session_id = str(self._reauth_entry.data.get(CONF_SESSION_ID, "") or "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: Optional[dict] = None) -> FlowResult:
        """重输密码 → 12112 时进入终端授权码流程(复用 async_step_credit)."""
        errors = {}
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            try:
                login_data = await http_login(
                    self._username, self._password,
                    session_id=self._session_id, terminal_id=self._terminal_id,
                )
                return await self._after_login(login_data)
            except ImouAPIError as err:
                _LOGGER.error("Reauth login failed: %s", err)
                if err.code == 12114:
                    return await self.async_step_gt4()
                if err.code in NEED_CREDIT_CODES:
                    return await self.async_step_credit()
                errors["base"] = _login_error(err)
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.error("Reauth login network error: %s", err)
                errors["base"] = "network"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected reauth login error: %s", err)
                errors["base"] = "unknown"
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=PASSWORD_SCHEMA, errors=errors,
            description_placeholders={"username": self._username},
        )

    async def async_step_user(self, user_input: Optional[dict] = None) -> FlowResult:
        """选择登录方式:密码或短信验证码."""
        errors = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            # ★ 安装级持久终端标识: 已授权环境重添加/重装不再重复短信授权
            from .terminal_store import get_or_create_terminal_id

            self._terminal_id = await get_or_create_terminal_id(
                self.hass, self._username
            )
            if user_input["login_method"] == "sms":
                return await self.async_step_sms()
            return await self.async_step_password()
        return self.async_show_form(step_id="user", data_schema=LOGIN_METHOD_SCHEMA, errors=errors)

    async def async_step_password(self, user_input: Optional[dict] = None) -> FlowResult:
        """账号密码登录(12114→GT4 滑块; 12112→终端授权码流程)."""
        errors = {}
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            try:
                login_data = await http_login(
                    self._username, self._password,
                    session_id=self._session_id, terminal_id=self._terminal_id,
                )
                return await self._after_login(login_data)
            except ImouAPIError as err:
                _LOGGER.error("Login failed: %s", err)
                if err.code == 12114:
                    # 本地 GT4 滑块流程接管
                    return await self.async_step_gt4()
                if err.code in NEED_CREDIT_CODES:
                    # 终端管理开启: 新终端首次登录被拦截 → 授权码流程接管
                    return await self.async_step_credit()
                errors["base"] = _login_error(err)
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.error("Login network error: %s", err)
                errors["base"] = "network"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected login error: %s", err)
                errors["base"] = "unknown"
        return self.async_show_form(
            step_id="password", data_schema=PASSWORD_SCHEMA, errors=errors,
            description_placeholders={"username": self._username},
        )

    async def async_step_sms(self, user_input: Optional[dict] = None) -> FlowResult:
        """短信验证码登录:验证码从本地 GT4 滑块流程获取(风险态自动走 GT4)."""
        errors = {}
        if user_input is None:
            if not self._sms_already_sent:
                sent = await http_send_code(
                    self._username, session_id=self._session_id,
                    terminal_id=self._terminal_id,
                )
                if not sent:
                    # 风险态: 需要 GT4 → 引导滑块后自动重发
                    return await self.async_step_gt4()
            return self.async_show_form(
                step_id="sms", data_schema=SMS_CODE_SCHEMA,
                description_placeholders={"username": self._username},
            )
        try:
            login_data = await http_login_sms(
                self._username, user_input["valid_code"],
                session_id=self._session_id, terminal_id=self._terminal_id,
            )
            return await self._after_login(login_data)
        except ImouAPIError as err:
            _LOGGER.error("SMS login failed: %s", err)
            if err.code == 12114:
                return await self.async_step_gt4()
            if err.code in NEED_CREDIT_CODES:
                return await self.async_step_credit()
            errors["base"] = _login_error(err)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("SMS login network error: %s", err)
            errors["base"] = "network"
        return self.async_show_form(
            step_id="sms", data_schema=SMS_CODE_SCHEMA, errors=errors,
            description_placeholders={"username": self._username},
        )

    # ------------------------------------------------- 终端管理授权(12112)
    async def async_step_credit(self, user_input: Optional[dict] = None) -> FlowResult:
        """终端管理拦截(12112)授权流程 — 真机协议链(04-终端绑定与账号安全.md §5):

        ① GetValidCode(usage=GrantingCredit, AK 身份, 登录前) → 用户收短信码
        ② GrantingCredit(验证码原码直传, type=phone) → 本终端入授权清单
        ③ GetTokenBySMS(同一码复用, 服务端不消费原码) → 登录成功
        之后该终端密码 GetToken 不再被 12112 拦截。
        """
        errors = {}
        if user_input is None:
            if not self._credit_code_sent:
                if not OEM_AK or not OEM_SK:
                    return self.async_abort(reason="credit_unavailable")
                # 真机形态(grant_full_chain 复刻): 匿名会话本地生成并持久,
                # 授权链三步 + 登录共用同一 sid → sid 获得登录史(EVERGREEN 前提)
                if not self._session_id:
                    self._session_id = uuid.uuid4().hex
                sent = await http_send_credit_code(
                    self._username, session_id=self._session_id,
                    terminal_id=self._terminal_id,
                )
                if not sent:
                    return self.async_abort(reason="credit_send_failed")
                self._credit_code_sent = True
            return self.async_show_form(
                step_id="credit", data_schema=CREDIT_CODE_SCHEMA,
                description_placeholders={"username": self._username},
            )
        valid_code = str(user_input["valid_code"]).strip()
        try:
            await http_submit_credit(
                self._username, valid_code, session_id=self._session_id,
                terminal_id=self._terminal_id,
            )
        except ImouAPIError as err:
            _LOGGER.error("Terminal authorize (GrantingCredit) failed: %s", err)
            errors["base"] = "credit_failed"
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("Terminal authorize network error: %s", err)
            errors["base"] = "network"
        else:
            # 授权成功 → 同一验证码复用登录(服务端不消费原码)
            try:
                login_data = await http_login_sms(
                    self._username, valid_code, session_id=self._session_id,
                    terminal_id=self._terminal_id,
                )
                if self._password:
                    # 密码步骤已通过服务端校验(12112 是终端拦截而非密码错误)
                    # → 回填密码, 持久化后 EVERGREEN 自主续期可用
                    login_data["password_input"] = self._password
                return await self._after_login(login_data)
            except ImouAPIError as err:
                _LOGGER.error("Credit-chain SMS login failed: %s", err)
                # 授权链内的登录失败(11001/2011/...)统一报 credit_failed —
                # 兜底 invalid_auth 会误报为"账号或密码错误"
                errors["base"] = "credit_failed"
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.error("Credit-chain SMS login network error: %s", err)
                errors["base"] = "network"
        return self.async_show_form(
            step_id="credit", data_schema=CREDIT_CODE_SCHEMA, errors=errors,
            description_placeholders={"username": self._username},
        )

    def _gt4_slider_url(self, path: str) -> str:
        """解析完整可点击的滑块页 URL(外网 → 内网 → 云备份 → 任意可用)。

        相对路径在 HA 前端不可点击; 用户需要完整 URL(手机/另一台电脑打开)。
        全部不可用时回退相对路径(同源仍可用)。
        """
        for kwargs in (
            {"allow_cloud": False, "prefer_external": True},
            {"allow_cloud": False, "prefer_external": False},
            {"allow_cloud": True, "prefer_external": True},
            {},
        ):
            try:
                return get_url(self.hass, path=path, **kwargs)
            except NoURLAvailableError:
                continue
        return path

    # ------------------------------------------------------------- GT4
    async def async_step_gt4(self, user_input: Optional[dict] = None) -> FlowResult:
        """本地 GT4 滑块流程: HA view 渲染滑块页(8123端口, 容器零配置), 用户滑块后自动接力.

        自动接力链(全部无需用户离开 HA):
          CheckGeeTest4(default 前缀 AK 身份) → GetValidCode(SMSLogin) → 短信到手机
          → 用户输码(async_step_sms) → GetTokenBySMS → Login → 设备选择

        两阶段: ① 显示滑块页 URL 表单 → 用户在浏览器完成滑块, 四元组回传后
        CheckGeeTest4 + 重发短信(回调 _on_gt4_tuple 设置 _gt4_done);
        ② 用户点提交 → 等待 _gt4_done(通常已就绪) → 回到短信输码。
        """
        # 新版 HA: 纯 config-flow 集成首配前不调用 async_setup →
        # hass.data[DOMAIN] 可能不存在, 惰性幂等注册视图
        from .gt4_helper import ensure_gt4_views_registered

        ensure_gt4_views_registered(self.hass)
        listener = self.hass.data[DOMAIN].get("gt4_listener")
        if listener is None:
            return self.async_abort(reason="gt4_unavailable")
        if user_input is not None:
            # ② 等待滑块+短信完成(回调设置 _gt4_done); 超时 abort; 成功回到短信输入
            try:
                if self._gt4_done is not None:
                    await asyncio.wait_for(self._gt4_done.wait(), timeout=300)
            except asyncio.TimeoutError:
                listener.clear_callback(self._gt4_token)
                return self.async_abort(reason="gt4_timeout")
            if self._gt4_error:
                listener.clear_callback(self._gt4_token)
                return self.async_abort(reason="gt4_failed")
            # _on_gt4_tuple 已重发短信 → 直接展示输码表单, 不再重复发送
            self._sms_already_sent = True
            return self.async_show_form(
                step_id="sms", data_schema=SMS_CODE_SCHEMA,
                description_placeholders={"username": self._username},
            )
        # ① 一次性挑战令牌: 页面注入 + 回传校验 + 按 flow 路由(并发多流程互不覆盖)
        self._gt4_token = f"{self.flow_id}-{uuid.uuid4().hex[:16]}"
        listener.set_callback(self._gt4_token, self._on_gt4_tuple)
        # 生成滑块页(相对路径回传 — 反代/HTTPS 自动跟随)
        listener.html_for(
            account_label=self._username,
            verify_token="",            # 空串: CheckGeeTest4 接受空串
            usage=self._gt4_usage,
            endpoint="/api/lechange/gt4/tuple",
            token=self._gt4_token,
        )
        self._gt4_done = asyncio.Event()
        self._gt4_error = ""
        self._sms_already_sent = False
        slider_path = f"/api/lechange/gt4/slides?token={self._gt4_token}"
        return self.async_show_form(
            step_id="gt4",
            description_placeholders={
                "url": slider_path,
                # 完整可点击链接(HA 前端 description 支持 Markdown 渲染)
                "url_link": f"[{slider_path}]({self._gt4_slider_url(slider_path)})",
            },
        )

    async def _on_gt4_tuple(self, t: dict) -> None:
        """四元组回传 → CheckGeeTest4 → 重发短信 → 唤醒流程等待."""
        try:
            async with aiohttp.ClientSession() as session:
                client = ImouClient(
                    session, username=self._username,
                    session_id=self._session_id or None,
                    terminal_id=self._terminal_id,
                )
                # CheckGeeTest4(default 前缀 AK 身份 + SK 单哈希)
                await client.async_check_geetest4(
                    lot_number=t["lot_number"],
                    captcha_output=t["captcha_output"],
                    pass_token=t["pass_token"],
                    gen_time=t["gen_time"],
                    usage=self._gt4_usage,
                )
                # 重发短信(同 usage)
                await client.async_send_sms_code_gt4(usage=self._gt4_usage)
            self._gt4_error = ""
        except ImouAPIError as err:
            _LOGGER.error("GT4 relay failed: %s", err)
            self._gt4_error = str(err)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("GT4 relay network error: %s", err)
            self._gt4_error = f"network: {err}"
        finally:
            if self._gt4_done is not None:
                self._gt4_done.set()

    async def _after_login(self, login_data: dict) -> FlowResult:
        """登录成功 → 持久化自有 sid → 拉取设备列表 → 设备选择."""
        # 记住自有 sid(有登录史 → 后续密码 GetToken 永远直通)
        self._session_id = login_data.get("session_id") or self._session_id
        # ★ 会话扇出: 同账号已有运行时(其它设备条目在跑) → 把刚验证成功的
        #   会话推给共享 client。否则新条目建好后, 旧条目/新条目各自持旧
        #   会话, 重登互踢单活跃 token(10001) → 条目间数据干扰。
        try:
            from .account_runtime import apply_fresh_login

            apply_fresh_login(
                self.hass,
                login_data.get("username_input") or self._username,
                login_data,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Applying fresh login to account runtime failed", exc_info=True)
        devices = await http_list_devices(
            login_data["username_input"], login_data.get("password_input", ""),
            login_data, terminal_id=self._terminal_id,
        )
        devices.sort(key=lambda d: (not ImouClient.is_lock(d), d["name"]))
        if not devices:
            raise ImouAPIError(-4, "no devices")
        self._login_data = login_data
        self._devices = devices
        # 清理 GT4 挑战回调(单次挑战已消费; 防泄漏/重放)
        # hass.data[DOMAIN] 可能不存在(新版 HA 纯 config-flow 首配前
        # 不调用 async_setup) → 防御式读取, 不能 KeyError 收场
        listener = (self.hass.data.get(DOMAIN) or {}).get("gt4_listener")
        if listener is not None and self._gt4_token:
            listener.clear_callback(self._gt4_token)
        if self._reauth_entry is not None:
            # 重新认证: 不走设备选择, 原位更新现有 entry(实体/历史保留)
            login = login_data or {}
            data = {
                **(self._reauth_entry.data or {}),
                CONF_USERNAME: login.get("username_input", self._username),
                CONF_PASSWORD: login.get("password_input", self._password),
                CONF_SESSION_ID: login.get("session_id", ""),
                CONF_TOKEN: login.get("token", ""),
                CONF_INTERNAL_USERNAME: (
                    login.get("internal_username")
                    or login.get("username")
                    or ""
                ),
                CONF_API_HOST: login.get("host", self._reauth_entry.data.get(CONF_API_HOST, "")),
                # 终端标识与本次授权链一致 → coordinator 无缝续用
                "terminal_id": self._terminal_id,
            }
            self.hass.config_entries.async_update_entry(
                self._reauth_entry, data=data,
                options={**self._reauth_entry.options, "terminal_id": self._terminal_id},
            )
            # 同步安装级存储(删集成重装后同账号复用同一已授权终端)
            # ★ 登录/授权成功 → 指纹立即落盘(force: 用户亲自授权的终端
            #   必须覆盖 stale 旧指纹, 否则删重装后又生成新终端 → +1 循环)
            try:
                from .terminal_store import save_terminal_id

                await save_terminal_id(
                    self.hass, self._username, self._terminal_id, force=True
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Persisting terminal fingerprint failed", exc_info=True)
            _LOGGER.info(
                "Reauth completed for entry %s (terminal %s)",
                self._reauth_entry.entry_id, self._terminal_id,
            )
            return self.async_abort(reason="reauth_successful")
        return await self.async_step_device()

    async def async_step_device(self, user_input: Optional[dict] = None) -> FlowResult:
        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]
            device = next(
                (d for d in self._devices if d["deviceId"] == device_id), None
            )
            if device is None:
                return self.async_abort(reason="device_not_found")
            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()
            security_code = str(user_input.get(CONF_SECURITY_CODE, "")).strip()
            device_password = str(user_input.get(CONF_DEVICE_PASSWORD, "")).strip()
            login = self._login_data or {}
            # 登录数据缺键不再抛 KeyError(会以 HA "未知错误"收场): 全部回退空值,
            # 由 setup 阶段 EVERGREEN 自动续期链补全会话。
            data = {
                CONF_USERNAME: login.get("username_input", self._username),
                CONF_PASSWORD: login.get("password_input", self._password),
                CONF_SESSION_ID: login.get("session_id", ""),
                CONF_TOKEN: login.get("token", ""),
                # _apply_login_response 统一返回键为 "username";
                # 兼容历史 "internal_username"。
                CONF_INTERNAL_USERNAME: (
                    login.get("internal_username")
                    or login.get("username")
                    or ""
                ),
                CONF_USER_ID: login.get("user_id"),
                CONF_API_HOST: login.get("host", ""),
                CONF_DEVICE_ID: device_id,
                CONF_DEVICE_NAME: device.get("name") or device_id,
                CONF_PRODUCT_ID: device.get("productId", ""),
                CONF_MODEL_NAME: device.get("model", ""),
                CONF_FIRMWARE_VERSION: device.get("version", ""),
                CONF_CHANNEL_JSON: json.dumps(
                    device.get("channels", []), ensure_ascii=False
                ),
                CONF_LOCK_STATE: device.get("lockState", ""),
                CONF_STREAM_ENTRY: device.get("stream_entry", ""),
                # 设备密码体系(两套密码, 同一 KDF 两个代次):
                # 安全码=出厂代(告警图解密); 设备密码=当前代(流帧解密),
                # 未修改过设备密码时与安全码相同 → 留空回退用安全码。
                CONF_SECURITY_CODE: security_code,
                CONF_DEVICE_PASSWORD: device_password or security_code,
            }
            try:
                entry_result = self.async_create_entry(
                    title=device.get("name") or device_id,
                    data=data,
                    # 授权链绑定的终端标识必须与运行时一致 —
                    # coordinator 复用同一 terminal_id(UA terminalId 相同),
                    # 否则服务端把运行时视为"新终端" → 重登 12112/12001 循环
                    options={"terminal_id": self._terminal_id},
                )
            except (TypeError, ValueError) as err:
                _LOGGER.exception("Device step data build failed: %s", err)
                return self.async_abort(reason="device_not_found")
            # 登录成功 → 终端指纹落盘(force: 授权链亲测有效, 覆盖 stale)
            try:
                from .terminal_store import save_terminal_id

                await save_terminal_id(
                    self.hass, self._username, self._terminal_id, force=True
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Persisting terminal fingerprint failed", exc_info=True)
            return entry_result
        devices_dict = {
            d["deviceId"]: f"{d['name']} ({d['deviceId']}){'' if ImouClient.is_lock(d) else ' ·非门锁'}"
            for d in self._devices
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): vol.In(devices_dict),
                # 机身标签二维码/条码旁的 8 位大写字母数字串; 选填 —
                # 仅告警截图解密/WSSE 需要, 留空可后补(集成"配置"里可编辑)
                vol.Optional(CONF_SECURITY_CODE, default=""): cv.string,
                # App"修改设备密码"后的当前值; 未改过则留空(=安全码)
                vol.Optional(CONF_DEVICE_PASSWORD, default=""): cv.string,
            }
        )
        return self.async_show_form(step_id="device", data_schema=schema)

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> "LeChangeOptionsFlowHandler":
        return LeChangeOptionsFlowHandler(config_entry)


def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Old OpenAPI-based entries cannot be migrated automatically."""
    return False


class LeChangeOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow: 设备密码体系 + 媒体/RTSP + 节流设置。"""

    def __init__(self, config_entry) -> None:
        super().__init__()
        self._entry = config_entry

    async def async_step_init(self, user_input: Optional[dict] = None) -> FlowResult:
        options = self._entry.options or {}
        entry_data = self._entry.data or {}
        # ★ 表单预填用"有效值视图": options 覆盖 data(与 media._cred 读取
        #   一致)。此前只读 entry.data —— 选项里存过的安全码第二次打开
        #   显示为空, 用户以为没保存。
        effective = {**entry_data, **options}
        schema = vol.Schema(
            {
                # --- 设备密码体系 ---
                # ★ 安全码输入框已移除(出厂凭据, 仅初始配置设置; 显式
                #   清空/改写走 set_credentials 服务), 保存时原样保留旧值。
                vol.Optional(
                    CONF_DEVICE_PASSWORD,
                    description={"suggested_value": effective.get(CONF_DEVICE_PASSWORD, "")},
                ): cv.string,
                # --- 摄像头自动取图总开关(云端取流+局域网 CGI 都会让设备
                #     保持活跃耗电; 关=摄像头实体零自动取图, 手动不受限) ---
                vol.Optional(
                    CONF_CAMERA_AUTO_IMAGE,
                    default=bool(options.get(CONF_CAMERA_AUTO_IMAGE, DEFAULT_CAMERA_AUTO_IMAGE)),
                ): cv.boolean,
                vol.Optional(
                    CONF_SNAPSHOT_MIN_INTERVAL,
                    default=int(options.get(CONF_SNAPSHOT_MIN_INTERVAL, DEFAULT_SNAPSHOT_MIN_INTERVAL)),
                ): vol.All(vol.Coerce(int), vol.Range(min=15, max=3600)),
                # --- 码流偏好(默认主码流; 子码流中继无数据时自动回退主码流) ---
                vol.Optional(
                    CONF_SNAPSHOT_STREAM_ID,
                    default=str(options.get(CONF_SNAPSHOT_STREAM_ID, DEFAULT_SNAPSHOT_STREAM_ID)),
                ): vol.In({"1": "主码流(推荐)", "2": "子码流(实测中继无数据,自动回退)"}),
                # --- OSD 叠加层(门外截图: 时间戳+通道名; 默认不添加=干净截图) ---
                vol.Optional(
                    CONF_SNAPSHOT_OSD,
                    default=bool(options.get(CONF_SNAPSHOT_OSD, DEFAULT_SNAPSHOT_OSD)),
                ): cv.boolean,
                vol.Optional(
                    CONF_SNAPSHOT_OSD_ALPHA,
                    default=int(options.get(CONF_SNAPSHOT_OSD_ALPHA, DEFAULT_SNAPSHOT_OSD_ALPHA)),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
                # --- 实时预览 OSD(独立开关; 默认开, 可设置不添加) ---
                vol.Optional(
                    CONF_STREAM_PREVIEW_OSD,
                    default=bool(
                        options.get(CONF_STREAM_PREVIEW_OSD, DEFAULT_STREAM_PREVIEW_OSD)
                    ),
                ): cv.boolean,
                vol.Optional(
                    CONF_STREAM_PREVIEW_SECONDS,
                    default=int(
                        options.get(CONF_STREAM_PREVIEW_SECONDS, DEFAULT_STREAM_PREVIEW_SECONDS)
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=3, max=60)),
                # --- 多通道布局/通道选择: 已移到设备页 select 实体(唯一入口,
                #     动态读 entry.options, 避免双入口显示不同步) ---
                # --- 本地通道地址(可选; 每行 通道号=局域网地址, 设备在网时优先) ---
                vol.Optional(
                    CONF_CHANNEL_HOSTS,
                    default=str(options.get(CONF_CHANNEL_HOSTS, "")),
                ): cv.string,
                # --- 局域网 RTSP(可选覆盖) ---
                vol.Optional(CONF_RTSP_URL, default=options.get(CONF_RTSP_URL, "")): cv.string,
                vol.Optional(CONF_RTSP_HOST, default=options.get(CONF_RTSP_HOST, "")): cv.string,
                vol.Optional(CONF_RTSP_PORT, default=int(options.get(CONF_RTSP_PORT, 554))): int,
                vol.Optional(
                    CONF_RTSP_USERNAME, default=options.get(CONF_RTSP_USERNAME, "admin")
                ): cv.string,
                vol.Optional(
                    CONF_RTSP_PASSWORD, default=options.get(CONF_RTSP_PASSWORD, "")
                ): cv.string,
                vol.Optional(
                    CONF_RTSP_SUBTYPE, default=int(options.get(CONF_RTSP_SUBTYPE, 0))
                ): vol.In([0, 1]),
            }
        )
        if user_input is not None:
            # ★ 设备密码: 留空 = 保持原值(回填合并视图旧值; 键缺失/空串
            #   均回填)。[回滚记录] 曾短暂改为"留空=清空", 导致用户误清空
            #   设备密码 → 流帧解密回退出厂安全码 → 截图损坏。设备密码是
            #   功能性凭据(App 修改设备密码后的当前代), 表单不提供清空;
            #   验证/重置走 lechange_door_lock.set_credentials 服务。
            if not str(user_input.get(CONF_DEVICE_PASSWORD, "")).strip():
                old = str(effective.get(CONF_DEVICE_PASSWORD, "")).strip()
                if old:
                    user_input[CONF_DEVICE_PASSWORD] = old
                else:
                    user_input.pop(CONF_DEVICE_PASSWORD, None)
            # ★ 安全码不在表单(已移除) → 原样保留有效旧值(防整体替换丢失)
            if CONF_SECURITY_CODE not in user_input:
                old_sc = str(effective.get(CONF_SECURITY_CODE, "")).strip()
                if old_sc:
                    user_input[CONF_SECURITY_CODE] = old_sc
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="init", data_schema=schema)
