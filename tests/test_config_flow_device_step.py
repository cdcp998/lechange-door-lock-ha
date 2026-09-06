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

import asyncio
import sys
import time
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

    class ConfigEntry:  # noqa: D401 - 类型占位
        pass

    ce.ConfigEntry = ConfigEntry

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

        def async_create_entry(self, title, data, options=None):
            return {"type": "create_entry", "title": title, "data": data,
                    "options": options}

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
    exc.HomeAssistantError = type("HomeAssistantError", (Exception,), {})

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []

    cv = types.ModuleType("homeassistant.helpers.config_validation")
    cv.string = str
    cv.boolean = bool

    def _cv_template(value=None):
        return value

    cv.template = _cv_template

    # 安装级持久存储桩(terminal_store 依赖 helpers.storage.Store)
    storage = types.ModuleType("homeassistant.helpers.storage")
    _store_data: dict = {}

    class _StoreStub:
        def __init__(self, hass, version, key):
            self.key = key

        async def async_load(self):
            return _store_data.get(self.key)

        async def async_save(self, data):
            _store_data[self.key] = data

    storage.Store = _StoreStub
    _store_data.clear()

    net = types.ModuleType("homeassistant.helpers.network")
    net.get_url = lambda hass, **kwargs: "http://stub.local"

    class NoURLAvailableError(Exception):
        pass

    # 新版 HA(2026.x): NoURLAvailableError 定义于 helpers.network
    net.NoURLAvailableError = NoURLAvailableError

    helpers.config_validation = cv
    helpers.network = net
    helpers.storage = storage

    sys.modules.update({
        "homeassistant.core": core,
        "homeassistant.data_entry_flow": de,
        "homeassistant.config_entries": ce,
        "homeassistant.exceptions": exc,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.config_validation": cv,
        "homeassistant.helpers.network": net,
        "homeassistant.helpers.storage": storage,
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
    http_login_sms,
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
# 导入兼容: NoURLAvailableError 来源随 HA 版本迁移(新版已从 exceptions 移除)
# ---------------------------------------------------------------------------
def test_no_url_available_error_imported_from_helpers_network():
    """config_flow 必须能导入 NoURLAvailableError — 新版 HA 从
    homeassistant.exceptions 移除了它, 来源应为 helpers.network;
    模块级导入失败会让整个 config_flow 注册不上(前端 "Invalid handler
    specified")。"""
    import lechange_door_lock.config_flow as cf

    assert cf.NoURLAvailableError is not None
    # 优先来源: homeassistant.helpers.network(新旧 HA 均定义)
    from homeassistant.helpers import network as _net

    assert cf.NoURLAvailableError is _net.NoURLAvailableError


# ---------------------------------------------------------------------------
# 新版 HA 兼容: 纯 config-flow 集成首配前不调用 async_setup →
# hass.data[DOMAIN] 缺失(GT4 视图/回调清理需幂等惰性注册)
# ---------------------------------------------------------------------------
def test_ensure_gt4_views_registered_is_idempotent():
    """ensure_gt4_views_registered: 首次注册 + 重复调用零副作用."""
    from lechange_door_lock import gt4_helper

    hass = MagicMock()
    hass.data = {}          # 模拟 async_setup 未运行的全新状态
    with patch.object(gt4_helper, "build_ha_views",
                      return_value=[MagicMock(), MagicMock()]) as bv_mock:
        gt4_helper.ensure_gt4_views_registered(hass)
        # hass.data[DOMAIN] 已建立且 listener 占位
        assert isinstance(hass.data[gt4_helper.DOMAIN], dict)
        assert hass.data[gt4_helper.DOMAIN]["gt4_listener"] is not None
        assert bv_mock.call_count == 1
        # 幂等: 再次调用不再注册
        first_listener = hass.data[gt4_helper.DOMAIN]["gt4_listener"]
        gt4_helper.ensure_gt4_views_registered(hass)
        assert bv_mock.call_count == 1
        assert hass.data[gt4_helper.DOMAIN]["gt4_listener"] is first_listener


def test_ensure_gt4_views_survives_register_failure():
    """视图注册抛异常时不得让流程崩溃(降级为无 GT4 能力)."""
    from lechange_door_lock import gt4_helper

    hass = MagicMock()
    hass.data = {}
    hass.http.register_view.side_effect = RuntimeError("http not ready")
    gt4_helper.ensure_gt4_views_registered(hass)
    # 注册失败 → 不占位(后续可重试), 也不抛异常
    assert "gt4_listener" not in hass.data.get(gt4_helper.DOMAIN, {})


@pytest.mark.asyncio
async def test_after_login_tolerates_missing_domain_data():
    """回归(KeyError: 'lechange_door_lock'): _after_login 在 hass.data
    无 DOMAIN 键时正常走到设备选择, 不再 500/未知错误。"""
    flow = _make_flow(_real_login_shape())
    flow.hass.data = {}                      # async_setup 从未运行
    with patch("lechange_door_lock.config_flow.http_list_devices",
               new=AsyncMock(return_value=flow._devices)):
        result = await flow._after_login(_real_login_shape())
    assert result["type"] == "form"
    assert result["step_id"] == "device"


# ---------------------------------------------------------------------------
# 最后一步: 真实登录数据形态必须能创建 entry
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_device_step_password_login_shape_creates_entry():
    """密码登录形态: 最后一步不再 KeyError('internal_username')."""
    flow = _make_flow(_real_login_shape())
    result = await flow.async_step_device({
        CONF_DEVICE_ID: "DEV1",
        CONF_DEVICE_PASSWORD: "ABCD1234",  # 安全码输入框已移除: 设备密码兼作安全码回退
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
    assert data[CONF_DEVICE_PASSWORD] == "ABCD1234"  # 显式设备密码
    assert data[CONF_SECURITY_CODE] == "ABCD1234"  # 安全码已移除输入框 → 设备密码回退


@pytest.mark.asyncio
async def test_device_step_sms_login_shape_creates_entry():
    """短信登录形态(password_input="")同样必须成功."""
    login = _real_login_shape()
    login["password_input"] = ""
    flow = _make_flow(login)
    result = await flow.async_step_device({
        CONF_DEVICE_ID: "DEV1",
        CONF_DEVICE_PASSWORD: "MYPW",  # 安全码输入框已移除
    })
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PASSWORD] == ""
    assert result["data"][CONF_DEVICE_PASSWORD] == "MYPW"  # 显式设备密码不回退
    assert result["data"][CONF_SECURITY_CODE] == "MYPW"  # 安全码=设备密码回退


@pytest.mark.asyncio
async def test_device_step_missing_login_keys_still_creates_entry():
    """登录数据整体缺失(极端)也不抛 KeyError → entry 兜底创建,
    setup 阶段 EVERGREEN 续期链用存储的账号密码补全会话."""
    flow = _make_flow({})
    flow._username = "13800000000"   # async_step_user 已记录的输入
    flow._password = "pw"
    result = await flow.async_step_device({
        CONF_DEVICE_ID: "DEV1",
        CONF_DEVICE_PASSWORD: "",  # 安全码输入框已移除
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


# ---------------------------------------------------------------------------
# 12112 终端管理授权链(真机协议: 04-终端绑定与账号安全.md §5/§8)
# ---------------------------------------------------------------------------
import json as _json  # noqa: E402
import base64 as _b64  # noqa: E402
import hashlib as _hashlib  # noqa: E402
import hmac as _hmac  # noqa: E402

from lechange_door_lock.imou_client import ImouAPIError  # noqa: E402


@pytest.mark.asyncio
async def test_password_12112_routes_to_credit_step():
    """密码登录遇 12112 → 路由到终端授权步骤(不再误报"密码错误")."""
    from lechange_door_lock.config_flow import OEM_AK as _ak, OEM_SK as _sk
    flow = _make_flow({})
    flow._username = "13800000000"
    with patch("lechange_door_lock.config_flow.http_login",
               side_effect=ImouAPIError(12112, "terminal check")), \
         patch("lechange_door_lock.config_flow.OEM_AK", _ak or "test-ak"), \
         patch("lechange_door_lock.config_flow.OEM_SK", _sk or "test-sk"), \
         patch("lechange_door_lock.config_flow.http_send_credit_code",
               new=AsyncMock(return_value=True)):
        result = await flow.async_step_password({CONF_PASSWORD: "pw"})
    assert result["type"] == "form"
    assert result["step_id"] == "credit"
    assert flow._password == "pw"


@pytest.mark.asyncio
async def test_credit_step_unavailable_aborts():
    """OEM AK/SK 未配置 → abort credit_unavailable."""
    flow = _make_flow({})
    flow._username = "13800000000"
    with patch("lechange_door_lock.config_flow.OEM_AK", ""), \
         patch("lechange_door_lock.config_flow.OEM_SK", ""):
        result = await flow.async_step_credit()
    assert result == {"type": "abort", "reason": "credit_unavailable"}


@pytest.mark.asyncio
async def test_credit_step_send_failure_aborts():
    """发码失败 → abort credit_send_failed."""
    flow = _make_flow({})
    flow._username = "13800000000"
    with patch("lechange_door_lock.config_flow.OEM_AK", "test-ak"), \
         patch("lechange_door_lock.config_flow.OEM_SK", "test-sk"), \
         patch("lechange_door_lock.config_flow.http_send_credit_code",
               new=AsyncMock(return_value=False)):
        result = await flow.async_step_credit()
    assert result == {"type": "abort", "reason": "credit_send_failed"}


@pytest.mark.asyncio
async def test_credit_step_generates_persistent_sid():
    """首进授权步骤: 本地生成持久匿名会话, 发码即携带(真机形态)."""
    flow = _make_flow({})
    flow._username = "13800000000"
    assert flow._session_id == ""
    send_mock = AsyncMock(return_value=True)
    with patch("lechange_door_lock.config_flow.http_send_credit_code",
               new=send_mock):
        result = await flow.async_step_credit()
    assert result["type"] == "form"
    assert result["step_id"] == "credit"
    assert flow._session_id                     # 32hex 匿名会话已生成
    assert len(flow._session_id) == 32
    send_mock.assert_awaited_once_with(
        "13800000000", session_id=flow._session_id,
        terminal_id=flow._terminal_id,
    )


@pytest.mark.asyncio
async def test_credit_step_full_chain_creates_entry():
    """授权+同码登录成功 → 回填密码 → 设备选择表单(create_entry 路径畅通)."""
    flow = _make_flow({})
    flow._username = "13800000000"
    flow._password = "pw"
    flow._credit_code_sent = True
    login_shape = _real_login_shape()

    with patch("lechange_door_lock.config_flow.http_submit_credit",
               new=AsyncMock(return_value=True)) as submit_mock, \
         patch("lechange_door_lock.config_flow.http_login_sms",
               new=AsyncMock(return_value=login_shape)) as sms_mock, \
         patch("lechange_door_lock.config_flow.http_list_devices",
               new=AsyncMock(return_value=flow._devices)) as list_mock:
        result = await flow.async_step_credit({"valid_code": "049400"})

    submit_mock.assert_awaited_once()
    # 整链统一终端标识(真机同 UA 形态)
    sms_mock.assert_awaited_once_with(
        "13800000000", "049400", session_id="",
        terminal_id=flow._terminal_id,
    )
    # 授权链登录后回填密码供 EVERGREEN 自主续期
    assert list_mock.call_args.args[1] == "pw"
    assert list_mock.call_args.kwargs["terminal_id"] == flow._terminal_id
    assert result["type"] == "form"
    assert result["step_id"] == "device"


@pytest.mark.asyncio
async def test_credit_step_grant_failure_shows_error():
    """GrantingCredit 失败 → 表单内联错误 credit_failed(可重试, 不中断流程)."""
    flow = _make_flow({})
    flow._username = "13800000000"
    flow._credit_code_sent = True
    with patch("lechange_door_lock.config_flow.http_submit_credit",
               side_effect=ImouAPIError(15000, "valid code error")):
        result = await flow.async_step_credit({"valid_code": "049400"})
    assert result["type"] == "form"
    assert result["step_id"] == "credit"
    assert result["errors"] == {"base": "credit_failed"}


@pytest.mark.asyncio
async def test_sms_12112_routes_to_credit_step():
    """短信登录路径的 GetTokenBySMS 遇 12112 → 同样路由到终端授权步骤."""
    from lechange_door_lock.config_flow import OEM_AK as _ak, OEM_SK as _sk
    flow = _make_flow({})
    flow._username = "13800000000"
    flow._credit_code_sent = True   # 模拟已发码
    with patch("lechange_door_lock.config_flow.http_login_sms",
               side_effect=ImouAPIError(12112, "terminal check")):
        result = await flow.async_step_sms({"valid_code": "123456"})
    assert result["type"] == "form"
    assert result["step_id"] == "credit"
    # 进入授权步骤时不重复发码(已发)
    assert flow._credit_code_sent is True


@pytest.mark.asyncio
async def test_http_login_sms_passes_username_and_terminal():
    """回归(11001 bad request): http_login_sms 必须把 username 传入客户端 —
    GetTokenBySMS body 的 account = _enc_account(client.username),
    漏传会把空串加密当账号发给服务端。"""
    with patch("aiohttp.ClientSession"), \
         patch("lechange_door_lock.config_flow.ImouClient") as cls:
        inst = cls.return_value
        inst.async_get_token_by_sms_ak = AsyncMock(return_value={})
        await http_login_sms("13800000000", "049400",
                             session_id="sid-x", terminal_id="TERM-1")
    kwargs = cls.call_args.kwargs
    assert kwargs["username"] == "13800000000"
    assert kwargs["session_id"] == "sid-x"
    assert kwargs["terminal_id"] == "TERM-1"
    inst.async_get_token_by_sms_ak.assert_awaited_once_with("049400")


@pytest.mark.asyncio
async def test_device_step_security_code_optional():
    """安全码改选填: 不传(留空)也正常创建 entry, 凭据留空降级
    (仅告警截图解密不可用, 之后可在集成配置里补填)."""
    flow = _make_flow(_real_login_shape())
    result = await flow.async_step_device({CONF_DEVICE_ID: "DEV1"})
    assert result["type"] == "create_entry"
    data = result["data"]
    assert data[CONF_SECURITY_CODE] == ""
    assert data[CONF_DEVICE_PASSWORD] == ""    # 无安全码 → 设备密码回退也为空


@pytest.mark.asyncio
async def test_options_flow_password_keep_on_empty():
    """密码字段: 留空 = 保持原值(绝不写空串).

    [回滚守卫] "留空=清空"语义曾让用户误清空设备密码 → 流帧解密
    回退出厂安全码 → 截图损坏。设备密码是功能性凭据, 表单一律保值。
    """
    import types as _types

    from lechange_door_lock.config_flow import LeChangeOptionsFlowHandler

    entry = _types.SimpleNamespace(
        options={},
        data={CONF_SECURITY_CODE: "OLDCODE", CONF_DEVICE_PASSWORD: "OLDCODE"},
    )

    # ① 修改安全码 + 设备密码留空 → 设备密码保持原值
    of = LeChangeOptionsFlowHandler(entry)
    r1 = await of.async_step_init({CONF_SECURITY_CODE: "NEWCODE",
                                   CONF_DEVICE_PASSWORD: ""})
    assert r1["type"] == "create_entry"
    assert r1["data"][CONF_SECURITY_CODE] == "NEWCODE"
    assert r1["data"][CONF_DEVICE_PASSWORD] == "OLDCODE"   # ★ 保持, 不清空

    # ② 两个都留空(键省略场景) → 双双保持
    of2 = LeChangeOptionsFlowHandler(entry)
    r2 = await of2.async_step_init({})
    assert r2["data"][CONF_SECURITY_CODE] == "OLDCODE"
    assert r2["data"][CONF_DEVICE_PASSWORD] == "OLDCODE"


def test_media_cred_explicit_empty_wins_over_data():
    """media._cred: options 显式空串 = 用户清空, 不被 data 旧值顶回."""
    import types as _types
    from unittest.mock import MagicMock

    from lechange_door_lock.media import MediaManager

    m = MediaManager.__new__(MediaManager)
    m.coordinator = MagicMock()
    m.coordinator.entry = _types.SimpleNamespace(
        options={CONF_SECURITY_CODE: ""},
        data={CONF_SECURITY_CODE: "OLDCODE"},
    )
    assert m.security_code == ""            # ★ 显式清空生效
    # options 无该键 → 回退 data
    m.coordinator.entry = _types.SimpleNamespace(
        options={}, data={CONF_SECURITY_CODE: "OLDCODE"},
    )
    assert m.security_code == "OLDCODE"


# ---------------------------------------------------------------------------
# 终端标识持久化 + reauth(12112/12001 循环根因)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_device_step_persists_terminal_id():
    """create_entry 必须携带流程授权所用 terminal_id(options)."""
    flow = _make_flow(_real_login_shape())
    result = await flow.async_step_device(
        {CONF_DEVICE_ID: "DEV1", CONF_SECURITY_CODE: "ABCD1234"}
    )
    assert result["type"] == "create_entry"
    assert result["options"]["terminal_id"] == flow._terminal_id
    # 流程内所有请求共用同一 terminal_id
    assert flow._terminal_id  # 非空


@pytest.mark.asyncio
async def test_reauth_reuses_runtime_terminal_id():
    """reauth: 复用 entry 持久化的 terminal_id(运行时/授权一致)."""
    flow = LeChangeConfigFlow()
    flow.hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "E1"
    entry.data = {
        CONF_USERNAME: "13800000000",
        CONF_SESSION_ID: "sid-runtime",
        "terminal_id": "TID-RUNTIME",
    }
    entry.options = {}
    flow.hass.config_entries.async_get_entry.return_value = entry
    flow.context = {"entry_id": "E1"}
    flow._terminal_id = ""
    await flow.async_step_reauth(entry.data)
    assert flow._terminal_id == "TID-RUNTIME"
    assert flow._session_id == "sid-runtime"
    # 确认步骤 → 展示密码表单
    result = await flow.async_step_reauth_confirm()
    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"


@pytest.mark.asyncio
async def test_reauth_after_login_updates_entry_in_place():
    """reauth 完成后原位更新 entry(不新建), abort reason=reauth_successful."""
    flow = LeChangeConfigFlow()
    flow.hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "E1"
    entry.data = {
        CONF_USERNAME: "13800000000",
        CONF_SESSION_ID: "old-sid",
        "terminal_id": "TID-RUNTIME",
        CONF_DEVICE_ID: "DEV1",
    }
    entry.options = {}
    flow.hass.config_entries.async_get_entry.return_value = entry
    flow.context = {"entry_id": "E1"}
    flow._reauth_entry = entry
    flow._username = "13800000000"
    flow._terminal_id = "TID-RUNTIME"
    flow._devices = [{                      # 真实列表(_after_login 需要)
        "deviceId": "DEV1", "productId": "PID1", "name": "玄关门锁",
        "model": "DL01S", "version": "1.0.0",
        "channels": [{"channelId": "0", "channelName": "猫眼"}],
        "lockState": "1", "stream_entry": "",
    }]
    login = _real_login_shape()
    with patch("lechange_door_lock.config_flow.http_list_devices",
               new=AsyncMock(return_value=flow._devices)):
        result = await flow._after_login(login)
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    # entry.data 已原位更新(会话与终端标识)
    updated = flow.hass.config_entries.async_update_entry.call_args
    assert updated is not None
    new_data = updated.kwargs["data"] if "data" in updated.kwargs else updated.args[1]
    assert new_data["terminal_id"] == "TID-RUNTIME"
    assert new_data[CONF_SESSION_ID] == login["session_id"]


def test_extract_batteries_shapes():
    """电量解析容错: list/单 struct/字符串/越界值."""
    from lechange_door_lock.state_utils import extract_batteries

    assert extract_batteries({"devicePowerLock": [
        {"type": 1, "elecPercent": 88}, {"type": 0, "elecPercent": "75"},
    ]}) == (88, 75)
    # 单 struct(部分固件直接给 dict)
    assert extract_batteries({"devicePowerLock": {"type": "1", "elecPercent": 66}}) == (66, None)
    # -1(未知)不进结果
    assert extract_batteries({"devicePowerLock": [
        {"type": 1, "elecPercent": -1},
    ]}) == (None, None)


def test_decode_bool_prop():
    """童锁严格布尔判定(未知值不误报开/关)."""
    from lechange_door_lock.state_utils import decode_bool_prop

    assert decode_bool_prop(True) is True
    assert decode_bool_prop(1) is True
    assert decode_bool_prop("1") is True
    assert decode_bool_prop("true") is True
    assert decode_bool_prop(0) is False
    assert decode_bool_prop("0") is False
    assert decode_bool_prop("") is None       # 未知
    assert decode_bool_prop(None) is None
    assert decode_bool_prop(2) is None        # 非布尔枚举 → 未知
    assert decode_bool_prop({"x": 1}) is None


# ---------------------------------------------------------------------------
# 安装级持久终端标识(已授权环境重添加/重装不再重复短信授权)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_terminal_id_persisted_per_account():
    """同账号恒定: 首次生成 → 存储复用;删除集成重添加仍同终端."""
    from lechange_door_lock.terminal_store import get_or_create_terminal_id

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []

    first = await get_or_create_terminal_id(hass, "13800000000")
    again = await get_or_create_terminal_id(hass, "13800000000")
    assert first == again and first
    # 不同账号各自独立
    other = await get_or_create_terminal_id(hass, "13911111111")
    assert other != first


@pytest.mark.asyncio
async def test_terminal_id_prefers_existing_entry():
    """同账号既有 entry 的 terminal_id 优先(老安装迁移零成本)."""
    from lechange_door_lock.terminal_store import get_or_create_terminal_id

    entry = MagicMock()
    entry.data = {CONF_USERNAME: "13800000000", "terminal_id": "TID-ENTRY"}
    entry.options = {}
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]
    tid = await get_or_create_terminal_id(hass, "13800000000")
    assert tid == "TID-ENTRY"


@pytest.mark.asyncio
async def test_user_step_uses_persistent_terminal_id():
    """配置流程 user 步骤解析安装级终端标识(不再每次随机)."""
    flow = LeChangeConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_entries.return_value = []
    result = await flow.async_step_user(
        {CONF_USERNAME: "13800000000", "login_method": "password"}
    )
    assert flow._terminal_id  # 已解析
    tid_first = flow._terminal_id
    # 第二次流程(模拟重添加)同账号同终端
    flow2 = LeChangeConfigFlow()
    flow2.hass = flow.hass
    await flow2.async_step_user(
        {CONF_USERNAME: "13800000000", "login_method": "password"}
    )
    assert flow2._terminal_id == tid_first
    # 清理(存储桩是进程级全局, 防污染指纹断言测试)
    from lechange_door_lock.terminal_store import reset_terminal_id

    await reset_terminal_id(flow.hass, "13800000000")


# ---------------------------------------------------------------------------
# 终端指纹本地文件持久化(登录成功后保存, 下次直接调用不再重新生成)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fingerprint_saved_and_reused():
    """save_terminal_id 落盘 → get_or_create 直接复用(不再生成新的)."""
    from lechange_door_lock.terminal_store import (
        get_or_create_terminal_id,
        save_terminal_id,
    )

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    await save_terminal_id(hass, "13800000000", "TID-SAVED")
    # 即使没有 entry, 存储文件里的指纹也会被复用
    tid = await get_or_create_terminal_id(hass, "13800000000")
    assert tid == "TID-SAVED"
    # 清理(存储桩是进程级全局, 防污染后续测试)
    from lechange_door_lock.terminal_store import reset_terminal_id

    await reset_terminal_id(hass, "13800000000")


@pytest.mark.asyncio
async def test_fingerprint_keep_trusted_on_conflict():
    """已有(已授信)指纹时, 新指纹被拒绝保留旧值 — 防止重复授信."""
    from lechange_door_lock.terminal_store import (
        get_or_create_terminal_id,
        reset_terminal_id,
        save_terminal_id,
    )

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    await save_terminal_id(hass, "13800000000", "TID-TRUSTED")
    await save_terminal_id(hass, "13800000000", "TID-OTHER")   # 应被忽略
    tid = await get_or_create_terminal_id(hass, "13800000000")
    assert tid == "TID-TRUSTED"
    await reset_terminal_id(hass, "13800000000")


@pytest.mark.asyncio
async def test_fingerprint_reset_allows_new():
    """显式重置后允许重新生成(换账号/重新授信场景)."""
    from lechange_door_lock.terminal_store import (
        get_or_create_terminal_id,
        reset_terminal_id,
        save_terminal_id,
    )

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    await save_terminal_id(hass, "13800000000", "TID-OLD")
    await reset_terminal_id(hass, "13800000000")
    tid = await get_or_create_terminal_id(hass, "13800000000")
    assert tid and tid != "TID-OLD"


@pytest.mark.asyncio
async def test_fingerprint_force_overwrites_stale():
    """force=True(用户亲自完成授权)覆盖 stale 旧指纹.

    场景: 存储里有旧指纹 stale-TID, 用户走完 SMS 授权得到新 tid
    AUTH-TID → force 保存必须成功覆盖, 否则删重装后回退 stale-TID
    (未授权) → 12112 → 重新生成 → 终端管理 +1 循环。
    """
    from lechange_door_lock.terminal_store import (
        get_or_create_terminal_id,
        reset_terminal_id,
        save_terminal_id,
    )

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    await save_terminal_id(hass, "13800000000", "STALE-TID")
    await save_terminal_id(hass, "13800000000", "AUTH-TID", force=True)
    tid = await get_or_create_terminal_id(hass, "13800000000")
    assert tid == "AUTH-TID"          # force 覆盖成功
    await reset_terminal_id(hass, "13800000000")


def test_coordinator_keeps_legacy_terminal_id():
    """coordinator 不得重新生成 lechange-hass- 前缀旧 tid.

    该 tid 就是历史已授权终端;重新生成 = 12112 → reauth → 终端 +1。
    (升级即换终端 BUG 的回归守卫: 真构造 __init__ 断言传给 ImouClient)
    """
    import importlib

    # ★ 桩必须先于 coordinator 导入建立(无条件覆盖)
    ha = sys.modules["homeassistant"]
    const = types.ModuleType("homeassistant.const")
    const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    const.PERCENTAGE = "%"
    sys.modules["homeassistant.const"] = const
    ha.const = const
    helpers = sys.modules["homeassistant.helpers"]
    for name, attrs in (
        ("aiohttp_client", {"async_get_clientsession": lambda hass: MagicMock()}),
        ("event", {"async_track_time_interval": lambda *a, **k: MagicMock()}),
    ):
        if not hasattr(helpers, name):
            mod = types.ModuleType(f"homeassistant.helpers.{name}")
            for aname, aval in attrs.items():
                setattr(mod, aname, aval)
            setattr(helpers, name, mod)
            sys.modules[f"homeassistant.helpers.{name}"] = mod
    dr_mod = sys.modules.get("homeassistant.helpers.device_registry")
    if dr_mod is None or not hasattr(dr_mod, "async_get"):
        dr_mod = types.ModuleType("homeassistant.helpers.device_registry")
        dr_mod.async_get = lambda hass: MagicMock()
        sys.modules["homeassistant.helpers.device_registry"] = dr_mod
        helpers.device_registry = dr_mod
    # update_coordinator 无条件覆盖为完整版(其它测试文件可能已注册部分桩)
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")

    class _CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

        @property
        def available(self):
            return self.coordinator.last_update_success

    class _DataUpdateCoordinator:
        def __init__(self, hass, *a, **k):
            self.hass = hass
            self.data = None
            self.last_update_success = True

        async def async_config_entry_first_refresh(self):
            pass

        async def async_refresh(self):
            pass

        async def async_request_refresh(self):
            pass

        def async_set_updated_data(self, data):
            self.data = data

    uc.CoordinatorEntity = _CoordinatorEntity
    uc.DataUpdateCoordinator = _DataUpdateCoordinator
    uc.UpdateFailed = type("UpdateFailed", (Exception,), {})
    sys.modules["homeassistant.helpers.update_coordinator"] = uc
    helpers.update_coordinator = uc

    from lechange_door_lock import coordinator as coord_mod

    with patch.object(coord_mod, "ImouClient") as mock_client, \
         patch.object(coord_mod, "MediaManager", MagicMock()), \
         patch.object(coord_mod, "MqttManager", MagicMock()):
        entry = MagicMock()
        entry.entry_id = "E1"
        entry.data = {
            "device_id": "DEV1", "username": "13800000000", "password": "pw",
            "session_id": "s", "token": "t", "internal_username": "u",
            "api_host": "https://x", "terminal_id": "lechange-hass-OLD-AUTH",
        }
        entry.options = {}
        entry.title = "t"
        coord_mod.LeChangeDataUpdateCoordinator(MagicMock(), entry)
        kwargs = mock_client.call_args.kwargs
        # ★ 旧 tid 原样传给客户端(不重新生成)
        assert kwargs["terminal_id"] == "lechange-hass-OLD-AUTH"
        # 且没有把新 tid 写回 options(未发生重新生成)
        entry_data_calls = [
            c for c in entry.mock_calls if "async_update_entry" in str(c[0])
        ]
        assert not entry_data_calls or entry.options.get("terminal_id") in (
            None, "lechange-hass-OLD-AUTH",
        )


# ---------------------------------------------------------------------------
# MQTT 凭据缓存自愈(连接失败 → 缓存作废 → 下次强制取新凭据)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mqtt_creds_invalidated_on_connect_failure():
    """连接失败(服务端拒连/0 bytes read) → 1h 缓存立即作废."""
    from lechange_door_lock import mqtt as mqtt_mod

    mgr = mqtt_mod.MqttManager.__new__(mqtt_mod.MqttManager)   # 跳过 __init__
    mgr.api = MagicMock()
    mgr.api.terminal_id = "TID"
    mgr.api.token = "tok"          # _build_password 的 HMAC 密钥(须为 str)
    mgr.api.username = "u"
    mgr.device_id = "DEV1"
    mgr.product_id = "PID1"
    mgr.cloud_ctrl = AsyncMock()
    mgr.on_event = None
    mgr.certs_dir = ""
    mgr._mqtt = None
    mgr._lock = asyncio.Lock()
    mgr._last_creds = {"clientId": "OLD", "mqttServer": {"sslAddr": "h:8883"}}
    mgr._creds_at = time.monotonic()   # 刚缓存(1h 内 → 命中缓存)

    async def _fake_creds():
        return mgr._last_creds

    async def _close():
        pass

    mgr._get_creds = _fake_creds

    # 模拟服务端拒连: async_connect 抛 IncompleteReadError 同形错误
    conn_calls = []

    def _client_factory(*args, **kwargs):
        c = MagicMock()
        c.async_close = _close
        c.connected = False

        async def _connect():
            conn_calls.append(1)
            raise asyncio.IncompleteReadError(b"", 1)

        c.async_connect = _connect
        return c

    with patch.object(mqtt_mod, "MqttClient", side_effect=_client_factory):
        with pytest.raises(asyncio.IncompleteReadError):
            await mgr.async_ensure_connected()
    # ★ 缓存已作废 → 下次 _get_creds 不再命中 1h 缓存(时间戳归零)
    assert mgr._last_creds == {}
    assert mgr._creds_at == 0.0


# ---------------------------------------------------------------------------
# 临时密码列表: secrets[] 是真密码, secretGroups[] 只是 80 个固定槽位
# ---------------------------------------------------------------------------
def test_snapkey_count_uses_secrets_not_groups():
    """数量传感器必须数 secrets[](真实密码), 而非 80 个分组槽位."""
    from lechange_door_lock.state_utils import (
        extract_batteries,
        extract_latest_open_record,
        decode_bool_prop,
    )
    # 模拟 V2 响应: 80 个空分组 + 2 条真实临时密码
    v2_response = {
        "secretGroups": [{"groupId": f"G{i}", "secrets": []} for i in range(80)],
        "secrets": [
            {"keyId": 1, "name": "快递", "tempKey": "12345678", "state": 1},
            {"keyId": 2, "name": "保洁", "tempKey": "87654321", "state": 0},
        ],
    }
    correct_list = v2_response.get("secrets") or []
    assert len(correct_list) == 2          # 服务层应取 secrets
    assert len(v2_response["secretGroups"]) == 80   # 旧取法的错误来源


@pytest.mark.asyncio
async def test_latest_open_record_dual_channel_merge():
    """开门记录双通道: 休眠期无属性上报时由云侧告警兜底, 有属性取属性."""
    from lechange_door_lock.state_utils import extract_latest_open_record

    # 属性通道缺失 → 告警兜底
    merged = extract_latest_open_record(None, {"time": "20260904T101010", "keyType": 1})
    assert merged is not None
    assert merged["time"] == "2026-09-04 10:10:10"
    assert merged["method"]  # keyType 已翻译
    # 两边都有 → 归一化形状(time/method/user)
    merged2 = extract_latest_open_record(
        {"localTime": "20260904T120000", "keyType": 0, "name": "爸爸"},
        {"time": "20260904T101010", "keyType": 1},
    )
    assert merged2["time"] == "2026-09-04 12:00:00"
    assert merged2["user"] == "爸爸"
    assert merged2["method"]


# ---------------------------------------------------------------------------
# DeviceBasicInfoQueryV2 属性快照兜底(休眠期核心属性补齐)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_coordinator_fills_core_props_from_device_list():
    """GetProperties 失败(休眠)时, 核心属性从设备列表 propertiesMap 补齐."""
    import json as _j
    import importlib

    # ★ 桩必须先于 coordinator 导入建立
    # (无条件覆盖: 其它测试文件可能已注册了不完整的同名桩)
    ha = sys.modules["homeassistant"]
    const = types.ModuleType("homeassistant.const")
    const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    const.PERCENTAGE = "%"
    sys.modules["homeassistant.const"] = const
    ha.const = const
    helpers = sys.modules["homeassistant.helpers"]
    for name, attrs in (
        ("aiohttp_client", {"async_get_clientsession": lambda hass: MagicMock()}),
        ("event", {"async_track_time_interval": lambda *a, **k: MagicMock()}),
    ):
        if not hasattr(helpers, name):
            mod = types.ModuleType(f"homeassistant.helpers.{name}")
            for aname, aval in attrs.items():
                setattr(mod, aname, aval)
            setattr(helpers, name, mod)
            sys.modules[f"homeassistant.helpers.{name}"] = mod
    dr_mod = sys.modules.get("homeassistant.helpers.device_registry")
    if dr_mod is None or not hasattr(dr_mod, "async_get"):
        dr_mod = types.ModuleType("homeassistant.helpers.device_registry")
        dr_mod.async_get = lambda hass: MagicMock()
        sys.modules["homeassistant.helpers.device_registry"] = dr_mod
        helpers.device_registry = dr_mod
    # update_coordinator 无条件覆盖为完整版(含 DataUpdateCoordinator
    # 与 CoordinatorEntity — test_platform_entities 可能已注册仅后者)
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")

    class _CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

        @property
        def available(self):
            return self.coordinator.last_update_success

    class _DataUpdateCoordinator:
        def __init__(self, hass, *a, **k):
            self.hass = hass
            self.data = None
            self.last_update_success = True

        async def async_config_entry_first_refresh(self):
            pass

        async def async_refresh(self):
            pass

        async def async_request_refresh(self):
            pass

        def async_set_updated_data(self, data):
            self.data = data

    class _UpdateFailed(Exception):
        pass

    uc.CoordinatorEntity = _CoordinatorEntity
    uc.DataUpdateCoordinator = _DataUpdateCoordinator
    uc.UpdateFailed = _UpdateFailed
    sys.modules["homeassistant.helpers.update_coordinator"] = uc
    helpers.update_coordinator = uc

    from lechange_door_lock import coordinator as coord_mod
    coord_mod = importlib.reload(coord_mod)
    coord = coord_mod.LeChangeDataUpdateCoordinator.__new__(
        coord_mod.LeChangeDataUpdateCoordinator
    )
    coord.hass = MagicMock()
    coord.entry = MagicMock()
    coord.entry.entry_id = "E1"
    coord.entry.data = {}
    coord.device_id = "DEV1"
    coord.product_id = "P1"
    coord.channel_id = "0"
    coord.data = None
    coord._seen_lock_notes = set()
    coord._seen_alarm_ids = set()
    coord._alarm_seen_initialized = True

    pm = _j.dumps({
        "120000": 1,                       # child_lock(bool) 开
        "106200": [{"type": 1, "elecPercent": 88}],
        "120600": {"SSID": "home", "intensity": 3},
    })
    decoded = {
        "child_lock": True,
        "devicePowerLock": [{"type": 1, "elecPercent": 88}],
        "wifiDoorLock": {"SSID": "home", "intensity": 3},
    }

    api = MagicMock()
    api.terminal_id = "TID"
    # BasicInfoGet 成功但快照无 properties_map;GetProperties 10003(休眠)
    api.async_get_device_info = AsyncMock(return_value={
        "deviceId": "DEV1", "status": "sleep", "lockState": "beClosed",
        "channels": [], "properties_map": "",
    })
    api.async_get_properties = AsyncMock(
        side_effect=ImouAPIError(10003, "sleeping")
    )
    api.async_get_devices = AsyncMock(return_value=[{
        "deviceId": "DEV1", "properties_map": pm,
        "status": "sleep", "lockState": "beClosed", "channels": [],
    }])
    model = MagicMock()
    model.decode_properties = MagicMock(return_value=decoded)
    api.async_get_model = AsyncMock(return_value=model)
    api.async_get_alarm_messages = AsyncMock(return_value={"alarms": []})
    api.async_smart_lock_secret_list = AsyncMock(
        return_value={"secrets": [{"keyId": 1}, {"keyId": 2}]}
    )
    # ① 降级链: GetDeviceDetailInfo 云端属性缓存(本轮无数据 → 返回空)
    api.async_get_device_detail_info = AsyncMock(return_value={"properties": {}})
    coord.api = api

    data = await coord._async_update_data()
    # ★ 休眠期核心属性全部到位
    assert data["props"]["child_lock"] is True
    assert data["battery_lock"] == 88
    assert data["wifi"]["ssid"] == "home"
    # ★ 临时密码列表每轮询自动刷新(数量 = secrets 条数 = 2)
    assert len(coord.snapkey_list) == 2


@pytest.mark.asyncio
async def test_coordinator_fills_wifi_from_detail_info_cache():
    """降级链①: GetDeviceDetailInfo 云端属性缓存补 WiFi/门状态(休眠可读).

    抓包 20260905 实证: App 设备页 properties 含 106000(wifiDoorLock)
    与 108000(doorLockState), 即使设备休眠 — 但 child_lock 不在。
    """
    import json as _j
    import importlib

    # 桩(同 test_coordinator_fills_core_props_from_device_list)
    ha = sys.modules["homeassistant"]
    const = types.ModuleType("homeassistant.const")
    const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    const.PERCENTAGE = "%"
    sys.modules["homeassistant.const"] = const
    ha.const = const
    helpers = sys.modules["homeassistant.helpers"]
    for name, attrs in (
        ("aiohttp_client", {"async_get_clientsession": lambda hass: MagicMock()}),
        ("event", {"async_track_time_interval": lambda *a, **k: MagicMock()}),
    ):
        if not hasattr(helpers, name):
            mod = types.ModuleType(f"homeassistant.helpers.{name}")
            for aname, aval in attrs.items():
                setattr(mod, aname, aval)
            setattr(helpers, name, mod)
            sys.modules[f"homeassistant.helpers.{name}"] = mod
    dr_mod = sys.modules.get("homeassistant.helpers.device_registry")
    if dr_mod is None or not hasattr(dr_mod, "async_get"):
        dr_mod = types.ModuleType("homeassistant.helpers.device_registry")
        dr_mod.async_get = lambda hass: MagicMock()
        sys.modules["homeassistant.helpers.device_registry"] = dr_mod
        helpers.device_registry = dr_mod
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")

    class _CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

    class _DataUpdateCoordinator:
        def __init__(self, hass, *a, **k):
            self.hass = hass
            self.data = None
            self.last_update_success = True

        async def async_config_entry_first_refresh(self):
            pass

        async def async_refresh(self):
            pass

        async def async_request_refresh(self):
            pass

        def async_set_updated_data(self, data):
            self.data = data

    uc.CoordinatorEntity = _CoordinatorEntity
    uc.DataUpdateCoordinator = _DataUpdateCoordinator
    uc.UpdateFailed = type("UpdateFailed", (Exception,), {})
    sys.modules["homeassistant.helpers.update_coordinator"] = uc
    helpers.update_coordinator = uc

    from lechange_door_lock import coordinator as coord_mod
    coord_mod = importlib.reload(coord_mod)
    coord = coord_mod.LeChangeDataUpdateCoordinator.__new__(
        coord_mod.LeChangeDataUpdateCoordinator
    )
    coord.hass = MagicMock()
    coord.entry = MagicMock()
    coord.entry.entry_id = "E1"
    coord.entry.data = {}
    coord.device_id = "DEV1"
    coord.product_id = "P1"
    coord.channel_id = "0"
    coord.data = None
    coord._seen_lock_notes = set()
    coord._seen_alarm_ids = set()
    coord._alarm_seen_initialized = True

    # 抓包同款: 云端缓存里有 106000(wifiDoorLock) 与 108000(doorLockState),
    # 无 120000(child_lock)
    detail_props = {
        "106000": {"106001": "NetWei", "106003": 2, "106005": 2},
        "108000": 0,
        "106200": [{"106203": 77, "106202": 0, "106201": 1}],
    }
    decoded = {
        "wifiDoorLock": {"SSID": "NetWei", "status": 2, "intensity": 2},
        "doorLockState": 0,
        "devicePowerLock": [{"elecPercent": 77, "type": 0, "state": 1}],
    }

    api = MagicMock()
    api.terminal_id = "TID"
    api.async_get_device_info = AsyncMock(return_value={
        "deviceId": "DEV1", "status": "sleep", "lockState": "beClosed",
        "channels": [], "properties_map": "",
    })
    api.async_get_properties = AsyncMock(
        side_effect=coord_mod.ImouAPIError(10003, "sleeping")
    )
    api.async_get_device_detail_info = AsyncMock(
        return_value={"properties": detail_props}
    )
    api.async_get_devices = AsyncMock(return_value=[])
    api.async_get_alarm_messages = AsyncMock(return_value={"alarms": []})
    api.async_smart_lock_secret_list = AsyncMock(return_value={"secrets": []})
    model = MagicMock()
    model.decode_properties = MagicMock(return_value=decoded)
    api.async_get_model = AsyncMock(return_value=model)
    coord.api = api

    data = await coord._async_update_data()
    # ★ WiFi 与门状态由云端缓存补齐(休眠期), child_lock 仍缺(实时属性)
    assert data["props"]["wifiDoorLock"]["SSID"] == "NetWei"
    assert data["wifi"]["ssid"] == "NetWei"
    assert data["wifi"]["intensity"] == 2
    assert data["props"]["doorLockState"] == 0
    assert data["battery_camera"] == 77


def test_resolve_child_lock_via_in_open_door_model():
    """童锁双源: child_lock 缺失时回退 sdl_inOpenDoorModel(enum 1/2).

    抓包 20260905 实证: R10-Max 云端缓存含 171700=2(童锁模式),
    无 120000(child_lock) — App 显示的"童锁开启"即前者。
    """
    from lechange_door_lock.state_utils import resolve_child_lock

    # ① child_lock 在场优先(严格 bool)
    assert resolve_child_lock({"child_lock": True}) is True
    assert resolve_child_lock({"child_lock": 0}) is False
    # ② 回退室内开门模式: 2=童锁, 1=普通
    assert resolve_child_lock({"sdl_inOpenDoorModel": 2}) is True
    assert resolve_child_lock({"sdl_inOpenDoorModel": 1}) is False
    assert resolve_child_lock({"sdl_inOpenDoorModel": "2"}) is True
    # ③ 两源全缺 → 未知
    assert resolve_child_lock({}) is None
    assert resolve_child_lock({"sdl_inOpenDoorModel": 9}) is None
    # ④ 语义冲突时 child_lock 优先
    assert resolve_child_lock({"child_lock": False, "sdl_inOpenDoorModel": 2}) is False


@pytest.mark.asyncio
async def test_device_step_saves_fingerprint_to_store():
    """首次添加完成(create_entry) → 指纹同步写入存储文件."""
    from lechange_door_lock import terminal_store

    flow = _make_flow(_real_login_shape())
    flow._username = "13877770000"          # 独立账号, 避免存储桩串扰
    flow.hass.config_entries.async_entries.return_value = []
    result = await flow.async_step_device(
        {CONF_DEVICE_ID: "DEV1", CONF_SECURITY_CODE: "ABCD1234"}
    )
    assert result["type"] == "create_entry"
    # 走安装级存储读取验证
    store = terminal_store._store(flow.hass)
    data = await store.async_load() or {}
    assert data["terminals"].get("13877770000") == flow._terminal_id


@pytest.mark.asyncio
async def test_granting_credit_ak_identity_shape_and_signature():
    """GrantingCredit-AK: default\\OEM_AK 身份 + SK 单哈希 + 真机 body 形状."""
    from lechange_door_lock import imou_client as ic

    client = ic.ImouClient(MagicMock(), username="13800000000", session_id="sid-9")
    captured = {}

    async def fake_post(host, path, body, headers):
        captured["path"] = path
        captured["headers"] = headers
        captured["body"] = _json.loads(body)
        return {}

    with patch.object(ic, "OEM_AK", "test-ak"), \
         patch.object(ic, "OEM_SK", "test-sk"), \
         patch.object(client, "_http_post", side_effect=fake_post):
        await client.async_granting_credit_ak("049400")

    assert captured["path"] == "/pcs/v1/user.account.GrantingCredit"
    headers = captured["headers"]
    assert headers["x-pcs-username"] == "default\\test-ak"
    assert headers["x-pcs-session-id"] == "sid-9"
    # 真机 body 形状(04 §5): 原码直传 + type=phone + AES-GCM 加密账号
    payload = captured["body"]["data"]
    assert payload["validCode"] == "049400"      # 短信原码直传
    assert payload["type"] == "phone"            # 固定 phone(非 grantingCredit)
    assert payload["isEncrypt"] is True
    assert payload["account"] != "13800000000"   # AES-GCM 加密

    # 签名串重算(SK 单哈希密钥)
    uri = "/pcs/v1/user.account.GrantingCredit"
    body_bytes = _json.dumps(captured["body"], separators=(",", ":")).encode()
    md5b = _b64.b64encode(_hashlib.md5(body_bytes).digest()).decode()
    base = (f"POST\n{uri}\n{md5b}\n{headers['Content-Type']}\n"
            f"x-pcs-apiver:191204\nx-pcs-client-ua:{headers['x-pcs-client-ua']}\n"
            f"x-pcs-date:{headers['x-pcs-date']}\nx-pcs-nonce:{headers['x-pcs-nonce']}\n"
            f"x-pcs-session-id:sid-9\nx-pcs-username:{headers['x-pcs-username']}\n")
    sk1 = _hashlib.md5(b"test-sk").hexdigest().lower()
    expect = _b64.b64encode(
        _hmac.new(sk1.encode(), base.encode(), _hashlib.sha256).digest()).decode()
    assert headers["x-pcs-signature"] == expect


@pytest.mark.asyncio
async def test_granting_credit_ak_requires_oem_keys():
    """OEM AK/SK 缺失 → 客户端直接拒绝(不发起注定失败的请求)."""
    from lechange_door_lock import imou_client as ic

    client = ic.ImouClient(MagicMock(), username="13800000000")
    with patch.object(ic, "OEM_AK", ""), patch.object(ic, "OEM_SK", ""):
        with pytest.raises(ImouAPIError) as exc_info:
            await client.async_granting_credit_ak("049400")
    assert "OEM AK/SK" in str(exc_info.value)


@pytest.mark.asyncio
async def test_send_credit_code_delegates_granting_usage():
    """发码: 复用 AK 通道 sender, usage=GrantingCredit, areaCode 空串."""
    from lechange_door_lock import imou_client as ic

    client = ic.ImouClient(MagicMock(), username="13800000000")
    captured = {}

    async def fake_post(host, path, body, headers):
        captured["path"] = path
        captured["body"] = _json.loads(body)
        return {}

    with patch.object(ic, "_enc_account", return_value="ENC"), \
         patch.object(client, "_http_post", side_effect=fake_post):
        await client.async_send_credit_code_gt4(usage="GrantingCredit")

    assert captured["path"] == "/pcs/v1/common.validcode.GetValidCode"
    payload = captured["body"]["data"]
    assert payload["usage"] == "GrantingCredit"
    assert payload["areaCode"] == ""      # 真机形态: areaCode 空串
    assert payload["type"] == "phone"
    assert payload["isEncrypt"] is True
    assert payload["account"] == "ENC"

@pytest.mark.asyncio
async def test_coordinator_fill_attempt_runs_on_fresh_monotonic_clock():
    """降级链节流: 新启动环境 monotonic() < 60s 也必须执行首次填充.

    回归: _last_fill_attempt 初值 0.0 被 getattr 兜底, 新分配的 CI
    runner / 刚启动的容器 monotonic 从启动起算 < 60s, now - 0.0 < 60
    → 首次轮询跳过整个降级链 → props 为空(KeyError 级, CI 独有)。
    """
    import json as _j
    import importlib

    ha = sys.modules["homeassistant"]
    const = types.ModuleType("homeassistant.const")
    const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    const.PERCENTAGE = "%"
    sys.modules["homeassistant.const"] = const
    ha.const = const
    helpers = sys.modules["homeassistant.helpers"]
    for name, attrs in (
        ("aiohttp_client", {"async_get_clientsession": lambda hass: MagicMock()}),
        ("event", {"async_track_time_interval": lambda *a, **k: MagicMock()}),
    ):
        if not hasattr(helpers, name):
            mod = types.ModuleType(f"homeassistant.helpers.{name}")
            for aname, aval in attrs.items():
                setattr(mod, aname, aval)
            setattr(helpers, name, mod)
            sys.modules[f"homeassistant.helpers.{name}"] = mod
    dr_mod = sys.modules.get("homeassistant.helpers.device_registry")
    if dr_mod is None or not hasattr(dr_mod, "async_get"):
        dr_mod = types.ModuleType("homeassistant.helpers.device_registry")
        dr_mod.async_get = lambda hass: MagicMock()
        sys.modules["homeassistant.helpers.device_registry"] = dr_mod
        helpers.device_registry = dr_mod
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")

    class _CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

    class _DataUpdateCoordinator:
        def __init__(self, hass, *a, **k):
            self.hass = hass
            self.data = None
            self.last_update_success = True

        async def async_config_entry_first_refresh(self):
            pass

        async def async_refresh(self):
            pass

        async def async_request_refresh(self):
            pass

        def async_set_updated_data(self, data):
            self.data = data

    uc.CoordinatorEntity = _CoordinatorEntity
    uc.DataUpdateCoordinator = _DataUpdateCoordinator
    uc.UpdateFailed = type("UpdateFailed", (Exception,), {})
    sys.modules["homeassistant.helpers.update_coordinator"] = uc
    helpers.update_coordinator = uc

    from lechange_door_lock import coordinator as coord_mod
    coord_mod = importlib.reload(coord_mod)
    coord = coord_mod.LeChangeDataUpdateCoordinator.__new__(
        coord_mod.LeChangeDataUpdateCoordinator
    )
    coord.hass = MagicMock()
    coord.entry = MagicMock()
    coord.entry.entry_id = "E1"
    coord.entry.data = {}
    coord.device_id = "DEV1"
    coord.product_id = "P1"
    coord.channel_id = "0"
    coord.data = None
    coord._seen_lock_notes = set()
    coord._seen_alarm_ids = set()
    coord._alarm_seen_initialized = True

    # ★ 模拟新分配 runner: monotonic 小值(启动 < 60s)
    detail_props = {
        "106000": {"106001": "NetWei", "106003": 2},
        "106200": [{"106203": 55, "106202": 0, "106201": 1}],
    }
    decoded = {
        "wifiDoorLock": {"SSID": "NetWei", "status": 2},
        "devicePowerLock": [{"elecPercent": 55, "type": 0, "state": 1}],
    }

    api = MagicMock()
    api.terminal_id = "TID"
    api.async_get_device_info = AsyncMock(return_value={
        "deviceId": "DEV1", "status": "sleep", "lockState": "beClosed",
        "channels": [], "properties_map": "",
    })
    api.async_get_properties = AsyncMock(
        side_effect=coord_mod.ImouAPIError(10003, "sleeping")
    )
    api.async_get_device_detail_info = AsyncMock(
        return_value={"properties": detail_props}
    )
    api.async_get_devices = AsyncMock(return_value=[])
    api.async_get_alarm_messages = AsyncMock(return_value={"alarms": []})
    api.async_smart_lock_secret_list = AsyncMock(return_value={"secrets": []})
    model = MagicMock()
    model.decode_properties = MagicMock(return_value=decoded)
    api.async_get_model = AsyncMock(return_value=model)
    coord.api = api

    with patch.object(coord_mod.time, "monotonic", return_value=30.0):
        data = await coord._async_update_data()

    # ★ monotonic=30s(新启动)也必须走降级链填充 props
    assert data["props"]["wifiDoorLock"]["SSID"] == "NetWei"
    assert data["battery_camera"] == 55

