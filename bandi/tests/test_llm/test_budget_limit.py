from bandi.llm.cost_tracker import CostTracker
from bandi.llm.model_catalog import ModelCatalog


def _catalog() -> ModelCatalog:
    return ModelCatalog()


def test_cost_tracker_uses_config_budget_limit():
    """
    agent_runner 硬编码 -1，config 里的 budget_limit 从未生效。"""
    tracker = CostTracker(catalog=_catalog(), budget_limit=0.5)
    assert tracker._budget_limit == 0.5, "budget_limit 必须透传进 tracker"


def test_check_budget_blocks_over_limit():
    tracker = CostTracker(catalog=_catalog(), budget_limit=0.3)
    assert tracker.check_budget() is True
    # 记录一次大额调用（deepseek-v4-pro 输入 token → 0.475 CNY）
    tracker.record("s1", "deepseek", "deepseek-v4-pro", prompt_tokens=100_000, completion_tokens=0)
    assert tracker.daily_total >= 0.4
    assert tracker.check_budget() is False, "超过预算必须拦截"


def test_check_budget_unlimited_when_negative():
    tracker = CostTracker(catalog=_catalog(), budget_limit=-1)
    tracker.record(
        "s1", "deepseek", "deepseek-v4-pro", prompt_tokens=10_000_000, completion_tokens=0
    )
    assert tracker.check_budget() is True, "-1 表示不限制预算"


def test_check_budget_self_heals_across_days():
    """深度复审修复：昨天超预算 → 今天首次 check_budget 必须自愈（重置当日）——"""
    import time

    tracker = CostTracker(catalog=_catalog(), budget_limit=0.3)
    tracker.record("s1", "deepseek", "deepseek-v4-pro", prompt_tokens=100_000, completion_tokens=0)
    assert tracker.daily_total >= 0.4
    assert tracker.check_budget() is False, "当日超预算拦截"
    # 模拟跨日：把最后一条记录时间戳拨到昨天
    tracker._entries[-1].timestamp = time.time() - 86400 * 2
    assert tracker.check_budget() is True, "跨日后必须自愈（重置当日累计）"


def test_unknown_model_charged_default_price():
    """
    绕过预算（预算永不超）。"""
    tracker = CostTracker(catalog=_catalog(), budget_limit=0.3)
    tracker.record(
        "s1",
        "openai_compat",
        "custom-model-not-in-catalog",
        prompt_tokens=300_000,
        completion_tokens=0,
    )
    assert tracker.daily_total > 0, "catalog 外模型必须产生成本（防换名绕过预算）"
    assert tracker.check_budget() is False, "默认价下超预算必须拦截"
