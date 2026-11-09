# 功能：网页搜索工具（WebSearchTool 搜索查询）
from __future__ import annotations

import logging
import os
import re
import time
from html import unescape
from typing import Any, ClassVar

import httpx

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext

logger = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; BandiBot/1.0)"
_MAX_RESULTS = 5
# num_results 上限（schema 描述「1-10」的单一来源——原 execute 内 max*2 魔数）
_MAX_RESULTS_LIMIT = 10

_TAVILY_URL = "https://api.tavily.com/search"
_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_EXA_URL = "https://api.exa.ai/search"
_DDG_URL = "https://html.duckduckgo.com/html/"
# 免 key 兜底：抓取 bing 结果页（无 API 依赖，国内网络可达）
_BING_URL = "https://www.bing.com/search"

_TIMEOUTS = {"tavily": 8, "brave": 8, "exa": 10, "ddg": 15, "bing": 12}
# bing 结果页对非浏览器 UA 返回精简版，需浏览器形态 UA
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def configured_key(field: str, env_name: str) -> str:
    # 功能：检索凭据取值——设置页写入的配置优先（自动解密），空则回落环境变量
    try:
        from bandi.core.config import BandiConfig
        from bandi.core.security.secret_protect import unprotect

        raw = str(getattr(BandiConfig.load().tools.web_search, field, "") or "")
        if raw:
            value = unprotect(raw)
            if value:
                return value
    except Exception:
        logger.debug("web_search key resolve fell back to env", exc_info=True)
    return os.environ.get(env_name, "")


def any_search_key_configured() -> bool:
    # 功能：任一检索后端已配置 key（设置页加密配置或环境变量）——决定失败提示走「未配置」还是「暂时不可用」
    return bool(
        configured_key("tavily_api_key", "TAVILY_API_KEY")
        or configured_key("brave_api_key", "BRAVE_API_KEY")
    )


class WebSearchTool(BaseTool):
    external_content: bool = True
    name: str = "web_search"
    description: str = "Search the internet for current information"
    category: ToolCategory = ToolCategory.WEB
    risk_level: str = "medium"
    is_read_only: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["query"],
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query",
            },
            "num_results": {
                "type": "integer",
                "description": "Number of results (1-10)",
                "default": _MAX_RESULTS,
            },
        },
    }
    is_concurrency_safe: bool = True

    _circuit_fails: ClassVar[int] = 0
    _circuit_until: ClassVar[float] = 0.0

    def __init__(
        self,
        timeouts: dict[str, int] | None = None,
        circuit_fail_limit: int = 3,
        circuit_reset_secs: int = 60,
        max_results: int = 5,
    ):
        self._timeouts = timeouts or dict(_TIMEOUTS)
        self._circuit_fail_limit = circuit_fail_limit
        self._circuit_reset_secs = circuit_reset_secs
        self._max_results = max_results

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        query = params["query"]
        num_results = min(params.get("num_results", self._max_results), _MAX_RESULTS_LIMIT)

        if time.monotonic() < self._circuit_until:
            return self._unavailable()

        providers: list[tuple[str, Any]] = [
            ("tavily", self._search_tavily),
            ("brave", self._search_brave),
            ("exa", self._search_exa),
            ("ddg", self._search_ddg),
            ("bing", self._search_bing),
        ]

        reasons: dict[str, str] = {}
        for name, func in providers:
            try:
                results = await func(query, num_results)
                if results:
                    self._circuit_fails = 0
                    return self._format_results(results)
                if name == "tavily" and not configured_key("tavily_api_key", "TAVILY_API_KEY"):
                    reasons[name] = "未配置 key"
                else:
                    reasons[name] = "无结果"
            except httpx.TimeoutException:
                logger.warning("%s timed out for query: %s", name, query)
                reasons[name] = "超时"
            except httpx.HTTPStatusError as e:
                logger.warning("%s HTTP %s for query: %s", name, e.response.status_code, query)
                reasons[name] = f"HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                logger.warning("%s request failed: %s", name, e)
                reasons[name] = type(e).__name__

        self._circuit_fails += 1
        if self._circuit_fails >= self._circuit_fail_limit:
            self._circuit_until = time.monotonic() + self._circuit_reset_secs
            logger.warning("web_search circuit breaker opened for %ss", self._circuit_reset_secs)
        return self._unavailable(reasons)

    def _unavailable(self, reasons: dict[str, str] | None = None) -> str:
        detail = "；".join(f"{k}: {v}" for k, v in (reasons or {}).items())
        if not any_search_key_configured():
            msg = "search_unavailable: 搜索服务不可用"
            if detail:
                msg += f"（失败原因：{detail}）"
            msg += (
                "。未配置联网检索 API：请在「设置页 → 系统设置 → WebSearch」填入 "
                "Tavily 或 Brave 的 API Key（或设置 TAVILY_API_KEY / BRAVE_API_KEY 环境变量）后重试"
            )
            return msg
        if detail:
            return f"search_unavailable: 搜索服务暂时不可用，请稍后重试（失败原因：{detail}）"
        return "search_unavailable: 搜索服务暂时不可用，请稍后重试"

    async def _search_tavily(self, query: str, num_results: int) -> list[dict[str, str]] | None:
        api_key = configured_key("tavily_api_key", "TAVILY_API_KEY")
        if not api_key:
            return None
        async with httpx.AsyncClient(timeout=self._timeouts.get("tavily", 8)) as client:
            resp = await client.post(
                _TAVILY_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"query": query, "max_results": num_results, "search_depth": "basic"},
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("results", [])
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("content", ""),
                }
                for r in raw
            ]

    async def _search_brave(self, query: str, num_results: int) -> list[dict[str, str]] | None:
        # 功能：Brave Search Web API（未配置 key 直接跳过，不产生请求）
        api_key = configured_key("brave_api_key", "BRAVE_API_KEY")
        if not api_key:
            return None
        async with httpx.AsyncClient(timeout=self._timeouts.get("brave", 8)) as client:
            resp = await client.get(
                _BRAVE_URL,
                headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
                params={"q": query, "count": max(1, num_results)},
            )
            resp.raise_for_status()
            data = resp.json()
            raw = (data.get("web") or {}).get("results") or []
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("description", ""),
                }
                for r in raw
            ]

    async def _search_exa(self, query: str, num_results: int) -> list[dict[str, str]] | None:
        exa_key = os.environ.get("EXA_API_KEY")
        headers = {"Content-Type": "application/json"}
        if exa_key:
            headers["x-api-key"] = exa_key
        async with httpx.AsyncClient(timeout=self._timeouts.get("exa", 10)) as client:
            resp = await client.post(
                _EXA_URL,
                headers=headers,
                json={"query": query, "numResults": num_results},
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("results", data.get("data", []))
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("text", "")}
                for r in raw
            ]

    async def _search_ddg(self, query: str, num_results: int) -> list[dict[str, str]] | None:
        async with httpx.AsyncClient(
            timeout=self._timeouts.get("ddg", 15), follow_redirects=True
        ) as client:
            resp = await client.post(
                _DDG_URL, data={"q": query}, headers={"User-Agent": _USER_AGENT}
            )
            resp.raise_for_status()
            return self._parse_ddg_html(resp.text, num_results)

    def _parse_ddg_html(self, html: str, num_results: int) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        lines = html.splitlines()
        i = 0
        while i < len(lines) and len(results) < num_results:
            line = lines[i]
            if 'class="result-link"' in line or 'class="result__a"' in line:
                title, url = self._extract_link(line)
                if not title or not url:
                    i += 1
                    continue
                snippet = ""
                for j in range(i + 1, min(i + 5, len(lines))):
                    txt = self._extract_snippet(lines[j])
                    if txt:
                        snippet = txt
                        break
                results.append({"title": title, "url": url, "snippet": snippet})
            i += 1
        return results

    def _extract_link(self, line: str) -> tuple[str, str]:
        title = ""
        url = ""
        href_start = line.find('href="')
        if href_start >= 0:
            href_start += 6
            href_end = line.find('"', href_start)
            if href_end >= 0:
                url = line[href_start:href_end]
        gt = line.find(">", max(href_end, 0) if 'href="' in line else 0)
        if gt >= 0:
            close = line.find("</a>", gt)
            if close >= 0:
                title = line[gt + 1 : close]
                title = self._strip_html(title)
        return title, url

    def _extract_snippet(self, line: str) -> str:
        if "snippet" in line or "result__snippet" in line:
            snippet_pos = line.find("snippet")
            if snippet_pos < 0:
                return ""
            gt = line.find(">", snippet_pos)
            if gt >= 0:
                close = line.find("</", gt)
                if close >= 0:
                    return self._strip_html(line[gt + 1 : close])
        return ""

    def _strip_html(self, text: str) -> str:
        text = re.sub(r"<[^>]+>", "", text)
        return unescape(text).strip()

    async def _search_bing(self, query: str, num_results: int) -> list[dict[str, str]] | None:
        async with httpx.AsyncClient(
            timeout=self._timeouts.get("bing", 12), follow_redirects=True
        ) as client:
            resp = await client.get(
                _BING_URL,
                params={"q": query, "count": str(num_results)},
                headers={"User-Agent": _BROWSER_UA},
            )
            resp.raise_for_status()
            return self._parse_bing_html(resp.text, num_results)

    def _parse_bing_html(self, html_text: str, num_results: int) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for block in html_text.split('class="b_algo"')[1:]:
            if len(results) >= num_results:
                break
            h2 = block.find("<h2")
            if h2 < 0:
                continue
            h2_end = block.find("</h2>", h2)
            seg = block[h2 : h2_end + 5] if h2_end > 0 else block[h2 : h2 + 800]
            href_start = seg.find('href="')
            if href_start < 0:
                continue
            href_start += 6
            url = seg[href_start : seg.find('"', href_start)]
            a_open_end = seg.find(">", href_start)
            a_end = seg.find("</a>", a_open_end)
            title = (
                self._strip_html(seg[a_open_end + 1 : a_end])
                if a_open_end >= 0 and a_end > 0
                else ""
            )
            snippet = ""
            p = block.find("<p", h2)
            if p >= 0:
                p_open_end = block.find(">", p)
                p_end = block.find("</p>", p_open_end)
                if p_open_end >= 0 and p_end > 0:
                    snippet = self._strip_html(block[p_open_end + 1 : p_end])
            if url and title:
                results.append({"title": title, "url": url, "snippet": snippet})
        return results

    def _format_results(self, results: list[dict[str, str]]) -> str:
        parts = []
        for i, r in enumerate(results, 1):
            snippet = f" - {r['snippet']}" if r.get("snippet") else ""
            parts.append(f"{i}. {r['title']}\n   URL: {r['url']}{snippet}")
        return "\n\n".join(parts)
