"""
契约族：trace 成本拆解字段（七-）。

背景：CostEntry 只有 provider/model/prompt_tokens/completion_tokens/cost_cny/
meta——无法回答「system prompt 花多少、history 多少、cache 命中多少、哪次
尝试、预估 vs 实际差多少」。审计要求每次 LLM call 记录 13 个拆解字段。

本契约钉的行为族（生产调用路径）：
- `llm/cost_tracker.py::CostEntry`（拆解字段载体）
- `llm/cost_tracker.py::CostTracker.record`（透传拆解字段）
- `llm/cost_tracker.py::CostTracker.on_llm_response`（LLM_RESPONSE 事件提取）
- `agent/states.py` LLM_RESPONSE 事件 data（生产发出点——由 契约钉字段契约
  事件构造处扩展见 states.py 修改）

断言「最终产物」：CostEntry 含全部 13 拆解字段；record 透传生效；
LLM_RESPONSE 事件 data 携带拆解字段且 on_llm_response 落库；
无拆解字段的旧事件向后兼容（默认值）。
"""

from __future__ import annotations

import asyncio

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.llm.cost_tracker import CostTracker
from bandi.llm.model_catalog import ModelCatalog

_BREAKDOWN_KEYS = [
    "trace_id",
    "turn_id",
    "attempt_id",
    "prompt_hash",
    "system_tokens",
    "history_tokens",
    "memory_tokens",
    "tool_schema_tokens",
    "input_tokens_estimated",
    "input_tokens_reported",
    "output_tokens",
    "cache_tokens",
    "reason",
]


def _tracker() -> CostTracker:
    return CostTracker(catalog=ModelCatalog(), budget_limit=-1)


def test_contract_cost_entry_has_all_breakdown_fields() -> None:
    """CostEntry 必须含审计要求的 13 个拆解字段（默认值向后兼容）。"""
    entry = _tracker().record(
        session_id="s1",
        provider="openai",
        model="gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
    )
    for key in _BREAKDOWN_KEYS:
        assert hasattr(entry, key), f"CostEntry 缺拆解字段 {key}"
    assert entry.trace_id == "" and entry.turn_id == 0 and entry.reason == "", (
        "未提供拆解字段时必须保持默认值（旧事件向后兼容）"
    )


def test_contract_record_persists_breakdown_fields() -> None:
    """record 必须把拆解字段透传到 CostEntry（值级验证）。"""
    entry = _tracker().record(
        session_id="s1",
        provider="openai",
        model="gpt-4o",
        prompt_tokens=100,
        completion_tokens=50,
        breakdown={
            "trace_id": "trace-1",
            "turn_id": 3,
            "attempt_id": 2,
            "prompt_hash": "abc123",
            "system_tokens": 200,
            "history_tokens": 400,
            "memory_tokens": 50,
            "tool_schema_tokens": 120,
            "input_tokens_estimated": 780,
            "input_tokens_reported": 770,
            "output_tokens": 50,
            "cache_tokens": 300,
            "reason": "length_recovery",
        },
    )
    assert entry.trace_id == "trace-1"
    assert entry.turn_id == 3 and entry.attempt_id == 2
    assert entry.prompt_hash == "abc123"
    assert entry.system_tokens == 200 and entry.history_tokens == 400
    assert entry.memory_tokens == 50 and entry.tool_schema_tokens == 120
    assert entry.input_tokens_estimated == 780 and entry.input_tokens_reported == 770
    assert entry.output_tokens == 50 and entry.cache_tokens == 300
    assert entry.reason == "length_recovery"


def test_contract_llm_response_event_breakdown_flows_to_entry() -> None:
    """LLM_RESPONSE 事件 data 携带拆解字段 → on_llm_response 必须落库到 CostEntry。"""
    tracker = _tracker()
    bus = EventBus()
    asyncio.run(bus.subscribe(EventType.LLM_RESPONSE, tracker.on_llm_response))
    asyncio.run(
        bus.emit(
            Event(
                type=EventType.LLM_RESPONSE,
                data={
                    "provider": "openai",
                    "model": "gpt-4o",
                    "session_id": "s1",
                    "turn": 2,
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                    "prompt_hash": "h-1",
                    "system_tokens": 200,
                    "history_tokens": 400,
                    "input_tokens_estimated": 780,
                    "input_tokens_reported": 100,
                    "output_tokens": 50,
                    "cache_tokens": 0,
                    "reason": "normal",
                },
            )
        )
    )
    entry = tracker.entries[-1]
    assert entry.turn_id == 2, f"turn_id 必须从事件 data.turn 透传，实际 {entry.turn_id}"
    assert entry.prompt_hash == "h-1"
    assert entry.system_tokens == 200 and entry.history_tokens == 400
    assert entry.input_tokens_estimated == 780
    assert entry.input_tokens_reported == 100 and entry.output_tokens == 50
    assert entry.reason == "normal"
