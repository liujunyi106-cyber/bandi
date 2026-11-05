from __future__ import annotations

import asyncio
import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.api.app import app
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.core.config import AgentConfig, ToolsConfig
from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import LLMResponse, MessageRole, ToolCall, ToolResult
from bandi.llm.streaming import StreamEvent, StreamEventType
from bandi.observability.write_failures import (
    WriteFailureRegistry,
    get_registry,
    reset_registry,
)
from bandi.tools.approval import _MAX_SESSION_BUS_BINDINGS, ApprovalStore
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.cache import ToolResultCache
from bandi.tools.errors import NonRetryableToolError
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry

# 功能：契约——审批决策事件路由表的并发安全与淘汰策略（并发工程 批次1 / ）
#
# `_session_buses` 是「会话 → EventBus」路由表——ApprovalStore 是进程级单例而
# EventBus 每 run 新建，故必须按 session 记录路由，否则人工决策事件会串到别的
# ① `bind_event_bus` / `emit_decision_event` 读写该表**全程无锁**；
# ② 超上限（`_MAX_SESSION_BUS_BINDINGS`）时**整张 clear()**——连带丢掉所有仍
# 活跃会话的路由，使它们全部回落 `self._event_bus`（最近一次绑定的总线）→
# 决策事件**串号**；
# ③ 会话结束无主动注销，路由表只增不减。
# 本契约钉死行为：上限内精确路由 / 超限**逐条 LRU 淘汰**（不是全清）/ 淘汰后
# 不得串到其他会话总线 / 读写并发安全 / 会话结束主动注销。


class _RecordingBus:
    """记录收到的决策事件（替代真实 EventBus）。"""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_decision_routes_to_its_own_session_bus() -> None:
    """基线：决策事件只发到该会话绑定的总线，不串到别的会话。"""
    store = ApprovalStore()
    bus_a, bus_b = _RecordingBus("a"), _RecordingBus("b")
    store.bind_event_bus(bus_a, "s1")
    store.bind_event_bus(bus_b, "s2")

    await store.emit_decision_event("allow", "s1", "bash")

    assert len(bus_a.events) == 1
    assert not bus_b.events


def test_overflow_evicts_oldest_one_by_one() -> None:
    """超限时逐条淘汰最旧，而非整张清空。"""
    store = ApprovalStore()
    total = _MAX_SESSION_BUS_BINDINGS + 5
    for i in range(total):
        store.bind_event_bus(_RecordingBus(str(i)), f"s{i}")

    assert len(store._session_buses) == _MAX_SESSION_BUS_BINDINGS
    assert f"s{total - 1}" in store._session_buses, "最新绑定必须保留"
    assert "s0" not in store._session_buses, "最旧绑定应被逐条淘汰"
    assert f"s{total - _MAX_SESSION_BUS_BINDINGS}" in store._session_buses, (
        "除被淘汰的最旧项外，其余会话必须保留（旧实现 clear() 会全部丢失）"
    )


@pytest.mark.asyncio
async def test_non_oldest_session_keeps_exact_routing_after_overflow() -> None:
    """**破坏者视角**：旧实现在超限时整张 `clear()`，导致**所有**会话一并回落串号。

    新实现只淘汰最旧，其余会话必须仍精确路由到自己的总线。
    """
    store = ApprovalStore()
    buses: dict[int, _RecordingBus] = {}
    total = _MAX_SESSION_BUS_BINDINGS + 1  # 恰好触发一次溢出淘汰
    for i in range(total):
        bus = _RecordingBus(f"b{i}")
        buses[i] = bus
        store.bind_event_bus(bus, f"s{i}")

    target = 50  # 早期绑定：旧实现下已被 clear() 抹掉，会回落串号
    await store.emit_decision_event("deny", f"s{target}", "write")

    assert len(buses[target].events) == 1, "未被淘汰的会话必须精确命中自己的总线"
    leaked = [i for i, b in buses.items() if i != target and b.events]
    assert not leaked, f"决策事件串到了其它会话总线：{leaked}"


def test_concurrent_bind_is_thread_safe() -> None:
    """并发绑定不得抛异常、不得破坏表结构（原实现读写无锁）。"""
    store = ApprovalStore()
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            for _ in range(20):
                store.bind_event_bus(_RecordingBus(str(idx)), f"s{idx}")
                store._session_buses.get(f"s{idx}")
        except BaseException as exc:  # noqa: BLE001 — 收集后统一断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发绑定抛出异常：{errors[:3]}"
    assert len(store._session_buses) <= _MAX_SESSION_BUS_BINDINGS


def test_clear_session_unbinds_routing() -> None:
    """会话注销时必须同步移除其路由，避免残留指向已关闭总线。"""
    store = ApprovalStore()
    store.bind_event_bus(_RecordingBus("a"), "s1")
    store.bind_event_bus(_RecordingBus("b"), "s2")

    store.clear_session("s1")

    assert "s1" not in store._session_buses
    assert "s2" in store._session_buses, "其它会话路由不受影响"


# ==================== b1_concurrency_caps ====================
# 功能：契约——批内工具并发上限 + 子代理 fan-out 上限（并发工程加固 批次1 / ）
#
# `core/config.py:97` `max_concurrency` 默认 None（= 不限制）→ 一批 N 个并发安全工具
# 全部同时执行、无背压；而子代理侧有 `subagent_max_concurrency=4` 的上限——工具侧裸奔。
# deepseek-harness maxParallelSubCalls）。
#
# 本契约钉死的是**行为**（峰值并发数），不是实现细节：
# 1) 默认配置带上限（工具 8 / 子代理 10）
# 2) 批内并发峰值 ≤ max_concurrency
# 3) 显式 0 = 保持"不限"语义，但必须告警（不再静默裸奔）
# 4) max_concurrency=1 → 完全串行


class _ProbeTool(BaseTool):
    """并发探针：统计同时在跑的峰值数量。"""

    name = "concurrency_probe"
    description = "measure in-flight concurrency"
    is_concurrency_safe = True
    is_read_only = True
    dedup_scope = "none"
    parameters: dict = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.seen: list[str] = []
        self._guard = asyncio.Lock()

    async def execute(self, params: dict, context: object | None = None) -> str:
        async with self._guard:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.05)
            self.seen.append("done")
        finally:
            async with self._guard:
                self.active -= 1
        return "ok"


def _executor(tool: BaseTool, *, max_concurrency: int | None) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register_instance(tool)
    return ToolExecutor(registry=registry, max_concurrency=max_concurrency)


def _calls(n: int) -> list[ToolCall]:
    return [ToolCall(id=f"c{i}", name="concurrency_probe", arguments={}) for i in range(n)]


def test_default_config_carries_concurrency_caps() -> None:
    """默认配置必须带上限：工具 8、子代理 10（不再默认不限）。"""
    cfg = ToolsConfig()
    assert cfg.max_concurrency == 8
    assert cfg.subagent_max_concurrency == 10


@pytest.mark.asyncio
async def test_batch_peak_respects_cap() -> None:
    """20 个并发安全工具同批执行 → 同时在跑的数量不超过上限。"""
    tool = _ProbeTool()
    executor = _executor(tool, max_concurrency=4)

    await executor.execute(_calls(20))

    assert tool.peak <= 4, f"批内并发峰值 {tool.peak} 超上限 4"
    assert len(tool.seen) == 20, "全部调用都应真实执行（限流是排队不是丢弃）"


@pytest.mark.asyncio
async def test_cap_one_is_fully_serial() -> None:
    """上限为 1 时完全串行（峰值恒为 1）。"""
    tool = _ProbeTool()
    executor = _executor(tool, max_concurrency=1)

    await executor.execute(_calls(5))

    assert tool.peak == 1


@pytest.mark.asyncio
async def test_zero_cap_keeps_unbounded_but_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """破坏者视角：显式 0 仍可"不限"，但必须告警——不许静默裸奔。

    用桩替 logger.warning 而非 caplog：本仓 stdlib 日志桥接会接管 handler
    caplog 捕获不可靠（踩过，改用桩断言）。
    """
    from bandi.tools import executor as executor_mod

    warnings: list[str] = []
    monkeypatch.setattr(
        executor_mod.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg))
    )

    tool = _ProbeTool()
    executor = _executor(tool, max_concurrency=0)
    await executor.execute(_calls(6))

    assert tool.peak > 1, "0 应保持不限语义（全部并发）"
    assert warnings, "显式 0 必须产生告警"
    assert any("未设上限" in w or "不限" in w for w in warnings)


@pytest.mark.asyncio
async def test_none_cap_does_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """反向验证：None（沿用旧行为）不得告警——否则噪声淹没真告警。"""
    from bandi.tools import executor as executor_mod

    warnings: list[str] = []
    monkeypatch.setattr(
        executor_mod.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg))
    )

    tool = _ProbeTool()
    _executor(tool, max_concurrency=None)

    assert not warnings


@pytest.mark.asyncio
async def test_none_cap_keeps_legacy_behaviour() -> None:
    """回归：传 None（显式关闭上限）保持改造前的全并发行为。"""
    tool = _ProbeTool()
    executor = _executor(tool, max_concurrency=None)

    await executor.execute(_calls(6))

    assert tool.peak > 1


@pytest.mark.asyncio
async def test_results_keep_input_order_under_cap() -> None:
    """回归：限流不得破坏结果与入参 tool_calls 的顺序对应。"""
    tool = _ProbeTool()
    executor = _executor(tool, max_concurrency=3)
    calls = _calls(9)

    results = await executor.execute(calls)

    assert [r.tool_call_id for r in results] == [c.id for c in calls]


# ==================== b1_session_id ====================


def test_execution_context_exposes_explicit_session_id() -> None:
    from bandi.agent.base import ExecutionContext

    ctx = ExecutionContext(messages=[], provider=object(), config=object(), bus=object())
    assert ctx.session_id == ""
    ctx.session_id = "s-explicit"
    assert ctx.session_id == "s-explicit"


def test_steer_drain_uses_explicit_session_id_without_meta() -> None:
    from bandi.agent.base import ExecutionContext
    from bandi.agent.states import on_executing
    from bandi.agent.steer import SessionSteerRegistry
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import Message, MessageRole, ToolCall, ToolResult

    class Provider:
        model_name = "stub"
        protocol = "openai"

        def count_tokens(self, text: str) -> int:
            return 1

    class Executor:
        async def execute(self, calls: list[ToolCall]) -> list[ToolResult]:
            return [ToolResult(c.id, c.name, True, "ok") for c in calls]

    async def run() -> list[Message]:
        registry = SessionSteerRegistry()
        registry.push("s-field", "显式字段转向")
        call = ToolCall("t", "read", {})
        ctx = ExecutionContext(
            messages=[Message(MessageRole.ASSISTANT, tool_calls=[call])],
            provider=Provider(),
            config=AgentConfig(),
            bus=EventBus(),
            session_id="s-field",
            meta={"steer_registry": registry},
        )
        ctx.current_tool_calls = [call]
        ctx.tool_executor = Executor()
        await on_executing(ctx)
        return ctx.messages

    messages = asyncio.run(run())
    assert any(m.role == MessageRole.USER and m.content == "显式字段转向" for m in messages)


def test_factory_assembly_sets_explicit_session_id(monkeypatch: Any) -> None:
    from bandi.agent import factory as factory_mod
    from bandi.agent.factory import build_agent_context
    from bandi.core.config import BandiConfig
    from bandi.core.event_bus import EventBus

    class ProviderStub:
        model_name = "stub"
        protocol = "openai"
        version = "1"

        def count_tokens(self, text: str) -> int:
            return 1

    class RegistryStub:
        def list_tools(self) -> list[Any]:
            return []

        def get(self, name: str) -> Any | None:
            return None

        def list_schemas(self, allowed_tools: Any = None) -> list[Any]:
            return []

    monkeypatch.setattr(factory_mod, "build_provider", lambda cfg: ProviderStub())
    monkeypatch.setattr(factory_mod, "build_tool_registry", lambda *a, **k: RegistryStub())
    from bandi.tools.pipeline import ToolPipeline

    monkeypatch.setattr(factory_mod, "build_tool_pipeline", lambda **k: ToolPipeline())

    ctx = build_agent_context(
        user_input="b1 装配测试",
        session_id="s-assembled",
        bandi_config=BandiConfig(),
        bus=EventBus(),
    )
    assert ctx.session_id == "s-assembled"
    assert ctx.meta.get("session_id") == "s-assembled"


# ==================== b1_tool_cache ====================
# 功能：契约——工具结果缓存的并发安全、单飞与容量（并发工程 批次1 / ）
#
# `ToolResultCache` 内部 `_cache`(OrderedDict) 与 `_tool_keys` **无任何锁**
# 且同 key 并发未命中时**无 in-flight 协调**——N 个并发请求同时穿透缓存
# 各算一遍（缓存击穿 / stampede）。容量 128 偏小。
# 本契约钉死：读写在多线程下安全 / 同 key 并发只计算一次 / 不同 key 不互相
# 阻塞 / 计算抛异常不得留下死锁 / 容量按 LRU 逐出。


def _result(name: str, output: str = "ok") -> ToolResult:
    return ToolResult(tool_call_id="c1", tool_name=name, success=True, output=output)


@pytest.mark.asyncio
async def test_single_flight_computes_once() -> None:
    """同 key 并发 50 次 → 底层工厂只被调用 1 次（防缓存击穿）。"""
    cache = ToolResultCache()
    calls = 0

    async def factory() -> ToolResult:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return _result("read", "payload")

    results = await asyncio.gather(*(cache.get_or_compute("k1", factory) for _ in range(50)))

    assert calls == 1, f"工厂被调用 {calls} 次，单飞未生效"
    assert all(r.output == "payload" for r in results)


@pytest.mark.asyncio
async def test_different_keys_compute_in_parallel() -> None:
    """不同 key 不得被串行化（单飞只对同 key 生效）。"""
    cache = ToolResultCache()
    done: list[str] = []

    def make_factory(tag: str):
        async def _run() -> ToolResult:
            await asyncio.sleep(0.05)
            done.append(tag)
            return _result("read", tag)

        return _run

    await asyncio.gather(*(cache.get_or_compute(f"k{i}", make_factory(f"v{i}")) for i in range(5)))

    assert sorted(done) == ["v0", "v1", "v2", "v3", "v4"]


@pytest.mark.asyncio
async def test_factory_failure_releases_lock() -> None:
    """破坏者视角：工厂抛异常后锁必须释放——后续同 key 请求仍可计算（不死锁）。"""
    cache = ToolResultCache()
    attempts = 0

    async def failing() -> ToolResult:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await cache.get_or_compute("k1", failing)

    # 第二次仍能进入（若锁未释放会挂起直至超时）
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(cache.get_or_compute("k1", failing), timeout=2)

    assert attempts == 2


def test_capacity_evicts_oldest_lru() -> None:
    """容量按 LRU 逐出：写入超过 maxsize 时最旧被淘汰且总量受控。"""
    cache = ToolResultCache(maxsize=4)
    for i in range(6):
        cache.set(f"k{i}", _result("read", f"v{i}"))

    assert cache.size <= 4
    assert cache.get("k0") is None, "最旧条目应被逐出"
    assert cache.get("k5") is not None, "最新条目必须保留"


class _SlowReadTool(BaseTool):
    name = "slow_read"
    description = "slow read tool for single-flight wiring"
    category = ToolCategory.UTIL
    cache_ttl = 60.0
    is_read_only = True

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def execute(self, params: dict, context=None):  # type: ignore[override]
        self.calls += 1
        await asyncio.sleep(0.05)
        return "payload"


@pytest.mark.asyncio
async def test_executor_single_flight_is_wired() -> None:
    """DoD-11 接线：生产链 ToolExecutor 同 key 并发未命中只真实执行一次。

    只测 cache API 会漏「新符号零生产调用」——本用例走真实 executor。
    """
    tool = _SlowReadTool()
    registry = ToolRegistry()
    registry.register_instance(tool)
    executor = ToolExecutor(registry=registry, result_cache=ToolResultCache())

    async def one(call_id: str) -> ToolResult:
        batch = [ToolCall(id=call_id, name="slow_read", arguments={"x": 1})]
        return (await executor.execute(batch))[0]

    r1, r2 = await asyncio.gather(one("c1"), one("c2"))

    assert tool.calls == 1, f"生产链未接单飞：工具真实执行 {tool.calls} 次（应 1 次）"
    assert r1.output == "payload" and r2.output == "payload"
    assert {r1.tool_call_id, r2.tool_call_id} == {"c1", "c2"}, (
        "等待者必须重映射自己的 tool_call_id，不得复用赢家的调用标识"
    )


def test_concurrent_read_write_is_thread_safe() -> None:
    """并发读写下不得抛异常（原实现无锁，OrderedDict 混用 move_to_end/pop 有竞态）。"""
    cache = ToolResultCache(maxsize=64)
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            for i in range(60):
                key = f"k{i % 20}"
                cache.set(key, _result("read", f"{idx}-{i}"))
                cache.get(key)
                cache.has(key)
                cache.invalidate(key)
        except BaseException as exc:  # noqa: BLE001 — 收集后统一断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发读写抛出异常：{errors[:3]}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def _failure_result() -> ToolResult:
    return ToolResult(
        tool_call_id="winner",
        tool_name="read",
        success=False,
        output="",
        error="boom",
        should_abort=True,
        meta={"error_class": "non_retryable"},
    )


@pytest.mark.asyncio
async def test_single_flight_failure_shared_but_not_cached() -> None:
    """同 key 并发 + factory 失败：只执行一次，等待者共享该失败结果，缓存不落失败。"""
    cache = ToolResultCache()
    calls = 0
    failure = _failure_result()

    async def factory() -> ToolResult:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return failure

    results = await asyncio.gather(*(cache.get_or_compute("k1", factory) for _ in range(20)))

    assert calls == 1, f"失败路径单飞未生效：factory 执行 {calls} 次（应 1 次）"
    assert all(not r.success for r in results), "等待者必须拿到失败结果，不得重复执行成功"
    assert all(r.meta.get("error_class") == "non_retryable" for r in results), (
        "等待者丢失赢家的 error_class"
    )
    assert cache.get("k1") is None, "失败结果不得入缓存"

    await cache.get_or_compute("k1", factory)
    assert calls == 2, "失败不入缓存 → 后续调用必须重新执行（1 次）"


class _FailingReadTool(BaseTool):
    name = "failing_read"
    description = "failing read tool for single-flight failure semantics"
    category = ToolCategory.UTIL
    cache_ttl = 60.0
    is_read_only = True

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def execute(self, params: dict, context=None):  # type: ignore[override]
        self.calls += 1
        await asyncio.sleep(0.05)
        raise NonRetryableToolError("boom")


@pytest.mark.asyncio
async def test_executor_single_flight_failure_keeps_waiter_meta() -> None:
    """生产链：同 key 并发失败只执行一次，等待者结果保留 meta/should_abort。"""
    tool = _FailingReadTool()
    registry = ToolRegistry()
    registry.register_instance(tool)
    # 阈值=1 使赢家首败即达阈值（后续调用将被暂停）；-3：失败回喂不熔断——
    # should_abort 恒 False（验证等待者同样继承赢家的结果字段）
    executor = ToolExecutor(
        registry=registry,
        result_cache=ToolResultCache(),
        consecutive_failure_threshold=1,
    )

    async def one(call_id: str) -> ToolResult:
        batch = [ToolCall(id=call_id, name="failing_read", arguments={"x": 1})]
        return (await executor.execute(batch))[0]

    r1, r2 = await asyncio.gather(one("c1"), one("c2"))

    assert tool.calls == 1, f"失败路径未接单飞：工具真实执行 {tool.calls} 次（应 1 次）"
    assert not r1.success and not r2.success, "等待者不得被重算成成功"
    assert {r1.tool_call_id, r2.tool_call_id} == {"c1", "c2"}
    assert r1.meta.get("error_class") == "non_retryable"
    assert r2.meta.get("error_class") == "non_retryable", f"等待者丢失 meta：{r2.meta}"
    assert r1.should_abort is False, "137b-3：失败回喂 LLM（不再因失败计数熔断整轮）"
    assert r2.should_abort is False, "等待者继承赢家的 should_abort（False）"


class R3xb1RecordingBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_cache_hit_failure_emits_tool_error() -> None:
    """命中重映射：meta/should_abort 保留；失败命中必须发 TOOL_ERROR（与赢家 _fail_result
    同口径，带 error_message）——原先只发 TOOL_COMPLETED 导致审计/UI 看不到失败原因。"""
    bus = R3xb1RecordingBus()
    executor = ToolExecutor(registry=ToolRegistry(), event_bus=bus)
    cached = _failure_result()
    call = ToolCall(id="new-call", name="read", arguments={})

    result = await executor._as_cache_hit(call, cached)

    assert result.tool_call_id == "new-call"
    assert result.success is False
    assert result.should_abort is True
    assert result.meta == {"error_class": "non_retryable"}, f"meta 未保留：{result.meta}"
    errors = [e for e in bus.events if getattr(e, "type", None) is EventType.TOOL_ERROR]
    assert errors, "失败命中必须补发 TOOL_ERROR 事件"
    assert errors[0].data["error_message"] == "boom", f"失败原因丢失：{errors[0].data}"
    completed = [e for e in bus.events if getattr(e, "type", None) is EventType.TOOL_COMPLETED]
    assert not completed, "失败命中不得伪装成 TOOL_COMPLETED（UI/审计会误判成功）"


@pytest.mark.asyncio
async def test_cache_hit_success_still_emits_completed() -> None:
    """成功路径不回归：缓存命中仍发 TOOL_COMPLETED{from_cache:true, success:true}。"""
    bus = R3xb1RecordingBus()
    executor = ToolExecutor(registry=ToolRegistry(), event_bus=bus)
    cached = _result("read", "payload")
    call = ToolCall(id="new-call", name="read", arguments={})

    result = await executor._as_cache_hit(call, cached)

    assert result.success is True and result.tool_call_id == "new-call"
    completed = [e for e in bus.events if getattr(e, "type", None) is EventType.TOOL_COMPLETED]
    assert completed, "成功命中必须补发 TOOL_COMPLETED 事件"
    assert completed[0].data.get("from_cache") is True
    assert completed[0].data["success"] is True
    assert not [e for e in bus.events if getattr(e, "type", None) is EventType.TOOL_ERROR]


@pytest.mark.asyncio
async def test_single_flight_failure_waiter_event_matches_winner() -> None:
    """生产链：同 key 并发失败 → 等待者（c2）必须发 TOOL_ERROR 且 error_message 与
    赢家（c1）一致（审计链与 SSE 由此拿到失败原因）。"""
    tool = _FailingReadTool()
    registry = ToolRegistry()
    registry.register_instance(tool)
    bus = R3xb1RecordingBus()
    executor = ToolExecutor(registry=registry, result_cache=ToolResultCache(), event_bus=bus)

    async def one(call_id: str) -> ToolResult:
        batch = [ToolCall(id=call_id, name="failing_read", arguments={"x": 1})]
        return (await executor.execute(batch))[0]

    r1, r2 = await asyncio.gather(one("c1"), one("c2"))

    assert tool.calls == 1, f"工具真实执行 {tool.calls} 次（应 1 次）"
    assert not r1.success and not r2.success
    errors = [e for e in bus.events if getattr(e, "type", None) is EventType.TOOL_ERROR]
    by_call = {e.data.get("tool_call_id"): e.data.get("error_message") for e in errors}
    assert set(by_call) >= {"c1", "c2"}, f"赢家与等待者的失败都须入错误事件流：{by_call}"
    assert by_call["c1"] == by_call["c2"], f"等待者原因与赢家不一致：{by_call}"
    assert "boom" in (by_call["c1"] or ""), f"失败原因须带原始报错：{by_call}"
    assert not [e for e in bus.events if getattr(e, "type", None) is EventType.TOOL_COMPLETED], (
        "失败路径不得出现 TOOL_COMPLETED"
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class _MetaFailReadTool(BaseTool):
    name = "meta_fail_read"
    description = "read-only failing tool for dedup meta preservation"
    category = ToolCategory.UTIL
    is_read_only = True

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def execute(self, params: dict, context=None):  # type: ignore[override]
        self.calls += 1
        raise NonRetryableToolError("meta boom")


@pytest.mark.asyncio
async def test_dedup_reuse_preserves_meta() -> None:
    """契约：批内 dedup 第二次复用的结果必须保留赢家 meta（error_class）。"""
    tool = _MetaFailReadTool()
    registry = ToolRegistry()
    registry.register_instance(tool)
    executor = ToolExecutor(registry=registry)

    results = await executor.execute(
        [
            ToolCall(id="d1", name="meta_fail_read", arguments={"x": 1}),
            ToolCall(id="d2", name="meta_fail_read", arguments={"x": 1}),
        ]
    )

    assert len(results) == 2
    assert tool.calls == 1, f"第二次必须去重复用（实际执行 {tool.calls} 次）"
    assert results[0].meta.get("error_class") == "non_retryable"
    assert results[1].meta.get("error_class") == "non_retryable", (
        f"dedup 复用结果丢失 meta：{results[1].meta}"
    )


def test_executor_normalizes_none_agent_id() -> None:
    """契约：agent_id=None 构造不得炸（原实现缓存 scope 拼接 TypeError）。"""
    executor = ToolExecutor(registry=ToolRegistry(), agent_id=None)  # type: ignore[arg-type]
    assert executor.agent_id == "default"
    assert isinstance(executor.cache_scope, str)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def test_set_agent_id_purges_old_scope_cache() -> None:
    """🟢-7：切换 agent_id 后旧 scope 键不得命中，新 scope 正常工作。"""
    cache = ToolResultCache()
    executor = ToolExecutor(registry=ToolRegistry(), result_cache=cache, agent_id="agent-a")
    old_scope = executor.cache_scope
    old_key = cache.build_key("read", {"x": 1}, scope=old_scope)
    cache.set(old_key, _result("read", "old"), scope=old_scope)
    assert cache.get(old_key) is not None

    unscoped_key = cache.build_key("read", {"x": 0})
    cache.set(unscoped_key, _result("read", "direct"))

    executor.set_agent_id("agent-b")

    assert cache.get(old_key) is None, "切换身份后旧 scope 缓存必须清理（防越权复用）"
    assert cache.get(unscoped_key) is not None, "未带 scope 的直接调用方键不得被误清"

    new_scope = executor.cache_scope
    assert new_scope != old_scope
    new_key = cache.build_key("read", {"x": 1}, scope=new_scope)
    cache.set(new_key, _result("read", "new"), scope=new_scope)
    assert cache.get(new_key) is not None, "新 scope 缓存必须正常工作"


@pytest.mark.asyncio
async def test_single_flight_factory_exception_broadcast_once() -> None:
    """🟢-9：同 key 并发 factory 抛异常 → 只执行 1 次、等待者收到同异常
    异常不入缓存、槽清空后可重试。"""
    cache = ToolResultCache()
    calls = 0
    gate = asyncio.Event()

    async def failing() -> ToolResult:
        nonlocal calls
        calls += 1
        await gate.wait()
        raise RuntimeError("boom")

    async def one() -> object:
        try:
            await cache.get_or_compute("k-exc", failing)
            return None
        except RuntimeError as exc:
            return exc

    tasks = [asyncio.create_task(one()) for _ in range(20)]
    await asyncio.sleep(0.05)
    gate.set()
    outcomes = await asyncio.gather(*tasks)

    assert calls == 1, f"异常路径必须单飞：factory 执行 {calls} 次（应 1 次）"
    assert all(isinstance(o, RuntimeError) for o in outcomes), "全部等待者必须收到异常"
    assert {str(o) for o in outcomes} == {"boom"}, "等待者必须收到与赢家同源的异常"
    assert cache.get("k-exc") is None, "异常不得进缓存"

    async def ok() -> ToolResult:
        nonlocal calls
        calls += 1
        return _result("read", "ok")

    assert (await cache.get_or_compute("k-exc", ok)).output == "ok"
    assert calls == 2, "异常批结束后新调用必须能重新执行（共享槽已清空）"


# ==================== b2_lease_guard ====================
# 功能：契约——WorkItemStore.save 的租约保护（并发工程 批次2 / ）
#
# `company/store.py:223-256` 的 `save()` 是「读 revision → 全字段 INSERT OR REPLACE」
# **会把 metadata 整体覆盖**。而执行租约正存放在 `metadata["execution_claim"]`
# （`claim_for_execution:308` / `renew_execution_lease:389` 写入）。于是
# **任何拿着旧快照的调用方 save() 一次，他方刚建立的租约就被抹掉** → 同一
# 本仓 `company_mode.py:1116` 的注释已记载该风险（"旧快照 store.save 会覆盖他进程
#
# 设计取舍：**保留而非拒绝**。抛错会让 9 个既有 save() 调用点（company_mode ×4 /
# heartbeat ×2 / reorg_manager ×2 / custom_runtime）面临大面积误伤；改为
# 「库内存在未过期租约且调用方未携带 claim 时，保留库内 claim 字段」——零误伤
# 且业务字段照常更新。


def _store(tmp_path) -> WorkItemStore:
    return WorkItemStore(str(tmp_path / "wi.db"))


def _item(title: str = "task", phase: Phase = Phase.READY) -> object:
    # 默认 READY：claim_for_execution 仅接受 READY / READY_FOR_REWORK（store.py:320）
    item = create_work_item(
        title=title,
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        phase=phase,
    )
    item.id = "wi-1"
    return item


def _claim(
    item_id: str, store: WorkItemStore, instance: str = "inst-A", seconds: int = 900
) -> None:
    assert store.claim_for_execution(item_id, instance, seconds) is True


def _claim_of(store: WorkItemStore, item_id: str) -> dict:
    fresh = store.get(item_id)
    return (fresh.metadata or {}).get("execution_claim") or {}


def test_stale_save_does_not_wipe_live_lease(tmp_path) -> None:
    """**破坏者视角**：他方持有效租约时，旧快照 save() 不得抹掉该租约。

    （原实现下 metadata 被整体覆盖 → 租约消失 → 该 work item 可被第二个执行者领走。）
    """
    store = _store(tmp_path)
    store.save(_item())
    _claim("wi-1", store)

    stale = _item(title="updated by stale writer")  # 旧快照：metadata 无 execution_claim
    store.save(stale)

    claim = _claim_of(store, "wi-1")
    assert claim.get("lease_expires_at"), "他方有效租约被旧快照抹掉了（双执行回归）"
    assert store.get("wi-1").title == "updated by stale writer", "业务字段仍应正常更新"


def test_incoming_claim_wins_when_provided(tmp_path) -> None:
    """调用方显式携带 claim 时以其为准（持有者可自行更新/释放租约）。"""
    store = _store(tmp_path)
    store.save(_item())
    _claim("wi-1", store, instance="inst-A")

    holder = store.get("wi-1")
    holder.metadata["execution_claim"] = {
        "instance_id": "inst-A",
        "lease_expires_at": "2099-01-01T00:00:00+00:00",
    }
    store.save(holder)

    assert _claim_of(store, "wi-1")["lease_expires_at"].startswith("2099")


def test_expired_lease_is_not_preserved(tmp_path) -> None:
    """租约已过期时不得保留——否则接管方永远无法推进（回归：过期语义不变）。"""
    store = _store(tmp_path)
    store.save(_item())
    _claim("wi-1", store, seconds=-1)  # 立即过期

    store.save(_item(title="takeover"))

    assert not _claim_of(store, "wi-1"), "过期租约不应被保留"
    assert store.get("wi-1").title == "takeover"


def test_save_without_lease_unchanged(tmp_path) -> None:
    """回归：无租约场景下 save() 行为与改造前完全一致（零破坏）。"""
    store = _store(tmp_path)
    store.save(_item(title="v1"))
    assert store.get("wi-1").title == "v1"

    store.save(_item(title="v2"))
    assert store.get("wi-1").title == "v2"
    assert not _claim_of(store, "wi-1")


def test_revision_still_increments_monotonically(tmp_path) -> None:
    """回归：租约保护不得破坏 revision 递增语义。"""
    store = _store(tmp_path)
    store.save(_item())
    first = store.get("wi-1").revision
    store.save(_item(title="v2"))
    assert store.get("wi-1").revision > first


@pytest.mark.parametrize("phase", [Phase.READY, Phase.READY_FOR_REWORK])
def test_claim_survives_regardless_of_phase(tmp_path, phase) -> None:
    """参数化：两种可认领 phase 下租约保护都生效（逐成员覆盖，防单路径假绿）。"""
    store = _store(tmp_path)
    store.save(_item(phase=phase))
    _claim("wi-1", store)

    store.save(_item(title="stale"))

    assert _claim_of(store, "wi-1"), f"phase={phase.value} 下租约被抹掉"


class TestLeaseValidSingleSource:
    """契约：`_lease_valid` 单源判定**只看过期时刻**。

    原签名第二个形参 `instance_id` 在函数体内零消费（docstring 声称"本实例续约/
    重认领不受阻"，但实现根本没有读 claim 的 owner 字段）——owner 判定实际由
    调用方承担（`renew_execution_lease` 的 instance_id 比较）。死参会在调用点
    制造"传了就会生效"的错觉，须删除并以签名契约钉住。
    """

    def test_lease_valid_takes_expires_at_only(self) -> None:
        """接口契约：签名收敛为单参（调用点不再传废参）。"""
        import inspect

        params = list(inspect.signature(WorkItemStore._lease_valid).parameters)
        assert params == ["expires_at"], (
            f"_lease_valid 必须只按过期时刻判定（死参 instance_id 应删除），实得 {params}"
        )

    def test_expiry_semantics_unchanged(self) -> None:
        """行为回归：单参调用的过期语义与既有判定完全一致。"""
        from datetime import UTC, datetime, timedelta

        future = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
        past = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        assert WorkItemStore._lease_valid(future) is True
        assert WorkItemStore._lease_valid(past) is False
        assert WorkItemStore._lease_valid(None) is False
        assert WorkItemStore._lease_valid("not-a-timestamp") is False

    def test_claim_gate_behavior_unchanged(self, tmp_path) -> None:
        """行为回归：有效租约拒绝他方认领；过期租约允许接管（owner 由调用方判定）。"""
        from datetime import UTC, datetime, timedelta

        store = _store(tmp_path)
        live = _item()
        live.metadata["execution_claim"] = {
            "instance_id": "inst-A",
            "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
        }
        store.save(live)
        assert store.claim_for_execution("wi-1", "inst-B", 900) is False, "他方有效租约不得被认领"

        expired = _item()
        expired.metadata["execution_claim"] = {
            "instance_id": "inst-A",
            "lease_expires_at": (datetime.now(UTC) - timedelta(seconds=60)).isoformat(),
        }
        store.save(expired)
        assert store.claim_for_execution("wi-1", "inst-B", 900) is True, "过期租约必须允许接管"

    def test_owner_judgement_still_lives_in_caller(self, tmp_path) -> None:
        """行为回归：续约的 owner 判定仍由调用方比较（删除死参不得改变语义）。"""
        store = _store(tmp_path)
        store.save(_item())
        _claim("wi-1", store, instance="inst-A")

        assert store.renew_execution_lease("wi-1", "inst-B", 900) is False, "非持有者不得续约"
        assert store.renew_execution_lease("wi-1", "inst-A", 900) is True, "持有者必须可续约"


# ==================== b2_project_files ====================
# 功能：契约——项目数据面（项目文件树 / 项目文件内容 / 团队产物聚合）
# 钉行为不钉实现：树形懒加载列一级 + 目录在前 + 中文名 / 忽略名单不列出 /
# 越界与敏感路径 fail-closed（破坏者）/ 项目缺失 404 / 非法 project_id 400 不裸崩 /
# root_dir 未设置空列表不 500 / 文本读回中文 / 二进制与超大只回元数据 /
# 团队产物聚合（跨会话 + 排除秘书会话 + 项目隔离 + created_at 升序）。


r5xb2_client = TestClient(app)
r5xb2_client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def r5xb2_isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """会话/项目库隔离：Path.home→tmp + HOME/USERPROFILE 同步 + 三单例重置。"""
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._company_dispatcher", None),
        patch("bandi.api.app._custom_runtime_runner", None),
        patch("bandi.api.app._team_store", None),
        patch("bandi.api.app._project_store", None),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


def r5xb2_create_project(
    root: Path | None = None, project_id: str = "p-files", name: str = "文件树项目"
) -> dict:
    body: dict[str, str] = {"project_id": project_id, "name": name}
    if root is not None:
        body["root_dir"] = str(root)
    resp = r5xb2_client.post("/api/company/projects", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["project"]


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    return root


class TestProjectFileTree:
    def test_lists_root_then_nested_with_cjk(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path)
        (root / "报告.md").write_text("内容", encoding="utf-8")
        (root / "素材").mkdir()
        (root / "素材" / "logo.png").write_bytes(b"\x89PNG")
        r5xb2_create_project(root)

        resp = r5xb2_client.get("/api/company/projects/p-files/files/tree")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["root"] == str(root)
        assert [entry["name"] for entry in payload["entries"]] == ["素材", "报告.md"], (
            "目录在前、文件在后；中文名原样列出"
        )
        assert payload["entries"][0]["type"] == "dir"
        assert payload["entries"][1]["size"] == len("内容".encode())

        resp = r5xb2_client.get("/api/company/projects/p-files/files/tree", params={"path": "素材"})
        assert resp.status_code == 200
        nested = resp.json()
        assert nested["path"] == "素材"
        assert [entry["name"] for entry in nested["entries"]] == ["logo.png"]

    def test_ignores_build_dirs(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path)
        (root / "node_modules").mkdir()
        (root / ".git").mkdir()
        (root / "src").mkdir()
        r5xb2_create_project(root)
        resp = r5xb2_client.get("/api/company/projects/p-files/files/tree")
        assert [entry["name"] for entry in resp.json()["entries"]] == ["src"]

    def test_breaker_outside_and_sensitive_403(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path)
        (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
        (root / ".env").write_text("KEY=1", encoding="utf-8")
        r5xb2_create_project(root)

        resp = r5xb2_client.get("/api/company/projects/p-files/files/tree", params={"path": "../"})
        assert resp.status_code == 403, "越界目录必须 fail-closed"

        resp = r5xb2_client.get("/api/company/projects/p-files/files/tree", params={"path": ".env"})
        assert resp.status_code == 403, "敏感文件路径必须 fail-closed"

        resp = r5xb2_client.get(
            "/api/company/projects/p-files/files/tree", params={"path": "C:\\Windows"}
        )
        assert resp.status_code == 403, "绝对路径越界同样 fail-closed"

    def test_missing_project_404_and_no_root_empty(self, tmp_path: Path) -> None:
        resp = r5xb2_client.get("/api/company/projects/ghost/files/tree")
        assert resp.status_code == 404

        r5xb2_create_project(root=None, project_id="p-bare", name="无目录")
        resp = r5xb2_client.get("/api/company/projects/p-bare/files/tree")
        assert resp.status_code == 200, "未设置工作目录必须空列表不 500"
        assert resp.json()["entries"] == []

    def test_breaker_invalid_project_id_400(self) -> None:
        resp = r5xb2_client.get("/api/company/projects/a b/files/tree")
        assert resp.status_code == 400, "非法 project_id 必须 400 不裸崩"


class TestProjectFileContent:
    def test_text_binary_and_too_large(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path)
        (root / "说明.txt").write_text("中文内容", encoding="utf-8")
        (root / "blob.bin").write_bytes(bytes([0, 1, 2, 0, 255]))
        (root / "big.txt").write_text("x" * 3000, encoding="utf-8")
        r5xb2_create_project(root)

        resp = r5xb2_client.get(
            "/api/company/projects/p-files/files/content", params={"path": "说明.txt"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "text"
        assert body["content"] == "中文内容"
        assert body["size"] == len("中文内容".encode())

        resp = r5xb2_client.get(
            "/api/company/projects/p-files/files/content", params={"path": "blob.bin"}
        )
        assert resp.json()["kind"] == "binary"
        assert resp.json()["content"] == ""

        resp = r5xb2_client.get(
            "/api/company/projects/p-files/files/content",
            params={"path": "big.txt", "max_bytes": 1024},
        )
        assert resp.json()["kind"] == "too_large", "超大文件只回元数据"

    def test_breaker_outside_and_sensitive_403(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path)
        (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
        (root / ".ssh").mkdir()
        (root / ".ssh" / "id_rsa").write_text("secret", encoding="utf-8")
        r5xb2_create_project(root)

        resp = r5xb2_client.get(
            "/api/company/projects/p-files/files/content", params={"path": "../outside.txt"}
        )
        assert resp.status_code == 403
        resp = r5xb2_client.get(
            "/api/company/projects/p-files/files/content", params={"path": ".ssh/id_rsa"}
        )
        assert resp.status_code == 403, "敏感子路径必须 fail-closed"

    def test_missing_file_404(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path)
        r5xb2_create_project(root)
        resp = r5xb2_client.get(
            "/api/company/projects/p-files/files/content", params={"path": "nope.txt"}
        )
        assert resp.status_code == 404


class TestProjectArtifacts:
    def _seed(self) -> None:
        # 注意：`from bandi.api import app` 会被包 __init__ 的 re-export 遮蔽（拿到 FastAPI 对象）
        # 这里必须从子模块导入函数本身（其 globals 即 app 模块字典，与端点共享 store 单例）。
        from bandi.api.app import _get_store

        store = _get_store()
        store.create_session("s-team", project_id="p-art")
        store.create_session("s-secretary", project_id="p-art", origin="secretary")
        store.create_session("s-other", project_id="p-other")
        store.register_artifact("s-team", tool_name="write", path="输出/报告.md")
        store.register_artifact("s-team", tool_name="write", path="输出/图表.svg")
        store.register_artifact("s-secretary", tool_name="write", path="秘书记录.md")
        store.register_artifact("s-other", tool_name="write", path="别的.md")

    def test_aggregates_excludes_secretary_and_isolates_projects(self) -> None:
        r5xb2_create_project(project_id="p-art", name="产物项目")
        r5xb2_create_project(project_id="p-other", name="别的项目")
        self._seed()

        resp = r5xb2_client.get("/api/company/projects/p-art/artifacts")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["project_id"] == "p-art"
        assert body["count"] == 2, "仅团队会话产物（排除秘书会话）"
        paths = [item["path"] for item in body["artifacts"]]
        # 契约只应钉「属于本项目、排除秘书」的成员关系，顺序不属承诺。
        assert set(paths) == {"输出/报告.md", "输出/图表.svg"}, f"实际 {paths}"
        assert all(item["session_id"] == "s-team" for item in body["artifacts"])

        resp = r5xb2_client.get("/api/company/projects/p-other/artifacts")
        assert [item["path"] for item in resp.json()["artifacts"]] == ["别的.md"]

    def test_empty_project_and_missing_404(self, tmp_path: Path) -> None:
        r5xb2_create_project(project_id="p-empty", name="空项目")
        resp = r5xb2_client.get("/api/company/projects/p-empty/artifacts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == "p-empty"
        assert body["artifacts"] == []
        assert body["count"] == 0

        resp = r5xb2_client.get("/api/company/projects/ghost/artifacts")
        assert resp.status_code == 404

        resp = r5xb2_client.get("/api/company/projects/a b/artifacts")
        assert resp.status_code == 400


# ==================== b2_tool_results ====================


def test_extra_tool_results_are_dropped_with_log_notice(caplog: logging.LogCaptureFixture) -> None:
    ctx = ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    call = ToolCall("t1", "read", {})
    ctx.current_tool_calls = [call]
    ctx.tool_results = [
        ToolResult("t1", "read", True, "first"),
        ToolResult("t2", "read", True, "extra"),
    ]
    with caplog.at_level(logging.WARNING):
        from bandi.agent.states import _append_tool_results_to_messages

        _append_tool_results_to_messages(ctx)

    tool_msgs = [m for m in ctx.messages if m.role == MessageRole.TOOL]
    assert len(tool_msgs) == 1, f"多余结果必须丢弃，只保留配对结果（实际 {len(tool_msgs)}）"
    assert tool_msgs[0].tool_call_id == "t1"
    assert any("dropped" in r.message for r in caplog.records), (
        f"多余结果丢弃必须记日志（实际 {[r.message for r in caplog.records]}）"
    )


def test_no_extra_results_no_drop_log(caplog: logging.LogCaptureFixture) -> None:
    ctx = ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    call = ToolCall("t1", "read", {})
    ctx.current_tool_calls = [call]
    ctx.tool_results = [ToolResult("t1", "read", True, "only")]
    with caplog.at_level(logging.WARNING):
        from bandi.agent.states import _append_tool_results_to_messages

        _append_tool_results_to_messages(ctx)

    assert len(ctx.messages) == 1
    assert not any("dropped" in r.message for r in caplog.records)


def test_extra_call_without_result_is_also_logged(caplog: logging.LogCaptureFixture) -> None:
    ctx = ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    call = ToolCall("t1", "read", {})
    ctx.current_tool_calls = [call]
    ctx.tool_results = []
    with caplog.at_level(logging.WARNING):
        from bandi.agent.states import _append_tool_results_to_messages

        _append_tool_results_to_messages(ctx)

    assert any("mismatch" in r.message for r in caplog.records), (
        "调用数多于结果数时必须有 mismatch 告警"
    )


# ==================== b2_write_reliability ====================


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    reset_registry()


class _Sink:
    """可切换「是否抛错」的写入桩，用于验证失败→重试→落库全链。"""

    def __init__(self, fail_times: int = 0) -> None:
        self.written: list[str] = []
        self._remaining_failures = fail_times

    def __call__(self, payload: str) -> None:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise OSError("disk full")
        self.written.append(payload)


def test_failure_is_recorded_and_callable_queued() -> None:
    """契约 1：失败被计数 + 快照可见通道维度；可重试项入队。"""
    reg = WriteFailureRegistry()
    reg.record("episodic", OSError("disk full"), retry=lambda: None)

    assert reg.total == 1, "失败必须被计数（不静默）"
    assert reg.pending_count == 1, "带 retry 的失败应入待重试队列"
    snap = reg.snapshot()
    assert snap["total"] == 1
    assert "episodic" in snap["channels"], "快照须含通道维度明细"
    assert "disk full" in snap["recent"][0]["error"], "快照须含异常信息"


def test_retry_succeeds_then_data_lands() -> None:
    """契约 2：下一轮重试成功 → 数据最终落库（at-least-once）。"""
    reg = WriteFailureRegistry()
    sink = _Sink(fail_times=1)

    def _write() -> None:
        sink("turn-1")

    try:
        _write()
    except OSError as e:
        reg.record("long_term", e, retry=_write)

    assert sink.written == [], "首轮失败未落库"
    report = reg.retry_pending()

    assert sink.written == ["turn-1"], "重试必须真正落库"
    assert report["succeeded"] == 1
    assert reg.pending_count == 0, "成功项须出队"
    assert reg.dropped == 0


def test_persistent_failure_is_bounded_and_counted() -> None:
    """契约 3（破坏者视角）：持续失败不得无限重试——有上界且丢弃数可见。"""
    reg = WriteFailureRegistry(max_attempts=2)
    sink = _Sink(fail_times=99)

    def _write() -> None:
        sink("never")

    reg.record("hierarchy", OSError("locked"), retry=_write)

    for _ in range(5):  # 远超 max_attempts 的调用次数
        reg.retry_pending()

    assert reg.pending_count == 0, "重试次数用尽须丢弃，不得无限堆积"
    assert reg.dropped == 1, "丢弃必须计数（不静默）"
    assert reg.total >= 1


def test_pending_queue_has_hard_limit_and_drops_oldest() -> None:
    """契约（残余）：待重试队列硬上限——同轮/持续故障下不得无限占内存。

    超限驱逐最老项并计入 dropped（不静默），保留最新（新失败更值得重试）。
    """
    reg = WriteFailureRegistry(pending_limit=3)
    for i in range(5):
        reg.record("ch", OSError(f"boom {i}"), retry=lambda: None)

    assert reg.pending_count == 3, "超上限必须驱逐（队列保持有界）"
    assert reg.dropped == 2, "被驱逐项必须计入丢弃（不静默）"
    snap = reg.snapshot()
    assert snap["pending"] == 3 and snap["dropped"] == 2


def test_pending_queue_keeps_newest_entries() -> None:
    """驱逐方向契约：保留最新失败（最老先丢），重试只打最新窗口。"""
    reg = WriteFailureRegistry(pending_limit=2)
    seen: list[str] = []
    for i in range(3):
        reg.record("ch", OSError(f"boom {i}"), retry=lambda i=i: seen.append(f"r{i}"))

    reg.retry_pending()

    assert seen == ["r1", "r2"], f"必须丢弃最老项、保留最新项：{seen}"
    assert reg.dropped == 1


def test_default_pending_limit_is_bounded() -> None:
    """破坏者视角：默认构造也不得无界——大批失败后 pending 有上界且丢弃可见。"""
    reg = WriteFailureRegistry()
    for _ in range(300):
        reg.record("ch", OSError("persistent"), retry=lambda: None)

    assert reg.pending_count <= 256, f"默认上限失效：{reg.pending_count}"
    assert reg.dropped >= 44, "超限丢弃必须计数"


def test_failure_without_retry_is_still_counted() -> None:
    """边界：无重试回调的失败也要计数（不因不可重试而隐形）。"""
    reg = WriteFailureRegistry()
    reg.record("working", ValueError("boom"))

    assert reg.total == 1
    assert reg.pending_count == 0, "无 retry 回调不应入队"


def test_structured_warning_emitted(caplog: pytest.LogCaptureFixture) -> None:
    """可观测：失败必须留下结构化 warning（通道 + 异常），不静默。"""
    reg = WriteFailureRegistry()
    with caplog.at_level(logging.WARNING, logger="bandi.observability.write_failures"):
        reg.record("episodic", OSError("nope"), retry=lambda: None)

    messages = [r.getMessage() for r in caplog.records]
    assert any("episodic" in m for m in messages), f"warning 须含通道名: {messages}"


def test_registry_singleton_is_resettable() -> None:
    """测试隔离契约：全局单例可重置（防跨用例污染）。"""
    get_registry().record("episodic", OSError("x"))
    assert get_registry().total == 1
    reset_registry()
    assert get_registry().total == 0


# ---------------------------------------------------------------------------
# 集成：best-effort 通道失败不阻断主链 / 关键通道失败必须抛出
# ---------------------------------------------------------------------------


def test_run_write_swallows_and_records() -> None:
    """契约 1（集成）：`_run_write` 吞错但登记——主链不因记忆写失败而中断。"""
    from bandi.core.agent_runner import _run_write

    reg = WriteFailureRegistry()

    def _boom() -> None:
        raise OSError("disk full")

    assert _run_write(reg, "episodic", _boom) is False, "失败须返回 False"
    assert reg.total == 1
    assert reg.pending_count == 1, "失败项须入待重试队列"


def test_run_write_returns_true_on_success() -> None:
    """边界：成功写入不产生任何失败计数。"""
    from bandi.core.agent_runner import _run_write

    reg = WriteFailureRegistry()
    assert _run_write(reg, "episodic", lambda: None) is True
    assert reg.total == 0
    assert reg.pending_count == 0


def test_persist_or_raise_is_fail_closed() -> None:
    """契约 4：fail-closed 边界——关键落库（工具消息/checkpoint 前）失败必须抛出。"""
    from bandi.core.agent_runner import _persist_or_raise

    reg = WriteFailureRegistry()

    def _boom() -> None:
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        _persist_or_raise(reg, "tool_messages", _boom)

    assert reg.total == 1, "抛出前也必须登记计数"


@pytest.mark.asyncio
async def test_persist_memory_survives_all_channels_failing() -> None:
    """破坏者视角：五通道**全部**失败时，`_persist_memory` 仍不得抛出。

    记忆是 best-effort 非关键路径；任一通道故障都不得把整个 Agent 轮次打挂。
    """
    from bandi.agent.base import ExecutionContext
    from bandi.core.agent_runner import _persist_memory

    class _BoomStore:
        def __getattr__(self, name: str):
            def _boom(*a, **k):
                raise sqlite3.OperationalError("readonly database")

            return _boom

        def check_conflict(self, item_id: str, content: str) -> bool:
            return False

    class _FakeBus:
        async def emit(self, *a, **k) -> None:
            raise RuntimeError("bus down")

    class _FakeManager:
        episodic = _BoomStore()
        working = _BoomStore()
        long_term = _BoomStore()

    ctx = ExecutionContext(messages=[], provider=object(), config=object(), bus=_FakeBus())
    ctx.raw_input = "记住：请帮我总结这份材料，重点是第 3 章"
    ctx.current_content = "结论：这是一段足够长的最终回答内容，用于触发长期记忆写入通道。" * 3
    ctx.turn_index = 1

    await _persist_memory(ctx, _FakeManager(), "sess-1")

    reg = get_registry()
    assert reg.total >= 3, f"至少 episodic/working/long_term 三通道失败须被登记: {reg.total}"
    assert reg.snapshot()["channels"], "快照须能读出失败通道"


@pytest.mark.asyncio
async def test_persist_memory_retry_lands_data_on_second_turn() -> None:
    """契约 2（集成）：首轮失败 → 次轮进入 `_persist_memory` 时补写成功。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.agent_runner import _persist_memory

    records: list[str] = []
    attempts = {"n": 0}

    class _FlakyEpisodic:
        def record(self, episode: object) -> None:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            records.append(getattr(episode, "episode_id", ""))

    class _FakeManager:
        episodic = _FlakyEpisodic()
        working = None
        long_term = None

    class _FakeBus:
        async def emit(self, *a, **k) -> None:
            return None

    def _ctx() -> ExecutionContext:
        c = ExecutionContext(messages=[], provider=object(), config=object(), bus=_FakeBus())
        c.raw_input = "帮我看下这个报错"
        c.current_content = "结论：这是回答内容"
        c.turn_index = 1
        return c

    await _persist_memory(_ctx(), _FakeManager(), "sess-2")
    assert records == [], "首轮失败未落库"
    assert get_registry().pending_count == 1

    await _persist_memory(_ctx(), _FakeManager(), "sess-2")
    assert "ep:sess-2:1" in records, "次轮必须补写成功（at-least-once）"
    assert get_registry().pending_count == 0, "补写成功后须出队"
    assert get_registry().dropped == 0, "补写成功不得计入丢弃"


# ---------------------------------------------------------------------------
# 生产复刻：manager 每轮新建/轮末关闭，重试不得打在已关闭的旧实例上
# ---------------------------------------------------------------------------


def test_retry_pending_passes_live_manager_to_callback() -> None:
    """跨轮根因（注册表级）：重试回调必须收到调用方传入的当前 live manager。"""
    reg = WriteFailureRegistry()
    seen: list[object] = []

    def _replay(manager: object) -> None:
        seen.append(manager)

    reg.record("episodic", sqlite3.ProgrammingError("closed"), retry=_replay)
    live = object()

    report = reg.retry_pending(live)

    assert report["succeeded"] == 1
    assert seen == [live], "重试必须用传入的 live 实例重放（不是闭包捕获的旧实例）"


def test_snapshot_attempts_updated_after_failed_retry() -> None:
    """快照 attempts 不写死 0：重试失败必须反映真实尝试次数。"""
    reg = WriteFailureRegistry(max_attempts=1)

    def _always_fail(context: object = None) -> None:
        raise sqlite3.OperationalError("still down")

    reg.record("episodic", sqlite3.OperationalError("down"), retry=_always_fail)
    reg.retry_pending()

    assert reg.snapshot()["recent"][0]["attempts"] == 1, "重试失败须更新 attempts"


class _ClosedEpisodic:
    """首轮 manager 的已关闭 episodic：任何写入都抛 sqlite 关闭错误（复刻生产 finally）。"""

    def record(self, episode: object) -> None:
        raise sqlite3.ProgrammingError("Cannot operate on a closed database")


class _RecordingEpisodic:
    """次轮 manager 的 live episodic：按 episode_id 落库（REPLACE 语义）。"""

    def __init__(self) -> None:
        self.episodes: dict[str, object] = {}

    def record(self, episode: object) -> None:
        self.episodes[str(getattr(episode, "episode_id", ""))] = episode


class _TurnManager:
    def __init__(self, episodic: object) -> None:
        self.episodic = episodic
        self.working = None
        self.long_term = None


@pytest.mark.asyncio
async def test_retry_replays_on_live_manager_after_previous_closed() -> None:
    """契约 2（生产复刻）：manager1 关闭后，第 2 轮重试必须写入 manager2 而非旧实例。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.agent_runner import _persist_memory

    manager1 = _TurnManager(_ClosedEpisodic())
    manager2_episodic = _RecordingEpisodic()
    manager2 = _TurnManager(manager2_episodic)

    class _FakeBus:
        async def emit(self, *a, **k) -> None:
            return None

    def _ctx() -> ExecutionContext:
        c = ExecutionContext(messages=[], provider=object(), config=object(), bus=_FakeBus())
        c.raw_input = "帮我看下这个报错"
        c.current_content = "结论：这是回答内容"
        c.turn_index = 1
        return c

    await _persist_memory(_ctx(), manager1, "sess-live")
    assert get_registry().pending_count == 1, "首轮失败须入待重试队列"
    assert manager2_episodic.episodes == {}, "首轮不得写到次轮 manager"

    await _persist_memory(_ctx(), manager2, "sess-live")

    assert "ep:sess-live:1" in manager2_episodic.episodes, "重试必须写入当前轮 live manager"
    assert get_registry().pending_count == 0, "补写成功后须出队"
    assert get_registry().dropped == 0, "补写成功不得计入丢弃"


@pytest.mark.asyncio
async def test_compaction_channel_not_claimed_when_all_writes_failed() -> None:
    """破坏者视角：compaction 两处写全失败时，通道明细不得谎报已写入 compaction。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.agent_runner import _COMPACTION_SUMMARY_PREFIX, _persist_memory
    from bandi.core.events import EventType
    from bandi.core.message import Message, MessageRole

    events: list[object] = []

    class _FakeBus:
        async def emit(self, event: object) -> None:
            events.append(event)

    class _BoomLongTerm:
        def store(self, *a: object, **k: object) -> None:
            raise sqlite3.OperationalError("disk full")

    class _BoomHierarchy:
        def append_event(self, *a: object, **k: object) -> None:
            raise sqlite3.OperationalError("disk full")

        def append_memory(self, *a: object, **k: object) -> None:
            raise sqlite3.OperationalError("disk full")

    class _Manager:
        episodic = None
        working = None
        long_term = _BoomLongTerm()

    ctx = ExecutionContext(messages=[], provider=object(), config=object(), bus=_FakeBus())
    ctx.raw_input = "你好"
    ctx.current_content = "回答"
    ctx.turn_index = 1
    ctx.messages = [
        Message(role=MessageRole.USER, content=f"{_COMPACTION_SUMMARY_PREFIX}压缩后的摘要正文")
    ]

    await _persist_memory(ctx, _Manager(), "sess-compact", hierarchy=_BoomHierarchy())

    written = [e for e in events if getattr(e, "type", None) == EventType.MEMORY_WRITTEN]
    assert written, "必须发出 MEMORY_WRITTEN 事件"
    channels = written[-1].data.get("channels", [])
    assert "compaction" not in channels, f"两处写均失败不得记入通道明细: {channels}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_dependency_keeps_pending_not_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """契约（F1a）：重放时目标依赖缺失——保留 pending 不计成功/丢弃，并告警含通道名。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.agent_runner import _persist_memory

    class _FailingEpisodic:
        def record(self, episode: object) -> None:
            raise sqlite3.OperationalError("disk full")

    class _WithEpisodic:
        episodic = _FailingEpisodic()
        working = None
        long_term = None

    class _NoEpisodic:
        episodic = None
        working = None
        long_term = None

    class _FakeBus:
        async def emit(self, *a: object, **k: object) -> None:
            return None

    def _ctx() -> ExecutionContext:
        c = ExecutionContext(messages=[], provider=object(), config=object(), bus=_FakeBus())
        c.raw_input = "结论：帮我看下这个报错"
        c.current_content = "分析已完成，结论是继续执行当前方案。"
        c.turn_index = 1
        return c

    await _persist_memory(_ctx(), _WithEpisodic(), "sess-miss")
    assert get_registry().pending_count == 1, "首轮失败须入待重试队列"

    with caplog.at_level(logging.WARNING, logger="bandi.observability.write_failures"):
        await _persist_memory(_ctx(), _NoEpisodic(), "sess-miss")

    reg = get_registry()
    assert reg.pending_count == 1, "依赖缺失必须保留 pending（数据不得被清零）"
    assert reg.dropped == 0, "依赖缺失不得计入丢弃"
    assert reg.snapshot()["recent"][0]["attempts"] == 0, "未真正尝试不得计重试次数"
    messages = [r.getMessage() for r in caplog.records]
    assert any("episodic" in m for m in messages), f"依赖缺失须告警含通道名: {messages}"


@pytest.mark.asyncio
async def test_retry_does_not_write_other_database_scope(tmp_path: Path) -> None:
    """破坏者视角（F1b）：A 库失败项在 B 库轮次不得重放进 B 库——保留 pending 待目标库。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.agent_runner import _persist_memory
    from bandi.memory.episodic import EpisodicStore

    db_a = tmp_path / "scope_a.db"
    db_b = tmp_path / "scope_b.db"
    store_a = EpisodicStore(str(db_a))
    store_b = EpisodicStore(str(db_b))

    def _boom(episode: object) -> None:
        raise sqlite3.OperationalError("disk full")

    store_a.record = _boom  # type: ignore[method-assign]

    class _Manager:
        def __init__(self, episodic: object) -> None:
            self.episodic = episodic
            self.working = None
            self.long_term = None

    class _FakeBus:
        async def emit(self, *a: object, **k: object) -> None:
            return None

    def _ctx(raw_input: str) -> ExecutionContext:
        c = ExecutionContext(messages=[], provider=object(), config=object(), bus=_FakeBus())
        c.raw_input = raw_input
        c.current_content = "分析已完成，结论是继续执行当前方案。"
        c.turn_index = 1
        return c

    await _persist_memory(_ctx("结论：A 库的输入"), _Manager(store_a), "sess-scope")
    assert get_registry().pending_count == 1, "A 库失败项须入待重试队列"

    await _persist_memory(_ctx(""), _Manager(store_b), "sess-scope")

    with sqlite3.connect(db_b) as conn:
        count = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    assert count == 0, "A 库失败项不得写入 B 库"

    reg = get_registry()
    assert reg.pending_count == 1, "跨范围项必须保留 pending（留待目标库轮次）"
    assert reg.dropped == 0, "跨范围不得计入丢弃"

    store_a.close()
    store_b.close()


def test_missing_dependency_does_not_consume_retry_budget() -> None:
    """契约（五审 ）：依赖缺失不计真实尝试——missing 不消耗 max_attempts 预算。"""
    from bandi.observability.write_failures import (
        RetryDependencyMissingError,
        WriteFailureRegistry,
    )

    registry = WriteFailureRegistry(max_attempts=3)
    state = {"missing": True}

    def _retry(manager: object = None) -> None:
        if state["missing"]:
            raise RetryDependencyMissingError("no dependency this turn")
        raise sqlite3.OperationalError("still locked")

    registry.record("hierarchy_event", sqlite3.OperationalError("first"), retry=_retry, scope="s")
    for _ in range(2):
        report = registry.retry_pending(object())
        assert report["dropped"] == 0
    state["missing"] = False
    report = registry.retry_pending(object())
    assert report["failed"] == 1, "missing 后首次真实失败必须保留 pending（预算未被消耗）"
    assert report["dropped"] == 0
    assert registry.pending_count == 1


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def test_zero_arg_retry_replays_under_live_manager() -> None:
    """🟡-1：零参 retry 回调在 `retry_pending(manager)` 下必须正常重放。

    原实现以 `retry(manager)` 直呼零参闭包（`_run_write` 的 fn 兜底路径）→
    TypeError 被当真实失败，max_attempts=1 直接 dropped（数据静默丢弃）。
    """
    reg = WriteFailureRegistry()  # 默认 max_attempts=1
    calls: list[str] = []

    def _zero_arg() -> None:
        calls.append("replayed")

    reg.record("episodic", sqlite3.OperationalError("disk full"), retry=_zero_arg)
    report = reg.retry_pending(object())

    assert calls == ["replayed"], "零参回调必须被重放（不得被 call(manager) 的 TypeError 吞掉）"
    assert report["succeeded"] == 1
    assert report["dropped"] == 0
    assert reg.dropped == 0, "兼容适配后不得计入丢弃"
    assert reg.pending_count == 0, "成功重放须出队"


def test_zero_arg_retry_real_failure_counts_dropped() -> None:
    """边界：零参回调真实失败（非适配 TypeError）仍按 max_attempts 计丢弃。"""
    reg = WriteFailureRegistry()

    def _boom() -> None:
        raise sqlite3.OperationalError("still down")

    reg.record("episodic", sqlite3.OperationalError("down"), retry=_boom)
    report = reg.retry_pending(object())

    assert report["dropped"] == 1
    assert reg.dropped == 1
    assert reg.pending_count == 0


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class _LockProbeRow:
    """探针行：`attempts` 被读取时未持注册表锁即记违规（旧实现在锁外物化）。"""

    def __init__(self, registry: WriteFailureRegistry) -> None:
        self._registry = registry
        self.violations = 0
        self.channel = "probe"
        self.error = "probe"

    @property
    def attempts(self) -> int:
        owned = getattr(self._registry._lock, "_is_owned", lambda: False)()
        if not owned:
            self.violations += 1
        return 0


def test_snapshot_materializes_recent_under_lock() -> None:
    """🟢-6：`snapshot()` 的 recent 明细必须在锁内物化（attempts 不得锁外读）。"""
    reg = WriteFailureRegistry()
    probe = _LockProbeRow(reg)
    reg._recent.append(probe)  # type: ignore[arg-type]
    snap = reg.snapshot()

    assert snap["recent"] == [{"channel": "probe", "error": "probe", "attempts": 0}]
    assert probe.violations == 0, "recent 明细在锁外物化——attempts 读取与重试更新竞态"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_magic_stub_store_scope_not_permanently_deferred() -> None:
    """破坏者视角：store 桩的 `db_path`/`_conn` 每次访问返回新对象
    （repr 含地址、跨轮不稳定）时，范围必须归一为空（=不过滤）——双轮重试
    不得因「范围漂移」被永久 deferred、live store 上真实重放。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.agent_runner import _persist_memory

    class _MagicEpisodic:
        """魔法桩形态：路径属性访问返回新对象（对象地址 repr 每轮不同）。"""

        def __init__(self, fail: bool) -> None:
            self.fail = fail
            self.recorded: list[object] = []
            self._holders: list[object] = []

        def __getattr__(self, name: str) -> object:
            if name in ("db_path", "_db_path", "_conn"):
                holder = object()
                self._holders.append(holder)
                return holder
            raise AttributeError(name)

        def record(self, episode: object) -> None:
            if self.fail:
                raise sqlite3.OperationalError("disk full")
            self.recorded.append(episode)

    class _Manager:
        def __init__(self, episodic: object) -> None:
            self.episodic = episodic
            self.working = None
            self.long_term = None

    class _FakeBus:
        async def emit(self, *a: object, **k: object) -> None:
            return None

    def _ctx() -> ExecutionContext:
        c = ExecutionContext(messages=[], provider=object(), config=object(), bus=_FakeBus())
        c.raw_input = "结论：范围桩场景的输入"
        c.current_content = "分析已完成，结论是继续执行当前方案。"
        c.turn_index = 1
        return c

    flaky = _MagicEpisodic(fail=True)
    await _persist_memory(_ctx(), _Manager(flaky), "sess-magic-scope")
    assert get_registry().pending_count == 1, "首轮失败须入待重试队列"

    live = _MagicEpisodic(fail=False)
    await _persist_memory(_ctx(), _Manager(live), "sess-magic-scope")

    reg = get_registry()
    assert reg.pending_count == 0, "魔法桩范围归一为空后重试不得永久 deferred"
    assert reg.dropped == 0, "重放成功不得计入丢弃"
    assert live.recorded, "pending 必须在本轮 live store 上真实重放"


# ==================== b3_close_race ====================
# 功能：契约——关闭期持久化不丢尾批 / 旧代不写新文件（并发工程复核 🟡-5 + /12/13）
#
# ① `disable_persistence` 不递增 `_persist_generation`（只在 `enable` 递增）
# → disable 后仍在飞的旧 worker 能通过 `_persist_one` 的二次校验继续写；
# ② final `flush/close` 在 `_persist_state_lock` **之外**，而写段在锁内
# → join 超时（worker 仍存活）时会与锁内写竞态：worker 抛
# `I/O operation on closed file`，正在排空的尾批静默丢失；


def r8xb3_bus(tmp_path: Path) -> EventBus:
    bus = EventBus(persist_path=tmp_path / "events.jsonl")
    bus.enable_persistence()
    return bus


def r8xb3_lines(tmp_path: Path) -> list[str]:
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


async def _capture_one_record(bus: EventBus) -> dict[str, Any]:
    """经实例级替换捕获一条真实落盘记录（emit 侧构造，非手搓）。"""
    captured: dict[str, Any] = {}
    real = bus._persist_one
    bus._persist_one = lambda record, generation=None: captured.update(  # type: ignore[method-assign]
        {"record": record, "generation": generation}
    )
    await bus.emit(Event(type=EventType.TURN_STARTED, data={"probe": 1}))
    await bus.drain()
    bus._persist_one = real  # type: ignore[method-assign]
    assert captured, "未能捕获落盘记录（写线程未运行？）"
    return captured


class TestDisableGuards:
    """🟡-5：代数递增 + 锁内关文件。"""

    def test_disable_increments_generation(self, tmp_path: Path) -> None:
        """机理：disable 必须递增代数——否则旧 worker 的二次校验仍放行。"""
        bus = r8xb3_bus(tmp_path)
        gen_before = bus._persist_generation
        bus.disable_persistence()
        assert bus._persist_generation == gen_before + 1, "disable 未递增代数"

    @pytest.mark.asyncio
    async def test_stale_generation_write_is_dropped(self, tmp_path: Path) -> None:
        """**破坏者视角**：用 disable 前的代数落盘 → 必须被丢弃，不得写入已关文件。"""
        bus = r8xb3_bus(tmp_path)
        captured = await _capture_one_record(bus)
        stale_gen = captured["generation"]
        assert r8xb3_lines(tmp_path) == [], "捕获路径不应真正落盘"

        bus.disable_persistence()
        # 旧代 worker 场景：拿到 disable 前捕获的代数再写一次（不得抛错、不得写入）
        bus._persist_one(captured["record"], stale_gen)

        assert r8xb3_lines(tmp_path) == [], "旧代写入未被丢弃（disable 后仍写入了已关文件）"

    def test_write_without_generation_after_close_does_not_raise(self, tmp_path: Path) -> None:
        """关闭后无代数写入 → 早退（文件已关），不得抛 I/O 错。"""
        bus = r8xb3_bus(tmp_path)
        bus.disable_persistence()

        bus._persist_one({"event_id": "x", "type": "TURN_STARTED", "data": {}}, None)

        assert r8xb3_lines(tmp_path) == []

    def test_disable_is_idempotent(self, tmp_path: Path) -> None:
        """回归：重复 disable 幂等（不抛错、代数照常递增）。"""
        bus = r8xb3_bus(tmp_path)
        bus.disable_persistence()
        first = bus._persist_generation
        bus.disable_persistence()
        assert bus._persist_generation >= first


class TestDrainSemantics:
    """/ 。"""

    @pytest.mark.asyncio
    async def test_drain_on_disabled_warns_when_events_were_dropped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """关闭态 drain 虽无队列可等，但有丢弃必须告警（不得谎报"已持久化"）。"""
        bus = r8xb3_bus(tmp_path)
        bus._account_dropped(3)
        bus.disable_persistence()

        with caplog.at_level(logging.WARNING, logger="bandi.core.event_bus"):
            assert await bus.drain() is True

        assert any("dropped" in r.getMessage() for r in caplog.records), (
            f"丢弃未被告警：{[r.getMessage() for r in caplog.records]}"
        )

    def test_dead_worker_does_not_wait_full_timeout(self, tmp_path: Path) -> None:
        """worker 已死 → `_drain_blocking` 立即返回，不白等满超时。"""
        bus = r8xb3_bus(tmp_path)
        pending: queue.Queue[Any] = queue.Queue()
        pending.put({"event_id": "e1"})  # 制造 unfinished_tasks = 1
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()

        started = time.monotonic()
        drained = bus._drain_blocking(2000, pending, dead)
        elapsed = time.monotonic() - started

        assert drained is False, "队列未空却报已排空"
        assert elapsed < 0.5, f"worker 已死仍等满超时（{elapsed:.2f}s）"


class TestCloseDoesNotLoseEvents:
    """回归：正常的 enable → emit → close 链路不得丢事件、链不得断。"""

    @pytest.mark.asyncio
    async def test_close_flushes_queued_events(self, tmp_path: Path) -> None:
        bus = r8xb3_bus(tmp_path)
        for i in range(5):
            await bus.emit(Event(type=EventType.TURN_STARTED, data={"i": i}))
        await bus.close()

        assert len(r8xb3_lines(tmp_path)) == 5, "关闭时未排空已入队事件"

    @pytest.mark.asyncio
    async def test_reenable_after_disable_still_persists(self, tmp_path: Path) -> None:
        """回归：代数递增不得破坏「关闭后重开」——重开后事件照常落盘。"""
        bus = r8xb3_bus(tmp_path)
        await bus.emit(Event(type=EventType.TURN_STARTED, data={"phase": 1}))
        await bus.drain()
        bus.disable_persistence()

        bus2 = EventBus(persist_path=tmp_path / "events.jsonl")
        bus2.enable_persistence()
        await bus2.emit(Event(type=EventType.TURN_STARTED, data={"phase": 2}))
        assert await bus2.drain() is True

        assert len(r8xb3_lines(tmp_path)) >= 2, "重开后事件未落盘"


# ==================== b3_event_persist_async ====================
# 功能：契约——事件持久化移出事件循环（并发工程 批次2 / ）
#
# write+flush+关键事件 fsync，慢磁盘/大事件阻塞事件循环。本契约钉：
# 1) 文件写（含行锁）只发生在写线程，emit 仅入队不触碰文件；
# 2) drain 后队列排空且落盘条数/哈希链完整；
# 3) 队列满 → 丢弃并计数，emit 不阻塞；
# 4) drain 有界——写线程卡住时按时返回 False；
# 5) close 前自动排空（关闭不丢已入队事件）。


def r9xb3_bus(tmp_path: Path) -> EventBus:
    bus = EventBus(persist_path=tmp_path / "events.jsonl")
    bus.enable_persistence()
    return bus


def r9xb3_lines(tmp_path: Path) -> list[str]:
    return (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()


@pytest.mark.asyncio
async def test_file_write_happens_on_writer_thread(tmp_path: Path, monkeypatch: Any) -> None:
    """核心：行锁/文件写只发生在写线程——emit 本身不触碰文件（不阻塞事件循环）。"""
    import bandi.core.event_bus as eb_mod

    threads: list[str] = []
    original = eb_mod._lock_byte

    def spy(file: Any) -> None:
        threads.append(threading.current_thread().name)
        original(file)

    monkeypatch.setattr(eb_mod, "_lock_byte", spy)
    bus = r9xb3_bus(tmp_path)
    await bus.emit(Event(type=EventType.TURN_STARTED, data={"i": 1}))
    assert await bus.drain(), "drain 必须排空"

    main_name = threading.current_thread().name
    assert threads, "drain 后必须发生过文件写"
    assert all(name != main_name for name in threads), f"文件写在事件循环线程发生：{threads}"
    await bus.close()


@pytest.mark.asyncio
async def test_drain_persists_all_and_chain_intact(tmp_path: Path) -> None:
    """drain 后：全部事件落盘 + hash 链/sequence 校验通过。"""
    bus = r9xb3_bus(tmp_path)
    for i in range(5):
        await bus.emit(Event(type=EventType.TURN_STARTED, data={"i": i}))

    assert await bus.drain(), "drain 超时"
    assert len(r9xb3_lines(tmp_path)) == 5
    assert bus.replay(dry_run=True).ok, "异步写不得破坏哈希链"
    await bus.close()


@pytest.mark.asyncio
async def test_queue_full_drops_and_counts_not_blocks(tmp_path: Path, monkeypatch: Any) -> None:
    """破坏者视角：写线程被卡住 + 小队列 → 多余事件丢弃并计数，emit 不阻塞。"""
    bus = r9xb3_bus(tmp_path)
    release = threading.Event()
    monkeypatch.setattr(bus, "_persist_one", lambda record, *args: release.wait(5))
    bus._persist_queue = queue.Queue(maxsize=2)  # type: ignore[assignment]

    started = time.monotonic()
    for i in range(6):
        await bus.emit(Event(type=EventType.TURN_STARTED, data={"i": i}))
    elapsed = time.monotonic() - started

    assert bus.persist_dropped >= 1, "队列满必须丢弃并计数（不得无限入队）"
    assert elapsed < 2.0, f"emit 被队列阻塞（{elapsed:.2f}s）"
    release.set()
    await bus.close()


@pytest.mark.asyncio
async def test_drain_bounded_when_writer_stuck(tmp_path: Path, monkeypatch: Any) -> None:
    """边界：写线程卡住时 drain 按时返回 False（不无限等待）。"""
    bus = r9xb3_bus(tmp_path)
    release = threading.Event()
    monkeypatch.setattr(bus, "_persist_one", lambda record, *args: release.wait(5))
    await bus.emit(Event(type=EventType.TURN_STARTED, data={"i": 0}))

    started = time.monotonic()
    assert await bus.drain(timeout=150) is False, "卡住时必须返回 False"
    assert time.monotonic() - started < 2.0, "drain 超时有界性不足"

    release.set()
    assert await bus.drain(timeout=2000) is True, "释放后重试必须排空"
    await bus.close()


@pytest.mark.asyncio
async def test_close_drains_backlog_before_close(tmp_path: Path) -> None:
    """close 语义：已入队事件在关闭前排空落盘（不丢尾批）。"""
    bus = r9xb3_bus(tmp_path)
    for i in range(20):
        await bus.emit(Event(type=EventType.STATE_CHANGED, data={"i": i}))

    await bus.close()
    assert len(r9xb3_lines(tmp_path)) == 20, "close 前必须排空全部已入队事件"


@pytest.mark.asyncio
async def test_disable_persists_slow_backlog(tmp_path: Path, monkeypatch: Any) -> None:
    """慢写线程（未超排空上界）：disable 必须等全部积压落盘，零丢弃。"""
    bus = r9xb3_bus(tmp_path)
    original = bus._persist_one

    def _slow(record: dict[str, Any], *args: Any) -> None:
        time.sleep(0.2)
        original(record, *args)

    monkeypatch.setattr(bus, "_persist_one", _slow)
    for i in range(3):
        await bus.emit(Event(type=EventType.STATE_CHANGED, data={"i": i}))

    bus.disable_persistence()

    assert len(r9xb3_lines(tmp_path)) == 3, "慢写积压必须在关闭前排空落盘"
    assert bus.persist_dropped == 0, "未超时不得把已落盘事件计入丢弃"


@pytest.mark.asyncio
async def test_disable_bounded_and_counts_when_writer_stuck(
    tmp_path: Path, monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """破坏者视角：写线程真卡死 → disable 有界返回，积压计入 persist_dropped 并告警。"""
    bus = r9xb3_bus(tmp_path)
    release = threading.Event()
    monkeypatch.setattr(bus, "_persist_one", lambda record, *args: release.wait(5))
    for i in range(3):
        await bus.emit(Event(type=EventType.TURN_STARTED, data={"i": i}))

    with caplog.at_level(logging.WARNING, logger="bandi.core.event_bus"):
        started = time.monotonic()
        bus.disable_persistence(timeout_ms=200)
        elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"disable 被卡死写线程拖住（{elapsed:.2f}s）"
    assert bus.persist_dropped >= 1, "卡死积压必须计入丢弃（不静默）"
    messages = [str(r.message) for r in caplog.records if r.name == "bandi.core.event_bus"]
    assert any("drop" in m.lower() for m in messages), f"必须留下含条数的告警: {messages}"

    release.set()


@pytest.mark.asyncio
async def test_enable_disable_enable_two_rounds(tmp_path: Path) -> None:
    """复用实例：enable→disable→enable 两轮事件分别完整落盘（worker 不跨队列消费）。"""
    bus = EventBus(persist_path=tmp_path / "events.jsonl")
    bus.enable_persistence()
    for i in range(2):
        await bus.emit(Event(type=EventType.TURN_STARTED, data={"round": 1, "i": i}))
    assert await bus.drain()
    bus.disable_persistence()
    assert len(r9xb3_lines(tmp_path)) == 2, "第一轮事件必须完整落盘"

    bus.enable_persistence()
    for i in range(3):
        await bus.emit(Event(type=EventType.TURN_STARTED, data={"round": 2, "i": i}))
    bus.disable_persistence()

    assert len(r9xb3_lines(tmp_path)) == 5, "第二轮事件必须完整落盘（不跨队列消费）"
    assert bus.persist_dropped == 0


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_snapshots_event_data(tmp_path: Path, monkeypatch: Any) -> None:
    """契约：emit 后改写原 data dict 不得污染落盘内容（冻结在 emit 时刻）。"""
    bus = r9xb3_bus(tmp_path)
    original = bus._persist_one
    started = threading.Event()
    release = threading.Event()

    def _blocked(record: dict[str, Any], *args: Any) -> None:
        started.set()
        release.wait(5)
        original(record, *args)

    monkeypatch.setattr(bus, "_persist_one", _blocked)
    data: dict[str, Any] = {"state": "原始值", "nested": {"k": "v"}}
    await bus.emit(Event(type=EventType.STATE_CHANGED, data=data))
    assert started.wait(2.0), "写线程必须已取到记录"

    data["state"] = "被改写"
    data["nested"]["k"] = "mutated"
    release.set()
    assert await bus.drain()

    payload = json.loads(r9xb3_lines(tmp_path)[0])
    assert payload["data"]["state"] == "原始值", "落盘必须是 emit 时刻的值"
    assert payload["data"]["nested"]["k"] == "v", "嵌套对象同样不得被后续改写污染"
    await bus.close()


@pytest.mark.asyncio
async def test_stale_worker_cannot_write_new_generation(tmp_path: Path, monkeypatch: Any) -> None:
    """破坏者视角：旧代写线程卡住 + disable 超时 + enable → 旧代不得写入新文件。"""
    import bandi.core.event_bus as eb_mod

    monkeypatch.setattr(eb_mod, "_PERSIST_THREAD_JOIN_TIMEOUT_S", 0.2)
    bus = r9xb3_bus(tmp_path)
    original = bus._persist_one
    started = threading.Event()
    release = threading.Event()
    blocked_once = threading.Event()

    def _slow(record: dict[str, Any], *args: Any) -> None:
        # 只卡住旧代（第一个取记录的写线程）；新代线程直通
        if not blocked_once.is_set():
            blocked_once.set()
            started.set()
            release.wait(5)
        original(record, *args)

    monkeypatch.setattr(bus, "_persist_one", _slow)
    await bus.emit(Event(type=EventType.TURN_STARTED, data={"round": 1}))
    assert started.wait(2.0), "旧代写线程必须已取到记录并卡住"
    old_worker = bus._persist_thread

    bus.disable_persistence(timeout_ms=50)  # join 超时：旧代线程存活
    bus.enable_persistence()  # 新代开启（旧代记录须被代数校验丢弃）
    await bus.emit(Event(type=EventType.TURN_STARTED, data={"round": 2}))
    assert await bus.drain(), "新代事件必须排空"

    release.set()
    if old_worker is not None:
        old_worker.join(timeout=2.0)
    if bus._persist_file is not None and not bus._persist_file.closed:
        bus._persist_file.flush()  # 缓冲区落盘后再断言（跨代写入不得藏于缓冲）

    lines = r9xb3_lines(tmp_path)
    assert len(lines) == 1, f"旧代事件写入新文件（跨代混写）：{lines}"
    assert '"round": 2' in lines[0], "新文件只应含新代事件"
    assert bus.replay(dry_run=True).ok, "跨代混写会破坏哈希链"
    await bus.close()


@pytest.mark.asyncio
async def test_enable_syncs_written_bytes_to_file_size(tmp_path: Path, monkeypatch: Any) -> None:
    """契约：enable 按现有文件大小初始化已写字节——外部截断后首个事件不得误 rotate。"""
    import bandi.core.event_bus as eb_mod

    monkeypatch.setattr(eb_mod, "_PERSIST_ROTATE_BYTES", 120)
    bus = r9xb3_bus(tmp_path)
    await bus.emit(Event(type=EventType.TURN_STARTED, data={"pad": "x" * 300}))
    assert await bus.drain()
    path = tmp_path / "events.jsonl"
    assert path.stat().st_size > 120, "前置：已写内容超旋转阈值"

    bus.disable_persistence()
    path.write_text("old\n", encoding="utf-8")

    bus.enable_persistence()
    await bus.emit(Event(type=EventType.TURN_STARTED, data={"i": 1}))
    assert await bus.drain()

    assert not path.with_suffix(path.suffix + ".1").exists(), "小文件启用后首个事件不得误旋转"
    await bus.close()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
@pytest.mark.asyncio
async def test_disable_counts_backlog_when_worker_dead(
    tmp_path: Path, monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """🟡-2（破坏者视角）：worker 被 BaseException 杀死 + 队列有积压 →
    disable 必须计入 persist_dropped + 告警，不得静默丢（且不挂死）。"""

    def _die(record: dict[str, Any], *args: Any) -> None:
        raise SystemExit("worker killed")

    bus = r9xb3_bus(tmp_path)
    monkeypatch.setattr(bus, "_persist_one", _die)
    for i in range(3):
        await bus.emit(Event(type=EventType.TURN_STARTED, data={"i": i}))

    deadline = time.monotonic() + 2.0
    while (
        bus._persist_thread is not None
        and bus._persist_thread.is_alive()
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert bus._persist_thread is not None and not bus._persist_thread.is_alive(), (
        "前置：写线程必须已被 BaseException 杀死"
    )

    with caplog.at_level(logging.WARNING, logger="bandi.core.event_bus"):
        started = time.monotonic()
        bus.disable_persistence(timeout_ms=200)
        elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"worker 已死时 disable 不得挂死（{elapsed:.2f}s）"
    assert bus.persist_dropped >= 1, "worker 死亡 + 积压必须计入丢弃（原实现静默丢）"
    messages = [str(r.message) for r in caplog.records if r.name == "bandi.core.event_bus"]
    assert any("drop" in m.lower() for m in messages), f"必须留下含丢弃条数的告警: {messages}"


@pytest.mark.asyncio
async def test_drain_flush_race_does_not_raise(tmp_path: Path, monkeypatch: Any) -> None:
    """🟢-4：drain 的 flush 撞上并发关闭窗口（ValueError/OSError）不得炸调用方。"""
    bus = r9xb3_bus(tmp_path)
    await bus.emit(Event(type=EventType.TURN_STARTED, data={"i": 0}))
    assert await bus.drain()

    def _boom() -> None:
        raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(bus._persist_file, "flush", _boom)
    assert await bus.drain() is True, "关闭窗口 flush 失败不得改变排空判定/不得抛出"
    monkeypatch.undo()
    await bus.close()


@pytest.mark.asyncio
async def test_emit_snapshots_non_dict_event_data(tmp_path: Path, monkeypatch: Any) -> None:
    """🟢-8：非 dict 事件 data（list）同样冻结——emit 后改写不污染落盘。"""
    bus = r9xb3_bus(tmp_path)
    original = bus._persist_one
    started = threading.Event()
    release = threading.Event()

    def _blocked(record: dict[str, Any], *args: Any) -> None:
        started.set()
        release.wait(5)
        original(record, *args)

    monkeypatch.setattr(bus, "_persist_one", _blocked)
    data: list[Any] = [{"k": "v"}, "原始"]
    await bus.emit(Event(type=EventType.STATE_CHANGED, data=data))
    assert started.wait(2.0), "写线程必须已取到记录"

    data[0]["k"] = "被改写"
    data.append("追加")
    release.set()
    assert await bus.drain()

    payload = json.loads(r9xb3_lines(tmp_path)[0])
    assert payload["data"] == [{"k": "v"}, "原始"], "非 dict data 必须冻结 emit 时刻内容"
    await bus.close()


# ==================== b3_project_usage ====================
# 功能：契约——项目数据面（项目用量：团队/员工口径 + 排除秘书）+
# 项目产物角色过滤 + 工作项项目归属字段。
# 钉行为不钉实现：团队口径 = 项目普通会话（排除秘书）+ 项目全部员工执行会话；
# 员工口径 = 仅该角色的员工执行会话（role-session 倒数第二段）/ 项目隔离 /
# 空台账零值不 500 / 项目缺失 404 / 非法 id 400 / 产物 role 过滤 / 工作项带 project_id。


r10xb3_client = TestClient(app)
r10xb3_client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def r10xb3_isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """会话/项目/台账三库隔离：Path.home→tmp + 单例重置（每测试重建装配）。"""
    from bandi.core.config import BandiConfig
    from bandi.observability.cost_store import reset_shared_cost_stores

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    reset_shared_cost_stores()
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._company_dispatcher", None),
        patch("bandi.api.app._custom_runtime_runner", None),
        patch("bandi.api.app._team_store", None),
        patch("bandi.api.app._project_store", None),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield
    reset_shared_cost_stores()


def r10xb3_create_project(project_id: str, name: str = "用量项目") -> None:
    resp = r10xb3_client.post(
        "/api/company/projects", json={"project_id": project_id, "name": name}
    )
    assert resp.status_code == 200, resp.text


def _seed_cost(rows: list[dict]) -> None:
    """功能：向台账写 LLM 记录（公共入口 enqueue + flush，模拟真实记账链路）。"""
    from bandi.core.config import default_cost_db_path
    from bandi.observability.cost_store import CostRecord, get_shared_cost_store

    store = get_shared_cost_store(default_cost_db_path())
    for index, row in enumerate(rows):
        store.enqueue(
            CostRecord(
                event_id=f"evt-{index}",
                created_at=datetime.now().isoformat(),
                **row,
            )
        )
    store.flush()


def _seed_sessions(rows: list[tuple[str, str, str]]) -> None:
    """功能：建会话（session_id, project_id, origin）——usage 项目归属第二来源。"""
    from bandi.api.app import _get_store

    store = _get_store()
    for session_id, project_id, origin in rows:
        store.create_session(session_id, project_id=project_id or None, origin=origin or None)


class TestProjectUsage:
    def test_team_usage_excludes_secretary_and_isolates_projects(self) -> None:
        r10xb3_create_project("p-usage")
        r10xb3_create_project("p-other", "别的项目")
        _seed_sessions(
            [
                ("s-normal", "p-usage", ""),
                ("s-secretary", "p-usage", "secretary"),
                ("s-other", "p-other", ""),
            ]
        )
        _seed_cost(
            [
                {"session_id": "s-normal", "input_tokens": 100, "output_tokens": 10},
                {"session_id": "s-secretary", "input_tokens": 200, "output_tokens": 20},
                {
                    "session_id": "role-session::p-usage::scope::dev::e1",
                    "input_tokens": 300,
                    "output_tokens": 30,
                    "agent_role": "dev",
                },
                {
                    "session_id": "role-session::p-other::scope::dev::e1",
                    "input_tokens": 400,
                    "output_tokens": 40,
                    "agent_role": "dev",
                },
            ]
        )

        resp = r10xb3_client.get("/api/company/projects/p-usage/usage")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["project_id"] == "p-usage"
        assert body["total_tokens"] == 440, "项目普通会话 110 + 员工执行 330；秘书 220 必须排除"
        assert body["requests"] == 2

        resp = r10xb3_client.get("/api/company/projects/p-other/usage")
        assert resp.json()["total_tokens"] == 440, "项目隔离（仅 p-other 的 440）"

    def test_role_usage_only_that_employees_sessions(self) -> None:
        r10xb3_create_project("p-usage")
        _seed_sessions([("s-normal", "p-usage", "")])
        _seed_cost(
            [
                {"session_id": "s-normal", "input_tokens": 100, "output_tokens": 0},
                {
                    "session_id": "role-session::p-usage::scope::dev::e1",
                    "input_tokens": 300,
                    "output_tokens": 30,
                    "agent_role": "dev",
                },
                {
                    "session_id": "role-session::p-usage::scope::ops::e2",
                    "input_tokens": 500,
                    "output_tokens": 50,
                    "agent_role": "ops",
                },
            ]
        )

        resp = r10xb3_client.get("/api/company/projects/p-usage/usage", params={"role": "dev"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["role"] == "dev"
        assert body["total_tokens"] == 330, "仅该员工的执行会话（不含普通会话与他角色）"

        resp = r10xb3_client.get("/api/company/projects/p-usage/usage", params={"role": "ghost"})
        assert resp.json()["total_tokens"] == 0

    def test_empty_ledger_zero_missing_404_invalid_400(self) -> None:
        r10xb3_create_project("p-empty")
        resp = r10xb3_client.get("/api/company/projects/p-empty/usage")
        assert resp.status_code == 200, "空台账零值不 500"
        assert resp.json()["total_tokens"] == 0
        assert resp.json()["total_cost_cny"] == 0.0

        resp = r10xb3_client.get("/api/company/projects/ghost/usage")
        assert resp.status_code == 404

        resp = r10xb3_client.get("/api/company/projects/a b/usage")
        assert resp.status_code == 400


class TestProjectArtifactsRoleFilter:
    def _seed_artifacts(self) -> None:
        from bandi.api.app import _get_store

        store = _get_store()
        store.create_session("s-team", project_id="p-art")
        # 员工执行会话在生产链由 run_agent_turn 补建（同 store），产物注册受 FK 约束
        store.create_session("role-session::p-art::scope::dev::e1", project_id="p-art")
        store.create_session("role-session::p-art::scope::ops::e2", project_id="p-art")
        store.register_artifact("s-team", tool_name="write", path="会话产物.md")
        store.register_artifact(
            "role-session::p-art::scope::dev::e1", tool_name="write", path="开发报告.md"
        )
        store.register_artifact(
            "role-session::p-art::scope::ops::e2", tool_name="write", path="运维清单.md"
        )

    def test_team_artifacts_include_role_sessions(self) -> None:
        r10xb3_create_project("p-art")
        self._seed_artifacts()

        resp = r10xb3_client.get("/api/company/projects/p-art/artifacts")
        assert resp.status_code == 200, resp.text
        items = resp.json()["artifacts"]
        assert {item["path"] for item in items} == {"会话产物.md", "开发报告.md", "运维清单.md"}, (
            "团队产物 = 普通会话产物 + 项目全部员工执行产物"
        )
        created = [item["created_at"] for item in items]
        assert created == sorted(created), "created_at 非降序"

    def test_role_filter_only_that_employee(self) -> None:
        r10xb3_create_project("p-art")
        self._seed_artifacts()

        resp = r10xb3_client.get("/api/company/projects/p-art/artifacts", params={"role": "dev"})
        assert resp.status_code == 200, resp.text
        items = resp.json()["artifacts"]
        assert [item["path"] for item in items] == ["开发报告.md"]
        assert items[0]["session_id"] == "role-session::p-art::scope::dev::e1"

        resp = r10xb3_client.get("/api/company/projects/p-art/artifacts", params={"role": "ghost"})
        assert resp.json()["count"] == 0

    def test_invalid_and_missing_project(self) -> None:
        resp = r10xb3_client.get("/api/company/projects/ghost/artifacts", params={"role": "dev"})
        assert resp.status_code == 404
        resp = r10xb3_client.get("/api/company/projects/a b/artifacts")
        assert resp.status_code == 400


class TestWorkItemProjectField:
    def test_work_items_carry_project_id(self) -> None:
        from bandi.api.app import _get_store  # noqa: F401 — 触发装配隔离

        r10xb3_create_project("p-wi")
        dispatcher = r10xb3_client.get("/api/company/work-items")
        assert dispatcher.status_code == 200
        # 播种一张带 project_id metadata 的工作项（模拟派单落卡）
        from bandi.api.app import _org_api_dispatcher
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        item = create_work_item(
            "项目任务一",
            WorkItemTurnType.EXECUTE,
            assignee_role="dev",
            metadata={"project_id": "p-wi"},
        )
        _org_api_dispatcher().store.save(item)

        resp = r10xb3_client.get("/api/company/work-items")
        assert resp.status_code == 200, resp.text
        rows = {row["id"]: row for row in resp.json()["items"]}
        assert rows[item.id]["project_id"] == "p-wi", "工作项必须带项目归属字段"

        resp = r10xb3_client.get("/api/company/work-items", params={"role": "dev"})
        rows = {row["id"]: row for row in resp.json()["items"]}
        assert rows[item.id]["project_id"] == "p-wi"


# ==================== b3_stream_filter ====================


class PiiStub:
    def sanitize(self, text: str) -> str:
        return text.replace("13800138000", "[PHONE]")


class SanitizerStub:
    def sanitize(self, text: str | None) -> str:
        return (text or "").replace("BADWORD", "[REDACTED]")


class FakeStreamingProvider:
    model_name = "stub"
    protocol = "openai"

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = deltas

    async def stream(self, messages: list[dict], **kwargs: Any) -> Any:
        for d in self._deltas:
            yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta=d)
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")


def _ctx_with_safety(pii: Any, sanitizer: Any) -> ExecutionContext:
    ctx = ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.pii_detector = pii
    ctx.output_sanitizer = sanitizer
    return ctx


def _subscribe(emitted: list[str], ctx: ExecutionContext) -> None:
    async def _on(ev: Event) -> None:
        if ev.type == EventType.LLM_STREAM_EVENT and ev.data.get("type") == "content_delta":
            emitted.append(ev.data.get("content", ""))

    asyncio.run(ctx.bus.subscribe(EventType.LLM_STREAM_EVENT, _on))


def test_stream_delta_emitted_after_pii_filter() -> None:
    from bandi.agent.states import _stream_thinking

    ctx = _ctx_with_safety(PiiStub(), SanitizerStub())
    provider = FakeStreamingProvider(["我的电话是 13800138000，请记录"])
    emitted: list[str] = []
    _subscribe(emitted, ctx)

    resp = asyncio.run(_stream_thinking(ctx, [], provider=provider))
    assert all("13800138000" not in c for c in emitted), f"emit 前必须脱敏（实际 {emitted}）"
    assert any("[PHONE]" in c for c in emitted)
    assert "13800138000" not in (resp.content or ""), "最终产物（buffer 累积）必须为过滤后文本"


def test_stream_delta_filtered_by_output_sanitizer() -> None:
    from bandi.agent.states import _stream_thinking

    ctx = _ctx_with_safety(PiiStub(), SanitizerStub())
    provider = FakeStreamingProvider(["这是 BADWORD 内容"])
    emitted: list[str] = []
    _subscribe(emitted, ctx)

    resp = asyncio.run(_stream_thinking(ctx, [], provider=provider))
    assert all("BADWORD" not in c for c in emitted)
    assert "[REDACTED]" in (resp.content or "")


def test_stream_delta_unchanged_when_no_filters() -> None:
    from bandi.agent.states import _stream_thinking

    ctx = ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    provider = FakeStreamingProvider(["正常内容"])
    emitted: list[str] = []
    _subscribe(emitted, ctx)

    resp = asyncio.run(_stream_thinking(ctx, [], provider=provider))
    assert emitted == ["正常内容"]
    assert resp.content == "正常内容"


class _SplitPhoneProvider:
    """手机号跨两个 content delta 边界（PII 模式被切碎）。"""

    model_name = "stub"
    protocol = "openai"

    async def stream(self, messages: list[dict], **kwargs: Any) -> Any:
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="联系电话 1380")
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="0138000 请回拨")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")


def test_cross_delta_phone_number_not_leaked() -> None:
    """PII 模式跨 delta 边界（切碎）时，尾部悬空前缀与下一 delta 合并
    过滤——emitted 与最终产物均不得泄漏完整手机号。"""
    from bandi.agent.states import _stream_thinking

    ctx = _ctx_with_safety(PiiStub(), SanitizerStub())
    provider = _SplitPhoneProvider()
    emitted: list[str] = []
    _subscribe(emitted, ctx)

    resp = asyncio.run(_stream_thinking(ctx, [], provider=provider))
    blob = "".join(emitted)
    assert "13800138000" not in blob, f"跨 delta 手机号不得泄漏（实际 {blob}）"
    assert "13800138000" not in (resp.content or "")


# ==================== b4_stream_error ====================


class ErrorStreamingProvider:
    model_name = "stub"
    protocol = "openai"

    async def stream(self, messages: list[dict], **kwargs: Any) -> Any:
        yield StreamEvent(type=StreamEventType.ERROR, error_message="connection reset by peer")


def test_stream_error_not_masked_as_safety_blocked() -> None:
    from bandi.agent.states import _stream_thinking

    ctx = ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    resp = asyncio.run(_stream_thinking(ctx, [], provider=ErrorStreamingProvider()))

    assert resp.refusal is False, (
        "stream error 不得携带 refusal（避免被 on_parsing 判 SAFETY_BLOCKED）"
    )
    assert resp.finish_reason == "error"
    assert ctx.terminal_reason == TerminalReason.MODEL_ERROR
    assert ctx.final_message == "connection reset by peer"


def test_on_parsing_keeps_model_error_not_safety_blocked() -> None:
    from bandi.agent.states import on_parsing

    ctx = ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.terminal_reason = TerminalReason.MODEL_ERROR
    ctx.final_message = "connection reset by peer"
    ctx.current_response = LLMResponse(content="", tool_calls=[], finish_reason="error")

    asyncio.run(on_parsing(ctx))

    assert ctx.terminal_reason == TerminalReason.MODEL_ERROR, (
        "on_parsing 不得把 MODEL_ERROR 覆盖为 SAFETY_BLOCKED"
    )
    assert ctx.final_message == "connection reset by peer"


def test_real_refusal_still_maps_to_safety_blocked() -> None:
    from bandi.agent.states import on_parsing

    ctx = ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.current_response = LLMResponse(
        content="", tool_calls=[], finish_reason="stop", refusal=True
    )

    asyncio.run(on_parsing(ctx))

    assert ctx.terminal_reason == TerminalReason.SAFETY_BLOCKED, "真实 refusal 仍必须判安全拒答"


# ==================== b5_done_detection ====================


class NoDoneProvider:
    model_name = "stub"
    protocol = "openai"
    supports_streaming = True

    async def stream(self, messages: list[dict], **kwargs: Any) -> Any:
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="半截内容")


class DoneProvider:
    model_name = "stub"
    protocol = "openai"

    async def stream(self, messages: list[dict], **kwargs: Any) -> Any:
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="完整内容")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")


def test_stream_without_done_marked_incomplete(caplog: logging.LogCaptureFixture) -> None:
    from bandi.agent.states import _stream_thinking

    ctx = ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    with caplog.at_level(logging.WARNING):
        resp = asyncio.run(_stream_thinking(ctx, [], provider=NoDoneProvider()))

    assert resp.finish_reason == "incomplete", "无 DONE 的流必须标记不完整（不能伪装正常 stop）"
    assert resp.content == "半截内容"
    assert any("DONE" in r.message for r in caplog.records), "必须告警 DONE 缺失"


def test_stream_with_done_keeps_finish_reason() -> None:
    from bandi.agent.states import _stream_thinking

    ctx = ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    resp = asyncio.run(_stream_thinking(ctx, [], provider=DoneProvider()))

    assert resp.finish_reason == "stop"
    assert resp.content == "完整内容"


def test_incomplete_blocks_final_artifact() -> None:
    """半截断线（无 DONE）经 on_thinking 全链 → MODEL_ERROR
    半截 content 不得进入最终产物（不再伪装正常回答）。"""
    from bandi.agent.base import TerminalReason
    from bandi.agent.states import on_thinking
    from bandi.core.message import Message, MessageRole

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="任务")],
        provider=NoDoneProvider(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.config.stream_to_ui = True

    asyncio.run(on_thinking(ctx))

    assert ctx.terminal_reason == TerminalReason.MODEL_ERROR, (
        f"incomplete 无工具轮必须 MODEL_ERROR（实际 {ctx.terminal_reason}）"
    )
    assert ctx.final_message, "必须给出失败语义 final_message（半截流不进最终产物）"
    assert "incomplete" in ctx.final_message
