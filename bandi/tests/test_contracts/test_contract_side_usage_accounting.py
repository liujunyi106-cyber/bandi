# 功能：契约——压缩/截断等旁路 LLM 调用必须计入预算、成本台账与观测事件。
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bandi.agent.states import _record_side_usage
from bandi.core.event_bus import EventBus
from bandi.core.events import EventType
from bandi.core.message import LLMResponse, TokenUsage


class _Provider:
    model_name = "m1"
    api_protocol = "openai"


class _Tracker:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_side_usage_updates_budget_and_ledger() -> None:
    tracker = _Tracker()
    bus = EventBus()
    events: list[Any] = []

    async def _capture(event: Any) -> None:
        events.append(event)

    await bus.subscribe(EventType.LLM_RESPONSE, _capture)
    ctx = SimpleNamespace(
        provider=_Provider(),
        meta={"session_id": "s1", "cost_tracker": tracker},
        bus=bus,
        turn_index=2,
        total_tokens=0,
        consumed_tokens=0,
    )
    response = LLMResponse(
        content="",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, input_tokens_total=10),
    )

    await _record_side_usage(ctx, response, "compaction")

    assert ctx.total_tokens == 15
    assert ctx.consumed_tokens == 15
    assert tracker.calls[0]["prompt_tokens"] == 10
    assert tracker.calls[0]["completion_tokens"] == 5
    assert events[-1].data["settled"] is True
    assert events[-1].data["label"] == "compaction"
