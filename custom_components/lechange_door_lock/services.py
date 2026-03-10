"""Service handlers for LeChange integration."""

import logging
import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.exceptions import HomeAssistantError

from .const import (
    DOMAIN,
    SERVICE_GENERATE_SNAPKEY,
    SERVICE_GET_SNAPKEY_LIST,
    SERVICE_OPEN_DOOR_REMOTE,
    SERVICE_WAKE_UP_DEVICE,
    SERVICE_GET_OPEN_DOOR_RECORD,
)

_LOGGER = logging.getLogger(__name__)


EFFECT_PERIOD_SCHEMA = vol.Schema(
    {
        vol.Required("week"): cv.string,
        vol.Required("beginTime"): cv.string,
        vol.Required("endTime"): cv.string,
    }
)

GENERATE_SNAPKEY_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("name"): cv.string,
        vol.Required("effective_num"): vol.Coerce(int),
        vol.Required("effective_day"): vol.Coerce(int),
        vol.Required("effect_period"): vol.All(cv.ensure_list, [EFFECT_PERIOD_SCHEMA]),
        vol.Required("begin_time"): cv.string,
        vol.Required("end_time"): cv.string,
    }
)

GET_OPEN_DOOR_RECORD_SCHEMA = vol.Schema({
    vol.Required("device_id"): cv.string,
    vol.Optional("count", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
})

DEVICE_ID_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
    }
)


def _get_coordinator(hass: HomeAssistant, device_id: str):
    """Find coordinator by device_id."""
    for coordinator in hass.data.get(DOMAIN, {}).values():
        if coordinator.device_id == device_id:
            return coordinator
    return None


async def async_generate_snapkey(call: ServiceCall):
    """Generate snapkey."""
    hass: HomeAssistant = call.hass
    data = call.data
    device_id = data["device_id"]

    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")

    api = coordinator.api

    result = await api.async_generate_snapkey(
        data["name"],
        data["effective_num"],
        data["effective_day"],
        data["effect_period"],
        data["begin_time"],
        data["end_time"],
    )

    if result is None:
        raise HomeAssistantError("Generate snapkey failed")

    _LOGGER.debug("Generate snapkey result: %s", result)


async def async_get_snapkey_list(call: ServiceCall):
    """Get snapkey list."""
    hass: HomeAssistant = call.hass
    device_id = call.data["device_id"]

    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")

    result = await coordinator.api.async_get_snapkey_list()

    if result is None:
        raise HomeAssistantError("Get snapkey list failed")

    _LOGGER.debug("Snapkey list: %s", result)


async def async_open_door_remote(call: ServiceCall):
    """Remote open door."""
    hass: HomeAssistant = call.hass
    device_id = call.data["device_id"]

    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")

    result = await coordinator.api.async_open_door_remote()

    if result is None:
        raise HomeAssistantError("Remote open door failed")

    _LOGGER.debug("Remote open door result: %s", result)


async def async_wake_up_device(call: ServiceCall):
    """Wake up device."""
    hass: HomeAssistant = call.hass
    device_id = call.data["device_id"]

    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")

    result = await coordinator.api.async_wake_up_device()

    if result is None:
        raise HomeAssistantError("Wake up device failed")

    _LOGGER.debug("Wake up result: %s", result)

async def async_get_open_door_record(call: ServiceCall):
    """Handle get_open_door_record service call."""
    hass = call.hass
    device_id = call.data["device_id"]
    count = call.data.get("count", 30)

    coordinator = _get_coordinator(hass, device_id)
    if not coordinator:
        raise HomeAssistantError(f"Device {device_id} not found")

    result = await coordinator.api.async_get_open_door_record(count=count)
    if result is None:
        raise HomeAssistantError("Failed to get open door records")

    _LOGGER.debug("Open door records for %s: %s", device_id, result)

    # 可选：将结果作为事件发送，方便自动化捕获
    hass.bus.async_fire(f"{DOMAIN}_open_door_records", {
        "device_id": device_id,
        "records": result.get("records", [])
    })

async def async_setup_services(hass: HomeAssistant) -> None:
    """Register LeChange services."""

    if hass.services.has_service(DOMAIN, SERVICE_OPEN_DOOR_REMOTE):
        return

    hass.services.async_register(
        DOMAIN,
        SERVICE_GENERATE_SNAPKEY,
        async_generate_snapkey,
        schema=GENERATE_SNAPKEY_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_SNAPKEY_LIST,
        async_get_snapkey_list,
        schema=DEVICE_ID_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_OPEN_DOOR_REMOTE,
        async_open_door_remote,
        schema=DEVICE_ID_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_WAKE_UP_DEVICE,
        async_wake_up_device,
        schema=DEVICE_ID_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_OPEN_DOOR_RECORD,
        async_get_open_door_record,
        schema = GET_OPEN_DOOR_RECORD_SCHEMA,
    )

    _LOGGER.debug("LeChange services registered")
