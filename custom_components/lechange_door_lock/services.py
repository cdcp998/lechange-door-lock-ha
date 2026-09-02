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
    SERVICE_GENERATE_SNAPKEY,
    SERVICE_GET_OPEN_DOOR_RECORD,
    SERVICE_GET_SNAPKEY_LIST,
    SERVICE_GET_VOICE_REPLY,
    SERVICE_OPEN_DOOR_REMOTE,
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

SET_PROPERTIES_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("properties"): dict,
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
    """远程开门: iot.control.SetService remoteOpenDoor."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    result = await coordinator.api.async_set_service(
        coordinator.device_id, coordinator.product_id, "remoteOpenDoor", {}
    )
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
    """生成临时密码 (CreateDeviceSnapkey) -> 事件带 key."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    result = await coordinator.api.async_set_service(
        coordinator.device_id,
        coordinator.product_id,
        "CreateDeviceSnapkey",
        {
            "name": call.data["name"],
            "effectTimes": call.data["effect_times"],
            "number": call.data["number"],
            "effectPeriod": call.data["effect_period"],
        },
    )
    _fire_event(hass, "snapkey_created", device_id, result=result)


async def async_get_snapkey_list(call: ServiceCall):
    """获取临时密码列表 (GetDeviceSnapkeys) -> 事件."""
    hass, device_id = call.hass, call.data["device_id"]
    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")
    result = await coordinator.api.async_set_service(
        coordinator.device_id,
        coordinator.product_id,
        "GetDeviceSnapkeys",
        {"offset": call.data["offset"], "count": call.data["count"]},
    )
    _fire_event(hass, "snapkey_list", device_id, result=result)


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

    _LOGGER.debug("LeChange services registered")
