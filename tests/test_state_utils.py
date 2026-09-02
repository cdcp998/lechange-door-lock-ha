"""Tests for pure state-derivation helpers."""

from lechange_door_lock.state_utils import (
    derive_door_state,
    derive_lock_state,
    extract_batteries,
    normalize_wifi,
)


class TestDeriveLockState:
    def test_status_wins(self):
        assert derive_lock_state(0, 1, "beOpened") is True
        assert derive_lock_state(1, 0, "beClosed") is False

    def test_unknown_status_falls_back_to_door_state(self):
        assert derive_lock_state(2, 0, "") is True
        assert derive_lock_state(2, 1, "") is False

    def test_falls_back_to_device_list_lock_state(self):
        assert derive_lock_state(None, None, "beClosed") is True
        assert derive_lock_state(None, None, "beOpened") is False
        assert derive_lock_state(None, None, "beAjar") is False

    def test_unknown(self):
        assert derive_lock_state(None, None, "") is None
        assert derive_lock_state(2, None, "") is None


class TestDeriveDoorState:
    def test_from_property(self):
        assert derive_door_state(0, "beOpened") == "closed"
        assert derive_door_state(1, "beClosed") == "open"

    def test_from_device_list(self):
        assert derive_door_state(None, "beClosed") == "closed"
        assert derive_door_state(None, "beAjar") == "open"

    def test_unknown(self):
        assert derive_door_state(None, "") == "unknown"


class TestExtractBatteries:
    def test_split_of_two_batteries(self):
        props = {
            "devicePowerLock": [
                {"elecPercent": 59, "type": 1, "state": 1},
                {"elecPercent": 94, "type": 0, "state": 1},
            ]
        }
        lock, camera = extract_batteries(props)
        assert lock == 59
        assert camera == 94

    def test_missing_battery(self):
        assert extract_batteries({"devicePowerLock": []}) == (None, None)
        assert extract_batteries({}) == (None, None)

    def test_string_percent(self):
        lock, camera = extract_batteries(
            {"devicePowerLock": [{"elecPercent": "59", "type": 1}]}
        )
        assert lock == 59
        assert camera is None

    def test_garbage_items_skipped(self):
        assert extract_batteries(
            {"devicePowerLock": ["bad", {"elecPercent": "x", "type": 1}]}
        ) == (None, None)


class TestNormalizeWifi:
    def test_struct_dropdown(self):
        wifi = normalize_wifi(
            {"wifiDoorLock": {"SSID": "HomeWifi", "status": 2, "intensity": 4, "auth": "WPA2"}}
        )
        assert wifi == {
            "ssid": "HomeWifi",
            "status": 2,
            "intensity": 4,
            "auth": "WPA2",
        }

    def test_none_when_absent(self):
        assert normalize_wifi({}) is None

    def test_string_values_converted(self):
        wifi = normalize_wifi({"wifiDoorLock": {"SSID": "X", "status": "2", "intensity": "4"}})
        assert wifi["status"] == 2
        assert wifi["intensity"] == 4
