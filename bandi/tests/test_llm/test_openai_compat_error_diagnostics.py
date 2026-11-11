# 功能：契约——LLM HTTP 4xx/5xx 必须可诊断（抓包：状态码 + 响应体 + 请求摘要 +
# 非法控制字符侦测）。真机 400 排查时内核只记状态码，响应体与请求形态丢失。
from __future__ import annotations

import logging

import httpx
import pytest

from bandi.llm.adapters.openai_compat import _payload_digest, openai_complete, sse_stream


def _transport(status: int, body: dict) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_complete_http_error_logs_body_and_digest(caplog: pytest.LogCaptureFixture) -> None:
    transport = _transport(400, {"error": {"message": "invalid reasoning_content field"}})
    async with httpx.AsyncClient(transport=transport) as client:
        with caplog.at_level(logging.ERROR, logger="bandi.llm.adapters.openai_compat"):
            with pytest.raises(httpx.HTTPStatusError):
                await openai_complete(
                    client,
                    "https://api.example.com/v1",
                    "sk-x",
                    "m1",
                    [{"role": "user", "content": "hello"}],
                )
    blob = caplog.text
    assert "status=400" in blob, "状态码必须落日志"
    assert "invalid reasoning_content field" in blob, "响应体必须落日志（诊断关键）"
    assert "msgs=0:user:5" in blob, "请求体摘要（逐条角色/长度）必须落日志"


@pytest.mark.asyncio
async def test_stream_http_error_logs_body(caplog: pytest.LogCaptureFixture) -> None:
    transport = _transport(400, {"error": {"message": "stream rejected"}})
    async with httpx.AsyncClient(transport=transport) as client:
        with caplog.at_level(logging.ERROR, logger="bandi.llm.adapters.openai_compat"):
            with pytest.raises(httpx.HTTPStatusError):
                async for _ in sse_stream(
                    client,
                    "https://api.example.com/v1/chat/completions",
                    {},
                    {"model": "m1", "messages": []},
                ):
                    pass
    assert "stream rejected" in caplog.text, (
        "流式 400 响应体必须落日志（first event 前失败正是此路径）"
    )


def test_payload_digest_flags_control_chars() -> None:
    """非法控制字符侦测：隐藏字符混入正文是上游 400 常见诱因。"""
    digest = _payload_digest(
        {
            "model": "m1",
            "messages": [{"role": "user", "content": "bad\u0007bell"}],
        }
    )
    assert "SUSPECT_CHARS" in digest and "U+0007" in digest
