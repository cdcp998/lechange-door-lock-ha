"""Tests for the device model definition decoder (identifier<->ref mapping)."""

from lechange_door_lock.imou_client import ModelInfo

# 匿名化、精简后的型号定义(结构与真实 SKG8J5R0 定义一致:ref→identifier 映射、struct/array 嵌套)
MODEL = {
    "schema": "1.0",
    "profile": {"identifier": "TESTMODEL"},
    "services": [
        {
            "identifier": "GetVoiceReply",
            "ref": "24600",
            "inputData": [
                {"identifier": "relateType", "ref": "24601",
                 "dataType": {"type": "enum", "specs": {"list": [
                     {"value": "0", "desc": "device"}, {"value": "4", "desc": "quickReply"}]}}}
            ],
            "outputData": [
                {"identifier": "list", "ref": "24622", "dataType": {"type": "array"}}
            ],
        }
    ],
    "properties": [
        {"identifier": "doorLockStatus", "ref": "102800",
         "dataType": {"type": "enum", "specs": {"list": [
             {"value": "0", "desc": "门已锁"}, {"value": "1", "desc": "门未锁"}, {"value": "2", "desc": "未知状态"}]}}},
        {"identifier": "child_lock", "ref": "120000", "dataType": {"type": "bool"}},
        {"identifier": "powerState", "ref": "105300",
         "dataType": {"type": "enum", "specs": {"list": [
             {"value": "0", "desc": "正常"}, {"value": "1", "desc": "省电"}]}}},
        {"identifier": "wifiDoorLock", "ref": "106000",
         "dataType": {"type": "struct", "specs": [
             {"identifier": "SSID", "ref": "106001", "dataType": {"type": "text"}},
             {"identifier": "status", "ref": "106003",
              "dataType": {"type": "enum", "specs": {"list": [
                  {"value": "2", "desc": "已连接"}]}}},
             {"identifier": "intensity", "ref": "106005",
              "dataType": {"type": "enum", "specs": {"list": [
                  {"value": "4", "desc": "较好"}]}}},
         ]}},
        {"identifier": "devicePowerLock", "ref": "106200",
         "dataType": {"type": "array", "specs": {"item": {"type": "struct", "specs": [
             {"identifier": "state", "ref": "106201",
              "dataType": {"type": "enum", "specs": {"list": [{"value": "1", "desc": "使用中"}]}}},
             {"identifier": "type", "ref": "106202", "dataType": {"type": "enum"}},
             {"identifier": "elecPercent", "ref": "106203", "dataType": {"type": "int"}},
         ]}}}},
        {"identifier": "openDoorMsg", "ref": "107100", "dataType": {"type": "bool"}},
    ],
}


def _model() -> ModelInfo:
    return ModelInfo(MODEL)


class TestDecodeProperties:
    def test_scalar_mapping_and_cast(self):
        props = _model().decode_properties(
            {"102800": "0", "120000": "1", "107100": "0", "99999": "raw"}
        )
        assert props["doorLockStatus"] == 0
        assert props["child_lock"] is True
        assert props["openDoorMsg"] is False
        assert props["99999"] == "raw"  # 未知 ref 原样透传

    def test_struct_nested_refs(self):
        props = _model().decode_properties(
            {"106000": '{"106001":"HomeWiFi","106003":"2","106005":"4"}'}
        )
        wifi = props["wifiDoorLock"]
        assert wifi["SSID"] == "HomeWiFi"
        assert wifi["status"] == 2
        assert wifi["intensity"] == 4

    def test_struct_as_dict_input(self):
        props = _model().decode_properties(
            {"106000": {"106001": "X", "106003": "2"}}
        )
        assert props["wifiDoorLock"]["SSID"] == "X"

    def test_array_items_decoded(self):
        props = _model().decode_properties(
            {"106200": [{"106203": 59, "106202": 1, "106201": 1},
                        {"106203": 94, "106202": 0, "106201": 1}]}
        )
        batt = props["devicePowerLock"]
        assert batt[0] == {"state": 1, "type": 1, "elecPercent": 59}
        assert batt[1] == {"state": 1, "type": 0, "elecPercent": 94}

    def test_identifier_keys_passthrough(self):
        props = _model().decode_properties({"child_lock": True})
        assert props["child_lock"] is True

    def test_enum_desc(self):
        model = _model()
        assert model.enum_desc("doorLockStatus", 0) == "门已锁"
        assert model.enum_desc("doorLockStatus", 2) == "未知状态"
        assert model.enum_desc("unknownProp", 0) == ""


class TestDecodeOutputs:
    def test_service_output_refs(self):
        out = _model().decode_outputs({"24622": ["a", "b"]})
        assert out == {"list": ["a", "b"]}

    def test_unknown_output_passthrough(self):
        out = _model().decode_outputs({"999": "v"})
        assert out == {"999": "v"}


class TestModelServices:
    def test_services_indexed(self):
        assert "GetVoiceReply" in _model().services
