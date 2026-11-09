# 功能：契约 — 缓存计量口径统一
# ① openai_compat 只读 OpenAI 的 prompt_tokens_details.cached_tokens，而 DeepSeek
# 的命中字段是 usage 顶层 prompt_cache_hit_tokens ⇒ 默认模型 deepseek-chat 下
# 命中数**恒 0**（不报错、静默）——命中率不可观测（阻塞点）；
# ② 未跟踪 Anthropic cache_creation_input_tokens（账单按 1.计费）；
# ③ **命中率分母口径因协议而异**：OpenAI/DeepSeek/Gemini 的 prompt_tokens 已含命中
# 部分；Anthropic 的 input_tokens **不含**已缓存部分 ⇒ 用 input_tokens 作分母
# 契约：① 三协议均正确解析命中/写入；② input_tokens_total 口径正确；
# ③ cache_hit_rate 恒在 [0,1]（含「命中远大于原生 input」的破坏者用例）；
# ④ 聚合层用归一总量作分母、旧数据（无 breakdown）可回落。

from __future__ import annotations

from typing import Any

import pytest

from bandi.core.message import TokenUsage
from bandi.llm.adapters.anthropic import _usage_from_raw as anthropic_usage
from bandi.llm.adapters.google import _usage_from_raw as google_usage
from bandi.llm.adapters.openai_compat import _usage_from_raw as openai_usage
from bandi.observability.cost_store import _aggregate_rows

# ── ① 三协议解析 ───────────────────────────────────────────────────────


def test_openai_cached_tokens_parsed() -> None:
    """契约①：OpenAI 形态（prompt_tokens_details.cached_tokens）。"""
    usage = openai_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 640},
        }
    )
    assert usage.cache_read_tokens == 640
    assert usage.input_tokens_total == 1000, "OpenAI 的 prompt_tokens 已含命中部分 ⇒ 即总量"


def test_deepseek_top_level_hit_tokens_parsed() -> None:
    """契约①（本地缺陷回归）：DeepSeek 的 usage 顶层 prompt_cache_hit_tokens
    必须被解析——此前只认 OpenAI 字段 ⇒ 默认模型下命中数恒 0。"""
    usage = openai_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 5,
            "prompt_cache_hit_tokens": 896,
            "prompt_cache_miss_tokens": 104,
        }
    )
    assert usage.cache_read_tokens == 896, "DeepSeek 命中字段不得被静默忽略（恒 0）"
    assert usage.input_tokens_total == 1000


def test_deepseek_hit_miss_only_gateway() -> None:
    """契约①：少数网关只回 hit/miss 两字段——总量需自行相加（不得为 0）。"""
    usage = openai_usage({"prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200})
    assert usage.prompt_tokens == 1000
    assert usage.input_tokens_total == 1000
    assert usage.cache_hit_rate == pytest.approx(0.8)


def test_anthropic_creation_and_normalised_total() -> None:
    """契约①②：Anthropic 必须解析 cache_creation_input_tokens，且总量 =
    input + read + creation（input_tokens 本身**不含**已缓存部分）。"""
    usage = anthropic_usage(
        {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_read_input_tokens": 6400,
            "cache_creation_input_tokens": 2048,
        }
    )
    assert usage.cache_read_tokens == 6400
    assert usage.cache_creation_tokens == 2048
    assert usage.input_tokens_total == 120 + 6400 + 2048


def test_google_total_is_prompt_tokens() -> None:
    """契约②：Gemini 的 promptTokenCount 为总输入（cachedContentTokenCount 是其子集）。"""
    usage = google_usage(
        {"promptTokenCount": 900, "candidatesTokenCount": 12, "cachedContentTokenCount": 512}
    )
    assert usage.cache_read_tokens == 512
    assert usage.input_tokens_total == 900


# ── ② 命中率口径不越界（破坏者视角）────────────────────────────────────


def test_hit_rate_never_exceeds_one_for_anthropic() -> None:
    """契约③（破坏者）：Anthropic 行「命中 >> 原生 input」是常态（缓存命中时
    input_tokens 只剩几百）——用 input_tokens 作分母会算出 6400/120≈53 倍。
    必须用归一总量，且钳制 [0,1]。"""
    usage = anthropic_usage(
        {"input_tokens": 120, "output_tokens": 1, "cache_read_input_tokens": 6400}
    )
    naive = usage.cache_read_tokens / usage.prompt_tokens  # 53.3x —— 错误口径
    assert naive > 1.0, "本用例前提：naive 口径确实越界"
    assert 0.0 <= usage.cache_hit_rate <= 1.0, "归一后命中率必须落在 [0,1]"
    assert usage.cache_hit_rate == pytest.approx(6400 / 6520, rel=1e-6)


def test_hit_rate_edge_cases() -> None:
    """契约③：零 token 不得除零；负值/越界被钳制。"""
    assert TokenUsage().cache_hit_rate == 0.0
    assert TokenUsage(prompt_tokens=0, cache_read_tokens=0).cache_hit_rate == 0.0
    assert TokenUsage(prompt_tokens=10, cache_read_tokens=-5).cache_hit_rate == 0.0
    assert TokenUsage(prompt_tokens=10, cache_read_tokens=999).cache_hit_rate == 1.0
    assert TokenUsage(input_tokens_total=200, cache_read_tokens=50).cache_hit_rate == 0.25


# ── ③ 聚合层 ───────────────────────────────────────────────────────────


def _row(**kw: Any) -> dict[str, Any]:
    base = {
        "model": "deepseek-chat",
        "agent_role": "user",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
        "total_cost_cny": 0.0,
        "breakdown": None,
    }
    base.update(kw)
    return base


def test_aggregate_uses_normalised_total_as_denominator() -> None:
    """契约④：Anthropic 行必须用 breakdown.input_tokens_total 作分母。"""
    import json as _json

    rows = [
        _row(
            model="claude-sonnet",
            input_tokens=120,
            cache_tokens=6400,
            breakdown=_json.dumps({"input_tokens_total": 6520}),
        )
    ]
    agg = _aggregate_rows(rows)
    assert agg["total"]["input_tokens"] == 6520, "分母必须取归一总量而非原生 input_tokens"
    assert 0.0 < agg["total"]["cache_hit_rate"] <= 1.0


def test_aggregate_falls_back_when_breakdown_missing_or_malformed() -> None:
    """契约④：旧数据无 breakdown / JSON 损坏时回落 input_tokens，且不得抛。"""
    rows = [
        _row(input_tokens=500, cache_tokens=250, breakdown=None),
        _row(input_tokens=500, cache_tokens=250, breakdown="{ not json"),
        _row(input_tokens=500, cache_tokens=250, breakdown={"input_tokens_total": 0}),
    ]
    agg = _aggregate_rows(rows)
    assert agg["total"]["input_tokens"] == 1500
    assert agg["total"]["cache_read_tokens"] == 750
    assert agg["total"]["cache_hit_rate"] == pytest.approx(0.5)


def test_aggregate_groups_by_model_and_role() -> None:
    """契约④：按模型与角色分组，且各组命中率独立计算（多 Agent 成本归因的数据源）。"""
    import json as _json

    rows = [
        _row(
            model="m-a",
            agent_role="dev",
            input_tokens=100,
            cache_tokens=90,
            total_cost_cny=0.01,
            breakdown=_json.dumps({"input_tokens_total": 100}),
        ),
        _row(
            model="m-b",
            agent_role="dev",
            input_tokens=100,
            cache_tokens=10,
            total_cost_cny=0.02,
            breakdown=_json.dumps({"input_tokens_total": 100}),
        ),
        _row(
            model="m-a",
            agent_role="",
            input_tokens=50,
            cache_tokens=0,
            breakdown=_json.dumps({"input_tokens_total": 50, "cache_creation_tokens": 30}),
        ),
    ]
    agg = _aggregate_rows(rows)
    assert set(agg["by_model"]) == {"m-a", "m-b"}
    assert agg["by_model"]["m-a"]["requests"] == 2
    assert agg["by_model"]["m-a"]["cache_read_tokens"] == 90
    assert agg["by_model"]["m-a"]["cache_creation_tokens"] == 30
    assert agg["by_role"]["dev"]["requests"] == 2
    assert agg["by_role"]["unassigned"]["requests"] == 1, "空角色归 unassigned"
    assert agg["total"]["cost_cny"] == pytest.approx(0.03)
    assert agg["total"]["requests"] == 3


def test_aggregate_empty_rows_is_safe() -> None:
    """契约④：空台账不得崩（新装即打开设置页的场景）。"""
    agg = _aggregate_rows([])
    assert agg["total"]["requests"] == 0
    assert agg["total"]["cache_hit_rate"] == 0.0
    assert agg["by_model"] == {} and agg["by_role"] == {}


# ── ④ 主链接线：事件 data → CostTracker → 台账 breakdown ────────────────


def test_main_path_cache_fields_reach_ledger_breakdown(tmp_path: Any) -> None:
    """契约④（主链接线）：`states.py` 的 LLM_RESPONSE 事件顶层键（与 usage 并列的
    cache_tokens / cache_creation_tokens / input_tokens_total）必须经
    `CostTracker.on_llm_response` 落到台账 breakdown。

    手法：真实 `CostTracker` + 真实 `CostStore`（生产装配），事件 data 的键形状与
    `states.py` 发射点一致。上游「发射点确实带这些键」由同批代码改动保证
    本条钉的是**消费端不再丢字段**（此前只读 cache_tokens，两个新字段会被静默丢弃）。
    """
    from bandi.llm.cost_tracker import CostEntry, CostTracker
    from bandi.llm.model_catalog import ModelCatalog
    from bandi.observability.cost_store import CostStore

    store = CostStore(tmp_path / "cost.db")
    # 目录必须真实（record() 内 self._catalog.get(model) 无 None 兜底）
    tracker = CostTracker(ModelCatalog(), budget_limit=-1.0, store=store)
    captured: list[CostEntry] = []
    original = tracker.record

    def _spy(**kwargs: Any) -> CostEntry:
        entry = original(**kwargs)
        captured.append(entry)
        return entry

    tracker.record = _spy  # type: ignore[method-assign]

    event = type(
        "E",
        (),
        {
            "event_id": "evt-main-path-1",
            "data": {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "session_id": "s-1",
                "agent_role": "dev",
                "agent_id": "dev@s-1",
                "usage": {"prompt_tokens": 1000, "completion_tokens": 40},
                "cache_tokens": 900,
                "cache_creation_tokens": 0,
                "input_tokens_total": 1000,
                "reason": "",
            },
        },
    )()
    asyncio_run(tracker.on_llm_response(event))

    assert captured, "事件未被计费（接线断了）"
    entry = captured[0]
    assert entry.cache_tokens == 900, "命中量必须落账"
    assert entry.input_tokens_total == 1000, "归一总输入必须落账（缺失会让命中率分母回退）"
    assert entry.cache_creation_tokens == 0
    assert entry.agent_role == "dev", "角色必须落账（前端 by_role 依赖）"
    assert entry.agent_id == "dev@s-1", "agent 实例必须落账（同角色并发归因）"
    store.close()


def asyncio_run(coro: Any) -> Any:
    # 功能：同步执行协程（避免为本条契约引入 pytest-asyncio 事件循环依赖）
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)
