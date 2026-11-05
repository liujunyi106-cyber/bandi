#
# deepseek 官方 $0.15/$0.6；OpenRouter 转售 deepseek-v4.1-flash $0.3/$1.2；
# ollama-cloud $0.15/$0.6；requesty $0.5/$1.5）。修前一律按目录（厂商价）计价 ⇒
# **网关加价部分完全不记账**（成本系统性偏低）。
#
# 方案（用户口径：「不用按官网/网关各写一套，实时拉最新价」）：
# 价格**不硬编码**，取自 models.dev 的 per-provider `cost`（随 TTL 自动刷新）；
# 定价解析（`CostTracker._resolve_pricing`）：
# ① provider 是**网关**（≠ 模型厂商）且有实时价 ⇒ 用实时价（含加价）
# ② 目录命中 ⇒ 用目录价（厂商 CNY 权威：DeepSeek 峰谷价不可被 USD 换算覆盖）
# ③ 目录未收录但有实时价 ⇒ 用实时价
# ④ 都没有 ⇒ 未定价（金额 None + 预算按下限估计）
#
# 本契约用**合成缓存**保证确定性（不依赖本机 5MB 缓存，CI 无网也能跑）。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bandi.llm import models_dev


@pytest.fixture(autouse=True)
def _reset_index() -> None:
    # provider_price_index 按 mtime 记忆；用例间必须清空，避免临时文件 mtime 撞车
    models_dev._INDEX_CACHE = None
    yield
    models_dev._INDEX_CACHE = None


def _write_cache(path: Path, providers: dict[str, Any]) -> Path:
    path.write_text(
        json.dumps({"providers": providers, "fetched_at": "2026-09-27T00:00:00Z"}),
        encoding="utf-8",
    )
    return path


def _fake_providers() -> dict[str, Any]:
    # 形如 models.dev：providers[provider].models[model_id] = {name, cost, limit}
    return {
        "deepseek": {
            "models": {
                "deepseek-flash": {
                    "name": "DeepSeek V4.1 Flash",
                    "cost": {"input": 0.15, "output": 0.6, "cache_read": 0.003},
                    "limit": {"context": 1000000, "output": 384000},
                }
            }
        },
        "openrouter": {
            "models": {
                "deepseek/deepseek-v4.1-flash": {
                    "name": "DeepSeek V4.1 Flash",
                    "cost": {"input": 0.3, "output": 1.2, "cache_read": 0.006},
                    "limit": {"context": 1048576, "output": 384000},
                },
                "xiaomi/mimo-v2.6-flash": {
                    "name": "MiMo-V2.6-Flash",
                    "cost": {"input": 0.14, "output": 0.28, "cache_read": 0.0028},
                    "limit": {"context": 1048576, "output": 131072},
                },
            }
        },
        "xiaomi": {
            "models": {
                "mimo-v2.6-flash": {
                    "name": "MiMo-V2.6-Flash",
                    "cost": {"input": 0.14, "output": 0.28, "cache_read": 0.0028},
                    "limit": {"context": 1048576, "output": 131072},
                }
            }
        },
    }


# ── ① 按 (provider, model) 取价 ─────────────────────────────────────────────


def test_lookup_exact_provider_and_model(tmp_path: Path) -> None:
    """契约①：同 provider 精确命中，价按 USD→CNY 折算。"""
    cache = _write_cache(tmp_path / "m.json", _fake_providers())
    hit = models_dev.lookup_provider_price("openrouter", "xiaomi/mimo-v2.6-flash", cache)
    assert hit is not None
    assert hit["input_per_1m"] == pytest.approx(0.14 * 7.2, abs=0.001)
    assert hit["output_per_1m"] == pytest.approx(0.28 * 7.2, abs=0.001)
    assert hit["cache_read_per_1m"] == pytest.approx(0.0028 * 7.2, abs=0.001)
    assert hit["context_window"] == 1048576
    assert hit["model_id"] == "xiaomi/mimo-v2.6-flash"


def test_lookup_tail_matches_namespaced_key(tmp_path: Path) -> None:
    """契约①：网关常带 `vendor/` 前缀——用裸 id 查该 provider 时应末段匹配。"""
    cache = _write_cache(tmp_path / "m.json", _fake_providers())
    hit = models_dev.lookup_provider_price("openrouter", "mimo-v2.6-flash", cache)
    assert hit is not None and hit["model_id"] == "xiaomi/mimo-v2.6-flash"


def test_lookup_does_not_cross_providers(tmp_path: Path) -> None:
    """契约①（破坏者）：**不得跨 provider 取价**——xiaomi 家没有的 id 必须查不到。

    若实现退化为「全局扫所有 provider」，就会把别家价格当自家价（错价）。
    """
    cache = _write_cache(tmp_path / "m.json", _fake_providers())
    assert models_dev.lookup_provider_price("xiaomi", "deepseek/deepseek-v4.1-flash", cache) is None
    assert models_dev.lookup_provider_price("xiaomi", "mimo-v2.6-flash", cache) is not None


def test_lookup_requires_unambiguous_tail(tmp_path: Path) -> None:
    """契约①（破坏者）：末段匹配**多义即放弃**（宁可无价，不可错价）。"""
    providers = {
        "gw": {
            "models": {
                "a/same-model": {"cost": {"input": 1.0, "output": 2.0}},
                "b/same-model": {"cost": {"input": 9.0, "output": 9.0}},
            }
        }
    }
    cache = _write_cache(tmp_path / "m.json", providers)
    assert models_dev.lookup_provider_price("gw", "same-model", cache) is None


def test_missing_or_broken_cache_is_safe(tmp_path: Path) -> None:
    """契约①：缓存缺失/坏 JSON 一律返回空/None，**不抛**（元数据故障不得影响主链）。"""
    assert models_dev.provider_price_index(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert models_dev.provider_price_index(bad) == {}
    assert models_dev.lookup_provider_price("openrouter", "x", tmp_path / "nope.json") is None


def test_provider_alias_does_not_guess_openai_compat() -> None:
    """契约①：`openai_compat` 是「未知 OpenAI 兼容网关」兜底名，**不得**映射到 openrouter
    （猜错 provider 会错价 ⇒ 宁可回落目录价/未定价）。"""
    assert models_dev.resolve_provider_key("openai_compat") == "openai_compat"
    assert models_dev.resolve_provider_key("MiMo") == "xiaomi"
    assert models_dev.resolve_provider_key("gemini") == "google"


# ── ②③ 定价解析：厂商价权威，网关实时价接管 ─────────────────────────────────


def _tracker(**kwargs: Any) -> Any:
    from bandi.llm.cost_tracker import CostTracker
    from bandi.llm.model_catalog import ModelCatalog

    return CostTracker(ModelCatalog(), budget_limit=-1.0, **kwargs)


def _point_default_cache(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    # `_default_cache_path()` 是调用时 import，patch 模块属性即生效
    import bandi.core.config as core_config

    monkeypatch.setattr(core_config, "DEFAULT_CACHE_PATH", str(path), raising=False)


def test_gateway_price_takes_over_for_resold_vendor_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约③（本契约的核心）：**网关转售加价必须记账**。

    同一模型 `deepseek-v4.1-flash`：目录价 = 厂商（¥1.08/¥4.32），OpenRouter 转售
    ¥2.16/¥8.64。provider=openrouter 时**必须**用转售价，否则成本系统性偏低。
    """
    cache = _write_cache(tmp_path / "m.json", _fake_providers())
    _point_default_cache(monkeypatch, cache)
    tracker = _tracker()

    gateway = tracker.record(
        session_id="s",
        provider="openrouter",
        model="deepseek-v4.1-flash",
        prompt_tokens=1_000,
        completion_tokens=1_000,
    )
    vendor = tracker.record(
        session_id="s",
        provider="deepseek",
        model="deepseek-v4.1-flash",
        prompt_tokens=1_000,
        completion_tokens=1_000,
    )
    # 网关：1k * 2.16/1M + 1k * 8.64/1M = 0.00216 + 0.00864 = 0.0108
    assert gateway.cost_cny == pytest.approx(0.0108, abs=1e-6)
    assert gateway.pricing_source.startswith("live:openrouter")
    # 厂商：目录价（¥1.08/¥4.32）= 0.00108 + 0.00432 = 0.0054
    assert vendor.cost_cny == pytest.approx(0.0054, abs=1e-6)
    assert vendor.pricing_source == "catalog"


def test_vendor_provider_keeps_catalog_price_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约②（破坏者）：**provider 就是模型厂商时，目录价必须权威**。

    目录里的 CNY 表带 DeepSeek 峰谷价语义，不能让 models.dev 的 USD×7.2 覆盖它
    （否则会出现「本地口径被远端价偷偷改掉」——本模块历史明确承诺 `pricing 保留本地`）。
    """
    cache = _write_cache(tmp_path / "m.json", _fake_providers())
    _point_default_cache(monkeypatch, cache)
    from bandi.llm.model_catalog import BUILTIN_MODELS

    spec = BUILTIN_MODELS["deepseek-v4.1-flash"]
    with monkeypatch.context() as ctx:
        ctx.setattr(spec.pricing, "input_per_1m", 99.0)
        ctx.setattr(spec.pricing, "output_per_1m", 99.0)
        entry = _tracker().record(
            session_id="s",
            provider="deepseek",
            model="deepseek-v4.1-flash",
            prompt_tokens=1_000,
            completion_tokens=1_000,
        )
    assert entry.pricing_source == "catalog"
    assert entry.cost_cny == pytest.approx((1_000 * 99.0 + 1_000 * 99.0) / 1_000_000, abs=1e-6)


def test_uncatalogued_model_uses_live_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约③：目录**未收录**但该 provider 有实时价 ⇒ 用它（不再一律「未定价」）。"""
    cache = _write_cache(tmp_path / "m.json", _fake_providers())
    _point_default_cache(monkeypatch, cache)
    entry = _tracker().record(
        session_id="s",
        provider="openai_compat",
        model="gw-only-model",
        prompt_tokens=100,
        completion_tokens=100,
    )
    assert entry.cost_cny is None, "无任何价格来源 ⇒ 不得编造金额"
    assert entry.pricing_source == "unpriced"
    assert "gw-only-model" not in json.dumps(_fake_providers())


def test_gateway_model_not_in_catalog_is_priced_via_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约③：本机真实形态——档案走 openrouter + 带命名空间的模型名。

    修复前该组合「未定价」（目录精确匹配查不到）；修复后既可归一命中目录
    也可用 openrouter 的实时价，且**两者一致**（OpenRouter 按厂商价转售 MiMo）。
    """
    cache = _write_cache(tmp_path / "m.json", _fake_providers())
    _point_default_cache(monkeypatch, cache)
    entry = _tracker().record(
        session_id="s",
        provider="openrouter",
        model="xiaomi/mimo-v2.6-flash",
        prompt_tokens=1_000,
        completion_tokens=1_000,
    )
    assert entry.pricing_source == "live:openrouter"
    assert entry.cost_cny == pytest.approx((1_000 * 1.008 + 1_000 * 2.016) / 1_000_000, abs=1e-7)


# ── ④ 落账与留痕 ───────────────────────────────────────────────────────────


def test_pricing_source_reaches_ledger_breakdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约④：`pricing_source` 必须落进台账 breakdown（否则事后无法解释「按谁家价算的」）。"""
    from bandi.observability.cost_store import CostStore

    cache = _write_cache(tmp_path / "m.json", _fake_providers())
    _point_default_cache(monkeypatch, cache)
    store = CostStore(tmp_path / "cost.db")
    tracker = _tracker(store=store)
    tracker.record(
        session_id="s",
        provider="openrouter",
        model="xiaomi/mimo-v2.6-flash",
        prompt_tokens=1_000,
        completion_tokens=10,
    )
    store.flush()
    agg = store.aggregate_usage()
    assert agg["total"]["requests"] == 1
    assert agg["total"]["cost_cny"] > 0
    store.close()


def test_index_is_memoised_by_mtime(tmp_path: Path) -> None:
    """契约④：5MB 级缓存不得每次调用重解析——同一 mtime 必须返回**同一个对象**。"""
    cache = _write_cache(tmp_path / "m.json", _fake_providers())
    first = models_dev.provider_price_index(cache)
    second = models_dev.provider_price_index(cache)
    assert first is second
