from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


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


class _RecordingProvider(MockProvider):
    def __init__(self) -> None:
        super().__init__()
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.seen_messages.append(messages)
        return await super().complete(messages, tools=tools, **kwargs)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.seen_messages.append(messages)
        async for ev in super().stream(messages, tools=tools, **kwargs):
            yield ev


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_run_agent_turn_persists_session_and_employee_snapshot(
    home: Any, tmp_path: Any
) -> None:
    """生产主链路：轮末必须把本轮写入 session + employee 快照（最终产物断言）。"""
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
    # 原始回声——该回声已由 working/episodic B 通道承载）
    assert "已确认选品方法论" in session_text, "session 快照应含本会话最终产出"
    employee_text = hierarchy.load_memory("employee", project_id="proj1", employee_id="emp1")
    assert "已确认选品方法论" in employee_text, "employee 快照应含最终产出"


@pytest.mark.asyncio
async def test_run_agent_turn_without_hierarchy_is_noop(home: Any) -> None:
    """无 hierarchy 参数 → 零行为、不抛错（向后兼容）。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = MockProvider()
    provider.add_response(LLMResponse(content="好的"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="hello",
            session_id=sid,
            session_store=store,
        )
    assert result.reply


@pytest.mark.asyncio
async def test_run_agent_turn_missing_employee_id_skips_snapshot(home: Any, tmp_path: Any) -> None:
    """缺 employee_id → 跳过 session/employee 快照写入，不抛错。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    hierarchy = _build_hierarchy(tmp_path)
    provider = MockProvider()
    provider.add_response(LLMResponse(content="好的"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="hello",
            session_id=sid,
            session_store=store,
            hierarchy=hierarchy,
            project_id="proj1",
        )
    assert result.reply
    session_text = hierarchy.load_memory(
        "session", project_id="proj1", session_id=sid, employee_id="emp1"
    )
    assert session_text == "", "无 employee_id 时不得写入 session 快照"
    employee_text = hierarchy.load_memory("employee", project_id="proj1", employee_id="emp1")
    assert employee_text == "", "无 employee_id 时不得写入 employee 快照"


@pytest.mark.asyncio
async def test_context_loader_injects_hierarchy_memory(home: Any, tmp_path: Any) -> None:
    """检索注入：ContextLoader 带 hierarchy → load() 的 project_instructions 含四段记忆。"""
    from bandi.core.message import Message, MessageRole
    from bandi.memory.session.store import SessionStore
    from bandi.perception.context_loader import ContextLoader

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    hierarchy = _build_hierarchy(tmp_path)
    hierarchy.save_memory("global", "产品定位：个人多 Agent 数字员工组织", allow_shared=True)
    hierarchy.save_memory("project", "选品方法论：批量上架前先做竞品分层", project_id="proj1")
    hierarchy.save_memory(
        "session", "会话：完成竞品分析报告", project_id="proj1", session_id=sid, employee_id="emp1"
    )
    hierarchy.save_memory(
        "employee", "员工：选品专员擅长爆品分层", project_id="proj1", employee_id="emp1"
    )

    loader = ContextLoader(
        store,
        None,
        project_root=None,
        hierarchy=hierarchy,
        project_id="proj1",
        employee_id="emp1",
    )
    bundle = await loader.load(Message(role=MessageRole.USER, content="继续"), session_id=sid)
    # 避免 builder 后缀裁剪优先裁掉层级记忆）
    assert "" == bundle.project_instructions
    assert "个人多 Agent 数字员工组织" in bundle.memory_context
    assert "选品方法论" in bundle.memory_context
    assert "竞品分析报告" in bundle.memory_context
    assert "爆品分层" in bundle.memory_context


@pytest.mark.asyncio
async def test_context_loader_without_hierarchy_unchanged(home: Any) -> None:
    """无 hierarchy → project_instructions 不含记忆段（保持原行为）。"""
    from bandi.core.message import Message, MessageRole
    from bandi.memory.session.store import SessionStore
    from bandi.perception.context_loader import ContextLoader

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    loader = ContextLoader(store, None, project_root=None)
    bundle = await loader.load(Message(role=MessageRole.USER, content="继续"), session_id=sid)
    assert "记忆" not in bundle.project_instructions


@pytest.mark.asyncio
async def test_run_agent_turn_injects_memory_into_system_prompt(home: Any, tmp_path: Any) -> None:
    """端到端最终产物：预置全局记忆 → run_agent_turn → system prompt 含记忆段。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    hierarchy = _build_hierarchy(tmp_path)
    hierarchy.save_memory("global", "项目铁律：回复必须用简体中文", allow_shared=True)
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="好的"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="hello",
            session_id=sid,
            session_store=store,
            hierarchy=hierarchy,
            project_id="proj1",
            employee_id="emp1",
        )
    assert result.reply
    system_prompt = "\n".join(
        str(m.get("content") or "") for m in provider.seen_messages[0] if m.get("role") == "system"
    )
    assert "项目铁律：回复必须用简体中文" in system_prompt, (
        "四层记忆必须注入 system prompt（ContextLoader 未接 hierarchy 时注入失效）"
    )


@pytest.mark.asyncio
async def test_run_agent_turn_lazily_builds_hierarchy(home: Any) -> None:
    """不传 hierarchy 但传 project_id+employee_id → run_agent_turn 惰性构建并写快照。

    端到端最终产物：构建后的 hierarchy 必须真正接管（轮末快照落库可读回）。
    """
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = MockProvider()
    provider.add_response(LLMResponse(content="已确认选品方法论进入下一阶段"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="汇报选品方法论进展",
            session_id=sid,
            session_store=store,
            project_id="proj1",
            employee_id="emp1",
        )
    assert result.reply
    from bandi.company.store import WorkItemStore

    snapshot_store = WorkItemStore(str(home / ".bandi" / "data" / "hierarchy_work_items.db"))
    session_snaps = snapshot_store.list_memory_snapshots(
        project_id="proj1", employee_id="emp1", memory_scope="session"
    )
    employee_snaps = snapshot_store.list_memory_snapshots(
        project_id="proj1", employee_id="emp1", memory_scope="employee"
    )
    assert session_snaps, "惰性构建后 session 快照必须落库"
    assert employee_snaps, "惰性构建后 employee 快照必须落库"
    # 对话回声——该回声已由 working/episodic B 通道承载，避免双写）
    assert "已确认选品方法论" in session_snaps[0]["memory_text"]
    assert "已确认选品方法论" in employee_snaps[0]["memory_text"]


@pytest.mark.asyncio
async def test_run_agent_turn_lazy_hierarchy_injects_into_system_prompt(home: Any) -> None:
    """惰性构建的 hierarchy 必须接通 ContextLoader 记忆注入（最终产物断言）。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.global_store import GlobalMemoryStore
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    data_dir = home / ".bandi" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    global_store = GlobalMemoryStore(str(data_dir / "hierarchy_global.db"))
    global_store.put_global_memory("global_hierarchy_memory", "项目铁律：回复必须用简体中文")
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="好的"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="hello",
            session_id=sid,
            session_store=store,
            project_id="proj1",
            employee_id="emp1",
        )
    assert result.reply
    system_prompt = "\n".join(
        str(m.get("content") or "") for m in provider.seen_messages[0] if m.get("role") == "system"
    )
    assert "项目铁律：回复必须用简体中文" in system_prompt, (
        "惰性构建的 hierarchy 必须注入全局记忆到 system prompt"
    )
