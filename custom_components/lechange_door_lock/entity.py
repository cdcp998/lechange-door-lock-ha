"""Shared entity base for the LeChange integration (no HA runtime needed)."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class LeChangeEntity(CoordinatorEntity):
    """Base entity linked to one LeChange device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, device_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = {"identifiers": {(DOMAIN, device_id)}}

    @property
    def data(self) -> dict:
        """Coordinator data with an empty-dict fallback."""
        return self.coordinator.data or {}
