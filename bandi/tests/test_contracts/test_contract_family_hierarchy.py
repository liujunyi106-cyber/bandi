from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from bandi.company.store import WorkItemStore
from bandi.context.sources.memory_source import (
    _HIERARCHY_MAX_SESSIONS,
    HIERARCHY_INJECTED_BY_SESSION,
)
from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider
from bandi.memory.global_store import GlobalMemoryStore
from bandi.memory.markdown_memory import MarkdownMemoryStore
from bandi.memory.memory_manager import _GLOBAL_KEY, MemoryHierarchyManager
from bandi.perception.context_loader import _mark_hierarchy_injected

# 功能：层级记忆 GLOBAL 反序列化契约（非字符串值返回 JSON 文本，不落 Python repr）


def _hierarchy(tmp_path) -> MemoryHierarchyManager:
    return MemoryHierarchyManager(
        global_store=GlobalMemoryStore(tmp_path / "global.db"),
        markdown_memory=MarkdownMemoryStore(tmp_path / "md"),
        work_item_store=WorkItemStore(str(tmp_path / "company.db")),
    )


def test_load_memory_global_non_string_value_returns_json_text(tmp_path) -> None:
    """GLOBAL 存 dict 时 load_memory 必须返回 JSON 文本，而非 Python repr 字符串。"""
    global_store = GlobalMemoryStore(tmp_path / "global.db")
    global_store.put_global_memory(_GLOBAL_KEY, {"decision": "采用方案 A"})
    hierarchy = MemoryHierarchyManager(
        global_store=global_store,
        markdown_memory=MarkdownMemoryStore(tmp_path / "md"),
        work_item_store=WorkItemStore(str(tmp_path / "company.db")),
    )
    value = hierarchy.load_memory("global")
    assert isinstance(value, str)
    assert value.startswith('{"'), f"必须返回 JSON 文本而非 Python repr：{value!r}"
    assert "方案 A" in value


def test_load_memory_global_string_roundtrip(tmp_path) -> None:
    """回归：字符串值逐字返回。"""
    global_store = GlobalMemoryStore(tmp_path / "global.db")
    global_store.put_global_memory(_GLOBAL_KEY, "纯文本")
    hierarchy = MemoryHierarchyManager(
        global_store=global_store,
        markdown_memory=MarkdownMemoryStore(tmp_path / "md"),
        work_item_store=WorkItemStore(str(tmp_path / "company.db")),
    )
    assert hierarchy.load_memory("global") == "纯文本"


# ==================== hierarchy_injected_lru ====================
# 功能：契约——HIERARCHY_INJECTED_BY_SESSION 跨会话有界（原无上限
# 长运行多会话/多租场景无界堆积；与 SEEN_BY_SESSION 同口径 LRU 淘汰最旧）。


def test_many_sessions_bounded_evicts_oldest() -> None:
    """契约（核心）：写入超过上限的会话数后只保留最新 N 个会话
    最旧会话被淘汰（红：现状无上限无界增长）。"""
    HIERARCHY_INJECTED_BY_SESSION.clear()
    try:
        for i in range(_HIERARCHY_MAX_SESSIONS + 5):
            _mark_hierarchy_injected(f"s{i}", f"（原文见 entries/e{i}.md）")
        assert len(HIERARCHY_INJECTED_BY_SESSION) <= _HIERARCHY_MAX_SESSIONS, (
            f"会话数超上限：{len(HIERARCHY_INJECTED_BY_SESSION)}"
        )
        assert "s0" not in HIERARCHY_INJECTED_BY_SESSION, "最旧会话必须被淘汰"
        latest = f"s{_HIERARCHY_MAX_SESSIONS + 4}"
        assert latest in HIERARCHY_INJECTED_BY_SESSION, "最新会话必须保留"
    finally:
        HIERARCHY_INJECTED_BY_SESSION.clear()


def test_same_session_accumulates_within_cap() -> None:
    """契约（对照）：同会话多次写入累积于同桶，不因上限误清。"""
    HIERARCHY_INJECTED_BY_SESSION.clear()
    try:
        _mark_hierarchy_injected("sx", "（entries/a1.md）")
        _mark_hierarchy_injected("sx", "（entries/a2.md）")
        bucket = HIERARCHY_INJECTED_BY_SESSION["sx"]
        assert len(bucket) == 2
    finally:
        HIERARCHY_INJECTED_BY_SESSION.clear()


# ==================== hierarchy_snapshot_write ====================


def _build_hierarchy(tmp_path: Any) -> Any:
    from bandi.company.store import WorkItemStore
    from bandi.memory.global_store import GlobalMemoryStore
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.memory_manager import MemoryHierarchyManager
    from bandi.memory.session.store import SessionStore

    return MemoryHierarchyManager(
        global_store=GlobalMemoryStore(tmp_path / "global.db"),
        markdown_memory=MarkdownMemoryStore(tmp_path / "store", project_root=tmp_path / "proj"),
        work_item_store=WorkItemStore(str(tmp_path / "company.db")),
        session_store=SessionStore(tmp_path / "sessions.db"),
    )


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_session_snapshot_is_final_outcome_not_user_echo(home: Any, tmp_path: Any) -> None:
    """/session 快照 = 本会话最终产出；不含「用户: … → Agent: …」原始回声。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    hierarchy = _build_hierarchy(tmp_path)
    provider = MockProvider()
    provider.add_response(LLMResponse(content="已确认选品方法论进入下一阶段"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="汇报选品方法论进展",
            session_id=sid,
            session_store=store,
            hierarchy=hierarchy,
            project_id="proj1",
            employee_id="emp1",
        )
    assert result.reply
    session_text = hierarchy.load_memory(
        "session", project_id="proj1", session_id=sid, employee_id="emp1"
    )
    assert "已确认选品方法论" in session_text
    assert "用户:" not in session_text
    assert "汇报选品方法论进展" not in session_text
    employee_text = hierarchy.load_memory("employee", project_id="proj1", employee_id="emp1")
    assert "已确认选品方法论" in employee_text


@pytest.mark.asyncio
async def test_session_snapshot_readable_equals_outcome(home: Any, tmp_path: Any) -> None:
    """session 快照内容即最终产出本身（可读回对齐）。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    hierarchy = _build_hierarchy(tmp_path)
    provider = MockProvider()
    outcome = "数据看板已完成搭建与导出"
    provider.add_response(LLMResponse(content=outcome))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="请搭建数据看板",
            session_id=sid,
            session_store=store,
            hierarchy=hierarchy,
            project_id="proj1",
            employee_id="emp1",
        )
    assert result.reply
    session_text = hierarchy.load_memory(
        "session", project_id="proj1", session_id=sid, employee_id="emp1"
    )
    assert outcome in session_text
    assert "请搭建数据看板" not in session_text, "用户输入不应出现在 session 快照（回声双写）"
