import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PLATFORMS, CONF_DEVICE_ID
from .coordinator import LeChangeDataUpdateCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.debug("Starting async_setup_entry for device %s", entry.data[CONF_DEVICE_ID])

    coordinator = LeChangeDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug("Coordinator first refresh completed, data: %s", coordinator.data)

    # 注册设备（使用临时型号）
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
        name=entry.title,
        manufacturer="LeChange",
        model="Unknown",
        serial_number = "Unknown",
    )

    # 立即更新设备信息（型号、版本）
    await coordinator.async_update_device_info()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await async_setup_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        await coordinator.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok