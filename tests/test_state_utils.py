"""Tests for pure state-derivation helpers."""

from datetime import datetime, timedelta, timezone

from lechange_door_lock.state_utils import (
    build_usage_period,
    derive_door_state,
    derive_lock_state,
    extract_batteries,
    extract_latest_open_record,
    format_alarm_entries,
    format_alarm_fields,
    format_alarm_line,
    format_open_door_time,
    format_snapkey_fields,
    format_snapkey_line,
    format_snapkey_lines,
    format_snapkeys_display,
    normalize_wifi,
    open_method_label,
    parse_alarm_title,
    sort_records_by_time,
    work_mode_option,
    work_mode_value,
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
    """生效星期系统自动分配(每天, 掩码 127); 星期选择项已移除."""

    def test_usage_period_always_every_day_mask(self):
        now = datetime(2026, 9, 3, 12, 0, 0)
        # 掩码恒为 127(每天), 与任何"星期模式"无关(配置项已不存在)
        assert build_usage_period(1, now) == "127-20260903T0000Z-20260904T2359Z"

    def test_build_usage_period_multiple_days(self):
        now = datetime(2026, 9, 3, 12, 0, 0)
        assert build_usage_period(3, now) == "127-20260903T0000Z-20260906T2359Z"

    def test_build_usage_period_min_one_day(self):
        now = datetime(2026, 9, 3, 12, 0, 0)
        assert build_usage_period(0, now) == "127-20260903T0000Z-20260904T2359Z"


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


class TestWorkModeMapping:
    """工作模式 powerMode(0 自动/1 正常/2 省电/3 超级省电)↔ 选项键."""

    def test_value_to_option(self):
        assert work_mode_option(0) == "auto"
        assert work_mode_option(1) == "normal"
        assert work_mode_option(2) == "power_saving"
        assert work_mode_option(3) == "super_power_saving"

    def test_string_value_tolerated(self):
        assert work_mode_option("2") == "power_saving"

    def test_unknown_values(self):
        assert work_mode_option(4) is None
        assert work_mode_option(-1) is None
        assert work_mode_option(True) is None  # bool 防 0/1 误判
        assert work_mode_option(None) is None
        assert work_mode_option("auto") is None  # 原始值是数字枚举

    def test_option_to_value_roundtrip(self):
        for value, option in ((0, "auto"), (1, "normal"),
                              (2, "power_saving"), (3, "super_power_saving")):
            assert work_mode_value(option) == value
            assert work_mode_option(work_mode_value(option)) == option

    def test_invalid_option(self):
        assert work_mode_value("garbage") is None
        assert work_mode_value("") is None
        assert work_mode_value(None) is None
        assert work_mode_value(2) is None  # 必须是选项键字符串


class TestAlarmDisplay:
    """告警可读显示: 状态行 '行为 · 标题'(不含时间); 属性条目含时间键."""

    def test_label_type_behavior_mapping(self):
        # 抓包实锤: R10-Max 开门/出门事件 labelType 均为 accessAlarm;
        # 状态行只显示 行为 · 标题(时间在属性结构化条目里)
        line = format_alarm_line({
            "time": "20260905T082336",
            "labelType": "accessAlarm",
            "title": "伟使用指纹开门了",
        })
        assert line == "开门/出门事件 · 伟使用指纹开门了"

    def test_fields_structured_order(self):
        fields = format_alarm_fields({
            "time": "20260905T082336",
            "labelType": "accessAlarm",
            "title": "伟使用指纹开门了",
        })
        assert fields == {
            "行为": "开门/出门事件",
            "标题": "伟使用指纹开门了",
            "时间": "2026-09-05 08:23:36",
        }

    def test_fields_unknown_label_degrades(self):
        fields = format_alarm_fields({"labelType": "moveDetect"})
        assert fields == {"行为": "moveDetect", "标题": "未知标题", "时间": "未知时间"}
        assert format_alarm_fields({}) == {
            "行为": "未知行为", "标题": "未知标题", "时间": "未知时间",
        }
        assert format_alarm_fields("garbage") == {}

    def test_unknown_text_label_passthrough(self):
        line = format_alarm_line({
            "time": "20260905T082336",
            "labelType": "门口机呼叫",
            "title": "伟使用指纹开门了",
        })
        assert line == "门口机呼叫 · 伟使用指纹开门了"

    def test_unknown_camel_label_passthrough(self):
        # 未收录类型码原样透传(捕获后可补映射表)
        line = format_alarm_line({"time": "20260905T082336",
                                  "labelType": "moveDetect"})
        assert line == "moveDetect · 未知标题"

    def test_p_timestamp_fallback(self):
        # 时间回退仍工作(属性条目里可见), 但状态行不含时间
        fields = format_alarm_fields({"pTimestamp": "1788567816", "title": "有人出门了"})
        assert fields["时间"] == "2026-09-05 08:23:36"
        assert "2026" not in format_alarm_line({"pTimestamp": "1788567816"})

    def test_numeric_label_type_suppressed(self):
        # labelType 是枚举码(纯数字) → 行为降级为"未知行为"(码不外露)
        line = format_alarm_line({"time": "20260905T082336", "labelType": "12",
                                  "title": "侦测到移动"})
        assert line == "未知行为 · 侦测到移动"

    def test_line_missing_fields_degrade(self):
        assert format_alarm_line({}) == "未知行为 · 未知标题"
        assert format_alarm_line("garbage") == ""
        assert format_alarm_line(None) == ""

    def test_entries_newest_first_structured(self):
        alarms = [
            {"time": "20260905T080000", "title": "出门"},
            {"time": "20260905T082336", "title": "指纹开门"},
        ]
        entries = format_alarm_entries(alarms)
        assert entries[0]["标题"] == "指纹开门"   # 最新在最上
        assert entries[1]["标题"] == "出门"
        assert format_alarm_entries(alarms, newest_first=False)[0]["标题"] == "出门"

    def test_entries_skip_bad_elements(self):
        assert format_alarm_entries(["bad", None, 5]) == []
        assert format_alarm_entries(None) == []


class TestAlarmTitleParsing:
    """云侧告警无 keyType/name 结构化字段, 方式与用户在 title 文本里."""

    def test_named_user_with_method(self):
        assert parse_alarm_title("伟使用指纹开门了") == ("伟", "指纹")
        assert parse_alarm_title("爸爸使用卡片开门了") == ("爸爸", "卡片")

    def test_exit_event_maps_to_indoor(self):
        # "有人出门了": 服务端不给方式 → 室内开门(出门按钮)
        assert parse_alarm_title("有人出门了") == ("", "出门按钮/室内开门")

    def test_anonymous_user_cleared(self):
        assert parse_alarm_title("有人使用密码开门了") == ("", "密码")

    def test_temp_password_longest_match_first(self):
        assert parse_alarm_title("伟使用临时密码开门了")[1] == "临时密码"

    def test_unparseable_passthrough(self):
        assert parse_alarm_title("侦测到移动") == ("", "")
        assert parse_alarm_title("") == ("", "")


class TestAlarmTimeKeys:
    def test_time_field_wins_over_p_timestamp(self):
        # time(事件时间)优先; pTimestamp(服务器时间)仅作回退
        rec = {"time": "20260905T080000", "pTimestamp": "1788567816"}
        merged = extract_latest_open_record(None, rec)
        assert merged["time"] == "2026-09-05 08:00:00"

    def test_p_timestamp_fallback_epoch_seconds(self):
        # 1788567816 = 2026-09-05 00:23:36 UTC = 08:23:36 UTC+8
        merged = extract_latest_open_record(None, {"pTimestamp": "1788567816"})
        assert merged["time"] == "2026-09-05 08:23:36"

    def test_p_timestamp_fallback_epoch_millis(self):
        merged = extract_latest_open_record(None, {"pTimestamp": "1788567816000"})
        assert merged["time"] == "2026-09-05 08:23:36"

    def test_epoch_formatted_for_sorting(self):
        assert format_open_door_time("1788567816") == "2026-09-05 08:23:36"

    def test_garbage_time_passthrough(self):
        assert format_open_door_time("garbage") == "garbage"


class TestAlarmOrdering:
    def test_sort_oldest_to_newest(self):
        alarms = [
            {"time": "20260905T082336", "title": "伟使用指纹开门了"},
            {"time": "20260904T090000", "title": "有人出门了"},
            {"title": "缺时间的记录沉底"},
        ]
        ordered = sort_records_by_time(alarms)
        assert ordered[-1]["title"] == "伟使用指纹开门了"
        assert ordered[0]["title"] == "缺时间的记录沉底"

    def test_no_record_lost_on_unparseable_time(self):
        alarms = [{"time": "bad"}, {"time": "20260905T082336", "title": "x"}]
        ordered = sort_records_by_time(alarms)
        assert len(ordered) == 2


class TestLatestOpenRecordFromCloudAlarms:
    """用户实测数据回归: refId 339600/328800 双事件 + pTimestamp."""

    USER_ALARMS = [
        {"refId": 328800, "pTimestamp": "1788566400",
         "time": "20260905T080000", "title": "有人出门了"},
        {"refId": 339600, "pTimestamp": "1788567816",
         "time": "20260905T082336", "title": "伟使用指纹开门了"},
    ]

    def test_latest_open_alarm_parsed(self):
        merged = extract_latest_open_record(None, self.USER_ALARMS)
        assert merged["time"] == "2026-09-05 08:23:36"   # time 字段, 非 pTimestamp
        assert merged["method"] == "指纹"
        assert merged["user"] == "伟"

    def test_exit_only_alarm(self):
        merged = extract_latest_open_record(None, [self.USER_ALARMS[0]])
        assert merged["time"] == "2026-09-05 08:00:00"
        assert merged["method"] == "出门按钮/室内开门"
        assert merged["user"] == ""

    def test_motion_alarm_not_treated_as_open(self):
        # 新时间但非开门类 → 不混入"最近开门"
        alarms = [
            {"time": "20260905T090000", "title": "侦测到移动"},
            {"time": "20260905T082336", "title": "伟使用指纹开门了"},
        ]
        merged = extract_latest_open_record(None, alarms)
        assert merged["method"] == "指纹"
        assert merged["time"] == "2026-09-05 08:23:36"

    def test_structured_keytype_still_wins_over_title(self):
        rec = {"time": "20260905T082336", "keyType": 0, "title": "伟使用指纹开门了"}
        merged = extract_latest_open_record(None, rec)
        assert merged["method"] == "密码"  # keyType 枚举优先于文本解析

    def test_props_still_beats_older_alarm(self):
        merged = extract_latest_open_record(
            {"localTime": "20260905T120000", "keyType": 0, "name": "爸爸"},
            self.USER_ALARMS,
        )
        assert merged["user"] == "爸爸"
        assert merged["time"] == "2026-09-05 12:00:00"

    def test_alarms_lose_to_newer_props(self):
        merged = extract_latest_open_record(
            {"localTime": "20260905T060000", "keyType": 1},
            self.USER_ALARMS,
        )
        assert merged["user"] == "伟"  # 告警 08:23 更新于属性 06:00


class TestSnapkeyDisplay:
    """临时密码显示文本: 名称 | 密码 | 过期时间 | 次数 | 状态."""

    # 固定"当前时间"(UTC+8): 2026-09-06 10:00
    NOW = datetime(2026, 9, 6, 10, 0, tzinfo=timezone(timedelta(hours=8)))

    # 用户实测样例(expiredTime = createTime + 24h)
    SAMPLE = [
        {"number": -1, "effectTimes": "1", "createTime": "1788556812",
         "name": "Home Assistant", "keyId": "582978981",
         "usagePeriod": "127-20260905T0000Z-20260906T2359Z",
         "tempKey": "89561560", "state": 0, "type": 3,
         "expiredTime": "1788643212"},   # → 2026-09-06 05:20:12 UTC+8
        {"number": -1, "effectTimes": "1", "createTime": "1788550539",
         "name": "Home Assistant", "keyId": "583906087",
         "usagePeriod": "127-20260905T0000Z-20260906T2359Z",
         "tempKey": "46753218", "state": 1, "type": 3,
         "expiredTime": "1788636939"},   # → 2026-09-06 03:35:39 UTC+8
    ]

    def test_sample_entries(self):
        # 返回结构化条目列表(每条一个 dict): more-info 逐块渲染, 分行且不截断
        entries = format_snapkeys_display(self.SAMPLE, now=self.NOW)
        assert isinstance(entries, list) and all(isinstance(e, dict) for e in entries)
        # createTime 从新到旧: 样例[0] 较新在前
        # ★ 次数读 number(-1=不限), 不读 effectTimes(有效天数)—— 抓包实证
        assert entries[0] == {
            "名称": "Home Assistant", "密码": "89561560",
            "过期时间": "2026-09-06 05:20:12",
            "次数": "无限次", "状态": "未使用(已过期)",
        }
        assert entries[1] == {
            "名称": "Home Assistant", "密码": "46753218",
            "过期时间": "2026-09-06 03:35:39",
            "次数": "无限次", "状态": "已使用",
        }

    def test_sample_lines(self):
        # 编号属性场景: 单行串列表(新→旧)
        lines = format_snapkey_lines(self.SAMPLE, now=self.NOW)
        assert lines[0] == (
            "Home Assistant | 89561560 | 2026-09-06 05:20:12 | 无限次 | 未使用(已过期)"
        )
        assert lines[1] == (
            "Home Assistant | 46753218 | 2026-09-06 03:35:39 | 无限次 | 已使用"
        )

    def test_expired_time_preferred_over_usage_period(self):
        line = format_snapkey_line(self.SAMPLE[0], now=self.NOW)
        assert "05:20:12" in line  # 来自 expiredTime, 非 usagePeriod 的 23:59
        assert "(UTC)" not in line

    def test_usage_period_fallback_with_utc_marker(self):
        # 无 expiredTime → usagePeriod 结束时刻, 显示 UTC 挂钟 + "(UTC)" 标注
        rec = {"name": "P", "tempKey": "12345678",
               "usagePeriod": "127-20260905T0000Z-20260906T2359Z"}
        line = format_snapkey_line(rec, now=self.NOW)
        assert "2026-09-06 23:59:00 (UTC)" in line

    def test_usage_period_invalid_falls_back_to_unknown(self):
        rec = {"name": "P", "tempKey": "1", "usagePeriod": "garbage",
               "expiredTime": "xxx"}
        line = format_snapkey_line(rec, now=self.NOW)
        assert line == "P | 1 | 未知 | 未知 | 未知"

    def test_effect_times_variants(self):
        """次数来源 = number 字段(-1/0=不限; effectTimes 是有效天数, 不参与)."""
        base = {"name": "P", "tempKey": "1", "expiredTime": "1999999999"}
        assert " | 1 次 | " in format_snapkey_line(base | {"number": "1"})
        assert " | 2 次 | " in format_snapkey_line(base | {"number": "2"})
        assert " | 无限次 | " in format_snapkey_line(base | {"number": "-1"})
        assert " | 无限次 | " in format_snapkey_line(base | {"number": 0})
        assert " | 未知 | " in format_snapkey_line(base | {"number": "abc"})
        assert " | 未知 | " in format_snapkey_line(base)
        # 回归锁死: effectTimes(有效天数)不再影响次数显示
        assert " | 无限次 | " in format_snapkey_line(
            base | {"number": -1, "effectTimes": "1"}
        )

    def test_state_mapping(self):
        base = {"name": "P", "tempKey": "1", "expiredTime": "1999999999"}
        assert format_snapkey_line(base | {"state": 0}, now=self.NOW).endswith("未使用")
        assert format_snapkey_line(base | {"state": "1"}).endswith("已使用")
        assert format_snapkey_line(base | {"state": 2}).endswith("已过期")
        assert format_snapkey_line(base | {"state": 3}).endswith("已删除/失效")
        assert format_snapkey_line(base | {"state": 9}).endswith("未知")
        assert format_snapkey_line(base).endswith("未知")

    def test_state0_after_expiry_annotated(self):
        rec = {"name": "P", "tempKey": "1", "state": 0,
               "expiredTime": "1788643212"}  # 2026-09-06 05:20:12 UTC+8
        assert format_snapkey_line(rec, now=self.NOW).endswith("未使用(已过期)")

    def test_state0_before_expiry_plain(self):
        rec = {"name": "P", "tempKey": "1", "state": 0,
               "expiredTime": "1893456000"}  # 2030-01-01 UTC+8
        assert format_snapkey_line(rec, now=self.NOW).endswith("| 未使用")

    def test_defaults_for_missing_fields(self):
        rec = {"state": 1, "number": "1",
               "expiredTime": "1893456000"}  # 无 name/tempKey
        assert format_snapkey_line(rec, now=self.NOW) == (
            "未命名 | 无 | 2030-01-01 08:00:00 | 1 次 | 已使用"
        )

    def test_non_dict_element_skipped_and_sorting(self):
        secrets = [
            {"name": "old", "tempKey": "1", "createTime": "1788550539",
             "state": 1, "effectTimes": "1", "expiredTime": "1893456000"},
            "garbage",
            {"name": "new", "tempKey": "2", "createTime": "1788643212",
             "state": 0, "effectTimes": "1", "expiredTime": "1893456000"},
        ]
        entries = format_snapkeys_display(secrets, now=self.NOW)
        assert len(entries) == 2
        assert entries[0]["名称"] == "new"
        assert entries[1]["名称"] == "old"

    def test_empty_and_invalid_inputs(self):
        assert format_snapkeys_display([]) == []
        assert format_snapkeys_display(None) == []
        assert format_snapkeys_display("bad") == []

    def test_naive_now_treated_as_device_tz(self):
        rec = {"name": "P", "tempKey": "1", "state": 0,
               "expiredTime": "1788643212"}
        naive_now = datetime(2026, 9, 6, 10, 0)  # 无 tzinfo → 按 UTC+8
        assert format_snapkey_line(rec, now=naive_now).endswith("未使用(已过期)")


class TestI18n:
    """属性展示词映射的国际化(i18n.py; HA 不翻译属性, 集成按实例语言产出)."""

    NOW = datetime(2026, 9, 6, 10, 0, tzinfo=timezone(timedelta(hours=8)))

    def test_normalize_lang(self):
        from lechange_door_lock.i18n import DEFAULT_LANG, normalize_lang
        assert normalize_lang("zh-Hans") == "zh-Hans"
        assert normalize_lang("zh-CN") == "zh-Hans"   # 前缀归一
        assert normalize_lang("zh") == "zh-Hans"
        assert normalize_lang("en") == "en"
        assert normalize_lang("en-GB") == "en"
        assert normalize_lang("de") == DEFAULT_LANG   # 未收录回退
        assert normalize_lang("") == DEFAULT_LANG
        assert normalize_lang(None) == DEFAULT_LANG

    def test_tr_fallback_chain(self):
        from lechange_door_lock.i18n import tr
        assert tr("en", "alarm.field.behavior") == "Behavior"
        assert tr("zh-CN", "alarm.field.behavior") == "行为"
        # zh 目录有而 en 缺的词条 → 回退 zh-Hans(不再返回 None)
        assert tr("en", "nonexistent.key") is None

    def test_alarm_fields_localized(self):
        alarm = {"labelType": "accessAlarm", "title": "伟使用指纹开门了",
                 "time": "20260905T082336"}
        zh = format_alarm_fields(alarm, "zh-CN")
        assert set(zh) == {"行为", "标题", "时间"}
        assert zh["行为"] == "开门/出门事件"
        en = format_alarm_fields(alarm, "en")
        assert set(en) == {"Behavior", "Title", "Time"}
        assert en["Behavior"] == "Open/exit event"
        # 标题是云端数据文本, 不翻译
        assert en["Title"] == "伟使用指纹开门了"

    def test_alarm_unknown_label_localized(self):
        zh = format_alarm_fields({"title": "x"}, "zh-Hans")
        assert zh["行为"] == "未知行为"
        en = format_alarm_fields({"title": "x"}, "en")
        assert en["Behavior"] == "Unknown behavior"

    def test_snapkey_fields_localized(self):
        rec = {"name": "HA", "tempKey": "1", "state": 0,
               "number": -1, "expiredTime": "1893456000"}
        zh = format_snapkey_fields(rec, now=self.NOW, lang="zh-Hans")
        assert set(zh) == {"名称", "密码", "过期时间", "次数", "状态"}
        assert zh["状态"] == "未使用" and zh["次数"] == "无限次"
        en = format_snapkey_fields(rec, now=self.NOW, lang="en")
        assert set(en) == {"Name", "Password", "Expires", "Uses", "Status"}
        assert en["Status"] == "Unused" and en["Uses"] == "Unlimited"

    def test_snapkey_state0_expired_localized(self):
        rec = {"name": "P", "tempKey": "1", "state": 0,
               "expiredTime": "1788643212"}  # 2026-09-06 05:20 UTC+8
        assert format_snapkey_fields(
            rec, now=self.NOW, lang="zh-Hans"
        )["状态"] == "未使用(已过期)"
        assert format_snapkey_fields(
            rec, now=self.NOW, lang="en"
        )["Status"] == "Unused (expired)"

    def test_snapkey_defaults_localized(self):
        rec = {"state": 9, "effectTimes": "abc"}
        zh = format_snapkey_fields(rec, now=self.NOW, lang="zh-Hans")
        assert zh["名称"] == "未命名" and zh["密码"] == "无"
        assert zh["次数"] == "未知" and zh["状态"] == "未知"
        en = format_snapkey_fields(rec, now=self.NOW, lang="en")
        assert en["Name"] == "Unnamed" and en["Password"] == "None"
        assert en["Uses"] == "Unknown" and en["Status"] == "Unknown"

    def test_default_lang_unchanged_behavior(self):
        # 不传 lang(默认 zh-Hans)→ 与历史中文输出完全一致(存量兼容)
        rec = {"name": "P", "tempKey": "1", "state": 1,
               "number": 2, "expiredTime": "1893456000"}
        fields = format_snapkey_fields(rec, now=self.NOW)
        assert fields == {
            "名称": "P", "密码": "1", "过期时间": "2030-01-01 08:00:00",
            "次数": "2 次", "状态": "已使用",
        }
