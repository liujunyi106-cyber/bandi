"""契约：员工私聊/群聊的长期记忆写入与助理分支共用同一判定与出口。

三态必须一致：冲突检测 → 语义去重 → origin 门全过才直写，否则进暂存待批；
语义重复既不直写也不暂存；后台来源一律暂存。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bandi.agent.base import TerminalReason
from bandi.core.agent_runner import _persist_memory
from bandi.memory.dm_scope import dm_scope_id
from bandi.memory.episodic import EpisodicStore
from bandi.memory.group_scope import group_scope_id
from bandi.memory.long_term import LongTermMemory
from bandi.memory.memory_provider import MemoryManager
from bandi.memory.staging import (
    MemoryPendingStore,
    reset_pending_store,
    reset_write_origin,
    set_write_origin,
)

PROJECT_ID = "p1"
EMPLOYEE_ID = "emp-1"

# 功能：三态 → (长期记忆条数, 待批准暂存条数)
_STATES = {"written": (1, 0), "skipped": (0, 0), "staged": (0, 1)}

CONVERSATIONS = [("dm", "dm-conv"), ("group", "grp-conv")]


@pytest.fixture()
def pending_store_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """功能：隔离 HOME 与暂存队列——真实 store 只落 tmp，不写穿 ~/.bandi。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("BANDI_MEMORY_PENDING_DIR", str(tmp_path / "pending-home"))

    def _make(label: str) -> MemoryPendingStore:
        store = MemoryPendingStore(str(tmp_path / label / "staging"))
        reset_pending_store(store)
        return store

    yield _make
    reset_pending_store(None)


def _manager(root: Path) -> MemoryManager:
    root.mkdir(parents=True, exist_ok=True)
    return MemoryManager(
        long_term=LongTermMemory(str(root / "long_term.db")),
        episodic=EpisodicStore(str(root / "episodic.db")),
    )


def _ctx(
    conversation: tuple[str, str] | None, raw_input: str, *, turn_index: int = 1
) -> SimpleNamespace:
    meta: dict[str, str] = {}
    if conversation is not None:
        meta[f"{conversation[0]}_conversation_id"] = conversation[1]
    return SimpleNamespace(
        meta=meta,
        raw_input=raw_input,
        turn_index=turn_index,
        terminal_reason=TerminalReason.COMPLETED,
        final_message="好的，已记下。",
        current_content="",
        messages=[],
        tool_results=[],
        bus=AsyncMock(),
    )


def _persist(manager: MemoryManager, ctx: SimpleNamespace, session_id: str) -> None:
    asyncio.run(
        _persist_memory(ctx, manager, session_id, project_id=PROJECT_ID, employee_id=EMPLOYEE_ID)
    )


def _long_term_pending(pending: MemoryPendingStore) -> list:
    """功能：只取长期记忆的待批准记录（episodic 的暂存不属于本契约）。"""
    return [
        record
        for record in pending.list_pending()
        if (record.payload or {}).get("target") == "long_term"
    ]


def _scope_of(conversation: tuple[str, str]) -> str:
    kind, conv = conversation
    return dm_scope_id(PROJECT_ID, conv) if kind == "dm" else group_scope_id(conv)


def _run_entry(
    tmp_path: Path,
    pending_store_factory,
    label: str,
    conversation: tuple[str, str] | None,
    payload: str,
    origin: str,
) -> tuple[int, int]:
    pending = pending_store_factory(label)
    manager = _manager(tmp_path / label)
    token = set_write_origin(origin)
    try:
        _persist(manager, _ctx(conversation, payload), f"s-{label}")
    finally:
        reset_write_origin(token)
    return manager.long_term.count(), len(_long_term_pending(pending))


@pytest.mark.parametrize(
    ("case", "payload", "origin", "state"),
    [
        ("remember", "记住：部署端口统一用 7421", "foreground", "written"),
        ("chitchat", "你好", "foreground", "skipped"),
        ("background", "记住：部署端口统一用 7421", "background", "staged"),
    ],
)
def test_assistant_and_dm_entries_reach_same_verdict(
    tmp_path: Path,
    pending_store_factory,
    case: str,
    payload: str,
    origin: str,
    state: str,
) -> None:
    """同一内容在助理入口与私聊入口得到同一三态（写 / 不写 / 暂存）。"""
    assistant = _run_entry(
        tmp_path, pending_store_factory, f"assistant-{case}", None, payload, origin
    )
    dm = _run_entry(
        tmp_path, pending_store_factory, f"dm-{case}", ("dm", f"dm-{case}"), payload, origin
    )
    assert assistant == dm == _STATES[state], f"{case}: 助理入口 {assistant} vs 私聊入口 {dm}"


@pytest.mark.parametrize("conversation", CONVERSATIONS, ids=[kind for kind, _ in CONVERSATIONS])
def test_semantic_duplicate_not_written_twice(
    tmp_path: Path, pending_store_factory, conversation: tuple[str, str]
) -> None:
    """私聊/群聊入口的等价内容不重复落库（语义去重命中：既不写也不暂存）。"""
    label = f"{conversation[0]}-dup"
    pending = pending_store_factory(label)
    manager = _manager(tmp_path / label)

    _persist(manager, _ctx(conversation, "记住：部署端口统一用 7421", turn_index=1), "s-dup")
    assert manager.long_term.count() == 1

    _persist(manager, _ctx(conversation, "记住：部署端口统一用 7421。", turn_index=2), "s-dup")
    assert manager.long_term.count() == 1, "等价内容被重复落库"
    assert _long_term_pending(pending) == [], "语义重复不该进暂存区"


@pytest.mark.parametrize("conversation", CONVERSATIONS, ids=[kind for kind, _ in CONVERSATIONS])
def test_background_origin_stages_instead_of_direct_write(
    tmp_path: Path, pending_store_factory, conversation: tuple[str, str]
) -> None:
    """后台来源（origin != foreground）必须进暂存区，不得直写长期记忆。"""
    label = f"{conversation[0]}-bg"
    pending = pending_store_factory(label)
    manager = _manager(tmp_path / label)

    token = set_write_origin("background")
    try:
        _persist(manager, _ctx(conversation, "记住：部署端口统一用 7421"), "s-bg")
    finally:
        reset_write_origin(token)

    assert manager.long_term.count() == 0, "后台来源直写了长期记忆"
    records = _long_term_pending(pending)
    assert len(records) == 1, f"后台来源必须进暂存区，实际暂存 {len(records)} 条"
    record = records[0]
    assert record.origin == "background"
    assert record.payload["target"] == "long_term"
    assert "7421" in record.payload["item"]["content"]


@pytest.mark.parametrize("conversation", CONVERSATIONS, ids=[kind for kind, _ in CONVERSATIONS])
def test_same_turn_conflict_stages_without_overwrite(
    tmp_path: Path, pending_store_factory, conversation: tuple[str, str]
) -> None:
    """破坏者视角：同 turn 同 item_id 内容不同 → 冲突暂存，已存在条目不得被覆盖。"""
    label = f"{conversation[0]}-conflict"
    pending = pending_store_factory(label)
    manager = _manager(tmp_path / label)
    scope = _scope_of(conversation)

    _persist(manager, _ctx(conversation, "记住：部署端口统一用 7421", turn_index=1), "s-conflict")
    before = manager.long_term.list_items(project_id=scope, employee_id=None)
    assert len(before) == 1, "前置条件不成立：第一条未落库"

    _persist(manager, _ctx(conversation, "记住：部署端口统一用 8080", turn_index=1), "s-conflict")

    after = manager.long_term.list_items(project_id=scope, employee_id=None)
    assert len(after) == 1
    assert after[0].id == before[0].id
    assert after[0].content == before[0].content, "冲突写入覆盖了已存在条目"
    records = _long_term_pending(pending)
    assert len(records) == 1, f"冲突写入必须进暂存区，实际暂存 {len(records)} 条"
    assert records[0].payload["conflict"] is True
    assert records[0].payload["item"]["id"] == before[0].id
