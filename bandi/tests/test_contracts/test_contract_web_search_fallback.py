# 功能：契约 — web_search 免 key 兜底后端（bing 抓取）与不可用信号分级
from __future__ import annotations

import time

import httpx
import pytest

from bandi.tools.web_search import WebSearchTool

# 结构镜像真实 bing 结果页（class="b_algo" 块内 h2>a 与 b_caption>p）
_BING_FIXTURE = """
<html><body><ol id="b_results">
<li class="b_algo"><h2><a target="_blank" href="https://example.com/a" h="ID=SERP,1"><strong>OpenAI</strong> 官网</a></h2><div class="b_caption"><p class="b_lineclamp2">OpenAI 官方站点，介绍 GPT 系列模型。</p></div></li>
<li class="b_algo"><h2><a target="_blank" href="https://baike.baidu.com/item/x" h="ID=SERP,2">OpenAI_百科</a></h2><div class="b_caption"><p class="b_lineclamp2">OpenAI 词条，介绍其发展历史。</p></div></li>
</ol></body></html>
"""


def _fail(error: httpx.RequestError):
    async def _f(query: str, num_results: int):
        raise error

    return _f


class TestBingFallbackBackend:
    @pytest.mark.asyncio
    async def test_bing_backend_serves_as_final_fallback(self, monkeypatch) -> None:
        tool = WebSearchTool()
        calls: list[str] = []

        async def _tav(query: str, num_results: int):
            calls.append("tavily")
            return None

        async def _exa(query: str, num_results: int):
            calls.append("exa")
            raise httpx.ConnectError("down")

        async def _ddg(query: str, num_results: int):
            calls.append("ddg")
            raise httpx.TimeoutException("timeout")

        async def _bing(query: str, num_results: int):
            calls.append("bing")
            return [{"title": "T", "url": "https://example.com/x", "snippet": "s"}]

        monkeypatch.setattr(tool, "_search_tavily", _tav)
        monkeypatch.setattr(tool, "_search_exa", _exa)
        monkeypatch.setattr(tool, "_search_ddg", _ddg)
        monkeypatch.setattr(tool, "_search_bing", _bing)

        out = await tool.execute({"query": "test"})
        assert calls == ["tavily", "exa", "ddg", "bing"]
        assert "https://example.com/x" in out

    def test_parse_bing_html(self) -> None:
        tool = WebSearchTool()
        results = tool._parse_bing_html(_BING_FIXTURE, 5)
        assert len(results) == 2
        assert results[0]["url"] == "https://example.com/a"
        assert "OpenAI" in results[0]["title"]
        assert "官方站点" in results[0]["snippet"]
        assert results[1]["snippet"].startswith("OpenAI 词条")

    @pytest.mark.asyncio
    async def test_unconfigured_signal_when_no_keys_and_all_fail(self, monkeypatch) -> None:
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("EXA_API_KEY", raising=False)
        tool = WebSearchTool()
        fail = _fail(httpx.ConnectError("network unreachable"))
        monkeypatch.setattr(tool, "_search_tavily", fail)
        monkeypatch.setattr(tool, "_search_exa", fail)
        monkeypatch.setattr(tool, "_search_ddg", fail)
        monkeypatch.setattr(tool, "_search_bing", fail)

        out = await tool.execute({"query": "test"})
        assert "失败原因" in out
        assert "TAVILY_API_KEY" in out
        assert "设置页" in out

    @pytest.mark.asyncio
    async def test_temporary_signal_when_key_present(self, monkeypatch) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        tool = WebSearchTool()
        fail = _fail(httpx.ConnectError("network unreachable"))
        monkeypatch.setattr(tool, "_search_tavily", fail)
        monkeypatch.setattr(tool, "_search_exa", fail)
        monkeypatch.setattr(tool, "_search_ddg", fail)
        monkeypatch.setattr(tool, "_search_bing", fail)

        out = await tool.execute({"query": "test"})
        assert "暂时不可用" in out
        assert "设置页" not in out
        assert "未配置" not in out

    @pytest.mark.asyncio
    async def test_circuit_open_no_key_still_hints_config(self, monkeypatch) -> None:
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        tool = WebSearchTool()
        tool._circuit_until = time.monotonic() + 60
        try:
            out = await tool.execute({"query": "test"})
        finally:
            tool._circuit_until = 0.0
        assert "TAVILY_API_KEY" in out
