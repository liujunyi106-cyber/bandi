# 功能：记忆层行为契约（防 A~C 复审问题复发）
# 契约原则：只测公开行为不变式，不测实现细节——任何改动破坏这些承诺即红。
from __future__ import annotations

import sqlite3

from bandi.memory.memory_provider import MemoryManager


def test_contract_search_all_only_long_term() -> None:
    """契约 3：未启用 episodic 时 search_all 只含 long_term 分类（决策后
    working/procedural/semantic/preference 已删——防死分类复活或半删残留；
    memory_source 与 prefetch 都遍历 search_all 返回值）。
    启用场景行为见 test_contract_episodic_memory.py），本契约钉未启用默认态。"""
    mm = MemoryManager()
    results = mm.search_all("anything", limit=5)
    assert set(results.keys()) == {"long_term"}, (
        f"search_all 分类集必须只含 long_term，实际 {set(results.keys())}"
    )


def test_contract_memory_manager_close_propagates() -> None:
    """契约 4：MemoryManager.close 必须关闭底层 SQLite 连接（防连接泄漏复发）。"""
    from bandi.memory.long_term import LongTermMemory

    lt = LongTermMemory()
    mm = MemoryManager(long_term=lt)
    mm.close()
    import pytest

    with pytest.raises(sqlite3.ProgrammingError):
        lt._conn.execute("SELECT 1")


def test_contract_long_term_global_never_hits_dimension_rows() -> None:
    """契约 8（批3-4）：long_term 全局检索必须只返回全局行（「仅全局」语义）——
    维度行不得泄漏进全局检索。"""
    from bandi.memory.long_term import LongTermMemory, MemoryItem

    mem = LongTermMemory()
    mem.store(MemoryItem(item_id="g1", content="global shared secret"))
    mem.store(MemoryItem(item_id="d1", content="employee secret"), employee_id="e1")
    mem.store(MemoryItem(item_id="d2", content="project secret"), project_id="p1")
    mem.store(
        MemoryItem(item_id="d3", content="employee project secret"),
        project_id="p1",
        employee_id="e1",
    )
    assert {r.id for r in mem.retrieve("secret")} == {"g1"}, "全局检索不得命中维度行"
    assert {r.id for r in mem.search_fts("secret")} == {"g1"}, "全局 FTS 不得命中维度行"
    # 维度检索隔离
    assert {r.id for r in mem.retrieve("secret", project_id="p1")} == {"d2"}
    assert {r.id for r in mem.retrieve("secret", employee_id="e1")} == {"d1"}
    assert {r.id for r in mem.retrieve("secret", project_id="p1", employee_id="e1")} == {"d3"}
    mem.close()


def test_contract_total_count_long_term_only() -> None:
    """契约 10：total_count 只统计 long_term（决策后其余分类已删——
    防内存分类残留虚报计数）。"""
    from bandi.memory.long_term import LongTermMemory, MemoryItem

    mm = MemoryManager(long_term=LongTermMemory())
    mm.long_term.store(MemoryItem(item_id="a", content="x"))
    assert mm.total_count() == 1
