# 功能：旁路 LLM 调用统一计费契约测试（accounted_complete 行为族钉）
# 破坏者视角：Verifier/Judge/幻觉检查/文档工具等旁路直调 provider.complete()
# 游离于统一计费/预算/观测之外——契约：任何旁路调用必须 ① 走预算闸门
# ② 进 CostTracker 台账 ③ 可选事件/配额透传，且不改变调用方语义。

import asyncio

import pytest

from bandi.core.config import LLMConfig
from bandi.core.event_bus import EventBus
from bandi.core.events import EventType
from bandi.core.message import LLMResponse, TokenUsage
from bandi.llm.accounted import BudgetExhaustedError, accounted_complete
from bandi.llm.base import BaseProvider
from bandi.llm.cost_tracker import get_shared_cost_tracker, reset_shared_cost_tracker
from bandi.llm.model_catalog import ModelCatalog


class ProbeProvider(BaseProvider):
    """探针 Provider：捕获 kwargs 透传 + 返回带 usage 的响应。"""

    def __init__(self) -> None:
        super().__init__(LLMConfig())
        self.captured_kwargs: dict | None = None
        self.call_count = 0

    @property
    def model_name(self) -> str:
        return "probe-model"

    @property
    def protocol(self) -> str:
        return "openai"

    @property
    def api_protocol(self) -> str:
        return "openai"

    async def complete(self, messages, tools=None, **kwargs):
        self.captured_kwargs = kwargs
        self.call_count += 1
        return LLMResponse(
            content="ok",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )

    async def stream(self, messages, tools=None, **kwargs):
        yield  # pragma: no cover - 探针不需要流式


@pytest.fixture(autouse=True)
def _reset_tracker():
    reset_shared_cost_tracker()
    yield
    reset_shared_cost_tracker()


def test_budget_exhausted_rejects_call() -> None:
    """预算耗尽必须拒绝调用（provider 不得被触达）——fail-closed。"""
    get_shared_cost_tracker(ModelCatalog(), budget_limit=0.0)
    probe = ProbeProvider()
    with pytest.raises(BudgetExhaustedError):
        asyncio.run(accounted_complete(probe, [{"role": "user", "content": "hi"}], label="probe"))
    assert probe.call_count == 0, "预算耗尽时 provider 不得被触达"


def test_records_cost_without_bus() -> None:
    """无总线时直写共享 CostTracker——旁路调用必须进台账。"""
    tracker = get_shared_cost_tracker(ModelCatalog(), budget_limit=-1.0)
    probe = ProbeProvider()
    resp = asyncio.run(
        accounted_complete(
            probe, [{"role": "user", "content": "hi"}], label="probe", session_id="sess-1"
        )
    )
    assert resp.content == "ok"
    entries = tracker.entries
    assert len(entries) == 1, "旁路调用必须记录一条成本条目"
    e = entries[0]
    assert e.session_id == "sess-1"
    assert e.prompt_tokens == 10 and e.completion_tokens == 5
    assert e.model == "probe-model"


def test_kwargs_forwarded_to_provider() -> None:
    """max_tokens/temperature 等调用参数必须透传——不改变调用方语义。"""
    get_shared_cost_tracker(ModelCatalog(), budget_limit=-1.0)
    probe = ProbeProvider()
    asyncio.run(
        accounted_complete(
            probe,
            [{"role": "user", "content": "hi"}],
            label="probe",
            max_tokens=800,
            temperature=0.0,
        )
    )
    assert probe.captured_kwargs == {"max_tokens": 800, "temperature": 0.0}


def test_emits_events_when_bus_provided() -> None:
    """提供总线时发 LLM_REQUEST/LLM_RESPONSE 事件（观测桥接订阅方消费）
    台账由直写唯一通道记账——事件仅观测，不承载计费、不重复。"""
    bus = EventBus()
    seen: list[EventType] = []

    async def _collector(event) -> None:
        seen.append(event.type)

    asyncio.run(bus.subscribe(EventType.LLM_REQUEST, _collector))
    asyncio.run(bus.subscribe(EventType.LLM_RESPONSE, _collector))
    tracker = get_shared_cost_tracker(ModelCatalog(), budget_limit=-1.0)
    probe = ProbeProvider()

    asyncio.run(
        accounted_complete(probe, [{"role": "user", "content": "hi"}], label="probe", bus=bus)
    )
    assert EventType.LLM_REQUEST in seen
    assert EventType.LLM_RESPONSE in seen
    assert len(tracker.entries) == 1, "直写为唯一计费通道，事件不重复计费"


def test_no_usage_no_record_no_error() -> None:
    """usage 缺失（部分 provider 不回传用量）时调用仍成功、不抛错、不记条。"""

    class NoUsageProvider(ProbeProvider):
        async def complete(self, messages, tools=None, **kwargs):
            self.call_count += 1
            return LLMResponse(content="ok")

    tracker = get_shared_cost_tracker(ModelCatalog(), budget_limit=-1.0)
    resp = asyncio.run(
        accounted_complete(NoUsageProvider(), [{"role": "user", "content": "hi"}], label="probe")
    )
    assert resp.content == "ok"
    assert tracker.entries == []
