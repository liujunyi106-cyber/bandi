# 功能：契约——TierB 提交前破坏者审查修复批次。
#
# A. 日志生命周期（Logger 每 run 关闭 vs 桥进程级复用 / Windows 轮转失败）+ 身份维度
# C. 杂项（子代理策略继承 / 执行器防御 / DB synchronous / 并行超时释放 / 成本屏障）
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest


def _drain(seconds: float = 0.4) -> None:
    """等桥消费线程把队列落盘（桥异步消费，断言前必须给窗口）。"""
    time.sleep(seconds)


# ------------------------------------------------------------------ A. 日志生命周期


def test_bridge_writes_after_logger_close(tmp_path: Path) -> None:
    """破坏者：Logger 被 close 后（runner 每轮关闭句柄），桥仍应把主链日志落盘。"""
    from bandi.observability.logger import Logger
    from bandi.observability.stdlib_bridge import install_logger_bridge, uninstall_logger_bridge

    name = "bandi-t1-close-survive"
    logger = Logger(log_dir=tmp_path, console_output=False)
    handler = install_logger_bridge(logger, logger_name=name)
    try:
        logging.getLogger(name).info("before-close-entry")
        _drain()
        logger.close()
        logging.getLogger(name).info("after-close-entry")
        _drain()
    finally:
        handler.close()
        uninstall_logger_bridge(handler)

    content = (tmp_path / "bandi.log").read_text(encoding="utf-8")
    assert "before-close-entry" in content
    assert "after-close-entry" in content, "Logger close 后桥写入被静默丢弃"


def test_reinstall_bridge_rebinds_new_logger(tmp_path: Path) -> None:
    """接线契约：新 run 重装桥时必须重绑到该 run 的 Logger（写它的目录，不写旧目录）。"""
    from bandi.observability.logger import Logger
    from bandi.observability.stdlib_bridge import install_logger_bridge, uninstall_logger_bridge

    name = "bandi-t1-rebind"
    dir1, dir2 = tmp_path / "l1", tmp_path / "l2"
    logger1 = Logger(log_dir=dir1, console_output=False)
    h1 = install_logger_bridge(logger1, logger_name=name)
    logger1.close()
    logger2 = Logger(log_dir=dir2, console_output=False)
    h2 = install_logger_bridge(logger2, logger_name=name)
    try:
        assert h2 is h1, "已有未停止的桥应复用同一 handler（幂等契约）"
        logging.getLogger(name).info("run2-entry")
        _drain()
    finally:
        h2.close()
        uninstall_logger_bridge(h2)

    assert "run2-entry" in (dir2 / "bandi.log").read_text(encoding="utf-8"), (
        "重装桥后日志应写入新 Logger 的目录（重绑失效则写去旧目录或丢失）"
    )


def test_rotation_rename_failure_keeps_writing(tmp_path: Path) -> None:
    """破坏者：多句柄并存致 rename 失败时，轮转失败不得丢日志、不得留关闭句柄。"""
    from bandi.observability.logger import Logger

    logger1 = Logger(log_dir=tmp_path, console_output=False, max_bytes=200)
    logger2 = Logger(log_dir=tmp_path, console_output=False, max_bytes=200)
    try:
        logger1.info("m", "x" * 300)
        # 第二次写入触发大小轮转——Windows 上 logger2 句柄占住 rename 目标
        logger1.info("m", "post-rotation-failure")
        assert not logger1._file.closed, "轮转失败后句柄停在 closed（后续日志全丢）"
        logger1.info("m", "still-alive")
    finally:
        logger1.close()
        logger2.close()

    content = (tmp_path / "bandi.log").read_text(encoding="utf-8")
    assert "post-rotation-failure" in content
    assert "still-alive" in content


def test_log_context_and_audit_actor_carry_user_id() -> None:
    """契约：日志上下文/审计主体必须能携带真实 user_id（用户→岗位链的可观测投影）。"""
    from bandi.observability.audit_reporter import resolve_audit_actor
    from bandi.observability.context import bind_log_context, current_log_context

    with bind_log_context(session_id="s", user_id="alice"):
        assert current_log_context().user_id == "alice"
        actor = resolve_audit_actor()
        assert (actor.kind, actor.id) == ("user", "alice")

    with bind_log_context(agent_id="worker-1"):
        actor2 = resolve_audit_actor()
        assert (actor2.kind, actor2.id) == ("agent", "worker-1")


@pytest.mark.asyncio
async def test_main_chain_log_identity_wiring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """端到端接线：主链日志身份四维——loop 从 ctx.meta 绑定 + 桥快照进落盘条目。"""
    from bandi.agent import loop as loop_mod
    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.config import BandiConfig
    from bandi.memory.session.store import SessionStore

    # 1) loop 层：真实 run 时 bind_log_context 必须收到 meta 携带的身份
    calls: list[dict] = []
    real_bind = loop_mod.bind_log_context

    def _spy_bind(**kwargs: object) -> object:
        calls.append(dict(kwargs))
        return real_bind(**kwargs)

    monkeypatch.setattr(loop_mod, "bind_log_context", _spy_bind)
    cfg = BandiConfig()
    cfg.agent.max_iterations = 1
    store = SessionStore(str(tmp_path / "sess.db"))
    sid = store.create_session(mode="execute")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    result = await run_agent_turn(
        user_input="hi",
        session_id=sid,
        session_store=store,
        bandi_config=cfg,
        user_id="alice",
    )
    store.close()
    assert result.reply is not None
    assert calls, "主循环必须绑定日志上下文"
    bound = calls[-1]
    assert bound["session_id"] == sid
    assert bound["user_id"] == "alice"
    assert bound["role"] == "user", f"role 键名错配未修复：{bound}"
    assert bound["run_id"], "run_id 必须有真实生成值（此前无生产者恒空）"

    # 2) 桥层：绑定的身份必须快照进落盘条目（ContextVar 不跨线程，须在 emit 时快照）
    from bandi.observability.context import bind_log_context
    from bandi.observability.logger import Logger
    from bandi.observability.stdlib_bridge import install_logger_bridge, uninstall_logger_bridge

    log_dir = tmp_path / "bridge-logs"
    logger = Logger(log_dir=log_dir, console_output=False)
    handler = install_logger_bridge(logger, logger_name="bandi-probe-e2e")
    try:
        with bind_log_context(session_id=sid, user_id="alice", role="user", run_id="r-e2e"):
            logging.getLogger("bandi-probe-e2e").warning("identity-snapshot")
        _drain()
    finally:
        handler.close()
        uninstall_logger_bridge(handler)

    entries = [
        json.loads(line)
        for line in (log_dir / "bandi.log").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    snap = [e for e in entries if e.get("message") == "identity-snapshot"]
    assert snap, "桥未把绑定上下文快照进条目"
    assert snap[0]["session_id"] == sid
    assert snap[0]["user_id"] == "alice"
    assert snap[0]["role"] == "user"
    assert snap[0]["run_id"] == "r-e2e"


# ------------------------------------------------------------------ B. 审批拒绝追踪


def _make_pipeline(*, ui: object | None) -> object:
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    return PermissionsPipeline(user_interaction=ui, ask_tools={"bash"})


class _DenyUI:
    async def ask_permission(self, **kwargs: object) -> bool:
        return False


class _AllowUI:
    async def ask_permission(self, **kwargs: object) -> bool:
        return True


def test_interactive_denial_recorded_and_short_circuits() -> None:
    """破坏者：交互式（CLI/SSE）通道的拒绝必须记账——达阈值后不再重复追问。"""
    import asyncio

    from bandi.core.message import ToolCall
    from bandi.safety.denial_tracker import get_denial_tracker

    pp = _make_pipeline(ui=_DenyUI())
    pp.bind_session_id("sess-ui")
    tc = ToolCall(id="t1", name="bash", arguments={"command": "ls"})

    for _ in range(3):
        verdict = asyncio.run(pp.check(tc, None, "execute"))
        assert verdict.allow is False

    stats = get_denial_tracker().stats("sess-ui", "bash")
    assert stats.consecutive == 3, f"交互式拒绝未记账：{stats}"
    assert get_denial_tracker().should_auto_deny("sess-ui", "bash") is True


def test_interactive_approval_clears_consecutive() -> None:
    """契约：交互式放行同样清零连续计数（与批准语义一致）。"""
    import asyncio

    from bandi.core.message import ToolCall
    from bandi.safety.denial_tracker import get_denial_tracker

    tracker = get_denial_tracker()
    tracker.record_denial("sess-ui2", "bash")
    tracker.record_denial("sess-ui2", "bash")

    pp = _make_pipeline(ui=_AllowUI())
    pp.bind_session_id("sess-ui2")
    tc = ToolCall(id="t2", name="bash", arguments={"command": "ls"})
    verdict = asyncio.run(pp.check(tc, None, "execute"))
    assert verdict.allow is True
    assert tracker.stats("sess-ui2", "bash").consecutive == 0


@pytest.mark.asyncio
async def test_auto_review_approval_clears_consecutive() -> None:
    """契约：机审放行（allow）必须清零连续计数——否则「偶尔批准」仍被永久压制。"""
    from bandi.core.event_bus import EventBus
    from bandi.core.message import ToolCall
    from bandi.safety.denial_tracker import get_denial_tracker
    from bandi.tools.approval import get_approval_store
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.permissions_pipeline import PermissionsPipeline
    from bandi.tools.registry import ToolRegistry

    sid = "sess-ar"
    tracker = get_denial_tracker()
    tracker.record_denial(sid, "bash")
    tracker.record_denial(sid, "bash")

    store = get_approval_store()
    store.set_reviewer(sid, "auto_review")

    async def _allow_llm(messages: list[dict]) -> str:
        return '{"decision": "allow", "rationale": "looks safe"}'

    ex = ToolExecutor(
        ToolRegistry(),
        permissions_pipeline=PermissionsPipeline(),
        event_bus=EventBus(),
        auto_review_llm=_allow_llm,
    )
    ex.bind_session_meta({"session_id": sid})
    tc = ToolCall(id="t3", name="bash", arguments={"command": "ls"})
    gate = await ex._approval_gate(tc, None)
    store.clear_session(sid)

    assert gate is None, "机审 allow 应放行（返回 None 继续执行）"
    assert tracker.stats(sid, "bash").consecutive == 0, "机审放行必须清零连续计数"


def test_denial_tracker_ttl_prune_keeps_consecutive_accurate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """破坏者：批准前记录过期被裁剪时，连续计数不得被永久低估。"""
    import bandi.safety.denial_tracker as dt_mod
    from bandi.safety.denial_tracker import DenialTracker

    clock = {"t": 1000.0}
    monkeypatch.setattr(dt_mod.time, "time", lambda: clock["t"])

    tracker = DenialTracker(ttl_seconds=10.0)
    tracker.record_denial("s", "bash")  # A @1000
    tracker.record_approval("s", "bash")
    clock["t"] = 1005.0
    tracker.record_denial("s", "bash")  # B @1005（批准后）
    clock["t"] = 1012.0  # A 已过期（>10s），B 未过期（7s）
    assert tracker.stats("s", "bash").consecutive == 1, "批准后的 1 次拒绝应为连续 1"
    tracker.record_denial("s", "bash")  # C @1012
    assert tracker.stats("s", "bash").consecutive == 2, "批准后两次拒绝应为连续 2（被低估到 1）"


@pytest.mark.asyncio
async def test_short_circuit_emits_approval_denied_event() -> None:
    """契约：达阈值短路拒绝必须发审计事件（三路径口径一致，不出现「拒而无痕」）。"""
    from bandi.core.event_bus import EventBus
    from bandi.core.events import EventType
    from bandi.core.message import ToolCall
    from bandi.safety.denial_tracker import get_denial_tracker
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.permissions_pipeline import PermissionsPipeline
    from bandi.tools.registry import ToolRegistry

    sid = "sess-short"
    tracker = get_denial_tracker()
    for _ in range(3):
        tracker.record_denial(sid, "bash")

    bus = EventBus()
    seen: list[str] = []

    async def _collect(event: object) -> None:
        seen.append(str(getattr(event, "type", "")))

    await bus.subscribe(EventType.APPROVAL_DENIED, _collect)
    ex = ToolExecutor(
        ToolRegistry(),
        permissions_pipeline=PermissionsPipeline(),
        event_bus=bus,
    )
    ex.bind_session_meta({"session_id": sid})
    tc = ToolCall(id="t4", name="bash", arguments={"command": "ls"})
    gate = await ex._approval_gate(tc, None)

    assert gate is not None and gate.success is False
    assert "auto-denied" in str(gate.error)
    assert any("APPROVAL_DENIED" in s for s in seen), f"短路拒绝未发审计事件：{seen}"


# ------------------------------------------------------------------ C. 杂项修复


def test_subagent_inherits_policy_engine_two_points(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """接线契约（两端点）：父策略引擎进工具上下文 + spawn 装配传给子代理管理器。

    子执行器此前只继承 role_tool_tags/沙箱，不继承 policy_engine——绑定 member 的
    账号被硬拒 bash 后，经一次 subagent_spawn 即可在子代理内无限制执行（权限漂移）。
    """
    import asyncio

    from bandi.agent import subagent_tool as st_mod
    from bandi.agent.subagent import SubagentManager, SubagentResult
    from bandi.core.event_bus import EventBus
    from bandi.core.message import ToolCall
    from bandi.core.permission.policy_engine import PolicyEngine
    from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.registry import ToolRegistry

    engine = PolicyEngine()  # 真实空引擎（默认 ALLOW）——只验透传链路
    seen: dict[str, Any] = {}

    class _EchoExtraTool(BaseTool):
        name = "echo_extra"
        description = "stub"
        category = ToolCategory.UTIL

        async def execute(self, params: dict, context: Any = None) -> dict:
            seen["extra"] = dict(getattr(context, "extra", None) or {})
            return {"success": True, "output": "ok"}

    registry = ToolRegistry()
    registry.register_instance(_EchoExtraTool())
    ex = ToolExecutor(registry, policy_engine=engine, event_bus=EventBus())
    results = asyncio.run(ex.execute([ToolCall(id="t1", name="echo_extra", arguments={})]))
    assert results and results[0].success
    assert seen["extra"].get("policy_engine") is engine, (
        "父策略引擎未进工具上下文——子代理执行面可绕过用户权限档"
    )

    captured: dict[str, Any] = {}

    class _FakeManager:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def spawn_many(self, *a: Any, **k: Any) -> list:
            return [SubagentResult(success=True, summary="ok")]

        def close(self) -> None:
            pass

    monkeypatch.setattr(st_mod, "SubagentManager", _FakeManager)
    tool = st_mod.SubAgentTool()
    context = ToolPermissionContext(user_id="alice", extra={"policy_engine": engine})
    asyncio.run(tool.execute({"task": "做点事"}, context))
    assert captured.get("policy_engine") is engine, "spawn 装配未透传策略引擎"

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    mgr = SubagentManager(policy_engine=engine)  # type: ignore[arg-type]
    try:
        assert mgr._policy_engine is engine, "管理器未持有策略引擎（子执行器装配无来源）"
    finally:
        mgr.close()


def test_tool_executor_user_id_is_normalized() -> None:
    """契约：user_id 非 str（None/数字）不得崩在缓存 scope 拼接（构造期防御）。"""
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.registry import ToolRegistry

    ex_none = ToolExecutor(ToolRegistry(), agent_id="worker", user_id=None)
    assert ex_none._effective_user_id == "worker"
    ex_num = ToolExecutor(ToolRegistry(), agent_id="worker", user_id=123)  # type: ignore[arg-type]
    assert ex_num._effective_user_id == "123"
    assert isinstance(ex_num.cache_scope, str)


def test_connect_app_db_keeps_stdlib_synchronous(tmp_path: Path) -> None:
    """契约：connect_app_db 只开 WAL，不得擅自改 synchronous（未登记口径变化）。"""
    from bandi.utils.db import connect_app_db

    conn = connect_app_db(tmp_path / "app.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2, (
            "synchronous 被改为 NORMAL(1)——与 stdlib 默认 FULL(2) 不等价"
        )
    finally:
        conn.close()


def test_cost_store_flush_is_persistence_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约：flush 返回时已入队条目必须全部可查（此前只等队列空，在途未提交不入账）。"""
    from bandi.observability.cost_store import CostRecord, CostStore

    store = CostStore(tmp_path / "cost.db")
    try:
        real_insert = store._insert

        def _slow_insert(record: object) -> None:
            # 拖慢消费者模拟负载：flush 不得在「已出队未提交」窗口提前返回
            time.sleep(0.005)
            real_insert(record)  # type: ignore[arg-type]

        monkeypatch.setattr(store, "_insert", _slow_insert)
        for i in range(200):
            store.enqueue(CostRecord(session_id=f"s{i}", model="m", input_tokens=i))
        store.flush(timeout=10.0)
        row = store._conn.execute("SELECT COUNT(*) FROM llm_request_log").fetchone()
        assert row[0] == 200, f"flush 后仍有在途条目：{row[0]}/200"
    finally:
        store.close()


def test_cost_store_close_then_enqueue_counts_dropped(tmp_path: Path) -> None:
    """契约：close 后入队必须计入 dropped（口径：enqueued+dropped=收到条数）。"""
    from bandi.observability.cost_store import CostRecord, CostStore

    store = CostStore(tmp_path / "cost.db")
    store.close()
    before = store.stats().dropped
    store.enqueue(CostRecord(session_id="late", model="m"))
    assert store.stats().dropped == before + 1


def test_bridge_close_then_emit_counts_dropped(tmp_path: Path) -> None:
    """契约：桥停止后进入的日志必须计入 dropped（不静默蒸发）。"""
    from bandi.observability.logger import Logger
    from bandi.observability.stdlib_bridge import install_logger_bridge, uninstall_logger_bridge

    name = "bandi-t1-closed-count"
    logger = Logger(log_dir=tmp_path, console_output=False)
    handler = install_logger_bridge(logger, logger_name=name)
    try:
        handler.close()
        logging.getLogger(name).info("late-entry")
        assert handler.stats().dropped >= 1, "停止后丢弃未计数"
    finally:
        uninstall_logger_bridge(handler)
        logger.close()


def test_audit_rollback_does_not_truncate_concurrent_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """破坏者：回滚窗口内他方已 fsync 的记录不得被截掉（回滚误伤并发写者）。"""
    from bandi.observability import audit_reporter as ar_mod
    from bandi.observability.audit_reporter import AuditReporter

    chain = tmp_path / "chain.jsonl"
    chain.write_text('{"a": 1}\n', encoding="utf-8")
    real_fsync = ar_mod.os.fsync
    calls = {"n": 0}

    def _evil_fsync(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 1:  # 只失败一次：回滚自身的 fsync（若走到）必须正常执行
            with open(chain, "a", encoding="utf-8") as other:
                other.write('{"other": true}\n')
                other.flush()
                real_fsync(other.fileno())
            raise OSError("simulated fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(ar_mod.os, "fsync", _evil_fsync)
    with pytest.raises(OSError):
        AuditReporter._append_line(chain, '{"mine": 1}\n')

    text = chain.read_text(encoding="utf-8")
    assert '{"other": true}' in text, "并发写者已落盘记录被回滚截掉（误伤）"
