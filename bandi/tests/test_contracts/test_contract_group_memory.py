# 功能：契约——群聊记忆域（group:<conversation_id> 隔离 + 冻结快照 + 轮末写入）。
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bandi.agent.base import TerminalReason
from bandi.company.seat_executor import group_conversation_id_for
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.core.agent_runner import _persist_memory
from bandi.memory.episodic import EpisodicStore
from bandi.memory.global_store import GlobalMemoryStore
from bandi.memory.group_scope import group_scope_id
from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.markdown_memory import MarkdownMemoryStore
from bandi.memory.memory_manager import MemoryHierarchyManager
from bandi.memory.memory_provider import MemoryManager
from bandi.memory.working import ScratchpadStore


def _stores(tmp_path: Path) -> tuple[MemoryManager, LongTermMemory, EpisodicStore]:
    long_term = LongTermMemory(str(tmp_path / "long.db"))
    episodic = EpisodicStore(str(tmp_path / "epi.db"))
    manager = MemoryManager(long_term=long_term, episodic=episodic)
    return manager, long_term, episodic


def _ctx(conversation_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        meta={"group_conversation_id": conversation_id},
        raw_input="请记住：项目代号是青柠",
        turn_index=1,
        terminal_reason=TerminalReason.COMPLETED,
        final_message="已记住项目代号青柠",
        current_content="",
        messages=[],
        tool_results=[],
        bus=AsyncMock(),
    )


def test_group_turn_writes_group_scope_only(tmp_path: Path) -> None:
    manager, long_term, episodic = _stores(tmp_path)
    asyncio.run(
        _persist_memory(
            _ctx("conv-a"),
            manager,
            "s1",
            project_id="p1",
            employee_id="emp-1",
        )
    )
    scope = group_scope_id("conv-a")
    assert long_term.search_fts_ranked("青柠", project_id=scope, employee_id=None)
    assert long_term.search_fts_ranked("青柠", project_id="p1", employee_id="emp-1") == []
    assert episodic.search("青柠", project_id=scope, employee_id=None)


def test_group_search_isolated_from_other_group_and_employee_private(tmp_path: Path) -> None:
    manager, long_term, _episodic = _stores(tmp_path)
    scope_a = group_scope_id("conv-a")
    scope_b = group_scope_id("conv-b")
    long_term.store(
        MemoryItem("a1", "群 A 的暗号是青柠", importance=0.8),
        project_id=scope_a,
        employee_id=None,
    )
    long_term.store(
        MemoryItem("b1", "群 B 的暗号是青柠", importance=0.8),
        project_id=scope_b,
        employee_id=None,
    )
    long_term.store(
        MemoryItem("e1", "员工私有暗号是青柠", importance=0.8),
        project_id="p1",
        employee_id="emp-1",
    )
    long_term.store(
        MemoryItem("g1", "全局暗号是青柠", importance=0.8),
        project_id=None,
        employee_id=None,
    )
    values = [
        hit.value for hit in manager.search_unified("青柠", conversation_id="conv-a", limit=10)
    ]
    assert any("群 A" in value for value in values)
    assert any("全局" in value for value in values)
    assert not any("群 B" in value for value in values)
    assert not any("员工私有" in value for value in values)


def test_group_memory_context_refreshes_within_session(tmp_path: Path) -> None:
    global_store = GlobalMemoryStore(tmp_path / "global.db")
    markdown = MarkdownMemoryStore(tmp_path / "md")
    work_items = WorkItemStore(str(tmp_path / "snap.db"))
    hierarchy = MemoryHierarchyManager(
        global_store,
        markdown,
        work_items,
        store_dir=tmp_path / "md-store",
    )
    scope = group_scope_id("conv-a")
    hierarchy.append_memory("project", "条目A：青柠方案", project_id=scope)
    first = hierarchy.collect_memory_context(
        project_id="",
        session_id="s1",
        employee_id="",
        group_conversation_id="conv-a",
    )
    assert "## 群聊记忆" in first
    assert "条目A" in first
    hierarchy.append_memory("project", "条目B：薄荷方案", project_id=scope)
    second = hierarchy.collect_memory_context(
        project_id="",
        session_id="s1",
        employee_id="",
        group_conversation_id="conv-a",
    )
    assert "条目A" in second
    assert "条目B" in second
    fresh = hierarchy.collect_memory_context(
        project_id="",
        session_id="s2",
        employee_id="",
        group_conversation_id="conv-a",
    )
    assert "条目A" in fresh and "条目B" in fresh
    other = hierarchy.collect_memory_context(
        project_id="",
        session_id="s3",
        employee_id="",
        group_conversation_id="conv-b",
    )
    assert "条目A" not in other


def test_group_memory_scope_uses_session_across_rounds() -> None:
    item = create_work_item(
        title="group turn",
        turn_type=WorkItemTurnType.EXECUTE,
        metadata={
            "group_chat": True,
            "round_id": "round-1",
            "origin_session_id": "session-1",
        },
    )
    assert group_conversation_id_for(item) == "session-1"
    other_round = create_work_item(
        title="group turn 2",
        turn_type=WorkItemTurnType.EXECUTE,
        metadata={
            "group_chat": True,
            "round_id": "round-2",
            "origin_session_id": "session-1",
        },
    )
    assert group_conversation_id_for(other_round) == group_conversation_id_for(item)


def test_non_group_card_has_no_group_memory_scope() -> None:
    item = create_work_item(title="plain", turn_type=WorkItemTurnType.EXECUTE)
    assert group_conversation_id_for(item) == ""


def test_group_pass_does_not_write_memory(tmp_path: Path) -> None:
    manager, long_term, episodic = _stores(tmp_path)
    ctx = _ctx("conv-pass")
    ctx.final_message = "[PASS]"
    asyncio.run(
        _persist_memory(
            ctx,
            manager,
            "s1",
            project_id="p1",
            employee_id="emp-1",
        )
    )
    scope = group_scope_id("conv-pass")
    assert long_term.search_fts_ranked("项目", project_id=scope, employee_id=None) == []
    assert episodic.search("项目", project_id=scope, employee_id=None) == []


def test_group_turn_writes_working_memory(tmp_path: Path) -> None:
    manager, _long_term, _episodic = _stores(tmp_path)
    manager.working = ScratchpadStore(str(tmp_path / "working.db"))
    asyncio.run(_persist_memory(_ctx("conv-working"), manager, "s-working", project_id="p1"))

    assert "turn:1" in manager.working.all("s-working")


def test_dm_turn_writes_working_memory(tmp_path: Path) -> None:
    manager, _long_term, _episodic = _stores(tmp_path)
    manager.working = ScratchpadStore(str(tmp_path / "working.db"))
    ctx = _ctx("")
    ctx.meta = {"dm_conversation_id": "dm-working"}
    asyncio.run(
        _persist_memory(
            ctx,
            manager,
            "s-dm",
            project_id="p1",
            employee_id="emp-1",
        )
    )

    assert "turn:1" in manager.working.all("s-dm")
