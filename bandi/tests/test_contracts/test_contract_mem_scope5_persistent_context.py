# 功能：契约——员工持久上下文（轻量持久：会话键持久 + 历史轻量裁剪）
#
#
# - `session_scope_id` 还参与队列 frontier tag（`_pop_tag`/`scoped_queue_key`）——
# **不能改**；新增独立持久键 `history_scope_id` 只影响会话拼接（队列/claim 语义零变化）；
# - 「回灌」= working 检索 + episodic/long_term 注入（既有 perception 链路；
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.company.company_runtime import CompanyRuntime
from bandi.company.phase import Phase
from bandi.company.session_scoping import scoped_member_session_id, scoped_queue_key
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.core.event_bus import EventBus
from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore

_SCOPE = "staff"


# ── A. 会话键持久（history_scope 只改会话拼接；project/role/employee 隔离保持）──


def test_history_scope_makes_member_session_persistent_across_tasks() -> None:
    a = scoped_member_session_id("p", "task-1", "worker", "emp", history_scope_id=_SCOPE)
    b = scoped_member_session_id("p", "task-2", "worker", "emp", history_scope_id=_SCOPE)
    assert a == b, "同项目同角色跨任务必须共享同一员工会话（轻量持久）"
    assert "task-1" not in a and _SCOPE in a


def test_without_history_scope_keeps_task_level_legacy_key() -> None:
    a = scoped_member_session_id("p", "task-1", "worker", "emp")
    b = scoped_member_session_id("p", "task-2", "worker", "emp")
    assert a != b, "缺省（无持久键）必须保持任务级旧行为"


def test_history_scope_still_isolates_project_role_employee() -> None:
    """破坏者：持久键不得让不同项目/角色/员工的会话合并。"""
    base = dict(history_scope_id=_SCOPE)
    ids = {
        scoped_member_session_id("p1", "t", "worker", "e1", **base),
        scoped_member_session_id("p2", "t", "worker", "e1", **base),
        scoped_member_session_id("p1", "t", "market", "e1", **base),
        scoped_member_session_id("p1", "t", "worker", "e2", **base),
    }
    assert len(ids) == 4, "项目/角色/员工三维隔离在持久键下必须保持"


def test_runtime_member_session_and_member_state_share_persistent_key() -> None:
    """runtime 三处会话键（member/role 会话/状态）必须同一持久键——不一致会状态错位。"""
    rt_a = CompanyRuntime(
        project_id="p",
        session_scope_id="task-a",
        role_id="worker",
        employee_id="e",
        history_scope_id=_SCOPE,
    )
    rt_b = CompanyRuntime(
        project_id="p",
        session_scope_id="task-b",
        role_id="worker",
        employee_id="e",
        history_scope_id=_SCOPE,
    )
    assert rt_a.member_session_id() == rt_b.member_session_id()
    sess_a = rt_a.ensure_role_member_session("worker", "e")
    sess_b = rt_b.ensure_role_member_session("worker", "e")
    assert sess_a.member_session_id == sess_b.member_session_id, "成员状态会话必须与执行会话同键"
    assert rt_a._session_id_for_role("worker") == rt_b._session_id_for_role("worker")


def test_queue_key_ignores_history_scope() -> None:
    """调度语义护栏：队列 frontier tag 仍按任务 scope（history_scope 不进队列键）。"""
    assert scoped_queue_key(session_scope_id="task-1", role_id="worker") == "task-1::worker"


def _seed_history(sid: str, store: SessionStore, count: int) -> None:
    for i in range(count):
        role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
        store.append_message(sid, Message(role=role, content=f"历史-{i}"))


def _restore_ctx() -> Any:
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig

    ctx = ExecutionContext(messages=[], provider=None, config=AgentConfig(), bus=EventBus())
    return ctx


def test_restore_trims_to_recent_window(tmp_path: Path) -> None:
    from bandi.core.agent_runner import _session_restore

    store = SessionStore(str(tmp_path / "s.db"))
    sid = store.create_session(title="persist")
    _seed_history(sid, store, 20)
    ctx = _restore_ctx()

    _session_restore(sid, store, ctx, max_history_messages=12)

    assert len(ctx.messages) == 12, f"必须只恢复最近 12 条，实际 {len(ctx.messages)}"
    assert ctx.messages[0].role == MessageRole.USER, "窗口首条必须对齐 user（防孤儿）"
    assert ctx.messages[-1].content == "历史-19", "保留的必须是最近的尾部"


def test_restore_aligns_user_boundary_when_window_starts_mid_turn(tmp_path: Path) -> None:
    """破坏者：窗口首条若是 assistant（裁在半轮）→ 必须前移到 user。"""
    from bandi.core.agent_runner import _session_restore

    store = SessionStore(str(tmp_path / "s.db"))
    sid = store.create_session(title="persist")
    # [assistant, assistant, user]：max=2 → 窗口 [assistant, user] → 前移到 [user]
    roles = (MessageRole.ASSISTANT, MessageRole.ASSISTANT, MessageRole.USER)
    for i, role in enumerate(roles):
        store.append_message(sid, Message(role=role, content=f"历史-{i}"))
    ctx = _restore_ctx()

    _session_restore(sid, store, ctx, max_history_messages=2)

    assert len(ctx.messages) == 1, f"裁剪须前移到 user 边界，实际 {len(ctx.messages)}"
    assert ctx.messages[0].role == MessageRole.USER


def test_restore_default_is_full_history(tmp_path: Path) -> None:
    """回归护栏：缺省（max=0）必须保持全量恢复（普通会话零行为变化）。"""
    from bandi.core.agent_runner import _session_restore

    store = SessionStore(str(tmp_path / "s.db"))
    sid = store.create_session(title="plain")
    _seed_history(sid, store, 20)
    ctx = _restore_ctx()

    _session_restore(sid, store, ctx)

    assert len(ctx.messages) == 20


def test_restore_short_history_untouched(tmp_path: Path) -> None:
    from bandi.core.agent_runner import _session_restore

    store = SessionStore(str(tmp_path / "s.db"))
    sid = store.create_session(title="persist")
    _seed_history(sid, store, 6)
    ctx = _restore_ctx()

    _session_restore(sid, store, ctx, max_history_messages=12)

    assert len(ctx.messages) == 6, "未超窗口的历史不得被裁"


# ── C. 生产装配（run_work_item → 持久键；run_agent_turn 公司链路 → 裁剪上限）──


def _dispatcher_with_stub(store: WorkItemStore) -> Any:
    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.gate_harness import GateHarness
    from bandi.company.org_engine import OrgEngine
    from bandi.company.recruiter import Recruiter
    from bandi.company.seat_executor import SeatResult

    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")

    class _StubSeat:
        async def prepare_seat(self, runtime: Any, work_item: Any) -> None:
            pass

        async def run_turn(self, runtime: Any, work_item: Any) -> SeatResult:
            return SeatResult(success=True, output="完成")

        async def checkpoint(self, runtime: Any) -> dict:
            return {}

        async def interrupt(self, runtime: Any) -> None:
            pass

        async def shutdown(self) -> None:
            pass

    return CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=_StubSeat(),
        config=DispatcherConfig(max_concurrent=3),
    )


def test_run_work_item_builds_persistent_runtime(tmp_path: Path) -> None:
    """装配：run_work_item 必须把持久键传给 runtime（两个任务同会话）。"""
    import asyncio
    from unittest.mock import patch

    store = WorkItemStore(str(tmp_path / "w.db"))
    dispatcher = _dispatcher_with_stub(store)
    item = create_work_item(
        title="任务甲",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        phase=Phase.READY,
        metadata={"project_id": "p1"},
    )
    store.save(item)

    with patch.object(dispatcher, "_build_runtime", wraps=dispatcher._build_runtime) as spy:
        asyncio.run(dispatcher.run_work_item(item.id))

    seen = [kwargs.get("history_scope_id") for _, kwargs in spy.call_args_list]
    assert seen and all(s == _SCOPE for s in seen), f"run_work_item 未传持久键：{seen}"


def test_run_work_item_two_tasks_share_session(tmp_path: Path) -> None:
    import asyncio

    store = WorkItemStore(str(tmp_path / "w2.db"))
    dispatcher = _dispatcher_with_stub(store)
    session_ids: list[str] = []

    real_build = dispatcher._build_runtime

    def _spy_build(**kwargs: Any) -> Any:
        runtime = real_build(**kwargs)
        session_ids.append(runtime.member_session_id())
        return runtime

    dispatcher._build_runtime = _spy_build  # type: ignore[method-assign]
    for title in ("任务甲", "任务乙"):
        item = create_work_item(
            title=title,
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            phase=Phase.READY,
            metadata={"project_id": "p1"},
        )
        store.save(item)
        asyncio.run(dispatcher.run_work_item(item.id))

    assert len(session_ids) == 2 and session_ids[0] == session_ids[1], (
        f"同员工跨任务必须共享会话：{session_ids}"
    )


@pytest.mark.asyncio
async def test_run_agent_turn_company_path_limits_restore(tmp_path: Path, monkeypatch: Any) -> None:
    """生产装配：公司链路（company_runtime 存在）恢复必须带轻量上限；普通链路 0。"""
    from bandi.core import agent_runner as runner
    from bandi.core.config import BandiConfig
    from bandi.core.message import LLMResponse, TokenUsage
    from bandi.llm.mock import MockProvider

    seen_limits: list[int] = []
    real_restore = runner._session_restore

    def _spy_restore(
        session_id: str,
        store: Any,
        ctx: Any,
        checkpoint_store: Any = None,
        max_history_messages: int = 0,
    ) -> None:
        seen_limits.append(max_history_messages)
        real_restore(
            session_id,
            store,
            ctx,
            checkpoint_store=checkpoint_store,
            max_history_messages=max_history_messages,
        )

    monkeypatch.setattr(runner, "_session_restore", _spy_restore)
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="完成",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=2),
            finish_reason="stop",
        )
    )
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    runtime = CompanyRuntime(
        project_id="p1",
        session_scope_id="task-1",
        role_id="worker",
        employee_id="e",
        history_scope_id=_SCOPE,
    )

    with monkeypatch.context() as patcher:
        patcher.setattr("pathlib.Path.home", lambda: Path(str(tmp_path)))
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        with monkeypatch.context() as inner:
            inner.setattr(runner, "build_provider", lambda *a, **k: provider)
            await runner.run_agent_turn(
                user_input="任务正文",
                mode="execute",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
                company_runtime=runtime,
            )

    assert seen_limits, "run_agent_turn 未调用 _session_restore（装配路径断了）"
    assert seen_limits[0] == runner._COMPANY_SESSION_HISTORY_MESSAGES, (
        f"公司链路必须带轻量上限：{seen_limits}"
    )


@pytest.mark.asyncio
async def test_run_agent_turn_plain_path_keeps_full_restore(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """回归护栏：非公司链路（company_runtime=None）恢复上限必须为 0（全量）。"""
    from bandi.core import agent_runner as runner
    from bandi.core.config import BandiConfig
    from bandi.core.message import LLMResponse, TokenUsage
    from bandi.llm.mock import MockProvider

    seen_limits: list[int] = []
    real_restore = runner._session_restore

    def _spy_restore(
        session_id: str,
        store: Any,
        ctx: Any,
        checkpoint_store: Any = None,
        max_history_messages: int = 0,
    ) -> None:
        seen_limits.append(max_history_messages)
        real_restore(
            session_id,
            store,
            ctx,
            checkpoint_store=checkpoint_store,
            max_history_messages=max_history_messages,
        )

    monkeypatch.setattr(runner, "_session_restore", _spy_restore)
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="收到",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=2),
            finish_reason="stop",
        )
    )
    cfg = BandiConfig()
    cfg.llm.api_key = ""

    with monkeypatch.context() as patcher:
        patcher.setattr("pathlib.Path.home", lambda: Path(str(tmp_path)))
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        with monkeypatch.context() as inner:
            inner.setattr(runner, "build_provider", lambda *a, **k: provider)
            await runner.run_agent_turn(
                user_input="普通对话",
                mode="execute",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )

    assert seen_limits, "run_agent_turn 未调用 _session_restore"
    assert seen_limits[0] == 0, f"普通链路必须保持全量恢复：{seen_limits}"
