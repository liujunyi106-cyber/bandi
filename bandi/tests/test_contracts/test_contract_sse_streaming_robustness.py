# 功能：契约——SSE 流式解析健壮性（OpenAI 官方 usage-only chunk + 多字节字符跨块）。
# 破坏者视角：① 官方形态 usage 在独立 chunk（choices 空）且位于 finish 之后——
# ② 按字节块独立 decode → 中文/emoji 跨块被撕裂为 U+FFFD（数据损坏）。
from __future__ import annotations

from typing import Any

import httpx
import pytest

from bandi.llm.adapters.openai_compat import sse_stream
from bandi.llm.streaming import StreamEventType


class _ChunkedStream(httpx.AsyncByteStream):
    # 功能：按预置字节块逐块输出（模拟 TCP 分片，含多字节字符中间切分）
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


def _client_with_chunks(chunks: list[bytes]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_ChunkedStream(chunks),
            headers={"content-type": "text/event-stream"},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _collect(client: httpx.AsyncClient) -> list[Any]:
    events = []
    async for ev in sse_stream(
        client, "https://api.test/chat/completions", {"Authorization": "Bearer sk"}, {"model": "m"}
    ):
        events.append(ev)
    return events


def _done(events: list[Any]) -> Any:
    return next((e for e in events if e.type == StreamEventType.DONE), None)


def _content(events: list[Any]) -> str:
    return "".join(e.content_delta or "" for e in events if e.type == StreamEventType.CONTENT_DELTA)


@pytest.mark.asyncio
async def test_contract_official_usage_only_chunk_captured() -> None:
    """契约（计费正确性·破坏者）：OpenAI 官方形态——usage 在 finish 之后的独立
    chunk（choices 为空数组）——DONE 必须携带该 usage（修复前 finish 即 return
    usage 恒丢失）。"""
    frames = [
        'data: {"choices":[{"delta":{"content":"你好"},"finish_reason":null}]}\n\n',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n',
        "data: [DONE]\n\n",
    ]
    client = _client_with_chunks([f.encode("utf-8") for f in frames])
    try:
        events = await _collect(client)
    finally:
        await client.aclose()

    done = _done(events)
    assert done is not None, "必须产出 DONE"
    assert done.usage is not None, "官方形态 usage-only chunk 必须被捕获（否则计费漏账）"
    assert done.usage.prompt_tokens == 10
    assert done.usage.completion_tokens == 5


@pytest.mark.asyncio
async def test_contract_multibyte_split_across_chunks_not_corrupted() -> None:
    """契约（数据完整性·破坏者）：中文内容跨字节块切分（含 3 字节字符中间切分）
    不得产生 U+FFFD——修复前按块独立 decode(errors='replace') 撕裂多字节字符。"""
    frames = (
        'data: {"choices":[{"delta":{"content":"流式中文输出测试"},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    ).encode()
    # 每 7 字节分块——3 字节中文序列必然被跨块切开（模拟 TCP 任意分片）
    chunks = [frames[i : i + 7] for i in range(0, len(frames), 7)]

    client = _client_with_chunks(chunks)
    try:
        events = await _collect(client)
    finally:
        await client.aclose()

    text = _content(events)
    assert "\ufffd" not in text, f"多字节字符被撕裂（U+FFFD 出现）：{text!r}"
    assert text == "流式中文输出测试", f"跨块内容必须完整重组，实际 {text!r}"


@pytest.mark.asyncio
async def test_contract_deepseek_style_finish_usage_no_done_marker() -> None:
    """契约（兼容回归）：DeepSeek 形态——usage 挂在 finish chunk 上、流无 [DONE]
    自然关闭——DONE 仍必须产出且 usage 正确（修复不得牺牲该形态）。"""
    frames = [
        'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n',
    ]
    client = _client_with_chunks([f.encode("utf-8") for f in frames])
    try:
        events = await _collect(client)
    finally:
        await client.aclose()

    done = _done(events)
    assert done is not None, "无 [DONE] 但已收到 finish 帧——必须产出 DONE"
    assert done.finish_reason == "stop"
    assert done.usage is not None and done.usage.prompt_tokens == 3
    assert _content(events) == "hi"
