"""Switch platform: writable lock properties (童锁/呼叫转接)."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_ID,
    DOMAIN,
    EVENT_PREFIX,
    PROP_CALL_TRANSFER,
    PROP_CHILD_LOCK,
)
from .entity import LeChangeEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up switch entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data[CONF_DEVICE_ID]
    entities = [
        LeChangeChildLockSwitch(coordinator, device_id),
    ]
    async_add_entities(entities)


class _BaseLeChangeSwitch(LeChangeEntity, SwitchEntity):
    def __init__(
        self, coordinator, device_id: str, translation_key: str, prop: str
    ) -> None:
        super().__init__(coordinator, device_id, translation_key)
        self._prop = prop

    def _value(self):
        return (self.coordinator.data or {}).get("props", {}).get(self._prop)

    @property
    def is_on(self) -> bool:
        v = self._value()
        if isinstance(v, bool):
            return v
        return v in (1, "1", "true", "True")

    async def _set_on(self, on: bool) -> None:
        coordinator = self.coordinator
        # bool/enum 属性统一发 "1"/"0"(设备模型 _encode_value 对 bool 亦转 1/0,
        # 但部分设备把属性声明为 int: 发 "1"/"0" 最稳)
        value = "1" if on else "0"
        try:
            await coordinator.api.async_set_properties(
                coordinator.device_id, coordinator.product_id, {self._prop: value}
            )
        except Exception as err:
            _LOGGER.warning("属性 %s 设置失败: %s", self._prop, err)
            raise HomeAssistantError(f"设置 {self._prop} 失败: {err}") from err
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX,
            {"type": "set_property", "device_id": self._device_id,
             "property": self._prop, "value": on},
        )
        await coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        await self._set_on(True)

    async def async_turn_off(self) -> None:
        await self._set_on(False)


class LeChangeChildLockSwitch(_BaseLeChangeSwitch):
    """童锁开关 —— 真实链路: sdl_inOpenDoorModel(171700, enum 1=普通/2=童锁).

    ★ 2026-09-06 实测铁证(SKG8J5RP/R10-Max):
      - child_lock(120000, bool) 是只读上报属性: SetProperties 无论
        bool/"1"/"0"/int、设备在线与否均 40999 device error —— 写不进;
      - 童锁真实写入 = sdl_inOpenDoorModel: {"171700": 2}(童锁) / {"171700": 1}
        (普通) —— SetProperties 返回 10000, GetProperties 读回一致;
      - 设备休眠时先唤醒: things.media.GetRealTransferStreamUrl 请求
        自带唤醒语义(实测 sleep → ~2-4s online), 然后 SetProperties。

    读: resolve_child_lock 已兜底(171700==2 → 开)。写: 走 171700。
    """

    WRITE_PROP = "sdl_inOpenDoorModel"  # 171700

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "child_lock", PROP_CHILD_LOCK)

    async def _set_on(self, on: bool) -> None:
        coordinator = self.coordinator
        # ★ 写前唤醒(尽力而为): GetRealTransferStreamUrl 自带唤醒语义,
        #   sleep → ~2-4s online; 已在线则无副作用。失败不阻断(设备在线
        #   窗口由唤醒请求触发, SetProperties 在线时才被设备接受)。
        try:
            await coordinator.ensure_awake(max_wait=12.0)
        except Exception as err:  # noqa: BLE001 — 唤醒尽力而为
            _LOGGER.debug("child-lock wake failed (continuing): %s", err)
        # ★ 171700 enum: 2=童锁模式 / 1=普通开门模式(实测 ref 键 int 值 10000)
        value = 2 if on else 1
        try:
            await coordinator.api.async_set_properties(
                coordinator.device_id, coordinator.product_id,
                {self.WRITE_PROP: value},
            )
        except Exception as err:
            _LOGGER.warning("属性 %s 设置失败: %s", self.WRITE_PROP, err)
            raise HomeAssistantError(f"设置童锁失败: {err}") from err
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX,
            {"type": "set_property", "device_id": self._device_id,
             "property": self.WRITE_PROP, "value": value},
        )
        await coordinator.async_request_refresh()