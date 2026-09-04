"""LeChange (Imou) door lock integration.

Uses the client-side cloud API of the official mobile clients
instead of the slow-to-update Open Platform API.
"""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_FIRMWARE_VERSION,
    CONF_MODEL_NAME,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import LeChangeDataUpdateCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration (called by HA with configuration.yaml)."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("gt4_listeners", {})

    # GT4 本地滑块: 注册 HA 原生 view(挂在 HA 自身 HTTP 端口, 容器部署零配置).
    # 页面: GET /api/lechange/gt4/slides (config_flow 生成时缓存 HTML)
    # 回传: POST /api/lechange/gt4/tuple (requires_auth=False, 四元组单次有效)
    from .gt4_helper import GT4TupleListener, build_ha_views

    listener = GT4TupleListener()  # 无回调: config_flow 在用时注入
    for view in build_ha_views(listener):
        hass.http.register_view(view)
    hass.data[DOMAIN]["gt4_listener"] = listener
    _LOGGER.info("LeChange GT4 slider view registered on HA http port")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a LeChange door lock from a config entry."""
    _LOGGER.debug("Starting async_setup_entry for device %s", entry.data[CONF_DEVICE_ID])

    coordinator = LeChangeDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug("Coordinator first refresh completed")

    # 注册设备
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
        name=entry.data.get(CONF_DEVICE_NAME) or entry.title,
        manufacturer="LeChange",
        model=entry.data.get(CONF_MODEL_NAME) or "SmartLock",
        sw_version=entry.data.get(CONF_FIRMWARE_VERSION),
        serial_number=entry.data[CONF_DEVICE_ID],
    )

    # 立即拉取一次型号/固件信息
    await coordinator.async_update_device_info()

    # MQTT 实时通道: 后台连接, 断线自动重试; 失败不影响轮询
    await coordinator.async_start_mqtt()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await async_setup_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry."""
    coordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        await coordinator.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
