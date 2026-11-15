# 功能：契约测试——前端权限档位常量必须与后端 PRESETS 逐项相等（防静默漂移）
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bandi.api.settings_ext import PRESETS

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE = REPO_ROOT / "frontend" / "src" / "lib" / "permission-presets.ts"
MARKER = "export const PERMISSION_PRESETS"


def _frontend_presets() -> dict[str, Any]:
    """功能：从 TS 源码里抽出纯 JSON 对象体（常量块内不得写注释）。"""
    text = SOURCE.read_text(encoding="utf-8")
    start = text.index(MARKER)
    brace = text.index("{", start)
    end = text.index("\n};", brace)
    return json.loads(text[brace : end + 2])


def test_frontend_source_exists() -> None:
    assert SOURCE.exists(), f"权限档位常量文件缺失：{SOURCE}"


def test_frontend_presets_match_backend() -> None:
    """三档的档位名与三项取值必须逐项相等（改后端忘改前端会在这里红）。"""
    frontend = _frontend_presets()
    assert set(frontend) == set(PRESETS)
    for name, policy in PRESETS.items():
        assert frontend[name]["ask_risk_levels"] == list(policy["ask_risk_levels"]), name
        assert frontend[name]["ask_tools"] == list(policy["ask_tools"]), name
        assert frontend[name]["bypass_tools"] == list(policy["bypass_tools"]), name


def test_comparator_catches_single_item_drift() -> None:
    """破坏者视角：任一档位少一个工具都必须被比对出来（防止断言空转）。"""
    mutated = json.loads(json.dumps(_frontend_presets()))
    mutated["standard"]["ask_tools"] = [
        item for item in mutated["standard"]["ask_tools"] if item != "memory_assert"
    ]
    assert mutated["standard"]["ask_tools"] != list(PRESETS["standard"]["ask_tools"])


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_each_preset_has_exactly_three_keys(name: str) -> None:
    """功能：前端每个档位只带三项询问范围字段（deny 底线不在档位内）。"""
    frontend = _frontend_presets()
    assert set(frontend[name]) == {"ask_risk_levels", "ask_tools", "bypass_tools"}
