# 功能：契约——员工单聊记忆域（每条会话独立，任务/项目互不可见）
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bandi.agent.base import TerminalReason
from bandi.company.store import WorkItemStore
from bandi.core.agent_runner import _persist_memory
from bandi.memory.dm_scope import dm_scope_id
from bandi.memory.episodic import EpisodicStore
from bandi.memory.global_store import GlobalMemoryStore
from bandi.memory.group_scope import group_scope_id
from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.markdown_memory import MarkdownMemoryStore
from bandi.memory.memory_manager import MemoryHierarchyManager
from bandi.memory.memory_provider import MemoryManager


def _stores(tmp_path: Path) -> tuple[MemoryManager, LongTermMemory, EpisodicStore]:
    long_term = LongTermMemory(str(tmp_path / "long.db"))
    episodic = EpisodicStore(str(tmp_path / "epi.db"))
    manager = MemoryManager(long_term=long_term, episodic=episodic)
    return manager, long_term, episodic


def _ctx(conversation_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        meta={"dm_conversation_id": conversation_id},
        raw_input="任务单聊记住暗号 ALPHA-731",
        turn_index=1,
        terminal_reason=TerminalReason.COMPLETED,
        final_message="已记住暗号 ALPHA-731",
        current_content="",
        messages=[],
        tool_results=[],
        bus=AsyncMock(),
    )


def test_dm_scope_separates_task_project_and_group() -> None:
    task_scope = dm_scope_id("", "conv-a")
    project_scope = dm_scope_id("p1", "conv-a")
    group_scope = group_scope_id("conv-a")
    assert task_scope != project_scope
    assert task_scope != group_scope
    assert project_scope != group_scope


def test_dm_turn_writes_dm_scope_only(tmp_path: Path) -> None:
    manager, long_term, episodic = _stores(tmp_path)
    asyncio.run(
        _persist_memory(
            _ctx("dm-a"),
            manager,
            "s1",
            project_id="p1",
            employee_id="emp-1",
        )
    )
    scope = dm_scope_id("p1", "dm-a")
    assert long_term.search_fts_ranked("暗号", project_id=scope, employee_id=None)
    assert long_term.search_fts_ranked("暗号", project_id="p1", employee_id="emp-1") == []
    assert episodic.search("暗号", project_id=scope, employee_id=None)


def test_dm_search_isolated_between_sessions_group_and_employee(tmp_path: Path) -> None:
    manager, long_term, _episodic = _stores(tmp_path)
    dm_a = dm_scope_id("", "dm-a")
    dm_b = dm_scope_id("", "dm-b")
    group = group_scope_id("group-a")
    long_term.store(MemoryItem("a1", "单聊 A 的暗号是 ALPHA"), project_id=dm_a, employee_id=None)
    long_term.store(MemoryItem("b1", "单聊 B 的暗号是 BETA"), project_id=dm_b, employee_id=None)
    long_term.store(MemoryItem("g1", "群聊暗号是 GAMMA"), project_id=group, employee_id=None)
    long_term.store(
        MemoryItem("e1", "员工私有暗号是 DELTA"),
        project_id="p1",
        employee_id="emp-1",
    )
    long_term.store(MemoryItem("w1", "全局暗号是 OMEGA"), project_id=None, employee_id=None)
    values = [
        hit.value
        for hit in manager.search_unified(
            "暗号",
            dm_conversation_id="dm-a",
            project_id="",
            employee_id="emp-1",
            limit=10,
        )
    ]
    assert any("ALPHA" in value for value in values)
    assert any("OMEGA" in value for value in values)
    assert not any("BETA" in value for value in values)
    assert not any("GAMMA" in value for value in values)
    assert not any("DELTA" in value for value in values)

    group_values = [
        hit.value for hit in manager.search_unified("暗号", conversation_id="group-a", limit=10)
    ]
    assert not any("ALPHA" in value for value in group_values)
    assert not any("BETA" in value for value in group_values)

    normal_values = [
        hit.value
        for hit in manager.search_unified(
            "暗号",
            project_id="p1",
            employee_id="emp-1",
            limit=10,
        )
    ]
    assert not any("ALPHA" in value for value in normal_values)
    assert not any("BETA" in value for value in normal_values)
    assert not any("GAMMA" in value for value in normal_values)


def test_dm_memory_snapshot_freezes_per_session(tmp_path: Path) -> None:
    global_store = GlobalMemoryStore(tmp_path / "global.db")
    markdown = MarkdownMemoryStore(tmp_path / "md")
    work_items = WorkItemStore(str(tmp_path / "snap.db"))
    hierarchy = MemoryHierarchyManager(
        global_store,
        markdown,
        work_items,
        store_dir=tmp_path / "md-store",
    )
    scope = dm_scope_id("", "dm-a")
    hierarchy.append_memory("project", "单聊 A 的第一条结论", project_id=scope)
    first = hierarchy.collect_memory_context(
        project_id="",
        session_id="s1",
        employee_id="",
        dm_conversation_id="dm-a",
    )
    assert "## 员工单聊记忆" in first
    assert "第一条结论" in first
    hierarchy.append_memory("project", "单聊 A 的第二条结论", project_id=scope)
    second = hierarchy.collect_memory_context(
        project_id="",
        session_id="s1",
        employee_id="",
        dm_conversation_id="dm-a",
    )
    assert "第一条结论" in second
    assert "第二条结论" not in second
    other = hierarchy.collect_memory_context(
        project_id="",
        session_id="s2",
        employee_id="",
        dm_conversation_id="dm-b",
    )
    assert "第一条结论" not in other
