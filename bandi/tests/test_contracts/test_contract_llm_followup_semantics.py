# 功能：契约——llm 域复查加固：能力层凭证同源 / provider 不劫持目录模型 /
# Anthropic thinking budget 下限 / 退避真实分类与逐次重试。
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from bandi.core.config import BandiConfig
from bandi.llm.registry import ProviderRegistry

# ---------- 能力层凭证同源 ----------


def test_contract_capability_credentials_same_source_as_request(monkeypatch) -> None:
    """契约（凭证同源·破坏者）：仅设 OPENAI_API_KEY（请求侧 BandiConfig 不认）
    时能力层不得判可用——修复前能力层认该 env，与请求侧双源矛盾（假可用放行
    未配凭证模型）。"""
    monkeypatch.delenv("BANDI_LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-only")
    from bandi.llm.capabilities.adapters import _provider_credentials_available

    assert _provider_credentials_available("openai") is False, (
        "OPENAI_API_KEY 不被请求侧读取——能力层不得据此判可用（同源要求）"
    )


def test_contract_capability_credentials_bandi_env_available(monkeypatch) -> None:
    """契约（正向）：BANDI_LLM_API_KEY（请求侧同源凭证）设置时判可用。"""
    monkeypatch.setenv("BANDI_LLM_API_KEY", "sk-bandi")
    from bandi.llm.capabilities.adapters import _provider_credentials_available

    assert _provider_credentials_available("openai") is True


# ---------- 显式 provider 不劫持目录归属模型 ----------


def _cfg(*, provider: str) -> BandiConfig:
    cfg = BandiConfig()
    cfg.llm.default_model = "deepseek-v4-flash"
    cfg.llm.provider = provider
    cfg.llm.api_key = "sk"
    return cfg


def test_contract_fallback_resolution_ignores_explicit_family_provider() -> None:
    """契约（降级链协议正确性·破坏者）：链项（模型名）按目录归属解析 provider——
    显式 provider=deepseek 时 gpt-5-mini 链项仍解析到 OpenAIProvider（修复前
    劫持 deepseek 端点，跨厂商必 400）。"""
    cfg = _cfg(provider="deepseek")
    provider = ProviderRegistry(cfg).get_provider_for_model("gpt-5-mini", cfg)
    assert type(provider).__name__ == "OpenAIProvider", (
        f"降级链项必须按目录归属解析，实际 {type(provider).__name__}"
    )


def test_contract_explicit_family_provider_precedence_preserved() -> None:
    """契约（显式语义保留）：get_provider 对显式 provider 的优先行为不变——
    用户显式指定（含热切换）必须生效。"""
    cfg = _cfg(provider="deepseek")
    provider = ProviderRegistry(cfg).get_provider("gpt-5-mini", cfg)
    assert type(provider).__name__ == "DeepSeekProvider"


def test_contract_explicit_compat_provider_still_takes_precedence() -> None:
    """契约（通用协议显式指定）：provider=openai_compat（中转站场景）经 get_provider
    必须优先——目录内模型也走兼容端点。"""
    cfg = _cfg(provider="openai_compat")
    provider = ProviderRegistry(cfg).get_provider("deepseek-v4-flash", cfg)
    assert type(provider).__name__ == "OpenAICompatProvider"


def test_contract_explicit_family_provider_serves_unmapped_models() -> None:
    """契约（显式兜底）：目录外模型 + 显式 provider=deepseek → DeepSeekProvider
    （get_provider 与 get_provider_for_model 同兜底）。"""
    cfg = _cfg(provider="deepseek")
    p1 = ProviderRegistry(cfg).get_provider("totally-private-model", cfg)
    p2 = ProviderRegistry(cfg).get_provider_for_model("totally-private-model", cfg)
    assert type(p1).__name__ == "DeepSeekProvider"
    assert type(p2).__name__ == "DeepSeekProvider"


def test_contract_catalog_mapping_regression() -> None:
    """契约（回归）：无显式 provider 时目录模型按目录归属（deepseek）。"""
    cfg = _cfg(provider="")
    provider = ProviderRegistry(cfg).get_provider("deepseek-v4-flash", cfg)
    assert type(provider).__name__ == "DeepSeekProvider"


# ---------- Anthropic thinking budget 下限 ----------


def _anthropic_client(captured: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_contract_anthropic_small_budget_omits_thinking() -> None:
    """契约（Anthropic 下限·破坏者）：max_tokens=1024 → budget=512 < 1024（官方
    下限）——必须不发 thinking（修复前发出必 400 且不识别为超长错误）。"""
    from bandi.llm.adapters.anthropic import anthropic_complete

    captured: dict = {}
    client = _anthropic_client(captured)
    try:
        await anthropic_complete(
            client,
            "k",
            "claude-x",
            [{"role": "user", "content": "hi"}],
            max_tokens=1024,
            thinking_mode="enabled",
        )
    finally:
        await client.aclose()
    assert "thinking" not in captured["body"], (
        f"budget < 1024 时不得发送 thinking，实际 {captured['body'].get('thinking')!r}"
    )


@pytest.mark.asyncio
async def test_contract_anthropic_normal_budget_thinking_sent() -> None:
    """契约（正向）：max_tokens=4096 → thinking.budget_tokens == 2048。"""
    from bandi.llm.adapters.anthropic import anthropic_complete

    captured: dict = {}
    client = _anthropic_client(captured)
    try:
        await anthropic_complete(
            client,
            "k",
            "claude-x",
            [{"role": "user", "content": "hi"}],
            max_tokens=4096,
            thinking_mode="enabled",
        )
    finally:
        await client.aclose()
    assert captured["body"].get("thinking") == {"type": "enabled", "budget_tokens": 2048}


# ---------- 退避分类（真实异常）与逐次重试 ----------


def _http_429() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.test/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)


def test_contract_classify_httpx_429_is_backoff() -> None:
    """契约（分类真实化·破坏者）：httpx 429（生产真实异常）必须分类为 backoff——
    修复前只认 openai 类名，httpx 异常全落 UNKNOWN（默认 retry=切候选，无退避）。"""
    from bandi.llm.strategy import FallbackEngine

    action = FallbackEngine([])._classify(_http_429())
    assert action.action == "backoff", f"429 应退避，实际 {action.action}"


def test_contract_classify_httpx_connect_error_is_fallback() -> None:
    """契约（连接错误）：httpx ConnectError → fallback（切候选）。"""
    from bandi.llm.strategy import FallbackEngine

    action = FallbackEngine([])._classify(httpx.ConnectError("boom"))
    assert action.action == "fallback"


def test_contract_classify_unknown_defaults_to_fallback_not_retry() -> None:
    """契约（默认不落死名）：未知异常 → fallback（切候选）——修复前默认 'retry'
    是未实现分支名（call_with_fallback 无对应处理，等同 continue 但语义欺骗）。"""
    from bandi.llm.strategy import FallbackEngine

    action = FallbackEngine([])._classify(RuntimeError("weird"))
    assert action.action == "fallback"


@pytest.mark.asyncio
async def test_contract_backoff_retries_same_provider_max_times(monkeypatch) -> None:
    """契约（逐次退避语义）：429 时对同一 provider 退避重试最多 max_retries 次
    （修复前「连睡 1+2+4 后仅重试一次」）。"""
    from bandi.llm.strategy import FallbackEngine, ProviderChainExhaustedError

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("bandi.llm.strategy.asyncio.sleep", _no_sleep)

    calls: list[str] = []

    async def original_call(messages: list[dict], cfg: dict) -> Any:
        calls.append(cfg["provider"])
        raise _http_429()

    engine = FallbackEngine(["p1"])
    with pytest.raises(ProviderChainExhaustedError):
        await engine.call_with_fallback([], {"provider": "p0"}, original_call)

    assert calls == ["p1"] * 4, f"初始 1 次 + 退避重试 3 次，实际 {calls}"
