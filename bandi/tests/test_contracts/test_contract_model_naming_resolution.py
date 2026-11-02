#
#
# ① 同一模型两种端点命名。DeepSeek-.1-Flash：
# - api.deepseek.com 官方现役名 = ``deepseek-flash``
# - 网关命名 = ``deepseek-v4.1-flash``（OpenRouter `deepseek/deepseek-v4.1-flash`
#
# ② 网关以「vendor/model」暴露模型，目录键是厂商自有 id。
# OpenRouter 文档：「On OpenRouter they carry the ``xiaomi/`` prefix」
# 其 ``xiaomi/mimo-v2.6-flash`` 的厂商自有 id 即 ``mimo-v2.6-flash``（目录键）。
# ``ModelCatalog.get`` 原为精确匹配 ⇒ 带前缀的名字永远查不到 ⇒
# 「未定价」+ 上下文窗口回落 ``DEFAULT_CONTEXT_WINDOW``(128K) ⇒ **过早压缩**。
#
# ③ 上下文窗口必须**跟随模型目录动态解析**：``AgentConfig.context_window = 0`` 是
# 「未显式配置」哨兵；显式值只收紧不放松。目的：1M 模型按 1M 的 85%
# 触发压缩、**只有 200K 窗口的模型就按 200K 的 85%**，而不是一律钉在 128K。

from __future__ import annotations

from typing import Any

import pytest

from bandi.core.config import DEFAULT_CONTEXT_WINDOW, AgentConfig, BandiConfig, LLMConfig


def _cfg(model: str, window: int = 0) -> BandiConfig:
    # 功能：构造真实 BandiConfig（走生产装配同款校验），窗口 0 = 未显式配置哨兵
    return BandiConfig(llm=LLMConfig(default_model=model), agent=AgentConfig(context_window=window))


# ── ① 两个命名都在目录内且定价一致 ──────────────────────────────────────────────


def test_both_v41_naming_variants_are_catalogued() -> None:
    """契约①：官方命名与网关命名都必须收录（同一模型，两个端点各认一个名）。"""
    from bandi.llm.model_catalog import BUILTIN_MODELS

    official = BUILTIN_MODELS.get("deepseek-flash")
    gateway = BUILTIN_MODELS.get("deepseek-v4.1-flash")
    assert official is not None, "官方现役名 deepseek-flash 必须在目录内"
    assert gateway is not None, "网关命名 deepseek-v4.1-flash 必须在目录内（否则网关用户未定价）"


def test_both_v41_naming_variants_are_equally_priced() -> None:
    """契约①（破坏者视角）：两个命名必须同价同窗——否则按名字不同算出不同账单。

    若有人「顺手」把其中一个的定价改了（例如把别名当成退役旧模型价 1.584/4.752）
    这条会立刻红。
    """
    from bandi.llm.model_catalog import BUILTIN_MODELS

    official = BUILTIN_MODELS["deepseek-flash"]
    gateway = BUILTIN_MODELS["deepseek-v4.1-flash"]
    assert gateway.pricing.input_per_1m == official.pricing.input_per_1m
    assert gateway.pricing.output_per_1m == official.pricing.output_per_1m
    assert gateway.provider == official.provider == "deepseek"
    assert gateway.context_window == official.context_window == 1_000_000
    assert gateway.supports_vision and official.supports_vision, "V4.1-Flash 原生多模态"


def test_fallback_chain_is_user_configured_only() -> None:
    """契约①（2026-10-09 拍板修订）：降级链**只来自用户配置**，产品不预设。

    原契约「默认链同时含两个命名」已随「本地产品不预设降级」作废——模板不含该字段
    ⇒ 默认值生效；预设跨厂商链会让每个用户白烧注定打不通的请求。双命名解析能力本身
    仍由目录解析覆盖（见本文件上方两命名同源与下方网关命名归一条）。
    """
    assert LLMConfig().fallback_chain == [], "产品不得预设降级链"
    assert LLMConfig(fallback_chain=["deepseek-v4.1-flash", "deepseek-flash"]).fallback_chain == [
        "deepseek-v4.1-flash",
        "deepseek-flash",
    ], "用户显式配置的链必须原样保留（机制未删除）"


# ── ② 网关命名空间归一 ────────────────────────────────────────────────────────


def test_gateway_namespaced_name_resolves_to_vendor_id() -> None:
    """契约②：`vendor/model` 必须归一到厂商自有 id（OpenRouter 的 mimo 就是活例）。"""
    from bandi.llm.model_catalog import ModelCatalog

    catalog = ModelCatalog()
    bare = catalog.get("mimo-v2.6-flash")
    namespaced = catalog.get("xiaomi/mimo-v2.6-flash")
    assert bare is not None, "厂商自有 id 必须在目录内"
    assert namespaced is bare, "带网关前缀的名字必须归一到同一个 ModelSpec 对象"
    assert namespaced.context_window == 1_048_576, "MiMo-V2.6-Flash 窗口 1M（OpenRouter 实测）"
    assert namespaced.pricing.input_per_1m == pytest.approx(0.14 * 7.2, abs=0.001)
    assert namespaced.pricing.output_per_1m == pytest.approx(0.28 * 7.2, abs=0.001)


def test_namespaced_deepseek_name_resolves() -> None:
    """契约②：OpenRouter 的 `deepseek/deepseek-v4.1-flash` 同样必须归一命中。"""
    from bandi.llm.model_catalog import ModelCatalog

    assert ModelCatalog().get("deepseek/deepseek-v4.1-flash") is not None


def test_namespace_normalisation_does_not_misresolve() -> None:
    """契约②（破坏者）：归一只能「去掉前缀再查」，**不得**退化成子串/模糊匹配。

    `mimo-v2.6-flash/extra` 的最后一段是 `extra` ⇒ 必须查不到；
    未知名字的带前缀形式也必须是 None（否则会把任意请求错配到某个模型
    定价与窗口随之全错——比「未定价」更危险）。
    """
    from bandi.llm.model_catalog import ModelCatalog

    catalog = ModelCatalog()
    assert catalog.get("mimo-v2.6-flash/extra") is None
    assert catalog.get("some-vendor/definitely-not-a-real-model") is None
    assert catalog.get("definitely-not-a-real-model") is None


def test_exact_match_takes_priority_over_suffix() -> None:
    """契约②：精确命中优先（若目录里真的存在带斜杠的键，不得被归一截走）。"""
    from bandi.llm.model_catalog import ModelCatalog, ModelPricing, ModelSpec

    probe = ModelSpec(
        name="vendor/probe",
        provider="openai_compat",
        context_window=1234,
        pricing=ModelPricing(1.0, 2.0),
    )
    catalog = ModelCatalog(models={"vendor/probe": probe})
    assert catalog.get("vendor/probe") is probe, "精确键必须原样命中"


# ── ③ 上下文窗口动态解析（不钉死）────────────────────────────────────────────


def test_window_follows_catalog_when_not_explicitly_configured() -> None:
    """契约③：窗口 0（哨兵）⇒ 跟随目录。1M 模型按 1M 触发压缩，而非 128K。"""
    from bandi.agent.factory import resolve_context_window

    window = resolve_context_window(_cfg("deepseek-v4.1-flash", 0))
    assert window == 1_000_000, f"窗口应为目录的 1M，实际 {window}（被钉死在默认值？）"
    assert window * 0.85 > 800_000, "压缩应在 ~850K 触发，而非 ~109K"


def test_small_window_model_compresses_at_its_own_85_percent() -> None:
    """契约③（用户要求的口径）：**只有 200K 窗口的模型，就在 200K 的 85% 触发**。

    已收录小窗模型按目录窗口解析：o3（目录 200K）+ 配置 500K ⇒ 仍 200K；
    压缩点 170K——小窗模型的压缩水位由它自己的真实窗口决定，不因配置被放大。
    """
    from bandi.agent.factory import resolve_context_window

    window = resolve_context_window(_cfg("o3", 500_000))
    assert window == 200_000, "已收录小窗模型按目录窗口（不因配置放大）"
    assert window * 0.85 == 170_000


def test_window_follows_smallest_catalog_model_dynamically() -> None:
    """契约③：动态跟随要对**目录里最小的窗口**也成立（钉死 128K 的受害面最大处）。"""
    from bandi.agent.factory import resolve_context_window
    from bandi.llm.model_catalog import BUILTIN_MODELS

    smallest = min(BUILTIN_MODELS.values(), key=lambda s: s.context_window)
    window = resolve_context_window(_cfg(smallest.name, 0))
    assert window == smallest.context_window, (
        f"{smallest.name} 目录窗口 {smallest.context_window}，实际解析 {window}"
    )


def test_explicit_window_larger_than_catalog_is_clamped() -> None:
    """契约③（破坏者）：显式值**只收紧不放松**——配 5M 不得把 1M 模型放大到 5M。"""
    from bandi.agent.factory import resolve_context_window

    assert resolve_context_window(_cfg("deepseek-v4.1-flash", 5_000_000)) == 1_000_000


def test_window_falls_back_to_default_for_unknown_model() -> None:
    """契约③：目录未收录且未显式配置 ⇒ 回落安全默认（不猜、不退化）。"""
    from bandi.agent.factory import resolve_context_window

    assert resolve_context_window(_cfg("no-such-model-anywhere", 0)) == DEFAULT_CONTEXT_WINDOW


def test_gateway_named_profile_model_gets_real_window() -> None:
    """契约②+③（端到端）：**本机生效档案的真实形态**——带前缀的网关模型名。

    修复前：`xiaomi/mimo-v2.6-flash` 查不到 ⇒ 窗口回落 128K（1M 模型过早压缩）+ 未定价。
    修复后：归一到 `mimo-v2.6-flash` ⇒ 1M 窗口。
    """
    from bandi.agent.factory import resolve_context_window

    window = resolve_context_window(_cfg("xiaomi/mimo-v2.6-flash", 0))
    assert window == 1_048_576, f"网关命名模型窗口应跟随目录 1M，实际 {window}"


def _unused(_: Any) -> None:
    # 说明：本文件全部用例都走生产函数（resolve_context_window / ModelCatalog.get）
    # 未使用 SimpleNamespace 伪造 domain 对象（历史假绿高发模式）。
    return None
