"""Config flow for the LeChange (Imou) door lock integration."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

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
    CONF_SNAPSHOT_MIN_INTERVAL,
    CONF_SNAPSHOT_OSD,
    CONF_SNAPSHOT_OSD_ALPHA,
    CONF_SNAPSHOT_STREAM_ID,
    CONF_SNAPSHOT_LAYOUT,
    CONF_SNAPSHOT_CHANNELS,
    CONF_CHANNEL_HOSTS,
    CONF_STREAM_PREVIEW_OSD,
    CONF_STREAM_PREVIEW_SECONDS,
    DEFAULT_SNAPSHOT_MIN_INTERVAL,
    DEFAULT_SNAPSHOT_OSD,
    DEFAULT_SNAPSHOT_OSD_ALPHA,
    DEFAULT_SNAPSHOT_STREAM_ID,
    DEFAULT_SNAPSHOT_LAYOUT,
    DEFAULT_SNAPSHOT_CHANNELS,
    DEFAULT_STREAM_PREVIEW_OSD,
    DEFAULT_STREAM_PREVIEW_SECONDS,
    GT4_LISTEN_PORT,
)
from .gt4_helper import GT4TupleListener, parse_12114, parse_verify_token
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


async def http_login(username: str, password: str, session_id: str = "") -> dict:
    """Password login via the client-side API; returns session dict.

    传自有持久 sid(有登录史 → GetToken 直通); 首次为空。
    """
    async with aiohttp.ClientSession() as session:
        client = ImouClient(session, session_id=session_id)
        data = await client.async_login(username, password)
        data["username_input"] = username
        data["password_input"] = password
        data["sid_used"] = client.session_id
        return data


async def http_login_sms(username: str, valid_code: str, session_id: str = "") -> dict:
    """SMS verification-code login via GetTokenBySMS (default 前缀 AK 身份)."""
    async with aiohttp.ClientSession() as session:
        client = ImouClient(session, session_id=session_id)
        data = await client.async_get_token_by_sms_ak(valid_code)
        data["username_input"] = username
        data["password_input"] = ""
        data["sid_used"] = client.session_id
        return data


async def http_send_code(username: str, session_id: str = "", usage: str = "SMSLogin") -> bool:
    """Send SMS code (default 前缀 AK 身份; GT4 通过后调用).

    账号风险态下 usage=Login 的短信被服务端静默丢弃(响应 10000 但不发);
    SMSLogin 通路必须先过 GT4(CheckGeeTest4)。
    """
    async with aiohttp.ClientSession() as session:
        client = ImouClient(session, username=username, session_id=session_id)
        try:
            await client.async_send_sms_code_gt4(usage=usage)
            return True
        except (ImouAPIError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("GetValidCode failed: %s", err)
            return False


async def http_list_devices(username: str, password: str, session_data: dict) -> list[dict]:
    """Login (reuse session) and list devices."""
    async with aiohttp.ClientSession() as session:
        client = ImouClient(
            session,
            username=username,
            password=password,
            session_id=session_data.get("session_id"),
            token=session_data.get("token"),
            internal_username=session_data.get("internal_username"),
            api_host=session_data.get("host"),
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
        self._login_data: dict = {}
        self._devices: list[dict] = []
        self._username: str = ""
        self._password: str = ""
        self._session_id: str = ""          # 自有持久 sid(有登录史即直通)
        self._gt4_listener: Optional[GT4TupleListener] = None
        self._gt4_usage: str = "SMSLogin"
        self._gt4_done: asyncio.Event | None = None
        self._gt4_error: str = ""

    async def async_step_user(self, user_input: Optional[dict] = None) -> FlowResult:
        """选择登录方式:密码或短信验证码."""
        errors = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            if user_input["login_method"] == "sms":
                return await self.async_step_sms()
            return await self.async_step_password()
        return self.async_show_form(step_id="user", data_schema=LOGIN_METHOD_SCHEMA, errors=errors)

    async def async_step_password(self, user_input: Optional[dict] = None) -> FlowResult:
        """账号密码登录(12114 时自动进入本地 GT4 滑块流程)."""
        errors = {}
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            try:
                login_data = await http_login(
                    self._username, self._password, session_id=self._session_id
                )
                return await self._after_login(login_data)
            except ImouAPIError as err:
                _LOGGER.error("Login failed: %s", err)
                if err.code == 12114:
                    # 本地 GT4 滑块流程接管
                    return await self.async_step_gt4()
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
            sent = await http_send_code(self._username, session_id=self._session_id)
            if not sent:
                # 风险态: 需要 GT4 → 引导滑块后自动重发
                return await self.async_step_gt4()
            return self.async_show_form(
                step_id="sms", data_schema=SMS_CODE_SCHEMA,
                description_placeholders={"username": self._username},
            )
        try:
            login_data = await http_login_sms(
                self._username, user_input["valid_code"], session_id=self._session_id
            )
            return await self._after_login(login_data)
        except ImouAPIError as err:
            _LOGGER.error("SMS login failed: %s", err)
            if err.code == 12114:
                return await self.async_step_gt4()
            errors["base"] = _login_error(err)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("SMS login network error: %s", err)
            errors["base"] = "network"
        return self.async_show_form(
            step_id="sms", data_schema=SMS_CODE_SCHEMA, errors=errors,
            description_placeholders={"username": self._username},
        )

    # ------------------------------------------------------------- GT4
    async def async_step_gt4(self, user_input: Optional[dict] = None) -> FlowResult:
        """本地 GT4 滑块流程: HA view 渲染滑块页(8123端口, 容器零配置), 用户滑块后自动接力.

        自动接力链(全部无需用户离开 HA):
          CheckGeeTest4(default 前缀 AK 身份) → GetValidCode(SMSLogin) → 短信到手机
          → 用户输码(async_step_sms) → GetTokenBySMS → Login → 设备选择

        部署: view 已在 async_setup 注册到 HA 自身端口(8123), 浏览器访问
              http://<ha-host>:8123/api/lechange/gt4/slides
        (不再使用独立 8765 监听器 — 容器部署免端口映射)
        """
        # 全局 listener(async_setup 已注册 view 到 HA 8123)
        listener = self.hass.data[DOMAIN].get("gt4_listener")
        if listener is None:
            return self.async_abort(reason="gt4_unavailable")
        # 绑定本次 flow 的回调(四元组 → CheckGeeTest4 → 重发短信)
        listener._on_tuple = self._on_gt4_tuple
        # 生成滑块页(相对路径回传 — 反代/HTTPS 自动跟随)
        listener.html_for(
            account_label=self._username,
            verify_token="",            # 空串: CheckGeeTest4 接受空串
            usage=self._gt4_usage,
            endpoint="/api/lechange/gt4/tuple",
        )
        return self.async_show_form(
            step_id="gt4",
            description_placeholders={
                "url": "/api/lechange/gt4/slides",
            },
        )

    async def _on_gt4_tuple(self, t: dict) -> None:
        """四元组回传 → CheckGeeTest4 → 重发短信 → 唤醒流程等待."""
        try:
            async with aiohttp.ClientSession() as session:
                client = ImouClient(
                    session, username=self._username,
                    session_id=self._session_id or None,
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

    async def async_step_gt4_wait(self, user_input: Optional[dict] = None) -> FlowResult:
        """等待滑块+短信完成(用户输码后回到 sms 步骤)."""
        if self._gt4_done is not None:
            try:
                await asyncio.wait_for(self._gt4_done.wait(), timeout=180)
            except asyncio.TimeoutError:
                return self.async_abort(reason="gt4_timeout")
        if self._gt4_error:
            return self.async_abort(reason="gt4_failed")
        # GT4 通过 + 短信已发 → 回到短信输入步骤
        return await self.async_step_sms()

    async def _after_login(self, login_data: dict) -> FlowResult:
        """登录成功 → 持久化自有 sid → 拉取设备列表 → 设备选择."""
        # 记住自有 sid(有登录史 → 后续密码 GetToken 永远直通)
        self._session_id = login_data.get("session_id") or self._session_id
        devices = await http_list_devices(
            login_data["username_input"], login_data.get("password_input", ""), login_data
        )
        devices.sort(key=lambda d: (not ImouClient.is_lock(d), d["name"]))
        if not devices:
            raise ImouAPIError(-4, "no devices")
        self._login_data = login_data
        self._devices = devices
        # HA view 形态: view 挂在 HA 8123 上, 不需要 stop(页面仍在但无害)
        self._gt4_listener = None
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
            return self.async_create_entry(
                title=device["name"],
                data={
                    CONF_USERNAME: self._login_data["username_input"],
                    CONF_PASSWORD: self._login_data["password_input"],
                    CONF_SESSION_ID: self._login_data["session_id"],
                    CONF_TOKEN: self._login_data["token"],
                    CONF_INTERNAL_USERNAME: self._login_data["internal_username"],
                    CONF_USER_ID: self._login_data.get("user_id"),
                    CONF_API_HOST: self._login_data["host"],
                    CONF_DEVICE_ID: device_id,
                    CONF_DEVICE_NAME: device["name"],
                    CONF_PRODUCT_ID: device["productId"],
                    CONF_MODEL_NAME: device["model"],
                    CONF_FIRMWARE_VERSION: device["version"],
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
                },
            )

        devices_dict = {
            d["deviceId"]: f"{d['name']} ({d['deviceId']}){'' if ImouClient.is_lock(d) else ' ·非门锁'}"
            for d in self._devices
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): vol.In(devices_dict),
                # 机身标签二维码/条码旁的 8 位大写字母数字串
                vol.Required(CONF_SECURITY_CODE): cv.string,
                # App"修改设备密码"后的当前值; 未改过则留空(=安全码)
                vol.Optional(CONF_DEVICE_PASSWORD, default=""): cv.string,
            }
        )
        return self.async_show_form(step_id="device", data_schema=schema)

    @staticmethod
    def async_get_options_flow(config_entry):
        return LeChangeOptionsFlowHandler(config_entry)

    def async_migrate_entry(self, entry) -> bool:
        """Old OpenAPI-based entries cannot be migrated automatically."""
        return False


class LeChangeOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow: 设备密码体系 + 媒体/RTSP + 节流设置。"""

    def __init__(self, config_entry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: Optional[dict] = None) -> FlowResult:
        data = self._entry.options or {}
        entry_data = self._entry.data or {}
        schema = vol.Schema(
            {
                # --- 设备密码体系(两套密码) ---
                vol.Optional(
                    CONF_SECURITY_CODE,
                    description={"suggested_value": entry_data.get(CONF_SECURITY_CODE, "")},
                ): cv.string,
                vol.Optional(
                    CONF_DEVICE_PASSWORD,
                    description={"suggested_value": entry_data.get(CONF_DEVICE_PASSWORD, "")},
                ): cv.string,
                # --- 云端媒体节流(电池设备;取流请求自带唤醒) ---
                vol.Optional(
                    CONF_SNAPSHOT_MIN_INTERVAL,
                    default=int(data.get(CONF_SNAPSHOT_MIN_INTERVAL, DEFAULT_SNAPSHOT_MIN_INTERVAL)),
                ): vol.All(vol.Coerce(int), vol.Range(min=15, max=3600)),
                # --- 码流偏好(默认主码流; 子码流中继无数据时自动回退主码流) ---
                vol.Optional(
                    CONF_SNAPSHOT_STREAM_ID,
                    default=str(data.get(CONF_SNAPSHOT_STREAM_ID, DEFAULT_SNAPSHOT_STREAM_ID)),
                ): vol.In({"1": "主码流(推荐)", "2": "子码流(实测中继无数据,自动回退)"}),
                # --- OSD 叠加层(门外截图: 时间戳+通道名; 默认不添加=干净截图) ---
                vol.Optional(
                    CONF_SNAPSHOT_OSD,
                    default=bool(data.get(CONF_SNAPSHOT_OSD, DEFAULT_SNAPSHOT_OSD)),
                ): cv.boolean,
                vol.Optional(
                    CONF_SNAPSHOT_OSD_ALPHA,
                    default=int(data.get(CONF_SNAPSHOT_OSD_ALPHA, DEFAULT_SNAPSHOT_OSD_ALPHA)),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
                # --- 实时预览 OSD(独立开关; 默认开, 可设置不添加) ---
                vol.Optional(
                    CONF_STREAM_PREVIEW_OSD,
                    default=bool(
                        data.get(CONF_STREAM_PREVIEW_OSD, DEFAULT_STREAM_PREVIEW_OSD)
                    ),
                ): cv.boolean,
                vol.Optional(
                    CONF_STREAM_PREVIEW_SECONDS,
                    default=int(
                        data.get(CONF_STREAM_PREVIEW_SECONDS, DEFAULT_STREAM_PREVIEW_SECONDS)
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=3, max=60)),
                # --- 多通道布局(双摄组合: 左右/上下/单摄) ---
                vol.Optional(
                    CONF_SNAPSHOT_LAYOUT,
                    default=str(data.get(CONF_SNAPSHOT_LAYOUT, DEFAULT_SNAPSHOT_LAYOUT)),
                ): vol.In(
                    {
                        "hstack": "左右组合(双通道并列)",
                        "vstack": "上下组合(双通道纵排)",
                        "single": "单摄单图(按下方通道选择)",
                    }
                ),
                vol.Optional(
                    CONF_SNAPSHOT_CHANNELS,
                    default=str(data.get(CONF_SNAPSHOT_CHANNELS, DEFAULT_SNAPSHOT_CHANNELS)),
                ): vol.In(
                    {
                        "0+1": "双摄组合(通道0+1)",
                        "0": "仅通道0 主摄像头(猫眼)",
                        "1": "仅通道1 辅摄像头",
                    }
                ),
                # --- 本地通道地址(可选; 每行 通道号=局域网地址, 设备在网时优先) ---
                vol.Optional(
                    CONF_CHANNEL_HOSTS,
                    default=str(data.get(CONF_CHANNEL_HOSTS, "")),
                ): cv.string,
                # --- 局域网 RTSP(可选覆盖) ---
                vol.Optional(CONF_RTSP_URL, default=data.get(CONF_RTSP_URL, "")): cv.string,
                vol.Optional(CONF_RTSP_HOST, default=data.get(CONF_RTSP_HOST, "")): cv.string,
                vol.Optional(CONF_RTSP_PORT, default=int(data.get(CONF_RTSP_PORT, 554))): int,
                vol.Optional(
                    CONF_RTSP_USERNAME, default=data.get(CONF_RTSP_USERNAME, "admin")
                ): cv.string,
                vol.Optional(
                    CONF_RTSP_PASSWORD, default=data.get(CONF_RTSP_PASSWORD, "")
                ): cv.string,
                vol.Optional(
                    CONF_RTSP_SUBTYPE, default=int(data.get(CONF_RTSP_SUBTYPE, 0))
                ): vol.In([0, 1]),
            }
        )
        if user_input is not None:
            # 选项流中的密码留空 → 不覆盖 entry.data 中已有值
            if not str(user_input.get(CONF_SECURITY_CODE, "")).strip():
                user_input.pop(CONF_SECURITY_CODE, None)
            if not str(user_input.get(CONF_DEVICE_PASSWORD, "")).strip():
                user_input.pop(CONF_DEVICE_PASSWORD, None)
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="init", data_schema=schema)
