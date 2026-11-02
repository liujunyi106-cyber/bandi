# 功能：契约——请求参数语义翻译：thinking_mode 按 provider 分支 + o 系列 token 字段名。
# 破坏者视角：① 裸发自定义 thinking_mode 字段（DeepSeek/OpenAI 官方均不认：静默无效
# 或 400）；② 对 o 系列/GPT-5 发 max_tokens（官方要求 max_completion_tokens）。
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from bandi.core.config import LLMConfig
from bandi.llm.providers.deepseek import DeepSeekProvider
from bandi.llm.providers.openai import OpenAICompatProvider, OpenAIProvider


def _respond(captured: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    return httpx.MockTransport(handler)


def _patch_client(provider: Any, captured: dict) -> None:
    provider._client = httpx.AsyncClient(transport=_respond(captured))


async def _complete_body(provider: Any, captured: dict) -> dict:
    _patch_client(provider, captured)
    try:
        await provider.complete([{"role": "user", "content": "hi"}])
    finally:
        await provider.close()
    return captured.get("body", {})


def _config(model: str, mode: str = "") -> LLMConfig:
    return LLMConfig(api_key="sk", default_model=model, thinking_mode=mode, max_output_tokens=1024)


@pytest.mark.asyncio
async def test_contract_openai_reasoning_model_uses_max_completion_tokens() -> None:
    """契约（o 系列/GPT-5 字段名）：OpenAI 官方 + o4-mini → 必须发
    max_completion_tokens（修复前发 max_tokens，官方拒绝/不兼容）。"""
    captured: dict = {}
    provider = OpenAIProvider(_config("o4-mini"))
    body = await _complete_body(provider, captured)
    assert "max_completion_tokens" in body, f"o 系列必须用 max_completion_tokens，实际 {body}"
    assert "max_tokens" not in body


@pytest.mark.asyncio
async def test_contract_openai_regular_model_uses_max_tokens() -> None:
    """契约（常规模型回归）：gpt-4o 等常规模型保持 max_tokens。"""
    captured: dict = {}
    provider = OpenAIProvider(_config("gpt-4o"))
    body = await _complete_body(provider, captured)
    assert "max_tokens" in body
    assert "max_completion_tokens" not in body


@pytest.mark.asyncio
async def test_contract_openai_translates_thinking_to_reasoning_effort() -> None:
    """契约（OpenAI 参数语义）：thinking_mode=high → reasoning_effort=high
    且不得出现裸 thinking_mode 字段。"""
    captured: dict = {}
    provider = OpenAIProvider(_config("gpt-4o", mode="high"))
    body = await _complete_body(provider, captured)
    assert body.get("reasoning_effort") == "high", f"必须翻译为 reasoning_effort，实际 {body}"
    assert "thinking_mode" not in body


@pytest.mark.asyncio
async def test_contract_openai_max_maps_to_strongest_effort() -> None:
    """
    **xhigh**
    会让「最大」与「高」同档）。gpt-5.2 属推理模型 ⇒ 才会真正发送该字段。"""
    captured: dict = {}
    provider = OpenAIProvider(_config("gpt-5.2", mode="max"))
    body = await _complete_body(provider, captured)
    assert body.get("reasoning_effort") == "xhigh"


@pytest.mark.asyncio
async def test_contract_deepseek_translates_thinking_to_type_field() -> None:
    """契约：thinking_mode=high →
    `thinking: {"type": "enabled"}` + `reasoning_effort: high`；无裸 thinking_mode。"""
    captured: dict = {}
    provider = DeepSeekProvider(_config("deepseek-v4-flash", mode="high"))
    body = await _complete_body(provider, captured)
    assert body.get("thinking") == {"type": "enabled"}, f"实际 {body.get('thinking')!r}"
    assert body.get("reasoning_effort") == "high"
    assert "thinking_mode" not in body


@pytest.mark.asyncio
async def test_contract_deepseek_disabled_maps_type_disabled() -> None:
    """契约（关闭语义）：thinking_mode=disabled → `thinking: {"type": "disabled"}`。"""
    captured: dict = {}
    provider = DeepSeekProvider(_config("deepseek-v4-flash", mode="disabled"))
    body = await _complete_body(provider, captured)
    assert body.get("thinking") == {"type": "disabled"}


@pytest.mark.asyncio
async def test_contract_generic_compat_does_not_send_unknown_thinking_field() -> None:
    """契约（通用端点保护）：OpenAICompatProvider（第三方兼容端点）不得发送
    自定义 thinking_mode 裸字段（未知参数 400 风险）。"""
    captured: dict = {}
    provider = OpenAICompatProvider(_config("any-model", mode="high"), "https://api.test/v1")
    body = await _complete_body(provider, captured)
    assert "thinking_mode" not in body
    assert "reasoning_effort" not in body
    assert "thinking" not in body


@pytest.mark.asyncio
async def test_contract_deepseek_stream_path_same_translation() -> None:
    """契约（流式同源）：流式路径与 complete 同参数语义（thinking 翻译 + 无裸字段）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    provider = DeepSeekProvider(_config("deepseek-v4-flash", mode="high"))
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        async for _ev in provider.stream([{"role": "user", "content": "hi"}]):
            pass
    finally:
        await provider.close()
    body = captured.get("body", {})
    assert body.get("thinking") == {"type": "enabled"}
    assert body.get("reasoning_effort") == "high"
    assert "thinking_mode" not in body
