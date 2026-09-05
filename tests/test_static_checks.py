"""静态检查: 全组件 pyflakes 零未定义名(F821) + 服务 YAML 合法性.

回归背景(2026-09): __init__.py 引用 CONF_USERNAME 未导入 → setup 崩溃;
services.py 注册 reload_data 时常量未导入 → 全部服务注册失败;
services.yaml selector 裸数字 value / description 未引用冒号 → 服务解析
失败。此类错误 pytest 功能测试抓不住(测试桩不 import 真实模块路径),
静态扫描 + YAML 解析是唯一可靠防线。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "lechange_door_lock"


def test_pyflakes_no_undefined_names():
    """F821(undefined name)为零 — 导入遗漏在 CI 阶段即红."""
    r = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(_PKG)],
        capture_output=True, text=True, check=False,
    )
    undefined = [
        line for line in (r.stdout or "").splitlines()
        if "undefined name" in line
    ]
    assert not undefined, "pyflakes F821:\n" + "\n".join(undefined)


def test_services_yaml_valid():
    """services.yaml 可解析、reload_data 已声明."""
    data = yaml.safe_load((_PKG / "services.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "reload_data" in data
    assert "wake_up_device" in data


def test_services_translations_valid():
    """中英服务翻译 YAML 可解析且覆盖 reload_data."""
    for lang in ("zh-Hans", "en"):
        p = _PKG / "translations" / f"services.{lang}.yaml"
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert "reload_data" in data, lang
        # selector 选项值必须为字符串(HA 新版校验拒绝裸数字)
        for svc in data.values():
            for field in (svc or {}).get("fields", {}).values():
                opts = ((field or {}).get("selector") or {}).get("select", {}).get("options")
                if opts:
                    for opt in opts:
                        assert isinstance(opt.get("value"), str), (lang, opt)


def test_translations_json_valid():
    """中英 strings JSON 可解析且 options 键与 schema 键对齐(抽查)."""
    import json

    for lang in ("zh-Hans", "en"):
        d = json.loads(
            (_PKG / "translations" / f"{lang}.json").read_text(encoding="utf-8")
        )
        init = d["options"]["step"]["init"]
        assert "security_code" in init["data"]
        assert "security_code" in init.get("data_description", {})
        assert "reauth_confirm" in d["config"]["step"]
        assert "reauth_successful" in d["config"]["abort"]


def test_translations_cover_all_options_schema_fields():
    """options schema 的每个字段键都必须有中英 data+data_description 翻译.

    防漂移: 新增 schema 字段(如 camera_auto_image)忘加翻译时,
    HA 界面显示裸键名 —— 此测试在 CI 阶段就拦住。
    """
    import json
    import re

    src = (_PKG / "config_flow.py").read_text(encoding="utf-8")
    # 提取 options schema 区段(从 async_get_options_flow / OptionsFlowHandler
    # 的 vol.Optional( 到 schema 结束) —— 直接全文抓 CONF_* 引用更稳:
    conf_keys = set(re.findall(r"vol\.Optional\(\s*(CONF_[A-Z_]+)", src))
    assert conf_keys, "options schema 未解析到任何字段"

    # 常量 → 字符串键(从 const.py 取值)
    const_src = (_PKG / "const.py").read_text(encoding="utf-8")
    const_map = dict(re.findall(r"^(CONF_[A-Z_]+)\s*=\s*\"([a-z_]+)\"", const_src, re.M))
    keys = set()
    for name in conf_keys:
        assert name in const_map, f"{name} 未在 const.py 定义"
        keys.add(const_map[name])

    for lang in ("zh-Hans", "en"):
        d = json.loads(
            (_PKG / "translations" / f"{lang}.json").read_text(encoding="utf-8")
        )
        init = d["options"]["step"]["init"]
        for key in keys:
            assert key in init["data"], f"{lang}: options.data 缺 {key}"
            assert key in init.get("data_description", {}), \
                f"{lang}: options.data_description 缺 {key}"


def test_translations_no_bare_icu_variables_in_static_strings():
    """静态翻译串中不允许出现 HA 未提供值的 ICU 裸变量.

    抓 Bug: rtsp_url 描述含 "{channel}" → HA formatjs 抛 MISSING_VALUE,
    刷屏 95 次错误日志。允许两类例外:
    ① flow 层占位符(config_flow.py description_placeholders 实际提供:
       username / url / url_link);
    ② ICU 单引号转义(“'{'channel'}'”)与实体占位符(channel_id)。
    """
    import json
    import re

    allowed = {"username", "url", "url_link", "channel_id"}
    for lang in ("zh-Hans", "en"):
        d = json.loads(
            (_PKG / "translations" / f"{lang}.json").read_text(encoding="utf-8")
        )
        bad = []

        def walk(node, path=""):
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{path}.{k}")
            elif isinstance(node, str):
                for m in re.finditer(r"\{(\w+)\}", node):
                    before = node[: m.start()].rstrip()
                    if before.endswith("'"):      # '{'xxx'}' ICU 转义
                        continue
                    if m.group(1) in allowed:
                        continue
                    bad.append((path, m.group(1)))

        walk(d)
        assert not bad, f"{lang}: 静态翻译串含未提供的 ICU 变量: {bad[:6]}"
