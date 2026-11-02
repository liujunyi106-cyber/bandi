# 功能：契约——agent 核心复查加固：停滞封顶不覆盖 pass / 运行中模式切换同步
# executor / 工具限流生产形态 / on_compressing 终态早退。
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bandi.agent.base import ExecutionContext
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import Message, MessageRole
from bandi.llm.mock import MockProvider


def _ctx(**overrides: Any) -> ExecutionContext:
    provider = MockProvider()
    provider.count_tokens = lambda text: max(1, len(text or "") // 4) if text else 0
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=provider,
        config=AgentConfig(),
        bus=EventBus(),
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


# ---------- 停滞封顶不覆盖 pass ----------


def test_contract_stagnation_cap_does_not_override_pass() -> None:
    """契约（验证语义·破坏者）：第 N+1 次 judge 判 pass 时不得被停滞封顶改写为
    escalate——修复前 rework_count≥阈值 + 上轮 rework 会无条件覆盖（含 pass）
    模型已修好仍强制升级且 judge 花费被丢弃。"""
    from bandi.agent.verifier import Verdict, Verifier

    out = Verifier().apply_stagnation_cap(
        {"last_verdict_outcome": "rework", "rework_count": 3},
        Verdict(action="pass", reason="ok"),
    )
    assert out.action == "pass", f"已通过不得被停滞封顶改写，实际 {out.action}"


def test_contract_stagnation_cap_escalates_persistent_rework() -> None:
    """契约（原意保留）：本次仍判 rework 且历史达阈值 → 升级（停滞治理）。"""
    from bandi.agent.verifier import Verdict, Verifier

    out = Verifier().apply_stagnation_cap(
        {"last_verdict_outcome": "rework", "rework_count": 3},
        Verdict(action="rework", reason="still bad"),
    )
    assert out.action == "escalate"


# ---------- 运行中模式切换同步 executor ----------


class _StubRegistry:
    def list_tools(self) -> list[Any]:
        return [SimpleNamespace(name="write")]

    def list_schemas(self, allowed_tools: list[str] | None = None) -> list[Any]:
        return [{"name": n} for n in (allowed_tools or ["write"])]


class _StubExecutor:
    def __init__(self) -> None:
        self.registry = _StubRegistry()
        self._mode = "plan"

    async def execute(self, calls: list[Any]) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_contract_runtime_mode_switch_syncs_executor_mode() -> None:
    """契约（模式同步·破坏者）：运行中切换模式（exit_plan_mode 后 mode_manager 已
    execute）→ on_executing 必须同步 executor._mode——修复前权限检查/中间件仍按旧
    模式（plan→execute 后写工具被拒；execute→plan 后只读约束缺口）。"""
    from bandi.agent.mode_manager import ModeManager
    from bandi.agent.states import on_executing
    from bandi.core.message import ToolCall

    ctx = _ctx()
    executor = _StubExecutor()
    ctx.tool_executor = executor
    ctx.meta["mode_manager"] = ModeManager("execute")  # 工具内已切换
    ctx.meta["mode"] = "plan"  # ctx 侧仍是旧值
    ctx.current_tool_calls = [ToolCall(id="c1", name="write", arguments={})]

    await on_executing(ctx)
    assert executor._mode == "execute", (
        f"运行中切换后 executor._mode 必须同步，实际 {executor._mode}"
    )


@pytest.mark.asyncio
async def test_contract_tool_call_limiter_production_call_shape() -> None:
    """契约（治理生效·破坏者）：生产调用形态（位置参数 execute(calls)）超限时
    必须设 MAX_TURNS 终态——修复前 ctx 只从 kwargs 取（测试专属形态），生产位置
    调用永不生效、模型继续空转到 max_turns。"""
    from bandi.agent.subagent import _ToolCallLimiter
    from bandi.core.message import ToolCall

    class _Inner:
        async def execute(self, calls: list[Any]) -> list[Any]:
            return ["ok"]

    ctx = SimpleNamespace(terminal_reason=None, final_message="")
    limiter = _ToolCallLimiter(_Inner(), max_tool_calls=1, ctx=ctx)
    call = ToolCall(id="c1", name="bash", arguments={})
    await limiter.execute([call])
    out = await limiter.execute([call])

    assert ctx.terminal_reason is not None, "超限必须设 MAX_TURNS 终态（生产位置调用）"
    assert out[0].success is False
    assert "limit" in out[0].error


# ---------- on_compressing 终态早退 ----------


@pytest.mark.asyncio
async def test_contract_on_compressing_early_return_on_terminal() -> None:
    """契约（终态早退·破坏者）：terminal_reason 已设时 on_compressing 不得再启动
    压缩（修复前无早退：预算耗尽/异常终止仍会再烧一次压缩 LLM 摘要调用）。"""
    from bandi.agent import states
    from bandi.agent.base import TerminalReason
    from bandi.agent.deps import QueryDeps

    called: list[str] = []

    async def _boom(ctx: Any) -> Any:
        called.append("compress")
        raise AssertionError("终态下不得调用压缩")

    ctx = _ctx()
    ctx.terminal_reason = TerminalReason.TOKEN_BUDGET_EXHAUSTED
    ctx.query_deps = QueryDeps(
        call_model=_boom,
        autocompact=_boom,
        microcompact=_boom,
        new_id=lambda: "x",
    )
    await states.on_compressing(ctx)
    assert called == [], f"终态下不得启动压缩，实际调用 {called}"


def test_contract_initial_mode_resolution_precedence() -> None:
    """契约（配置生效·型）：会话初始模式解析——显式请求 > agent.mode
    配置 > execute（config.agent.mode 此前为死配置：有定义零消费）。"""
    from bandi.core.agent_runner import resolve_initial_mode
    from bandi.core.config import BandiConfig

    cfg = BandiConfig()
    cfg.agent.mode = "plan"
    assert resolve_initial_mode(cfg, "execute") == "execute", "显式请求必须优先"
    assert resolve_initial_mode(cfg, "") == "plan", "缺省必须回落 agent.mode 配置"
    cfg.agent.mode = ""
    assert resolve_initial_mode(cfg, "") == "execute", "配置缺失回落 execute"
    assert resolve_initial_mode(None, "") == "execute"


# ---------- checkpoint 写失败 best-effort ----------


@pytest.mark.asyncio
async def test_contract_checkpoint_save_failure_is_best_effort(tmp_path) -> None:
    """契约（保护机制不反向成单点故障·破坏者）：checkpoint 写入失败必须
    best-effort（告警 + False），不得裸抛毁掉整轮——修复前异常穿透（保护性
    动作反成单点故障，与相邻 flush 屏障的容错口径不一致）。"""
    import sqlite3

    from bandi.agent.checkpoint import CheckpointStore
    from bandi.agent.loop import AgentLoop
    from bandi.memory.session.store import SessionStore

    ckpt = CheckpointStore(str(tmp_path / "ckpt.db"))
    sess = SessionStore(str(tmp_path / "sess.db"))
    sid = sess.create_session(mode="execute")
    loop = AgentLoop(checkpoint_store=ckpt, checkpoint_interval=1, session_store=sess)
    ctx = _ctx()
    ctx.meta["session_id"] = sid
    ctx.turn_index = 1

    def _boom(checkpoint: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    ckpt.save = _boom  # type: ignore[method-assign]
    assert loop._persist_checkpoint_now(ctx, turns_taken=1) is False, (
        "checkpoint 写失败必须返回 False（best-effort），不得裸抛"
    )
    ckpt.close()
    sess.close()


# ---------- CheckpointStore 线程一致性 ----------


def test_contract_checkpoint_store_cross_thread_save(tmp_path) -> None:
    """契约（线程一致性）：CheckpointStore 跨线程 save 不得 sqlite3.
    ProgrammingError。"""
    import threading

    from bandi.agent.checkpoint import Checkpoint, CheckpointStore

    store = CheckpointStore(str(tmp_path / "ckpt.db"))
    errors: list[Exception] = []

    def _worker() -> None:
        try:
            store.save(Checkpoint(id="c1", session_id="s1", turn_index=1, state_json="{}"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=10)
    assert not errors, f"跨线程 save 失败: {errors}"
    assert store.list_by_session("s1"), "跨线程写入必须可读回"
    store.close()


# ---------- progressive 终产物为附录预留预算 ----------


@pytest.mark.asyncio
async def test_contract_progressive_reserves_appendix_budget(monkeypatch) -> None:
    """契约（预算一致性·破坏者）：progressive 终产物切片必须为附录预留预算——
    修复前 `_cut_summary(final_summary)` 未传 reserve_chars（与 MemoryCompactor
    调用点口径不一致），摘要+附录可超预算。"""
    from unittest.mock import patch

    from bandi.core.message import LLMResponse, TokenUsage
    from bandi.memory.compression_pipeline import ProgressiveCompressor

    async def stub(messages: list[dict]) -> LLMResponse:
        return LLMResponse(content="## 摘要\n- 要点", usage=TokenUsage(1, 1), finish_reason="stop")

    msgs = [Message(role=MessageRole.SYSTEM, content="sys")]
    for i in range(10):
        msgs.append(Message(role=MessageRole.USER, content=f"第{i}步 " + "x" * 200))

    comp = ProgressiveCompressor(max_chunks=2, target_token_budget=50)
    inner = comp._inner
    original = inner._cut_summary
    recorded: list[int] = []

    def spy(text: str, reserve_chars: int = 0) -> str:
        recorded.append(reserve_chars)
        return original(text, reserve_chars=reserve_chars)

    appendix = "\n\n[附录]" + "x" * 300
    with (
        patch.object(inner, "_cut_summary", spy),
        patch(
            "bandi.memory.compression_pipeline.build_summary_appendix",
            return_value=appendix,
        ),
    ):
        await comp.compress(msgs, stub)

    assert recorded, "前置：终产物切片必须被调用（progressive 路径）"
    assert any(r >= len(appendix) for r in recorded), (
        f"附录预算必须预留（reserve_chars ≥ 附录长度 {len(appendix)}），实际 {recorded}"
    )
