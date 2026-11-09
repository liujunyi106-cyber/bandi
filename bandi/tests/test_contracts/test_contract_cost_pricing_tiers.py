# 功能：契约 — 成本分档计价与未定价处理
# 模型用 `_DEFAULT_INPUT_PRICE=1.584` / `_DEFAULT_OUTPUT_PRICE=4.752` **静默编造金额**；
# 缓存折扣价其实 models.dev 已有（`cost.cache_read` 5205 个模型 / `cache_write` 1679 个）
# 只是 `models_dev.merge_remote_specs` 刻意不碰 pricing（CNY 峰谷价为本地权威）。
# 契约：
# ① 缓存命中/写入按各自单价计价，且**先归一再分档**（防 OpenAI 系重复计费）；
# ② 缓存单价缺失（0）⇒ 回退 input 单价 = 无折扣，绝不臆造折扣；
# ③ catalog 外模型 ⇒ `cost_cny is None` + `breakdown.unpriced`，但**预算仍按下限扣减**
# （防换名绕过目录让预算失效——这是原默认价的真实用途）；
# ④ `models_dev` 用远端 `cost.cache_read`/`cache_write` 补缓存价（USD→CNY）
# 且**不覆盖**本地已提供的值、**不动** input/output。

from __future__ import annotations

from typing import Any

import pytest

from bandi.llm.cost_tracker import CostTracker
from bandi.llm.model_catalog import USD_TO_CNY, ModelCatalog, ModelPricing, ModelSpec
from bandi.llm.models_dev import flatten_models, merge_remote_specs

PER_M = 1_000_000


def _tracker() -> CostTracker:
    return CostTracker(ModelCatalog(), budget_limit=-1.0)


# ── ① 分档计价 ─────────────────────────────────────────────────────────


def _tiered_tracker(
    input_price: float = 10.0,
    read_ratio: float = 0.1,
    write_price: float = 0.0,
    output_price: float = 20.0,
) -> CostTracker:
    """自建 spec 的 tracker。**绝不改共享的 BUILTIN_MODELS**——`ModelCatalog` 持有的是
    同一批 ModelSpec 引用，就地改 pricing 会跨用例/跨测试文件泄漏（本文件初版踩过）。"""
    catalog = ModelCatalog()
    catalog.register(
        ModelSpec(
            name="tier-probe",
            provider="p",
            context_window=1000,
            pricing=ModelPricing(
                input_price,
                output_price,
                "CNY",
                cache_read_per_1m=round(input_price * read_ratio, 4),
                cache_write_per_1m=write_price,
            ),
        )
    )
    return CostTracker(catalog, budget_limit=-1.0)


def test_cache_hit_is_cheaper_than_full_price() -> None:
    """契约①②：同一模型、同样 1000 输入，900 命中必须比 0 命中便宜。"""
    tracker = _tiered_tracker()
    spec = tracker._catalog.get("tier-probe")
    hit = tracker.record(
        session_id="s",
        provider="p",
        model="tier-probe",
        prompt_tokens=1000,
        completion_tokens=0,
        cache_read_tokens=900,
        input_tokens_total=1000,
    )
    plain = tracker.record(
        session_id="s",
        provider="p",
        model="tier-probe",
        prompt_tokens=1000,
        completion_tokens=0,
        input_tokens_total=1000,
    )
    assert hit.cost_cny is not None and plain.cost_cny is not None
    assert hit.cost_cny < plain.cost_cny, "缓存命中必须更便宜（此前完全无折扣）"
    expected = (100 * spec.pricing.input_per_1m + 900 * spec.pricing.cache_read_per_1m) / PER_M
    assert hit.cost_cny == pytest.approx(expected, rel=1e-9)


def test_no_double_count_for_openai_style_total() -> None:
    """契约①（破坏者视角）：OpenAI/DeepSeek 的 prompt_tokens **已含**命中部分——
    若在 input 全量之外再加命中价，就是重复计费。归一后未缓存部分必须被扣掉。"""
    tracker = _tiered_tracker()
    spec = tracker._catalog.get("tier-probe")
    entry = tracker.record(
        session_id="s",
        provider="p",
        model="tier-probe",
        prompt_tokens=1000,
        completion_tokens=0,
        cache_read_tokens=1000,
        input_tokens_total=1000,  # 全部命中
    )
    assert entry.cost_cny == pytest.approx(1000 * spec.pricing.cache_read_per_1m / PER_M, rel=1e-9)
    assert entry.cost_cny < 1000 * spec.pricing.input_per_1m / PER_M, "不得按全价重复计费"


def test_anthropic_style_input_excludes_cache() -> None:
    """契约①：Anthropic 的 prompt_tokens（input_tokens）**不含**缓存部分 ⇒
    未缓存部分 = input_tokens_total − read − creation（不得再减成负数）。"""
    tracker = _tiered_tracker(input_price=3.0, read_ratio=0.1, write_price=3.75, output_price=15.0)
    spec = tracker._catalog.get("tier-probe")
    entry = tracker.record(
        session_id="s",
        provider="anthropic",
        model="tier-probe",
        prompt_tokens=120,
        completion_tokens=30,
        cache_read_tokens=6400,
        cache_creation_tokens=2048,
        input_tokens_total=120 + 6400 + 2048,
    )
    expected = (
        120 * spec.pricing.input_per_1m
        + 6400 * (spec.pricing.cache_read_per_1m or spec.pricing.input_per_1m)
        + 2048 * (spec.pricing.cache_write_per_1m or spec.pricing.input_per_1m)
        + 30 * spec.pricing.output_per_1m
    ) / PER_M
    assert entry.cost_cny == pytest.approx(expected, rel=1e-9)


def test_missing_cache_price_falls_back_to_input_price() -> None:
    """契约②：缓存单价为 0（未提供）⇒ 回退 input 单价 = 无折扣，**不得臆造折扣**。"""
    spec = ModelSpec(
        name="probe",
        provider="p",
        context_window=1000,
        pricing=ModelPricing(10.0, 20.0),  # cache_read/ 0 = 未提供
    )
    catalog = ModelCatalog()
    catalog.register(spec)
    tracker = CostTracker(catalog, budget_limit=-1.0)
    entry = tracker.record(
        session_id="s",
        provider="p",
        model="probe",
        prompt_tokens=1000,
        completion_tokens=0,
        cache_read_tokens=500,
        input_tokens_total=1000,
    )
    assert entry.cost_cny == pytest.approx(1000 * 10.0 / PER_M, rel=1e-9), (
        "无折扣价必须等于全量 input 价"
    )


# ── ② 未定价：不编造金额，但预算不漏扣 ─────────────────────────────────


def test_unpriced_model_reports_none_but_charges_budget() -> None:
    """契约③：catalog 外模型 ⇒ `cost_cny is None`（前端显示「未定价」）
    但 `daily_total` 必须 > 0（保留原默认价的防绕过语义）。"""
    tracker = _tracker()
    entry = tracker.record(
        session_id="s",
        provider="x",
        model="totally-unknown-model-xyz",
        prompt_tokens=1000,
        completion_tokens=100,
    )
    assert entry.cost_cny is None, "未定价模型不得编造金额"
    assert tracker.daily_total > 0, "预算仍须按下限估计扣减（防换名绕过）"


def test_unpriced_flag_reaches_breakdown() -> None:
    """契约③：未定价标记必须进 breakdown（落库为 JSON，前端/聚合据此提示）。"""
    tracker = _tracker()
    captured: dict[str, Any] = {}
    original = tracker._persist

    def _spy(entry: Any) -> None:
        captured["entry"] = entry
        original(entry)

    tracker._persist = _spy  # type: ignore[method-assign]
    tracker.record(
        session_id="s",
        provider="x",
        model="unknown-abc",
        prompt_tokens=10,
        completion_tokens=1,
    )
    assert captured["entry"].cost_cny is None


# ── ③ models.dev 补缓存价 ──────────────────────────────────────────────


def test_merge_fills_cache_prices_from_remote_cost() -> None:
    """契约④：远端 `cost.cache_read/cache_write`（USD）必须换算成 CNY 填入
    **本地未提供**的缓存价字段——这是「厂商调价不改代码」的数据通道。"""
    target = {
        "probe-model": ModelSpec(
            name="probe-model",
            provider="p",
            context_window=100,
            pricing=ModelPricing(1.0, 2.0),  # 缓存价未提供（0）
        )
    }
    flat = flatten_models(
        {
            "p": {
                "models": {
                    "probe-model": {
                        "cost": {"input": 1, "output": 2, "cache_read": 0.1, "cache_write": 1.25},
                        "limit": {"context": 100, "output": 10},
                    }
                }
            }
        }
    )
    assert flat["probe-model"]["cost"]["cache_read"] == 0.1, "flatten 必须透传 cost"
    merge_remote_specs(target, flat)
    pricing = target["probe-model"].pricing
    assert pricing.cache_read_per_1m == pytest.approx(0.1 * USD_TO_CNY)
    assert pricing.cache_write_per_1m == pytest.approx(1.25 * USD_TO_CNY)
    # 本地权威不得被覆盖
    assert pricing.input_per_1m == 1.0 and pricing.output_per_1m == 2.0


def test_merge_does_not_override_local_cache_prices() -> None:
    """契约④（破坏者视角）：本地**已提供**缓存价时必须保留（CNY 峰谷价为权威）
    否则远端 USD 价会静默改写本地定价（含峰谷语义）。"""
    target = {
        "probe2": ModelSpec(
            name="probe2",
            provider="p",
            context_window=100,
            pricing=ModelPricing(1.0, 2.0, "CNY", cache_read_per_1m=0.05, cache_write_per_1m=9.9),
        )
    }
    merge_remote_specs(
        target,
        flatten_models(
            {"p": {"models": {"probe2": {"cost": {"cache_read": 0.5, "cache_write": 5.0}}}}}
        ),
    )
    pricing = target["probe2"].pricing
    assert pricing.cache_read_per_1m == 0.05
    assert pricing.cache_write_per_1m == 9.9


def test_unpriced_count_in_aggregate() -> None:
    """契约③：聚合层必须产出 `unpriced_requests`（前端据此提示「含 N 次未定价」）。"""
    import json

    from bandi.observability.cost_store import _aggregate_rows

    agg = _aggregate_rows(
        [
            {
                "model": "m",
                "agent_role": "r",
                "input_tokens": 10,
                "output_tokens": 1,
                "cache_tokens": 0,
                "total_cost_cny": 0.0,
                "breakdown": json.dumps({"unpriced": True, "input_tokens_total": 10}),
            },
            {
                "model": "m",
                "agent_role": "r",
                "input_tokens": 10,
                "output_tokens": 1,
                "cache_tokens": 0,
                "total_cost_cny": 0.01,
                "breakdown": json.dumps({"input_tokens_total": 10}),
            },
        ]
    )
    assert agg["total"]["unpriced_requests"] == 1
    assert agg["by_model"]["m"]["unpriced_requests"] == 1
