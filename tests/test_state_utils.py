"""Tests for pure state-derivation helpers."""

from lechange_door_lock.state_utils import (
    build_snapkey_periods,
    derive_door_state,
    derive_lock_state,
    extract_batteries,
    format_open_door_time,
    normalize_wifi,
    open_method_label,
    weekday_mode_to_periods,
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


class TestSnapkeyWeekdays:
    def test_modes(self):
        assert weekday_mode_to_periods("Every day") == [0, 1, 2, 3, 4, 5, 6]
        assert weekday_mode_to_periods("Weekdays") == [1, 2, 3, 4, 5]
        assert weekday_mode_to_periods("Weekend") == [0, 6]
        assert weekday_mode_to_periods("Monday") == [1]
        assert weekday_mode_to_periods("Sunday") == [0]
        assert weekday_mode_to_periods("Saturday") == [6]

    def test_unknown_mode_defaults_to_every_day(self):
        assert weekday_mode_to_periods("Nope") == [0, 1, 2, 3, 4, 5, 6]

    def test_build_periods(self):
        periods = build_snapkey_periods("08:00:00", "20:00:00", "Weekdays")
        assert periods == [
            {"period": 1, "beginTime": "08:00:00", "endTime": "20:00:00"},
            {"period": 2, "beginTime": "08:00:00", "endTime": "20:00:00"},
            {"period": 3, "beginTime": "08:00:00", "endTime": "20:00:00"},
            {"period": 4, "beginTime": "08:00:00", "endTime": "20:00:00"},
            {"period": 5, "beginTime": "08:00:00", "endTime": "20:00:00"},
        ]


class TestOpenRecordDisplay:
    def test_format_time_local_format(self):
        assert format_open_door_time("20260625T003156") == "2026-06-25 00:31:56"

    def test_format_time_already_formatted(self):
        assert format_open_door_time("2026-06-25 00:31:56") == "2026-06-25 00:31:56"

    def test_format_time_unknown_passthrough(self):
        assert format_open_door_time("garbage") == "garbage"

    def test_method_label(self):
        assert open_method_label(2) == "指纹"
        assert open_method_label("15") == "远程用户"
        assert open_method_label(99) == "99"
