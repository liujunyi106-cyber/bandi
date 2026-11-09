from __future__ import annotations

import threading
from typing import Any

import pytest

from bandi.agent import states
from bandi.agent.base import ExecutionContext
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.events import EventType
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage
from bandi.llm.accounted import accounted_complete, resolve_budget_tracker
from bandi.llm.cost_tracker import _MAX_COST_ENTRIES, CostTracker
from bandi.llm.mock import MockProvider
from bandi.llm.model_catalog import ModelCatalog

# 功能：契约——旁路 accounted_complete 单一计费源（直写台账后的事件不得再计一次）。
# 破坏者视角：accounted_complete 直写共享台账 + 发 LLM_RESPONSE，而同一 tracker 又是
# 该事件的订阅者（bootstrap 接线）→ 一次调用记两笔（daily_total 翻倍、预算双速）。


class _StubProvider:
    # 功能：鸭子类型旁路 provider（仅 complete，与 Verifier/Judge 等旁路一致）
    model_name = "stub-model"

    async def complete(self, messages, tools=None, **kwargs) -> LLMResponse:
        return LLMResponse(
            content="ok", usage=TokenUsage(prompt_tokens=1000, completion_tokens=500)
        )


@pytest.mark.asyncio
async def test_contract_accounted_complete_not_double_billed_with_subscribed_bus() -> None:
    """契约（单一计费源）：旁路调用经直写台账后，其 LLM_RESPONSE 观测事件不得被
    同一 tracker 再计一笔——修复前一次调用两条 entry、daily_total 翻倍。"""
    tracker = resolve_budget_tracker()
    bus = EventBus()
    await bus.subscribe(EventType.LLM_RESPONSE, tracker.on_llm_response)

    await accounted_complete(
        _StubProvider(),  # type: ignore[arg-type]
        [{"role": "user", "content": "hi"}],
        label="verifier",
        session_id="s-byass",
        bus=bus,
    )

    entries = [e for e in tracker._entries if e.session_id == "s-byass"]
    assert len(entries) == 1, f"旁路调用必须只记一笔，实际 {len(entries)} 笔（双计费）"


@pytest.mark.asyncio
async def test_contract_accounted_complete_observability_event_still_emitted() -> None:
    """契约（观测不牺牲）：修票不得以「不发事件」为代价——LLM_RESPONSE 观测事件
    仍必须发出（trace/metrics 订阅方依赖），计数责任由事件消费方豁免。"""
    tracker = resolve_budget_tracker()
    bus = EventBus()
    await bus.subscribe(EventType.LLM_RESPONSE, tracker.on_llm_response)
    seen: list[dict] = []

    async def observer(event) -> None:
        seen.append(event.data)

    await bus.subscribe(EventType.LLM_RESPONSE, observer)

    await accounted_complete(
        _StubProvider(),  # type: ignore[arg-type]
        [{"role": "user", "content": "hi"}],
        label="judge",
        session_id="s-obs",
        bus=bus,
    )

    assert seen, "LLM_RESPONSE 观测事件必须仍发出"
    assert seen[0].get("usage", {}).get("prompt_tokens") == 1000


# ==================== stream_usage_single_count ====================
# 功能：契约——流式路径 LLM usage 必须只计一次（ctx 计数与 LLM_RESPONSE 事件都不得双份）。
# 破坏者视角：_stream_thinking 与 on_thinking 各自累计同一 usage → token 预算双速消耗
# 成本台账翻倍（默认流式路径每次调用必现）。


def _provider_with_usage(prompt_tokens: int, completion_tokens: int) -> MockProvider:
    provider = MockProvider()
    provider.count_tokens = lambda text: max(1, len(text or "") // 4) if text else 0
    provider.add_response(
        LLMResponse(
            content="回答",
            usage=TokenUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
            finish_reason="stop",
        )
    )
    return provider


def _ctx(provider: MockProvider, *, stream_to_ui: bool) -> ExecutionContext:
    return ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="你好")],
        provider=provider,
        config=AgentConfig(stream_to_ui=stream_to_ui),
        bus=EventBus(),
    )


@pytest.mark.asyncio
async def test_contract_stream_usage_counted_once() -> None:
    """契约（计费正确性）：流式路径 usage 在 ctx 累计与 LLM_RESPONSE 事件都只能
    一次——修复前 _stream_thinking 与 on_thinking 各累计一次（token 双计
    成本台账翻倍、预算闸门双速耗尽）。"""
    ctx = _ctx(_provider_with_usage(1000, 500), stream_to_ui=True)
    events: list[Any] = []

    async def collector(event: Any) -> None:
        events.append(event)

    await ctx.bus.subscribe(EventType.LLM_RESPONSE, collector)
    await states.on_thinking(ctx)

    assert ctx.total_tokens == 1500, f"流式 usage 必须只计一次，实际 {ctx.total_tokens}"
    assert ctx.consumed_tokens == 1500, f"consumed 必须只计一次，实际 {ctx.consumed_tokens}"
    assert len(events) == 1, f"LLM_RESPONSE 事件必须恰一条，实际 {len(events)}"


@pytest.mark.asyncio
async def test_contract_nonstream_usage_counted_once() -> None:
    """契约（口径对齐）：非流式路径同口径单计一次（流式修复不得反向影响）。"""
    ctx = _ctx(_provider_with_usage(1000, 500), stream_to_ui=False)
    events: list[Any] = []

    async def collector(event: Any) -> None:
        events.append(event)

    await ctx.bus.subscribe(EventType.LLM_RESPONSE, collector)
    await states.on_thinking(ctx)

    assert ctx.total_tokens == 1500
    assert ctx.consumed_tokens == 1500
    assert len(events) == 1


# ==================== b1_cost_tracker ====================
# 功能：契约——成本追踪器的并发安全与内存有界（并发工程 批次1 / ）
#
# `CostTracker._entries` 是**无锁的 list 且永不回收**——长进程 + 多 Agent 并发
# 记录时，① 多线程写入存在竞态（丢更新 / list 内部状态不一致）；② 内存只涨不降。
# 本契约钉死：并发写入不丢记录 / 条目数有界且丢最旧 / 快照返回副本（外部改不污染
# 内部）/ 读写并发安全。
# 注：**刻意不改成"聚合 dict"**——`:319` 依赖 `_entries[-1]` 做 tool 调用归因打标


def _tracker(max_entries: int = _MAX_COST_ENTRIES) -> CostTracker:
    return CostTracker(catalog=ModelCatalog(), budget_limit=-1.0, max_entries=max_entries)


def _record(tracker: CostTracker, session_id: str, n: int = 1) -> None:
    for _ in range(n):
        tracker.record(
            session_id=session_id,
            provider="test",
            model="test-model",
            prompt_tokens=10,
            completion_tokens=5,
        )


def test_entries_are_bounded_dropping_oldest() -> None:
    """条目数受 max_entries 约束，超出后丢最旧、保最新（原实现无界）。"""
    tracker = _tracker(max_entries=5)
    for i in range(12):
        _record(tracker, f"s{i}")

    entries = tracker.entries
    assert len(entries) == 5, "条目数必须收敛到 max_entries"
    assert entries[-1].session_id == "s11", "最新记录必须保留"
    assert all(e.session_id != "s0" for e in entries), "最旧记录应被丢弃"


def test_concurrent_record_does_not_lose_entries() -> None:
    """并发写入不得丢记录（原实现无锁，list 追加虽原子但跨日重置判定有竞态）。"""
    tracker = _tracker()
    threads_n, per_thread = 8, 100
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            _record(tracker, f"s{idx}", per_thread)
        except BaseException as exc:  # noqa: BLE001 — 收集后统一断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发记录抛出异常：{errors[:3]}"
    assert len(tracker.entries) == threads_n * per_thread


def test_entries_snapshot_is_isolated_copy() -> None:
    """**破坏者视角**：`entries` 必须返回副本——外部清空不得污染内部状态。"""
    tracker = _tracker()
    _record(tracker, "s1", 3)

    snapshot = tracker.entries
    snapshot.clear()

    assert len(tracker.entries) == 3, "外部改动不得影响内部记录"


def test_daily_total_survives_concurrency() -> None:
    """并发累加后 daily_total 与实际条数一致（不得因丢更新而低估花费）。"""
    tracker = _tracker()
    threads_n, per_thread = 6, 50
    threads = [
        threading.Thread(target=_record, args=(tracker, f"s{i}", per_thread))
        for i in range(threads_n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(tracker.entries) == threads_n * per_thread
    assert tracker.check_budget() is True


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_attribution_survives_concurrent_entries_reset() -> None:
    """🟢-5：record 与归因块之间被并发 reset 清空 entries → 不得 IndexError。"""
    from bandi.core.events import Event, EventType

    tracker = _tracker()
    await tracker.on_tool_done(
        Event(
            type=EventType.TOOL_COMPLETED,
            data={"tool_call_id": "t1", "session_id": "s1"},
        )
    )

    original_record = tracker.record

    def _record_then_reset(*args: object, **kwargs: object) -> object:
        entry = original_record(*args, **kwargs)  # type: ignore[arg-type]
        tracker.reset_daily()  # 模拟并发窗口：归因块执行前 entries 被清空
        return entry

    tracker.record = _record_then_reset  # type: ignore[method-assign, assignment]

    await tracker.on_llm_response(
        Event(
            type=EventType.LLM_RESPONSE,
            data={
                "provider": "p",
                "model": "m",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "session_id": "s1",
            },
        )
    )
