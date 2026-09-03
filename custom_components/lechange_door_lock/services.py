"""Service handlers for the LeChange door lock integration.

Includes 对话 (doorbell call answer/refuse/hangup + voice replies) and
temporary password (snapkey) management on the client-side cloud API.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    EVENT_PREFIX,
    SERVICE_CALL_ANSWER,
    SERVICE_CALL_HANGUP,
    SERVICE_CALL_REFUSE,
    SERVICE_CALL_SERVICE,
    SERVICE_DOORFRONT_SNAPSHOT,
    SERVICE_ALARM_IMAGE,
    SERVICE_RECORD_PREVIEW,
    SERVICE_GENERATE_SNAPKEY,
    SERVICE_DELETE_SNAPKEY,
    SERVICE_GET_OPEN_DOOR_RECORD,
    SERVICE_GET_SNAPKEY_LIST,
    SERVICE_GET_VOICE_REPLY,
    SERVICE_OPEN_DOOR_REMOTE,
    SERVICE_SEND_SMS_CODE,
    SERVICE_AUTHORIZE_TERMINAL,
    SERVICE_SET_PROPERTIES,
    SERVICE_SET_VOICE_REPLY,
    SERVICE_WAKE_UP_DEVICE,
)

_LOGGER = logging.getLogger(__name__)

DEVICE_ID_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})

CALL_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("user_info", default=""): cv.string,
    }
)

CALL_REFUSE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("index", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Optional("user_info", default=""): cv.string,
    }
)

VOICE_REPLY_GET_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("relate_type", default=4): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=5)
        ),
    }
)

VOICE_REPLY_SET_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("index"): vol.All(vol.Coerce(int), vol.Range(min=0, max=256)),
        vol.Optional("relate_type", default=4): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=5)
        ),
        vol.Optional("time_stab", default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=999999)
        ),
    }
)

EFFECT_PERIOD_SCHEMA = vol.Schema(
    {
        vol.Required("period"): vol.All(vol.Coerce(int), vol.Range(min=0, max=6)),
        vol.Required("beginTime"): cv.string,
        vol.Required("endTime"): cv.string,
    }
)

CREATE_SNAPKEY_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("name"): cv.string,
        vol.Optional("effect_times", default=1): vol.All(
            vol.Coerce(int), vol.Range(min=-1, max=999999)
        ),
        vol.Optional("number", default=1): vol.All(
            vol.Coerce(int), vol.Range(min=-1, max=255)
        ),
        vol.Optional("effect_period", default=[]): vol.All(
            cv.ensure_list, [EFFECT_PERIOD_SCHEMA]
        ),
    }
)

GET_SNAPKEY_LIST_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("offset", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Optional("count", default=50): vol.All(vol.Coerce(int), vol.Range(min=1, max=255)),
    }
)

GET_OPEN_DOOR_RECORD_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})

DELETE_SNAPKEY_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("key_id"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional("extra", default={}): dict,
    }
)

SEND_SMS_CODE_SCHEMA = vol.Schema(
    {
        vol.Required("account"): cv.string,
        vol.Optional("usage", default="GrantingCredit"): vol.In(
            ["GrantingCredit", "SMSLogin", "GenerateSnapkey", "ChangeAccount"]
        ),
    }
)

AUTHORIZE_TERMINAL_SCHEMA = vol.Schema(
    {
        vol.Required("account"): cv.string,
        vol.Required("valid_code"): cv.string,
    }
)

SET_PROPERTIES_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("properties"): dict,
    }
)

DOORFRONT_SNAPSHOT_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("force", default=False): cv.boolean,  # 绕过节流(谨慎: 唤醒耗电)
        vol.Optional("filename", default=""): cv.string,   # 相对 www 的路径; 空=仅事件
        # 本地选择(按次覆盖, 不持久化; 留空=用配置/实体的当前选择)
        vol.Optional("channels", default=""): cv.string,   # '0+1' / '0' / '1'
        vol.Optional("layout", default=""): cv.string,     # 'hstack' / 'vstack' / 'single'
        vol.Optional("osd", default=""): cv.string,        # 'on' / 'off'
    }
)

ALARM_IMAGE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("alarm_id", default=""): cv.string,  # 空=取最新告警
        vol.Optional("filename", default=""): cv.string,
    }
)

RECORD_PREVIEW_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("seconds", default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=60)
        ),   # 0=用配置时长
        vol.Optional("osd", default=""): cv.string,      # 'on'/'off'; 空=用配置
        vol.Optional("channel_id", default="0"): cv.string,
        vol.Optional("filename", default=""): cv.string, # 相对 www(如 preview.h264)
    }
)

CALL_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("service_name"): cv.string,
        vol.Optional("input_data", default={}): dict,
        vol.Optional("channel_id", default=""): cv.string,
    }
)


def _get_coordinator(hass: HomeAssistant, device_id: str):
    """Find the coordinator by device_id."""
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if coordinator.device_id == device_id:
            return coordinator
    return None


def _fire_event(hass: HomeAssistant, event_type: str, device_id: str, **extra) -> None:
    hass.bus.async_fire(
        EVENT_PREFIX, {"type": event_type, "device_id": device_id, **extra}
    )


async def async_open_door_remote(call: ServiceCall):
    """远程开门: iot.control.SetService remoteOpenDoor (MQTT 优先, 云 API 兜底)."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    # ref 编码 payload(与 async_set_service 同源), 供 MQTT/云统一控制
    model = await coordinator.api.async_get_model(
        coordinator.device_id, coordinator.product_id
    )
    payload = {
        "deviceId": coordinator.device_id,
        "productId": coordinator.product_id,
        "channelId": "0",
        "service": model.service_ref("remoteOpenDoor"),
        "inputData": model.encode_service_input("remoteOpenDoor", {}),
    }
    result = await coordinator.async_iot_control("iot.control.SetService", payload)
    _fire_event(hass, "open_door", device_id, result=result)
    await coordinator.async_request_refresh()


async def async_wake_up_device(call: ServiceCall):
    """唤醒休眠设备(清除休眠标志,尽力而为)."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    errors = []
    for prop in ("Dormant", "sleepStatus"):
        try:
            await coordinator.api.async_set_properties(
                coordinator.device_id, coordinator.product_id, {prop: False}
            )
        except Exception as err:  # noqa: BLE001
            errors.append(str(err))
    _fire_event(hass, "wake_up", device_id, errors=errors)


async def async_call_answer(call: ServiceCall):
    """对话: 应答门铃呼叫 (CallAnswer)."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    result = await coordinator.api.async_set_service(
        coordinator.device_id,
        coordinator.product_id,
        "CallAnswer",
        {"userInfo": call.data["user_info"]},
    )
    _fire_event(hass, "call_answer", device_id, result=result)


async def async_call_refuse(call: ServiceCall):
    """对话: 拒绝门铃呼叫 (CallRefuse)."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    result = await coordinator.api.async_set_service(
        coordinator.device_id,
        coordinator.product_id,
        "CallRefuse",
        {"index": call.data["index"], "userInfo": call.data["user_info"]},
    )
    _fire_event(hass, "call_refuse", device_id, result=result)


async def async_call_hangup(call: ServiceCall):
    """对话: 挂断门铃呼叫 (CallHangup)."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    result = await coordinator.api.async_set_service(
        coordinator.device_id,
        coordinator.product_id,
        "CallHangup",
        {"userInfo": call.data["user_info"]},
    )
    _fire_event(hass, "call_hangup", device_id, result=result)


async def async_get_voice_reply(call: ServiceCall):
    """对话: 获取语音回复列表 (GetVoiceReply) -> 事件. """
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    result = await coordinator.api.async_set_service(
        coordinator.device_id,
        coordinator.product_id,
        "GetVoiceReply",
        {"relateType": call.data["relate_type"]},
    )
    _fire_event(hass, "voice_reply_list", device_id, result=result)


async def async_set_voice_reply(call: ServiceCall):
    """对话: 设置语音回复 (SetVoiceReply)."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    result = await coordinator.api.async_set_service(
        coordinator.device_id,
        coordinator.product_id,
        "SetVoiceReply",
        {
            "index": call.data["index"],
            "relateType": call.data["relate_type"],
            "timeStab": call.data["time_stab"],
        },
    )
    _fire_event(hass, "voice_reply_set", device_id, result=result)


async def async_create_snapkey(call: ServiceCall):
    """生成临时密码:抓包验证的云消息 API (设备休眠也可用)."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    config = {
        "name": call.data["name"],
        "effective_num": call.data["number"],
        "effective_day": call.data["effect_times"],
        "weekday_mode": "Every day",
        "begin_time": "00:00:00",
        "end_time": "23:59:59",
    }
    if call.data.get("effect_period"):
        # 兼容旧参数:星期/时间段由 effect_period 推断(未归一时按默认每天处理)
        _LOGGER.debug("effect_period provided, using default weekday mapping")
    result = await coordinator.async_create_snapkey_cloud(config)
    coordinator.set_snapkey_result(result)
    _fire_event(hass, "snapkey_created", device_id, result=result)
    await coordinator.async_request_refresh()


async def async_get_snapkey_list(call: ServiceCall):
    """获取临时密码分组列表 (iot.message.SmartLockSecretListV2, 设备休眠可用)."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    result = await coordinator.api.async_smart_lock_secret_list(
        coordinator.device_id, coordinator.product_id, types=3
    )
    if isinstance(result, dict):
        coordinator.set_snapkey_list(result.get("secretGroups") or [])
    _fire_event(hass, "snapkey_list", device_id, result=result)
    await coordinator.async_request_refresh()


async def async_get_open_door_record(call: ServiceCall):
    """获取开门记录(来自轮询到的 lockNoteReport 属性) -> 事件."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    await coordinator.async_request_refresh()
    records = []
    if coordinator.data:
        notes = coordinator.data.get("lock_notes") or []
        if isinstance(notes, list):
            records = notes[-50:]
    _fire_event(hass, "open_door_records", device_id, records=records)


async def async_delete_snapkey(call: ServiceCall):
    """删除临时密码 (iot.message.SmartLockSecretDelete, 消息域;尽量全字段)."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    result = await coordinator.api.async_smart_lock_secret_delete(
        coordinator.device_id,
        coordinator.product_id,
        call.data["key_id"],
        extra=call.data.get("extra") or {},
    )
    _fire_event(hass, "snapkey_deleted", device_id, key_id=call.data["key_id"], result=result)
    await coordinator.async_request_refresh()


async def async_send_sms_code(call: ServiceCall):
    """发送短信/邮箱验证码 (GetValidCode, 会话提交实测 10000).

    usage 与业务一一对应:GrantingCredit(终端授权,默认)/ GenerateSnapkey /
    SMSLogin 等。结果经事件 sms_code_sent 返回。
    """
    hass = call.hass
    coordinator = _first_coordinator(hass)
    if coordinator is None:
        raise HomeAssistantError("未找到已配置的集成(发送验证码需登录会话)")
    await coordinator.api.async_send_sms_code(call.data["account"], usage=call.data["usage"])
    _fire_event(hass, "sms_code_sent", call.data["account"], usage=call.data["usage"])


async def async_authorize_terminal(call: ServiceCall):
    """终端授权提交 (user.account.GrantingCredit, App 源码取证 + 实测).

    实测(2026-09-03):发送验证码(usage=GrantingCredit)10000 ✅;
    提交 → 账号未开终端管理=11001 bad request;App 专用未登录上下文=11010/11001。
    失败时请改在乐橙 App 完成授权(登录页触发终端管理验证)。
    """
    hass = call.hass
    coordinator = _first_coordinator(hass)
    if coordinator is None:
        raise HomeAssistantError("未找到已配置的集成(需登录会话)")
    result = await coordinator.api.async_granting_credit(
        call.data["account"], call.data["valid_code"]
    )
    _fire_event(hass, "terminal_authorized", call.data["account"], result=result)


def _first_coordinator(hass: HomeAssistant):
    for coordinator in hass.data.get(DOMAIN, {}).values():
        return coordinator
    return None


async def async_set_properties(call: ServiceCall):
    """通用属性设置 (iot.control.SetProperties)."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    result = await coordinator.api.async_set_properties(
        coordinator.device_id, coordinator.product_id, call.data["properties"]
    )
    _fire_event(hass, "set_properties", device_id, properties=call.data["properties"])
    await coordinator.async_request_refresh()


async def async_call_service(call: ServiceCall):
    """通用服务调用 (iot.control.SetService),用于调试/高级玩法."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    result = await coordinator.api.async_set_service(
        coordinator.device_id,
        coordinator.product_id,
        call.data["service_name"],
        call.data["input_data"],
        channel_id=call.data["channel_id"],
    )
    _fire_event(
        hass,
        "service_result",
        device_id,
        service=call.data["service_name"],
        result=result,
    )


# ------------------------------------------------------------- 媒体 (WI-005)
def _save_www_image(hass: HomeAssistant, filename: str, data: bytes) -> str | None:
    """保存图片到 <config>/www/lechange_door_lock/, 返回 /local/ URL;失败返回 None。"""
    import os

    safe = os.path.basename(filename.strip().replace("\\", "/")) or ""
    if not safe:
        return None
    if not safe.lower().endswith(".jpg"):
        safe += ".jpg"
    www_dir = hass.config.path("www", "lechange_door_lock")
    try:
        os.makedirs(www_dir, exist_ok=True)
        path = os.path.join(www_dir, safe)
        with open(path, "wb") as f:
            f.write(data)
        return f"/local/lechange_door_lock/{safe}"
    except OSError as err:
        _LOGGER.warning("保存图片到 www 失败: %s", err)
        return None


def _save_www_file(hass: HomeAssistant, filename: str, data: bytes) -> str | None:
    """保存任意文件(预览视频 h264/h265)到 www, 返回 /local/ URL。"""
    import os

    safe = os.path.basename(filename.strip().replace("\\", "/")) or ""
    if not safe:
        return None
    www_dir = hass.config.path("www", "lechange_door_lock")
    try:
        os.makedirs(www_dir, exist_ok=True)
        path = os.path.join(www_dir, safe)
        with open(path, "wb") as f:
            f.write(data)
        return f"/local/lechange_door_lock/{safe}"
    except OSError as err:
        _LOGGER.warning("保存预览到 www 失败: %s", err)
        return None


async def async_doorfront_snapshot(call: ServiceCall):
    """门外截图: 云端 RTSV1 取流抽帧(节流; force 绕过, 电池设备慎用).

    支持按次本地选择 channels/layout/osd(覆盖配置, 不持久化)。
    """
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    media = coordinator.media
    # 按次覆盖(仅本次调用生效)
    overrides = {}
    ch = str(call.data.get("channels") or "").strip()
    if ch:
        overrides["channels"] = [c.strip() for c in ch.split("+") if c.strip()]
    layout = str(call.data.get("layout") or "").strip()
    if layout:
        overrides["layout"] = layout
    osd = str(call.data.get("osd") or "").strip().lower()
    if osd in ("on", "off"):
        overrides["osd"] = osd == "on"

    jpeg = await media.async_cloud_snapshot(
        force=bool(call.data["force"]),
        want_channels=overrides.get("channels"),
        want_layout=overrides.get("layout"),
        want_osd=overrides.get("osd"),
    )
    if not jpeg:
        raise HomeAssistantError(
            "门外截图失败: 设备可能休眠/未配置安全码/ffmpeg 不可用(详见日志)"
        )
    url = None
    if call.data.get("filename"):
        url = await hass.async_add_executor_job(
            _save_www_image, hass, call.data["filename"], jpeg
        )
    _fire_event(
        hass,
        "doorfront_snapshot",
        device_id,
        size=len(jpeg),
        url=url,
        channels=call.data.get("channels", ""),
        layout=call.data.get("layout", ""),
    )


async def async_record_preview_service(call: ServiceCall):
    """实时预览录制: 取流 N 秒 → 可选 OSD 烧录 → 保存并触发事件."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    seconds = int(call.data.get("seconds") or 0) or None
    osd = str(call.data.get("osd") or "").strip().lower()
    with_osd = osd == "on" if osd in ("on", "off") else None
    video, codec = await coordinator.media.async_record_preview(
        seconds=seconds, with_osd=with_osd, channel_id=call.data.get("channel_id", "0")
    )
    if not video:
        raise HomeAssistantError("实时预览失败: 设备可能休眠/未配置安全码(详见日志)")
    url = None
    ext = "h265" if codec == "h265" else "h264"
    filename = call.data.get("filename") or f"preview_{ext}"
    url = await hass.async_add_executor_job(
        _save_www_file, hass, filename, video
    )
    _fire_event(
        hass,
        "record_preview",
        device_id,
        size=len(video),
        codec=codec,
        url=url,
        osd=with_osd,
    )


async def async_alarm_image(call: ServiceCall):
    """告警图: 下载最新(或指定)告警抓拍并解码(DHAV→JPEG, 安全码)."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")

    alarm_id = str(call.data.get("alarm_id") or "").strip()
    alarms = (coordinator.data or {}).get("alarms") or []
    alarm = None
    if alarm_id:
        alarm = next(
            (a for a in alarms if str(a.get("alarmId")) == alarm_id), None
        )
        if alarm is None:
            raise HomeAssistantError(f"告警 {alarm_id} 不在当前缓存(先刷新或检查 ID)")
    else:
        for a in reversed(alarms):
            if a.get("picUrl"):
                alarm = a
                break
    if not alarm or not alarm.get("picUrl"):
        raise HomeAssistantError("当前告警缓存中没有带抓拍图的告警")

    def _pick_pic_url(a: dict) -> str:
        """安全提取告警图 URL: 字符串直用 / list 取首 / dict 取 picUrl 键。"""
        v = a.get("picUrl") or a.get("pic_url") or a.get("thumbUrl") or ""
        if isinstance(v, str):
            return v
        if isinstance(v, list) and v:
            first = v[0]
            return first if isinstance(first, str) else ""
        if isinstance(v, dict):
            inner = v.get("picUrl") or v.get("url") or ""
            return inner if isinstance(inner, str) else ""
        return ""

    jpeg = await coordinator.media.async_alarm_jpeg(_pick_pic_url(alarm))
    if not jpeg:
        raise HomeAssistantError("告警图下载/解码失败(检查安全码配置, 详见日志)")
    url = None
    if call.data.get("filename"):
        url = await hass.async_add_executor_job(
            _save_www_image, hass, call.data["filename"] or f"alarm_{alarm.get('alarmId')}", jpeg
        )
    _fire_event(
        hass,
        "alarm_image",
        device_id,
        alarm_id=alarm.get("alarmId"),
        time=alarm.get("time"),
        title=alarm.get("title"),
        url=url,
    )


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register LeChange services."""
    if hass.services.has_service(DOMAIN, SERVICE_OPEN_DOOR_REMOTE):
        return

    hass.services.async_register(
        DOMAIN, SERVICE_OPEN_DOOR_REMOTE, async_open_door_remote, schema=DEVICE_ID_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_WAKE_UP_DEVICE, async_wake_up_device, schema=DEVICE_ID_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CALL_ANSWER, async_call_answer, schema=CALL_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CALL_REFUSE, async_call_refuse, schema=CALL_REFUSE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CALL_HANGUP, async_call_hangup, schema=CALL_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_GET_VOICE_REPLY, async_get_voice_reply, schema=VOICE_REPLY_GET_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_VOICE_REPLY, async_set_voice_reply, schema=VOICE_REPLY_SET_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_GENERATE_SNAPKEY, async_create_snapkey, schema=CREATE_SNAPKEY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_GET_SNAPKEY_LIST, async_get_snapkey_list, schema=GET_SNAPKEY_LIST_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_SNAPKEY, async_delete_snapkey, schema=DELETE_SNAPKEY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_SMS_CODE, async_send_sms_code, schema=SEND_SMS_CODE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_AUTHORIZE_TERMINAL,
        async_authorize_terminal,
        schema=AUTHORIZE_TERMINAL_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_OPEN_DOOR_RECORD,
        async_get_open_door_record,
        schema=GET_OPEN_DOOR_RECORD_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_PROPERTIES, async_set_properties, schema=SET_PROPERTIES_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CALL_SERVICE, async_call_service, schema=CALL_SERVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DOORFRONT_SNAPSHOT,
        async_doorfront_snapshot,
        schema=DOORFRONT_SNAPSHOT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ALARM_IMAGE, async_alarm_image, schema=ALARM_IMAGE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECORD_PREVIEW,
        async_record_preview_service,
        schema=RECORD_PREVIEW_SCHEMA,
    )

    _LOGGER.debug("LeChange services registered")
