"""功能：联网搜索必须在零配置下可用，且不得把「没配 key」谎报成「额度用完」。

真机缺陷（用户提问「为什么联网搜索不能用」）：

    WARN web_search  exa HTTP 402 for query: AI news today November 2026

排查结论：`any_search_key_configured() == False`——从未配置过任何检索 key。
旧 `_search_exa` 不看 key 就发请求，于是空 key 拿到 `402 Payment Required`，
被误读为「额度用完」。而 tavily/brave 无 key 时返回空，表现为「搜不到」。
结果：五个通道全部失效，Agent 拿不到任何实时信息。

修复后托管 MCP 通道（`https://mcp.exa.ai/mcp`）无需自备 key，
实测 2 秒返回数千字真实结果。

契约钉住：
1. 无 key 时 exa 直连不得发请求（防 402 冒充额度问题）；
2. 托管通道必须能在无任何 key 的情况下返回非空结果；
3. MCP 响应解析必须能从 SSE 与纯 JSON 两种形态取到正文。
"""

from __future__ import annotations

import asyncio
import inspect

from bandi.tools import web_search as ws


def test_exa_direct_requires_key() -> None:
    """功能：exa 直连无 key 必须提前返回，不得空转请求（402 的根因）。"""
    tool = ws.WebSearchTool()
    result = asyncio.run(tool._search_exa("probe", 3))
    assert result is None, "无 key 时 exa 直连必须直接跳过"


def test_zero_config_mcp_search_returns_results() -> None:
    """功能：零配置（无任何检索 key）下托管通道必须给出真实结果。"""
    assert not ws.any_search_key_configured(), "本契约前提：环境里没有检索 key"
    tool = ws.WebSearchTool()
    results = asyncio.run(tool._search_mcp("AI news today", 3))
    assert results, "零配置下搜索必须可用，否则 Agent 拿不到任何实时信息"
    assert any(r.get("url") for r in results), "结果必须带来源链接"


def test_primary_path_uses_mcp_not_scraping() -> None:
    """功能：零配置的首选通道必须是托管 MCP，不能只靠抓 bing 结果页。

    抓页面依赖对方 HTML 结构，随时会因改版失效；契约要求**真的走了 MCP**
    （它的正文块含 `Title:/URL:` 结构，抓页面不会有）。
    """
    tool = ws.WebSearchTool()
    original = tool._search_bing

    async def _forbid(*_a: object, **_kw: object) -> None:
        raise AssertionError("零配置首选通道应当命中 MCP，不该退到抓页面")

    tool._search_bing = _forbid  # type: ignore[method-assign]
    try:
        text = asyncio.run(
            asyncio.wait_for(tool.execute({"query": "AI news today P2"}), timeout=90)
        )
    finally:
        tool._search_bing = original  # type: ignore[method-assign]
    assert not text.startswith("search_unavailable"), f"仅禁用抓页面就失败：{text[:160]}"
    assert "URL:" in text


def test_search_tool_returns_readable_text_without_keys() -> None:
    """功能：对外主入口在零配置下必须返回可读结果，而不是 search_unavailable。"""
    tool = ws.WebSearchTool()
    text = asyncio.run(asyncio.wait_for(tool.execute({"query": "AI news today"}), timeout=90))
    assert not text.startswith("search_unavailable"), f"零配置仍报不可用：{text[:160]}"
    assert "URL:" in text, "结果必须含来源链接"


def test_mcp_provider_is_tried_first_when_no_keys() -> None:
    """功能：无 key 时托管通道排在首位（否则先白耗无 key 通道的超时）。"""
    source = inspect.getsource(ws.WebSearchTool.execute)
    assert '"mcp", self._search_mcp' in source
    first = source.index('("mcp", self._search_mcp)')
    others = [source.index(f'("{n}", self._search_') for n in ("brave", "tavily", "exa")]
    assert all(first < idx for idx in others), "托管通道必须排在需要 key 的通道之前"


def test_parse_mcp_results_splits_entries() -> None:
    """功能：MCP 正文块必须拆成条目（否则 URL 与正文混成一坨）。"""
    text = (
        "Title: Alpha\nURL: https://a.example/1\nPublished: N/A\nBody one.\n\n"
        "Title: Beta\nURL: https://b.example/2\nBody two.\n"
    )
    results = ws._parse_mcp_results(text, 5)
    assert results is not None and len(results) == 2
    assert results[0]["title"] == "Alpha"
    assert results[0]["url"] == "https://a.example/1"
    assert "Body one." in results[0]["snippet"]
    assert results[1]["url"] == "https://b.example/2"


def test_mcp_payload_text_reads_sse_and_plain_json() -> None:
    """功能：MCP 可能是 SSE 也可能是纯 JSON，两种都要能取到正文。"""
    sse = 'event: message\ndata: {"result":{"content":[{"type":"text","text":"HELLO"}]}}\n'
    assert ws._mcp_payload_text(sse) == "HELLO"
    plain = '{"result":{"content":[{"type":"text","text":"PLAIN"}]}}'
    assert ws._mcp_payload_text(plain) == "PLAIN"
    assert ws._mcp_payload_text("not-json") == ""
