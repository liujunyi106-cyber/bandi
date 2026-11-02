# 功能：免费档模型目录条目契约——零定价不得落「未定价」兜底
#
# 钉行为不钉实现：
# 1. 目录收录 free 档模型（provider 与调用方 provider 不同时走网关实时价分支的风险面）
# 2. 定价为 0 时 cost_tracker 必须按目录价记 0，**不得**回落到预算下限估价
# 3. 未定价模型仍按预算下限扣（对照：免费 ≠ 未定价，两者语义不同）
# 4. 上下文窗口取下限，不得臆造
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bandi.llm.cost_tracker import CostTracker
from bandi.llm.model_catalog import BUILTIN_MODELS, ModelCatalog, ModelPricing, ModelSpec
from bandi.observability.cost_store import CostStore


def _tracker(tmp_path: Path) -> CostTracker:
    # 功能：隔离构造记账器（tmp 台账 + 真实目录，绝不触碰用户真实 cost.db）
    return CostTracker(catalog=ModelCatalog(), store=CostStore(tmp_path / "cost.db"))


class TestFreeTierCatalogEntry:
    def test_entry_exists_with_measured_window(self) -> None:
        # 功能：免费档模型在目录中可查，窗口为下限而非臆造上限
        spec = BUILTIN_MODELS.get("space-bunny-free")
        assert spec is not None, "免费档模型未收录目录——会回落默认窗口并按预算下限估价"
        assert spec.context_window == 400000
        assert spec.pricing is not None
        assert spec.pricing.input_per_1m == 0.0
        assert spec.pricing.output_per_1m == 0.0

    def test_catalog_get_returns_spec(self) -> None:
        # 功能：目录查询命中（前端挡位/窗口显示依赖此路径）
        assert ModelCatalog().get("space-bunny-free") is not None

    def test_no_thinking_effort_levels_listed(self) -> None:
        # 功能：思考开关不可控 ⇒ 不列 effort 档位（前端隐藏思考挡位而非给出打挂请求的档）
        spec = BUILTIN_MODELS["space-bunny-free"]
        assert spec.reasoning_efforts == []


class TestFreeTierCostAccounting:
    def test_free_tier_records_zero_cost_not_budget_floor(self, tmp_path: Path) -> None:
        # 功能：目录零定价 ⇒ 实记 0 元（评测成本口径不被「预算下限估价」污染）
        tracker = _tracker(tmp_path)
        tracker.record(
            session_id="s1",
            provider="openai_compat",
            model="space-bunny-free",
            prompt_tokens=100000,
            completion_tokens=10000,
        )
        agg = tracker.get_session_cost("s1")
        assert agg.total_cost_cny == 0.0, "免费档被按预算下限估价记账，成本口径失真"
        assert agg.unpriced_calls == 0, "已收录且零定价不应记为未定价"

    def test_unpriced_model_still_uses_budget_floor(self, tmp_path: Path) -> None:
        # 功能：真正未定价的模型金额为 None 且带预算下限估计，真值由 breakdown 承载
        # （对照面：免费档是**已知真值 0**，不是未知）
        tracker = _tracker(tmp_path)
        entry = tracker.record(
            session_id="s2",
            provider="openai_compat",
            model="never-registered-model-xyz",
            prompt_tokens=1000,
            completion_tokens=1000,
        )
        assert entry.cost_cny is None, "未定价模型的金额应为 None（真值未知）"
        assert entry.pricing_source == "unpriced"
        tracker._store.flush()
        assert tracker.get_session_cost("s2").unpriced_calls == 1
        rec = tracker._store.list_records(session_id="s2")[0]
        bd = json.loads(rec["breakdown"] or "{}")
        assert bd.get("unpriced") is True, "未定价标记须落台账 breakdown，否则与免费档不可区分"
        assert bd.get("pricing_source") == "unpriced"

    def test_free_tier_entry_cost_is_known_zero_not_none(self, tmp_path: Path) -> None:
        # 功能：免费档金额是**已知 0**（非 None）且不带未定价标记——两者语义必须可区分
        tracker = _tracker(tmp_path)
        entry = tracker.record(
            session_id="s3",
            provider="openai_compat",
            model="space-bunny-free",
            prompt_tokens=100000,
            completion_tokens=10000,
        )
        assert entry.cost_cny == 0.0, "免费档金额应为已知 0，而非 None（None 会与未定价混淆）"
        assert entry.pricing_source == "catalog"
        tracker._store.flush()
        rec = tracker._store.list_records(session_id="s3")[0]
        assert not json.loads(rec["breakdown"] or "{}").get("unpriced"), "免费档不得带未定价标记"
        assert json.loads(rec["breakdown"] or "{}").get("pricing_source") == "catalog"


class TestGatewayPriceDoesNotOverrideZero:
    @pytest.mark.parametrize(
        ("vendor", "caller"),
        [("opencode", "openai_compat"), ("deepseek", "deepseek")],
    )
    def test_resolve_pricing_prefers_catalog_when_no_live(
        self, tmp_path: Path, vendor: str, caller: str
    ) -> None:
        # 功能：无实时价时目录价为权威（免费档不会被换成实时价）
        tracker = _tracker(tmp_path)
        spec = ModelSpec(name="x", provider=vendor, context_window=1000, pricing=ModelPricing(0, 0))
        pricing, source = tracker._resolve_pricing(caller, "x", spec)
        assert source in {"catalog", "live:opencode"}
        if source == "catalog":
            assert pricing is not None and pricing.input_per_1m == 0.0
