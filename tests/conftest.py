"""Pytest fixtures: load the component's HA-free modules without Home Assistant.

The component package's __init__.py imports `homeassistant`, so we register a
stub package in sys.modules first; Python then resolves submodules
(imou_client / const / streams / state_utils) directly from the source tree.
"""

import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "lechange_door_lock"

_pkg = types.ModuleType("lechange_door_lock")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules["lechange_door_lock"] = _pkg

# 保证 const 在任何测试引用前已加载(纯常量,无外部依赖)
import lechange_door_lock.const  # noqa: E402,F401
