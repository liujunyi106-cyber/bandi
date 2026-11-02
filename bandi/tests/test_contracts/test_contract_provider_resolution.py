# 功能：契约——provider 解析：显式 base_url 必须生效（含目录内模型）+ 缓存键含凭证。
# 破坏者视角：① 目录内模型（deepseek-v4-flash 等）映射官方 provider 类，类内写死
# 官方域名 → 用户显式 llm.base_url（私有网关）被静默忽略；② 缓存键不含 api_key →
# 同模型同网关不同员工凭证串用实例（计费/权限事故）。
from __future__ import annotations

import httpx
import pytest

from bandi.core.config import BandiConfig
from bandi.llm.registry import ProviderRegistry

_GW = "https://private-gw.example/v1"


def _cfg(*, key: str = "sk-a", base_url: str = _GW, provider: str = "") -> BandiConfig:
    cfg = BandiConfig()
    cfg.llm.default_model = "deepseek-v4-flash"
    cfg.llm.provider = provider
    cfg.llm.api_key = key
    cfg.llm.base_url = base_url
    return cfg


def _capture_transport(captured: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_contract_explicit_base_url_wins_for_catalog_model() -> None:
    """契约（配置生效·破坏者）：目录内模型 + 显式 llm.base_url → 请求必须打向
    用户配置的网关（修复前 DeepSeekProvider 写死官方域名，base_url 被静默忽略）。"""
    cfg = _cfg()
    provider = ProviderRegistry(cfg).get_provider("deepseek-v4-flash", cfg)
    captured: dict = {}
    provider._client = httpx.AsyncClient(transport=_capture_transport(captured))
    try:
        await provider.complete([{"role": "user", "content": "hi"}])
    finally:
        await provider.close()

    assert "private-gw.example" in captured.get("url", ""), (
        f"显式 base_url 必须生效，实际请求 {captured.get('url')!r}"
    )


@pytest.mark.asyncio
async def test_contract_explicit_provider_with_base_url_still_wins() -> None:
    """契约（同族）：显式 llm.provider="deepseek" + 显式 base_url（私有网关走
    deepseek 协议）——base_url 同样必须生效。"""
    cfg = _cfg(provider="deepseek")
    provider = ProviderRegistry(cfg).get_provider("deepseek-v4-flash", cfg)
    captured: dict = {}
    provider._client = httpx.AsyncClient(transport=_capture_transport(captured))
    try:
        await provider.complete([{"role": "user", "content": "hi"}])
    finally:
        await provider.close()

    assert "private-gw.example" in captured.get("url", ""), (
        f"显式 provider + 显式 base_url 时 base_url 必须生效，实际 {captured.get('url')!r}"
    )


def test_contract_provider_cache_key_includes_credentials() -> None:
    """契约（凭证隔离·破坏者）：同模型同 base_url、不同 api_key 的两次解析必须
    返回不同实例（修复前缓存键无凭证 → B 员工调用复用 A 员工 key 的实例）。"""
    reg = ProviderRegistry(_cfg(key="sk-a"))

    p_a = reg.get_provider("deepseek-v4-flash", _cfg(key="sk-a"))
    p_b = reg.get_provider("deepseek-v4-flash", _cfg(key="sk-b"))
    p_a2 = reg.get_provider("deepseek-v4-flash", _cfg(key="sk-a"))

    assert p_a is not p_b, "不同凭证不得共享 provider 实例（凭证串用）"
    assert p_a2 is p_a, "同配置必须复用实例（缓存语义保持）"


# ── 跨 provider 链候选端点隔离（BC-002 残）─────────────────────────────────


def _profile_cfg(*, provider: str, base_url: str, key: str = "sk-profile") -> BandiConfig:
    """模拟「激活档案解析后」的 config（with_resolved_llm 的效果）。"""
    cfg = BandiConfig()
    cfg.llm.default_model = "hub-flash"
    cfg.llm.provider = provider
    cfg.llm.api_key = key
    cfg.llm.base_url = base_url
    return cfg


def test_cross_provider_candidate_does_not_inherit_profile_endpoint() -> None:
    """契约（BC-002 残·破坏者）：链候选归属 provider ≠ 激活档案 provider 时，
    不得继承档案 base_url（修复前 xiaomi/mimo 候选被劫持到档案端点——
    「首个端点失败即锁定」的真实机制）。"""
    cfg = _profile_cfg(provider="openai_compat", base_url="https://private-hub.example/v1")
    reg = ProviderRegistry(cfg)
    provider = reg.get_provider_for_model("xiaomi/mimo-v2.6-flash", cfg)
    base = getattr(provider, "_base_url", "") or ""
    assert "private-hub.example" not in base, (
        f"跨 provider 候选不得继承档案端点，实际 {base!r}（修复前恒为档案端点）"
    )


def test_gateway_namespace_model_resolves_to_catalog_provider() -> None:
    """契约（破坏者）：网关命名空间模型（vendor/name）按末段归一命中目录归属——
    `xiaomi/mimo-v2.6-flash` → 目录 `mimo-v2.6-flash`（provider=mimo）。"""
    cfg = _profile_cfg(provider="openai_compat", base_url="https://private-hub.example/v1")
    reg = ProviderRegistry(cfg)
    provider = reg.get_provider_for_model("xiaomi/mimo-v2.6-flash", cfg)
    cfg_obj = getattr(provider, "config", None) or getattr(provider, "_config", None)
    provider_name = str(getattr(cfg_obj, "provider", "") or "")
    assert provider_name == "mimo", f"末段归一应命中 mimo，实际 {provider_name!r}"


def test_same_provider_candidate_keeps_profile_endpoint() -> None:
    """护栏：链候选归属 provider == 档案 provider 时，档案 base_url 照常生效
    （显式私有网关语义不回归）。"""
    cfg = _profile_cfg(provider="deepseek", base_url=_GW)
    reg = ProviderRegistry(cfg)
    provider = reg.get_provider_for_model("deepseek-flash", cfg)
    captured: dict = {}
    provider._client = httpx.AsyncClient(transport=_capture_transport(captured))
    import asyncio

    async def _call() -> None:
        try:
            await provider.complete([{"role": "user", "content": "hi"}])
        finally:
            await provider.close()

    asyncio.run(_call())
    assert "private-gw.example" in captured.get("url", ""), (
        f"同 provider 候选必须沿用档案端点，实际 {captured.get('url')!r}"
    )


def test_cross_provider_candidate_uses_env_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约：跨 provider 候选端点经 per-provider 环境变量配置
    （`{PROVIDER}_BASE_URL` / `{PROVIDER}_API_KEY`）。"""
    monkeypatch.setenv("MIMO_BASE_URL", "https://mimo-gw.example/v1")
    monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
    cfg = _profile_cfg(provider="openai_compat", base_url="https://private-hub.example/v1")
    reg = ProviderRegistry(cfg)
    provider = reg.get_provider_for_model("xiaomi/mimo-v2.6-flash", cfg)
    base = getattr(provider, "_base_url", "") or ""
    assert base == "https://mimo-gw.example/v1", f"env 端点应生效，实际 {base!r}"
    cfg_obj = getattr(provider, "config", None) or getattr(provider, "_config", None)
    assert str(getattr(cfg_obj, "api_key", "") or "") == "sk-mimo"
