"""Button platform for the LeChange door lock."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EVENT_PREFIX

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up button entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data["device_id"]
    entities = [
        LeChangeOpenDoorButton(coordinator, device_id),
        LeChangeWakeUpButton(coordinator, device_id),
        LeChangeGenerateSnapkeyButton(coordinator, device_id),
        LeChangeRefreshSnapkeyListButton(coordinator, device_id),
    ]
    async_add_entities(entities, True)


class _BaseLeChangeButton(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, device_id: str, translation_key: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{device_id}_{translation_key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, device_id)}}


class LeChangeOpenDoorButton(_BaseLeChangeButton):
    """远程开门 (iot.control.SetService remoteOpenDoor)."""

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "open_door")

    async def async_press(self) -> None:
        coordinator = self.coordinator
        result = await coordinator.api.async_set_service(
            coordinator.device_id,
            coordinator.product_id,
            "remoteOpenDoor",
            {},
        )
        _LOGGER.info("Remote open door result: %s", result)
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX,
            {"type": "open_door", "device_id": self._device_id, "result": result},
        )
        await coordinator.async_request_refresh()


class LeChangeWakeUpButton(_BaseLeChangeButton):
    """唤醒休眠设备 (清除休眠/省电标志后刷新)."""

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "wake_up")

    async def async_press(self) -> None:
        coordinator = self.coordinator
        errors = []
        for prop in ("Dormant", "sleepStatus"):
            try:
                await coordinator.api.async_set_properties(
                    coordinator.device_id, coordinator.product_id, {prop: False}
                )
            except Exception as err:  # noqa: BLE001 — best effort
                _LOGGER.debug("Wake via %s failed: %s", prop, err)
                errors.append(str(err))
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX,
            {"type": "wake_up", "device_id": self._device_id, "errors": errors},
        )
        if not errors:
            async def delayed_refresh():
                await asyncio.sleep(10)
                await coordinator.async_request_refresh()
            self.hass.async_create_task(delayed_refresh())
        else:
            raise HomeAssistantError(f"Wake up device failed: {errors}")


class LeChangeGenerateSnapkeyButton(_BaseLeChangeButton):
    """按实体化配置(名称/次数/天数/星期/时间段)生成临时密码."""

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "generate_snapkey")

    @property
    def extra_state_attributes(self) -> dict:
        result = self.coordinator.last_snapkey_result or {}
        attrs = {}
        if result.get("name"):
            attrs["last_generated_name"] = result["name"]
        if result.get("key"):
            attrs["last_generated_password"] = result["key"]
        return attrs

    async def async_press(self) -> None:
        coordinator = self.coordinator
        config = coordinator.snapkey_config
        result = await coordinator.api.async_set_service(
            coordinator.device_id,
            coordinator.product_id,
            "CreateDeviceSnapkey",
            {
                "name": config["name"],
                "effectTimes": config["effective_day"],
                "number": config["effective_num"],
                "effectPeriod": coordinator.get_snapkey_periods(config),
            },
        )
        # 服务调用成功即视为完成(部分固件无出参时 result 为空 dict)
        coordinator.set_snapkey_result(result or {})
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX,
            {"type": "snapkey_created", "device_id": self._device_id, "result": result},
        )
        self.async_write_ha_state()  # 立即刷新按钮的 last_generated_* 属性
        await coordinator.async_request_refresh()
        _LOGGER.info("Temporary password generated for %s", self._device_id)


class LeChangeRefreshSnapkeyListButton(_BaseLeChangeButton):
    """按需拉取当前临时密码列表."""

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "refresh_snapkey_list")

    async def async_press(self) -> None:
        coordinator = self.coordinator
        result = await coordinator.api.async_set_service(
            coordinator.device_id,
            coordinator.product_id,
            "GetDeviceSnapkeys",
            {"offset": 0, "count": 100},
        )
        keys = result.get("keys") if isinstance(result, dict) else None
        if keys is None:
            raise HomeAssistantError("Get temporary password list failed")
        coordinator.set_snapkey_list(keys)
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX,
            {"type": "snapkey_list", "device_id": self._device_id, "result": result},
        )
        await coordinator.async_request_refresh()
        _LOGGER.info("Temporary password list refreshed for %s", self._device_id)
