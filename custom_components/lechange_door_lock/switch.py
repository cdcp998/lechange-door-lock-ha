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

    def _value(self):
        """★ 单一真源: 与 binary_sensor 完全一致用 resolve_child_lock
        (171700 优先 + child_lock 兜底) — 两处永不相反。
        """
        from .state_utils import resolve_child_lock

        resolved = resolve_child_lock((self.coordinator.data or {}).get("props", {}))
        return bool(resolved) if resolved is not None else None

    @property
    def is_on(self) -> bool:
        return bool(self._value())

    async def _set_on(self, on: bool) -> None:
        coordinator = self.coordinator
        value = 2 if on else 1  # 171700 enum: 2=童锁模式 / 1=普通开门模式

        # ★ 官方链路(逆向 main.jsbundle 铁证): 写属性 = 直接
        #   iot.control.SetProperties {properties:{171700: value}},
        #   不唤醒/不取流/不 await 在线 — 官方允许休眠设备直接写,
        #   "下次设备唤醒后生效"(feature_take_effect_next_device)。
        #   RTS 采流"真唤醒"是自造多余动作(耗电/残留/等待源), 已去除。
        try:
            await coordinator.api.async_set_properties(
                coordinator.device_id, coordinator.product_id,
                {self.WRITE_PROP: value},
            )
        except Exception as err:
            _LOGGER.warning("属性 %s 设置失败: %s", self.WRITE_PROP, err)
            raise HomeAssistantError(f"设置童锁失败: {err}") from err
        # ★ 乐观同步 props(写入成功即视为目标状态, 避免回弹; 设备应答
        #   延迟/下次唤醒生效由轮询纠正)
        coordinator.data.setdefault("props", {})[self.WRITE_PROP] = value
        _LOGGER.info("child-lock 写入 %s=%s(官方链路, 不唤醒)", self.WRITE_PROP, value)
        # ★ 广播 coordinator 状态变化(实体立即重算 is_on — 否则 HA 等
        #   轮询才感知 props 变化 → '点击后 1 分钟才看到状态改变')
        try:
            coordinator.async_set_updated_data(coordinator.data)
        except Exception as upd_err:  # noqa: BLE001
            _LOGGER.debug("coordinator state broadcast failed: %s", upd_err)
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX,
            {"type": "set_property", "device_id": self._device_id,
             "property": self.WRITE_PROP, "value": value},
        )
        # ★ 不再 async_request_refresh / 不再 RTS 唤醒: props 已同步,
        #   轮询自会读到设备确认值; RTS 残留/等待/耗电一并消除。
