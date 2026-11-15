# 功能：契约——OpenCode Go 网关（opencode.ai/inference/go）接入：
# ① 请求必须带 x-opencode-session（网关强制，缺失 400 MissingSessionID）+ 客户端 UA；
# ② 思考挡位经顶层 reasoning_effort 下发（官方值域 low/high/max）；
# ③ 通用 extra_headers 通道（档案 extra 可配）跨 complete/stream 一致生效；
# ④ 兼容端点默认行为零变化（不发 session 头、不发 thinking 字段）。
from __future__ import annotations

import json

import httpx
import pytest

from bandi.core.config import LLMConfig
from bandi.llm.providers.openai import OpenAICompatProvider

_GO_BASE_URL = "https://opencode.ai/inference/go/openai/v1"


def _capture_transport(captured: list[httpx.Request], response_body: dict) -> httpx.MockTransport:
    # 功能：捕获请求的 MockTransport（返回 JSON 响应）
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=response_body)

    return httpx.MockTransport(handler)


def _capture_stream_transport(
    captured: list[httpx.Request], sse_lines: list[str]
) -> httpx.MockTransport:
    # 功能：捕获请求的 MockTransport（返回 SSE 流）
    payload = "".join(f"{line}\n\n" for line in sse_lines).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)


def _chat_response(content: str = "2") -> dict:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _go_config(**overrides: object) -> LLMConfig:
    base = {
        "default_model": "deepseek-v4.1-flash",
        "provider": "opencode-go",
        "api_key": "oc_sk_test",
        "base_url": _GO_BASE_URL,
        "thinking_mode": "max",
    }
    base.update(overrides)
    return LLMConfig(**base)


def _go_provider(config: LLMConfig | None = None) -> OpenAICompatProvider:
    from bandi.llm.providers.opencode_go import OpenCodeGoProvider

    return OpenCodeGoProvider(config or _go_config())


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("low", "low"), ("high", "high"), ("max", "max"), ("MAX", "max")],
)
def test_thinking_payload_maps_official_effort_values(mode: str, expected: str) -> None:
    provider = _go_provider(_go_config(thinking_mode=mode))
    assert provider._thinking_payload() == {"reasoning_effort": expected}


@pytest.mark.parametrize("mode", ["", "disabled", "off", "none", "xhigh", "medium"])
def test_thinking_payload_empty_for_unsupported_values(mode: str) -> None:
    """官方值域仅 low/high/max——其余一律不发送（防 400）。"""
    provider = _go_provider(_go_config(thinking_mode=mode))
    assert provider._thinking_payload() == {}


def test_extra_headers_include_stable_session_and_ua() -> None:
    provider = _go_provider()
    headers = provider._extra_headers()
    session = headers.get("x-opencode-session", "")
    assert session, "x-opencode-session 必须存在（网关强制）"
    assert headers.get("User-Agent"), "客户端须以自有 UA 标识"
    assert provider._extra_headers() == headers, "session 标识须稳定（同进程恒定）"


def test_extra_headers_explicit_override_wins() -> None:
    """档案显式配置的 extra_headers 优先于自动生成的 session/UA。"""
    provider = _go_provider(_go_config(extra_headers={"x-opencode-session": "bandi-custom-1"}))
    assert provider._extra_headers()["x-opencode-session"] == "bandi-custom-1"


@pytest.mark.asyncio
async def test_complete_sends_session_header_and_reasoning_effort() -> None:
    captured: list[httpx.Request] = []
    provider = _go_provider()
    provider._client = httpx.AsyncClient(transport=_capture_transport(captured, _chat_response()))

    resp = await provider.complete([{"role": "user", "content": "1+1=?"}])

    assert resp.content == "2"
    assert len(captured) == 1
    request = captured[0]
    assert request.headers.get("x-opencode-session"), "complete 必须携带会话头"
    assert request.headers.get("User-Agent")
    assert request.headers.get("Authorization") == "Bearer oc_sk_test"
    body = json.loads(request.content.decode("utf-8"))
    assert body["reasoning_effort"] == "max"
    assert body["model"] == "deepseek-v4.1-flash"
    await provider.close()


@pytest.mark.asyncio
async def test_stream_sends_session_header_and_reasoning_effort() -> None:
    captured: list[httpx.Request] = []
    provider = _go_provider()
    provider._client = httpx.AsyncClient(
        transport=_capture_stream_transport(
            captured,
            [
                'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}',
                'data: {"choices":[{"delta":{"content":"2"}}]}',
                "data: [DONE]",
            ],
        )
    )

    events = [event async for event in provider.stream([{"role": "user", "content": "1+1=?"}])]

    assert any(getattr(event, "content_delta", "") == "2" for event in events)
    assert len(captured) == 1
    assert captured[0].headers.get("x-opencode-session"), "stream 必须携带会话头"
    body = json.loads(captured[0].content.decode("utf-8"))
    assert body["reasoning_effort"] == "max"
    await provider.close()


@pytest.mark.asyncio
async def test_plain_compat_provider_has_no_session_header() -> None:
    """兼容端点默认零变化：无 extra_headers 配置时不发任何自定义头。"""
    captured: list[httpx.Request] = []
    provider = OpenAICompatProvider(_go_config(provider="openai_compat", thinking_mode=""))
    provider._client = httpx.AsyncClient(transport=_capture_transport(captured, _chat_response()))

    await provider.complete([{"role": "user", "content": "hi"}])

    assert "x-opencode-session" not in captured[0].headers
    body = json.loads(captured[0].content.decode("utf-8"))
    assert "reasoning_effort" not in body
    await provider.close()


def test_registry_resolves_opencode_go_provider_and_default_endpoint() -> None:
    from bandi.core.config import BandiConfig
    from bandi.llm.providers.opencode_go import OpenCodeGoProvider
    from bandi.llm.registry import ProviderRegistry

    config = BandiConfig()
    config.llm.provider = "opencode-go"
    config.llm.api_key = "oc_sk_test"
    config.llm.base_url = ""
    registry = ProviderRegistry(config)
    provider = registry.get_provider("deepseek-v4.1-flash", config)
    assert isinstance(provider, OpenCodeGoProvider)
    assert provider._base_url == _GO_BASE_URL, "缺省端点必须落到 Go 网关"


def test_profile_extra_allows_extra_headers_and_applies() -> None:
    from bandi.llm.profiles import LlmProfile, _apply_profile

    profile = LlmProfile(
        profile_id="opencode-go-v41",
        model="deepseek-v4.1-flash",
        provider="opencode-go",
        base_url=_GO_BASE_URL,
        extra={"extra_headers": {"x-opencode-session": "bandi-custom-2"}},
    )
    resolved = _apply_profile(LLMConfig(), profile)
    assert resolved.extra_headers == {"x-opencode-session": "bandi-custom-2"}


def test_profile_extra_still_rejects_unknown_keys() -> None:
    from bandi.llm.profiles import LlmProfile

    with pytest.raises(ValueError, match="extra 仅接受"):
        LlmProfile(profile_id="p1", model="m", extra={"unknown_key": 1})


@pytest.mark.asyncio
async def test_registry_provider_carries_extra_headers_into_request() -> None:
    """端到端：config.llm.extra_headers → registry → 实际请求头（行为断言）。"""
    from bandi.core.config import BandiConfig
    from bandi.llm.registry import ProviderRegistry

    config = BandiConfig()
    config.llm.provider = "opencode-go"
    config.llm.api_key = "oc_sk_test"
    config.llm.base_url = _GO_BASE_URL
    config.llm.thinking_mode = "max"
    config.llm.extra_headers = {"x-opencode-session": "bandi-e2e-1"}

    captured: list[httpx.Request] = []
    provider = ProviderRegistry(config).get_provider("deepseek-v4.1-flash", config)
    provider._client = httpx.AsyncClient(transport=_capture_transport(captured, _chat_response()))

    await provider.complete([{"role": "user", "content": "hi"}])

    assert captured[0].headers.get("x-opencode-session") == "bandi-e2e-1"
    body = json.loads(captured[0].content.decode("utf-8"))
    assert body["reasoning_effort"] == "max"
    await provider.close()
