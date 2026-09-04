"""Regression tests: 配置流程最后一步(设备选择)不得抛未捕获异常。

历史 bug(v1.6.0 起):ImouClient._apply_login_response 统一返回键
"username"(云端内部账号),而 config_flow 最后一步读取不存在的键
"internal_username" → KeyError → HA 前端显示 "Unknown error occurred",
设备永远无法添加。回归形态:

    _apply_login_response → {session_id, token, username, user_id, host, account}
    http_login/http_login_sms 追加 → username_input / password_input / sid_used

本文件自带 homeassistant 最小桩(config_flow 仅用其导入面),
与 conftest 的无 HA 加载策略一致。
"""
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# homeassistant 最小桩(必须在导入 config_flow 前注册)
# ---------------------------------------------------------------------------
def _install_ha_stubs() -> None:
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    ha.__path__ = []
    sys.modules["homeassistant"] = ha

    core = types.ModuleType("homeassistant.core")

    class HomeAssistant:  # noqa: D401 - 仅类型占位
        pass

    core.HomeAssistant = HomeAssistant
    core.callback = lambda fn: fn

    de = types.ModuleType("homeassistant.data_entry_flow")
    de.FlowResult = dict

    class AbortFlow(Exception):
        def __init__(self, reason: str) -> None:
            super().__init__(reason)
            self.reason = reason

    de.AbortFlow = AbortFlow

    ce = types.ModuleType("homeassistant.config_entries")

    class _StubFlowBase:
        hass = None
        flow_id = "stub-flow"
        unique_id = None

        def __init_subclass__(cls, domain=None, **kwargs):
            super().__init_subclass__(**kwargs)

        def async_show_form(self, step_id=None, data_schema=None,
                            errors=None, description_placeholders=None):
            return {"type": "form", "step_id": step_id,
                    "errors": dict(errors or {})}

        def async_create_entry(self, title, data):
            return {"type": "create_entry", "title": title, "data": data}

        def async_abort(self, reason):
            return {"type": "abort", "reason": reason}

        async def async_set_unique_id(self, unique_id):
            self.unique_id = unique_id

        def _abort_if_unique_id_configured(self):
            pass

    class ConfigFlow(_StubFlowBase):
        pass

    class OptionsFlow(_StubFlowBase):
        def __init__(self, config_entry=None) -> None:
            self._entry = config_entry

    ce.ConfigFlow = ConfigFlow
    ce.OptionsFlow = OptionsFlow
    ha.config_entries = ce

    exc = types.ModuleType("homeassistant.exceptions")

    class NoURLAvailableError(Exception):
        pass

    exc.NoURLAvailableError = NoURLAvailableError

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []

    cv = types.ModuleType("homeassistant.helpers.config_validation")
    cv.string = str

    net = types.ModuleType("homeassistant.helpers.network")
    net.get_url = lambda hass, **kwargs: "http://stub.local"

    helpers.config_validation = cv
    helpers.network = net

    sys.modules.update({
        "homeassistant.core": core,
        "homeassistant.data_entry_flow": de,
        "homeassistant.config_entries": ce,
        "homeassistant.exceptions": exc,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.config_validation": cv,
        "homeassistant.helpers.network": net,
    })


_install_ha_stubs()

from lechange_door_lock.const import (  # noqa: E402
    CONF_API_HOST,
    CONF_CHANNEL_JSON,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_DEVICE_PASSWORD,
    CONF_FIRMWARE_VERSION,
    CONF_INTERNAL_USERNAME,
    CONF_LOCK_STATE,
    CONF_MODEL_NAME,
    CONF_PASSWORD,
    CONF_PRODUCT_ID,
    CONF_SECURITY_CODE,
    CONF_SESSION_ID,
    CONF_STREAM_ENTRY,
    CONF_TOKEN,
    CONF_USER_ID,
    CONF_USERNAME,
    DOMAIN,
)
from lechange_door_lock.config_flow import (  # noqa: E402
    LeChangeConfigFlow,
    http_list_devices,
)


def _real_login_shape() -> dict:
    """ImouClient._apply_login_response + http_login 追加键的真实形态."""
    return {
        "session_id": "sid-1",
        "token": "tok-1",
        "username": "uuid-internal",       # 云端内部账号(唯一提供的键)
        "user_id": "u-1",
        "host": "https://app-v2.imou.com",
        "account": "13800000000",
        "username_input": "13800000000",
        "password_input": "pw",
        "sid_used": "sid-1",
    }


def _make_flow(login_data: dict) -> LeChangeConfigFlow:
    flow = LeChangeConfigFlow()
    flow.hass = MagicMock()
    flow.hass.data = {DOMAIN: {}}
    flow._username = login_data.get("username_input", "")
    flow._password = login_data.get("password_input", "")
    flow._login_data = login_data
    flow._devices = [{
        "deviceId": "DEV1",
        "productId": "PID1",
        "name": "玄关门锁",
        "model": "DL01S",
        "version": "1.0.0",
        "channels": [{"channelId": "0", "channelName": "猫眼"}],
        "lockState": "1",
        "stream_entry": "",
    }]
    return flow


# ---------------------------------------------------------------------------
# 最后一步: 真实登录数据形态必须能创建 entry
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_device_step_password_login_shape_creates_entry():
    """密码登录形态: 最后一步不再 KeyError('internal_username')."""
    flow = _make_flow(_real_login_shape())
    result = await flow.async_step_device({
        CONF_DEVICE_ID: "DEV1",
        CONF_SECURITY_CODE: "ABCD1234",
        CONF_DEVICE_PASSWORD: "",
    })
    assert result["type"] == "create_entry", result
    data = result["data"]
    # 内部账号必须从 "username" 键取到
    assert data[CONF_INTERNAL_USERNAME] == "uuid-internal"
    assert data[CONF_USERNAME] == "13800000000"
    assert data[CONF_PASSWORD] == "pw"
    assert data[CONF_SESSION_ID] == "sid-1"
    assert data[CONF_TOKEN] == "tok-1"
    assert data[CONF_API_HOST] == "https://app-v2.imou.com"
    assert data[CONF_DEVICE_ID] == "DEV1"
    assert data[CONF_DEVICE_NAME] == "玄关门锁"
    assert data[CONF_PRODUCT_ID] == "PID1"
    assert data[CONF_MODEL_NAME] == "DL01S"
    assert data[CONF_FIRMWARE_VERSION] == "1.0.0"
    assert data[CONF_LOCK_STATE] == "1"
    assert data[CONF_DEVICE_PASSWORD] == "ABCD1234"  # 留空回退安全码


@pytest.mark.asyncio
async def test_device_step_sms_login_shape_creates_entry():
    """短信登录形态(password_input="")同样必须成功."""
    login = _real_login_shape()
    login["password_input"] = ""
    flow = _make_flow(login)
    result = await flow.async_step_device({
        CONF_DEVICE_ID: "DEV1",
        CONF_SECURITY_CODE: "ABCD1234",
        CONF_DEVICE_PASSWORD: "MYPW",
    })
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PASSWORD] == ""
    assert result["data"][CONF_DEVICE_PASSWORD] == "MYPW"  # 显式设备密码不回退


@pytest.mark.asyncio
async def test_device_step_missing_login_keys_still_creates_entry():
    """登录数据整体缺失(极端)也不抛 KeyError → entry 兜底创建,
    setup 阶段 EVERGREEN 续期链用存储的账号密码补全会话."""
    flow = _make_flow({})
    flow._username = "13800000000"   # async_step_user 已记录的输入
    flow._password = "pw"
    result = await flow.async_step_device({
        CONF_DEVICE_ID: "DEV1",
        CONF_SECURITY_CODE: "ABCD1234",
        CONF_DEVICE_PASSWORD: "",
    })
    assert result["type"] == "create_entry"
    data = result["data"]
    assert data[CONF_INTERNAL_USERNAME] == ""
    assert data[CONF_USERNAME] == "13800000000"   # 回退流程内已输入账号
    assert data[CONF_PASSWORD] == "pw"
    assert data[CONF_SESSION_ID] == ""


# ---------------------------------------------------------------------------
# 边界: 未找到设备 → abort
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_device_step_unknown_device_aborts():
    flow = _make_flow(_real_login_shape())
    result = await flow.async_step_device({
        CONF_DEVICE_ID: "NOPE",
        CONF_SECURITY_CODE: "ABCD1234",
        CONF_DEVICE_PASSWORD: "",
    })
    assert result == {"type": "abort", "reason": "device_not_found"}


# ---------------------------------------------------------------------------
# http_list_devices: internal_username 键名回退(SMS 路径无密码时不再重登失败)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_http_list_devices_maps_username_key_fallback():
    """login_data 只有 "username" 键时必须映射为客户端 internal_username."""
    with patch("aiohttp.ClientSession"), \
         patch("lechange_door_lock.config_flow.ImouClient") as cls:
        cls.return_value.async_get_devices = AsyncMock(return_value=[])
        devices = await http_list_devices(
            "13800000000", "pw",
            {"session_id": "s", "token": "t",
             "username": "uuid-internal", "host": "https://h"},
        )
    assert devices == []
    kwargs = cls.call_args.kwargs
    assert kwargs["internal_username"] == "uuid-internal"


@pytest.mark.asyncio
async def test_http_list_devices_prefers_explicit_internal_username():
    """显式 internal_username(若有)优先于 "username"."""
    with patch("aiohttp.ClientSession"), \
         patch("lechange_door_lock.config_flow.ImouClient") as cls:
        cls.return_value.async_get_devices = AsyncMock(return_value=[])
        await http_list_devices(
            "u", "p",
            {"session_id": "s", "token": "t", "username": "fallback",
             "internal_username": "explicit", "host": "https://h"},
        )
    assert cls.call_args.kwargs["internal_username"] == "explicit"
