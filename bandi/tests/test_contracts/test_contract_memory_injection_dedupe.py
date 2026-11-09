# 功能：契约——记忆上下文跨层去重（审计高项：同一 final_message
# 同时落 session 与 employee 快照 ⇒ collect 两段注入同事实，白烧 token）。
from __future__ import annotations

from typing import Any

from bandi.memory.memory_manager import MemoryHierarchyManager


def test_collect_memory_context_dedupes_cross_scope_lines(monkeypatch: Any) -> None:
    mgr = MemoryHierarchyManager.__new__(MemoryHierarchyManager)
    shared_line = "- 项目代号：Prometheus-7"
    monkeypatch.setattr(
        mgr,
        "load_memory",
        lambda scope, **_kw: f"{shared_line}\n- 独有行 {scope}",
    )
    monkeypatch.setattr(
        mgr,
        "_apply_block_budget",
        lambda sections, max_tokens: "\n".join(text for _, _, text in sections),
    )

    out = mgr.collect_memory_context("p1", "s1", "e1")

    assert out.count(shared_line) == 1, f"跨层同事实必须只注入一次：{out}"
    for scope in ("global", "project", "session", "employee"):
        assert f"独有行 {scope}" in out, f"{scope} 独有内容不得被误删"


def test_collect_memory_context_keeps_blank_structure(monkeypatch: Any) -> None:
    mgr = MemoryHierarchyManager.__new__(MemoryHierarchyManager)
    monkeypatch.setattr(mgr, "load_memory", lambda scope, **_kw: "- A\n\n- B")
    monkeypatch.setattr(
        mgr,
        "_apply_block_budget",
        lambda sections, max_tokens: "\n".join(text for _, _, text in sections),
    )
    out = mgr.collect_memory_context("p1", "s1", "e1")
    assert "- A" in out and "- B" in out
