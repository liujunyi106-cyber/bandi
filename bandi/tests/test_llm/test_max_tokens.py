import pytest


class _Resp:
    def __init__(self, json_data: dict) -> None:
        self._json = json_data
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._json


class _CaptureClient:
    def __init__(self) -> None:
        self.captured: dict | None = None
        self._resp = _Resp(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}}
        )

    async def post(self, url: str, json: dict, headers: dict, timeout: int) -> _Resp:
        self.captured = json
        return self._resp


@pytest.mark.asyncio
async def test_openai_complete_body_includes_max_tokens():
    """
    config.max_output_tokens 从未进入请求）。"""
    from bandi.llm.adapters.openai_compat import openai_complete

    client = _CaptureClient()
    await openai_complete(
        client,
        base_url="https://api.example.com/v1",
        api_key="k",
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=1234,
    )
    assert client.captured is not None
    assert client.captured["max_tokens"] == 1234, "body 必须携带 config.max_output_tokens"


@pytest.mark.asyncio
async def test_anthropic_complete_uses_passed_max_tokens():
    from unittest.mock import AsyncMock

    from bandi.llm.adapters.anthropic import anthropic_complete

    client = AsyncMock()
    client.post.return_value = _Resp(
        {
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )
    await anthropic_complete(
        client,
        api_key="k",
        model="claude",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=5678,
    )
    _, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["max_tokens"] == 5678, "anthropic body max_tokens 必须用传入值而非默认 4096"


@pytest.mark.asyncio
async def test_http_provider_passes_max_tokens_to_adapter():
    from bandi.core.config import LLMConfig
    from bandi.llm.providers._http_provider import HttpAdapterProvider

    seen: dict = {}

    async def fake_complete(client, api_key, model, messages, tools=None, **kw):
        seen.update(kw)
        from bandi.core.message import LLMResponse, TokenUsage

        return LLMResponse(content="x", usage=TokenUsage(1, 1))

    class P(HttpAdapterProvider):
        _adapter_complete = staticmethod(fake_complete)
        _provider_label = "test"

    provider = P(LLMConfig(default_model="m", max_output_tokens=9999, api_key="k"))
    await provider.complete([{"role": "user", "content": "hi"}])
    assert seen.get("max_tokens") == 9999, (
        f"provider 必须透传 config.max_output_tokens，实际 {seen.get('max_tokens')}"
    )
    await provider.close()


def _adapter_provider_cls():
    from bandi.llm.providers._http_provider import HttpAdapterProvider

    seen: dict = {}

    async def fake_complete(client, api_key, model, messages, tools=None, **kw):
        seen.update(kw)
        from bandi.core.message import LLMResponse, TokenUsage

        return LLMResponse(content="x", usage=TokenUsage(1, 1))

    class P(HttpAdapterProvider):
        _adapter_complete = staticmethod(fake_complete)
        _provider_label = "test"

    return P, seen


@pytest.mark.asyncio
async def test_http_provider_honors_per_call_max_tokens_override():
    """契约（per-call 预算覆盖）：调用方显式传 max_tokens（如蒸馏 8192 预算）时必须
    优先采纳，不得被 config.max_output_tokens 吞掉——否则 accounted_complete /
    蒸馏器等旁路调用点的 max_tokens 全部形同虚设（根因）。"""
    from bandi.core.config import LLMConfig

    provider_cls, seen = _adapter_provider_cls()
    provider = provider_cls(LLMConfig(default_model="m", max_output_tokens=2048, api_key="k"))
    await provider.complete([{"role": "user", "content": "hi"}], max_tokens=8192)
    assert seen.get("max_tokens") == 8192, (
        f"per-call max_tokens 必须优先于 config，实际 {seen.get('max_tokens')}"
    )
    await provider.close()


@pytest.mark.asyncio
async def test_http_provider_override_clamped_to_model_catalog_limit():
    """契约（破坏者）：per-call 覆盖值超过模型目录上限时按目录上限钳制
    （不放大到请求 400）；目录未登记模型则按原值透传。"""
    from bandi.core.config import LLMConfig
    from bandi.llm.model_catalog import BUILTIN_MODELS

    model_name, spec = next(iter(BUILTIN_MODELS.items()))
    provider_cls, seen = _adapter_provider_cls()
    provider = provider_cls(
        LLMConfig(default_model=model_name, max_output_tokens=2048, api_key="k")
    )
    await provider.complete(
        [{"role": "user", "content": "hi"}], max_tokens=spec.max_output_tokens + 100_000
    )
    assert seen.get("max_tokens") == spec.max_output_tokens
    await provider.close()

    unknown_cls, seen2 = _adapter_provider_cls()
    unknown = unknown_cls(
        LLMConfig(default_model="not-in-catalog-model", max_output_tokens=2048, api_key="k")
    )
    await unknown.complete([{"role": "user", "content": "hi"}], max_tokens=8192)
    assert seen2.get("max_tokens") == 8192
    await unknown.close()


@pytest.mark.asyncio
async def test_openai_provider_honors_per_call_max_tokens_override():
    """契约（同族核对）：OpenAI 兼容主路线（openai.py 自带 complete）与
    HttpAdapterProvider 同族，必须同样采纳 per-call 覆盖。"""
    from unittest.mock import patch

    from bandi.core.config import LLMConfig
    from bandi.core.message import LLMResponse, TokenUsage
    from bandi.llm.providers.openai import OpenAICompatProvider

    seen: dict = {}

    async def fake_complete(client, base_url, api_key, model, messages, tools=None, **kw):
        seen.update(kw)
        return LLMResponse(content="x", usage=TokenUsage(1, 1))

    provider = OpenAICompatProvider(
        LLMConfig(default_model="m", max_output_tokens=2048, api_key="k"), "https://x/v1"
    )
    with patch("bandi.llm.providers.openai.openai_complete", fake_complete):
        await provider.complete([{"role": "user", "content": "hi"}], max_tokens=8192)
    assert seen.get("max_tokens") == 8192
    await provider.close()
