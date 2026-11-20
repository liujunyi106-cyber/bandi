"""功能：流式首事件前失败必须走重试策略，且重试次数/退避可观测。

真机缺陷：`openai.py` 的 `stream()` 曾直接迭代 `sse_stream`，异常就
`raise llm_error_with_status`，一次都不重试；而 `complete()`（非流式）
走 `_call_with_retry`（`RetryPolicy(max_retries=4, base_delay=2.0)`）。
生产主路径走流式 ⇒ 用户遇到的是「网络瞬断即整轮失败」，
回复体是内部异常串。

边界必须钉死：
1. **首事件前**失败 → 重试整个请求（此时一个字都没发出去，重发安全）。
2. **首事件后**中断 → 不重发，只 yield ERROR（重发会重复正文）。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator
from unittest.mock import patch

import pytest

from bandi.core.config import LLMConfig
from bandi.llm.circuit_breaker import RetryPolicy
from bandi.llm.providers.openai import OpenAICompatProvider
from bandi.llm.streaming import StreamEvent, StreamEventType


def _real_config() -> LLMConfig:
    """功能：真实 LLMConfig——不用 stub顶替。

    项目内 stub 平替 domain 对象会同时藏住多个缺陷（缺字段、类型不匹）。
    这里直接用真实配置工厂，字段变化会在本契约里立刻暴露。
    """
    return LLMConfig(default_model="stub-model", timeout=10)


def _make_provider(retry_policy: RetryPolicy | None) -> OpenAICompatProvider:
    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    provider.config = _real_config()
    provider._base_url = "https://stub.invalid/v1"
    provider.auth_token = "stub"
    provider._timeout = 10
    provider._client = object()
    provider._retry_policy = retry_policy
    provider._circuit_breaker = None
    return provider


def _patch_helpers(provider: OpenAICompatProvider) -> None:
    provider._extra_headers = lambda: {}  # type: ignore[method-assign]
    provider._thinking_payload = lambda: None  # type: ignore[method-assign]
    provider._max_tokens_field = lambda: "max_tokens"  # type: ignore[method-assign]


async def _collect(provider: OpenAICompatProvider) -> list[StreamEvent]:
    out: list[StreamEvent] = []
    async for event in provider.stream([{"role": "user", "content": "hi"}]):
        out.append(event)
    return out


def test_retries_before_first_event_then_succeeds() -> None:
    """功能：首事件前连续失败应按策略重试，最终成功取到事件。"""
    provider = _make_provider(RetryPolicy(max_retries=2, base_delay=0.0, jitter=False))
    _patch_helpers(provider)
    attempts = {"n": 0}

    async def flaky(
        client: Any, url: str, headers: Any, body: Any, **kwargs: Any
    ) -> AsyncGenerator[StreamEvent, None]:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise ConnectionError("All connection attempts failed")
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="ok")

    with patch("bandi.llm.providers.openai.sse_stream", flaky):
        events = asyncio.run(_collect(provider))

    assert attempts["n"] == 3, f"应重试到第 3 次才成功，实际 {attempts['n']} 次"
    assert [e.content_delta for e in events if e.type == StreamEventType.CONTENT_DELTA] == ["ok"]


def test_exhausted_retries_raise_llm_error() -> None:
    """功能：重试耗尽后必须抛错（不能静默返回空流让上层当成功）。"""
    provider = _make_provider(RetryPolicy(max_retries=1, base_delay=0.0, jitter=False))
    _patch_helpers(provider)
    calls = {"n": 0}

    async def always_fail(
        client: Any, url: str, headers: Any, body: Any, **kwargs: Any
    ) -> AsyncGenerator[StreamEvent, None]:
        calls["n"] += 1
        raise ConnectionError("All connection attempts failed")
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="unreachable")

    with patch("bandi.llm.providers.openai.sse_stream", always_fail):
        with pytest.raises(Exception) as excinfo:
            asyncio.run(_collect(provider))

    assert calls["n"] == 2, f"max_retries=1 应共尝试 2 次，实际 {calls['n']}"
    assert "connection attempts failed" in str(excinfo.value).lower()


def test_no_retry_after_first_event_emitted() -> None:
    """功能：首事件已发出后中断不得重发（否则正文重复）。"""
    provider = _make_provider(RetryPolicy(max_retries=3, base_delay=0.0, jitter=False))
    _patch_helpers(provider)
    opened = {"n": 0}

    async def ok_then_break(
        client: Any, url: str, headers: Any, body: Any, **kwargs: Any
    ) -> AsyncGenerator[StreamEvent, None]:
        opened["n"] += 1
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="first")
        raise ConnectionError("mid-stream reset")

    with patch("bandi.llm.providers.openai.sse_stream", ok_then_break):
        events = asyncio.run(_collect(provider))

    assert opened["n"] == 1, f"首事件后不得重新打开流，实际打开 {opened['n']} 次"
    kinds = [e.type for e in events]
    assert StreamEventType.CONTENT_DELTA in kinds
    assert StreamEventType.ERROR in kinds, "中途断流必须 yield ERROR 交上层处置"
    deltas = [e.content_delta for e in events if e.type == StreamEventType.CONTENT_DELTA]
    assert deltas == ["first"], f"正文不得重复，实际 {deltas}"


def test_works_without_retry_policy() -> None:
    """功能：未装配重试策略时不得崩（保持原有单次行为）。"""
    provider = _make_provider(None)
    _patch_helpers(provider)

    async def ok(
        client: Any, url: str, headers: Any, body: Any, **kwargs: Any
    ) -> AsyncGenerator[StreamEvent, None]:
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="solo")

    with patch("bandi.llm.providers.openai.sse_stream", ok):
        events = asyncio.run(_collect(provider))

    assert [e.content_delta for e in events if e.type == StreamEventType.CONTENT_DELTA] == ["solo"]
