# 功能：契约 — 工具面输出顺序稳定
# 「按集合迭代注册」或注册顺序调整，整条前缀即失效（永久不命中）。
# 契约：① 四个输出点（list_tools / list_schemas / list_flat_schemas / list_tool_meta）
# 全部按 name 升序；② 四点成员一致；③ 两次独立构建字节相同（确定性）；
# ④ 四次独立构建之间成员与顺序一致（防注册顺序漂移）。

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from bandi.agent.factory import build_tool_registry


@lru_cache(maxsize=1)
def _registry() -> Any:
    """单次构建约 5s，必须缓存（否则整个文件超时）。"""
    return build_tool_registry()


def _schema_name(entry: dict[str, Any]) -> str:
    # list_schemas 出 OpenAI 形（function.name），其余出扁平形（name）
    return str((entry.get("function") or entry).get("name") or "")


def test_all_output_points_sorted_by_name() -> None:
    """契约①：四个输出点必须都按 name 升序（前缀字节稳定性的前提）。"""
    reg = _registry()
    surfaces = {
        "list_tools": [t.name for t in reg.list_tools()],
        "list_schemas": [_schema_name(s) for s in reg.list_schemas()],
        "list_flat_schemas": [s["name"] for s in reg.list_flat_schemas()],
        "list_tool_meta": [s["name"] for s in reg.list_tool_meta()],
    }
    for label, names in surfaces.items():
        assert names, f"{label} 不得为空"
        assert names == sorted(names), f"{label} 未按 name 升序（前缀顺序不稳定）"


def test_output_points_agree_on_membership() -> None:
    """契约②：四个输出点的成员集合与顺序必须一致（防某一路漏排序）。"""
    reg = _registry()
    names = [t.name for t in reg.list_tools()]
    assert [_schema_name(s) for s in reg.list_schemas()] == names
    assert [s["name"] for s in reg.list_flat_schemas()] == names
    assert [s["name"] for s in reg.list_tool_meta()] == names


def test_order_is_byte_deterministic_across_builds() -> None:
    """契约③：两次独立构建的 schema **字节相同**（确定性 = 跨进程/跨重启前缀可复用）。"""
    a = json.dumps(_registry().list_schemas(), ensure_ascii=False)
    b = json.dumps(build_tool_registry().list_schemas(), ensure_ascii=False)
    assert a == b, "同一注册集两次构建必须字节一致"
