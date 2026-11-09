# 功能：生产落地——真实 WebFetchTool 流式覆盖单元验证
# 验证：① 真实生产工具被识别为流式工具（override stream 生效）；
# ② stream() 产出 fetch_started 状态 + complete 结果，供前端工具时间线。
# 用 monkeypatch 阻断真实网络（只保留流式结构断言）。
from __future__ import annotations

import pytest

from bandi.tools.executor import _is_streaming_tool
from bandi.tools.web_fetch import WebFetchTool


def test_web_fetch_is_detected_as_streaming_tool() -> None:
    tool = WebFetchTool()
    assert _is_streaming_tool(tool) is True


@pytest.mark.asyncio
async def test_web_fetch_stream_emits_started_then_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_execute(self, params: dict, context=None) -> str:
        return "fetched-content"

    monkeypatch.setattr(WebFetchTool, "execute", _fake_execute)
    tool = WebFetchTool()

    events = []
    async for ev in tool.stream({"url": "https://example.com"}, None):
        events.append(ev)

    types = [e.type for e in events]
    assert types == ["status", "complete"]
    assert events[0].data["step"] == "fetch_started"
    assert events[1].data["result"] == "fetched-content"
