from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from bandi.tools.web_search import WebSearchTool


class TestWebSearchTool:
    def test_name_description(self) -> None:
        tool = WebSearchTool()
        assert tool.name == "web_search"
        assert tool.description == "Search the internet for current information"

    @pytest.mark.asyncio
    async def test_execute_fallback(self) -> None:
        tool = WebSearchTool()
        tool._search_tavily = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        tool._search_exa = AsyncMock(
            return_value=[
                {"title": "Result 1", "url": "https://example.com/1", "snippet": "Snippet 1"}
            ]
        )

        result = await tool.execute({"query": "test query", "num_results": 5})

        assert "Result 1" in result
        assert "https://example.com/1" in result
        tool._search_tavily.assert_awaited_once()
        tool._search_exa.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_all_fallback_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bandi.tools.web_search.any_search_key_configured", lambda: False)
        tool = WebSearchTool()
        tool._search_tavily = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        tool._search_exa = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "error", request=MagicMock(), response=MagicMock(status_code=403)
            )
        )
        tool._search_ddg = AsyncMock(side_effect=httpx.RequestError("network error"))
        # bing 也是回退链的一环——漏 mock 会让本用例真发网络请求，
        # 网络可用时后端直接返回结果，「全部回退失败」的断言随之失效（flaky）。
        tool._search_bing = AsyncMock(side_effect=httpx.RequestError("network error"))

        result = await tool.execute({"query": "test", "num_results": 5})

        assert "搜索服务不可用" in result
        assert "未配置联网检索 API" in result
        tool._search_tavily.assert_awaited_once()
        tool._search_exa.assert_awaited_once()
        tool._search_ddg.assert_awaited_once()
        tool._search_bing.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_all_fallback_fail_with_configured_key(self, monkeypatch) -> None:
        tool = WebSearchTool()
        monkeypatch.setattr("bandi.tools.web_search.configured_key", lambda field, env: "test-key")
        tool._search_tavily = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        tool._search_brave = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        tool._search_exa = AsyncMock(side_effect=httpx.RequestError("network error"))
        tool._search_ddg = AsyncMock(side_effect=httpx.RequestError("network error"))
        tool._search_bing = AsyncMock(side_effect=httpx.RequestError("network error"))

        result = await tool.execute({"query": "test", "num_results": 5})

        # 已配置 key 时不得误报「未配置」——失败应诊断为暂时不可用
        assert "搜索服务暂时不可用" in result
        assert "未配置联网检索 API" not in result
