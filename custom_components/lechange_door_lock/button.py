"""Button platform for the LeChange door lock."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .camera import LeChangeCameraEntity
from .const import CONF_DEVICE_ID, DOMAIN, EVENT_PREFIX
from .entity import LeChangeEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    """Set up button entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data[CONF_DEVICE_ID]
    entities = [
        LeChangeOpenDoorButton(coordinator, device_id),
        LeChangeSnapshotDoorButton(coordinator, device_id),
        LeChangeGenerateSnapkeyButton(coordinator, device_id),
        LeChangeRefreshSnapkeyListButton(coordinator, device_id),
    ]
    async_add_entities(entities)


class _BaseLeChangeButton(LeChangeEntity, ButtonEntity):
    def __init__(self, coordinator, device_id: str, translation_key: str) -> None:
        super().__init__(coordinator, device_id, translation_key)


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


class LeChangeSnapshotDoorButton(_BaseLeChangeButton):
    """获取门外截图:拉取摄像头(门铃常在线)即唤醒锁体,快照保存到 HA www/ 目录."""

    _attr_icon = "mdi:camera"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "snapshot_door")
        self._attr_translation_placeholders = {"channel_id": "0"}

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "last_snapshot_url": str(
                self.coordinator.entry.options.get("last_snapshot_url", "")
            )
        }

    async def async_press(self) -> None:
        coordinator = self.coordinator
        image = await LeChangeCameraEntity.capture_image_via_options(coordinator, "0")
        if not image:
            raise HomeAssistantError(
                "获取门外截图失败:请确认集成选项已配置局域网 rtsp_host(及 RTSP 凭据)且设备在线"
            )

        def _write() -> str:
            www = Path(coordinator.hass.config.path("www"))
            (www / "lechange_door_lock").mkdir(parents=True, exist_ok=True)
            filename = f"lechange_{self._device_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            (www / "lechange_door_lock" / filename).write_bytes(image)
            return f"/local/lechange_door_lock/{filename}"

        url = await coordinator.hass.async_add_executor_job(_write)
        # 记录最近一次快照地址
        if isinstance(coordinator.entry.options, dict):
            coordinator.hass.config_entries.async_update_entry(
                coordinator.entry, options={**coordinator.entry.options, "last_snapshot_url": url}
            )
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX,
            {"type": "snapshot", "device_id": self._device_id, "url": url,
             "bytes": len(image)},
        )
        self.async_write_ha_state()
        _LOGGER.info("Door snapshot saved: %s (%d bytes)", url, len(image))


class LeChangeGenerateSnapkeyButton(_BaseLeChangeButton):
    """按实体化配置(名称/次数/天数/星期/时间段)生成临时密码.

    纯消息域云 API(iot.message.SmartLockSecretAdd):客户端自产 keyId/tempKey,
    不使用 iot.control.SetService(避免触发身份验证码),设备休眠也可用。
    """

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
        # 客户端生成 tempKey,经消息域 Add 直接登记(实测)
        result = await coordinator.async_create_snapkey_cloud()
        coordinator.set_snapkey_result(result)
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX,
            {"type": "snapkey_created", "device_id": self._device_id, "result": result},
        )
        self.async_write_ha_state()  # 立即刷新按钮的 last_generated_* 属性
        await coordinator.async_request_refresh()
        _LOGGER.info("Temporary password generated for %s", self._device_id)


class LeChangeRefreshSnapkeyListButton(_BaseLeChangeButton):
    """按需拉取当前临时密码分组列表(消息域,设备休眠可用)."""

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "refresh_snapkey_list")

    async def async_press(self) -> None:
        coordinator = self.coordinator
        result = await coordinator.api.async_smart_lock_secret_list(
            coordinator.device_id, coordinator.product_id, types=3
        )
        groups = result.get("secretGroups") if isinstance(result, dict) else None
        if groups is None:
            raise HomeAssistantError("Get temporary password list failed")
        coordinator.set_snapkey_list(groups)
        coordinator.hass.bus.async_fire(
            EVENT_PREFIX,
            {"type": "snapkey_list", "device_id": self._device_id, "result": result},
        )
        await coordinator.async_request_refresh()
        _LOGGER.info("Temporary password list refreshed for %s", self._device_id)
