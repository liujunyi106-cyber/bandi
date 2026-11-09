# 功能：web 工具族契约族——抓取提取/下载落盘/搜索 fail-open/登录安全/gate/装配
# 行为族级覆盖（DoD-10）：web_scraper/web_image_download/web_image_search/web_login 全路径有契约
from __future__ import annotations

from typing import Any

import httpx
import pytest

_HTML = """\
<html><head><title>示例页</title></head><body>
<h1 class="title">页面标题</h1>
<p id="intro">第一段中文内容</p>
<ul><li class="item">甲</li><li class="item">乙</li></ul>
</body></html>"""


def _ctx(tmp_path: Any, **extra: Any) -> Any:
    from bandi.tools.base import ToolPermissionContext

    merged: dict[str, Any] = {"project_root": str(tmp_path)}
    merged.update(extra)
    return ToolPermissionContext(user_id="unit-test", role="tester", mode="execute", extra=merged)


def _scraper(**kwargs: Any) -> Any:
    from bandi.tools.web import WebScraperTool

    return WebScraperTool(**kwargs)


def _downloader(**kwargs: Any) -> Any:
    from bandi.tools.web import WebImageDownloadTool

    return WebImageDownloadTool(**kwargs)


def _searcher(**kwargs: Any) -> Any:
    from bandi.tools.web import WebImageSearchTool

    return WebImageSearchTool(**kwargs)


def _html_transport(status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("robots.txt"):
            return httpx.Response(404)
        return httpx.Response(
            status, text=_HTML, headers={"content-type": "text/html; charset=utf-8"}
        )

    return httpx.MockTransport(handler)


# ---------- web_scraper：CSS 提取族 ----------


@pytest.mark.asyncio
async def test_scraper_extracts_class_id_and_multiple(tmp_path: Any) -> None:
    tool = _scraper(transport=_html_transport())
    result = await tool.execute(
        {
            "url": "https://example.org/page",
            "selectors": {
                "title": "h1.title",
                "intro": "#intro",
                "items": "li.item",
            },
            "multiple": True,
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True
    assert result["data"]["title"] == ["页面标题"]
    assert result["data"]["intro"] == ["第一段中文内容"]
    assert result["data"]["items"] == ["甲", "乙"]


@pytest.mark.asyncio
async def test_scraper_default_first_match(tmp_path: Any) -> None:
    tool = _scraper(transport=_html_transport())
    result = await tool.execute(
        {"url": "https://example.org/page", "selector": "li.item"}, _ctx(tmp_path)
    )
    assert result["success"] is True
    assert result["data"]["result"] == "甲"


@pytest.mark.asyncio
async def test_scraper_non_html_content_type_rejected(tmp_path: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"PNG", headers={"content-type": "application/octet-stream"}
        )

    tool = _scraper(transport=httpx.MockTransport(handler))
    result = await tool.execute({"url": "https://example.org/img", "selector": "p"}, _ctx(tmp_path))
    assert result["success"] is False
    assert "content type" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_scraper_missing_selector_returns_error(tmp_path: Any) -> None:
    tool = _scraper(transport=_html_transport())
    result = await tool.execute({"url": "https://example.org/page"}, _ctx(tmp_path))
    assert result["success"] is False
    assert "selectors" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_scraper_attr_quoted_values_match(tmp_path: Any) -> None:
    html = (
        "<html><body>"
        '<a href="/x" data-kind="hero">link1</a>'
        "<a href=\"/y\" data-kind='wide'>link2</a>"
        "</body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})

    tool = _scraper(transport=httpx.MockTransport(handler))
    r_dq = await tool.execute(
        {
            "url": "https://example.org/p",
            "selectors": {"dq": 'a[data-kind="hero"]'},
            "multiple": True,
        },
        _ctx(tmp_path),
    )
    assert r_dq["success"] is True
    assert r_dq["data"]["dq"] == ["link1"]
    r_sq = await tool.execute(
        {
            "url": "https://example.org/p",
            "selectors": {"sq": "a[data-kind='wide']"},
            "multiple": True,
        },
        _ctx(tmp_path),
    )
    assert r_sq["success"] is True
    assert r_sq["data"]["sq"] == ["link2"]


@pytest.mark.asyncio
async def test_scraper_unsupported_selector_reports_per_field(tmp_path: Any) -> None:
    tool = _scraper(transport=_html_transport())
    result = await tool.execute(
        {"url": "https://example.org/page", "selectors": {"bad": "div>span x", "ok": "h1.title"}},
        _ctx(tmp_path),
    )
    assert result["success"] is True
    assert "error" in result["data"]["bad"]
    assert result["data"]["ok"] == "页面标题"


# ---------- web_image_download：字节 → 落盘可读回族 ----------


@pytest.mark.asyncio
async def test_image_download_writes_readable_png(tmp_path: Any) -> None:
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, content=png_bytes, headers={"content-type": "image/png"})

    tool = _downloader(transport=httpx.MockTransport(handler))
    result = await tool.execute(
        {"url": "https://example.org/photo.png", "output_path": "shots/logo.png"}, _ctx(tmp_path)
    )
    assert result["success"] is True
    assert result["mime"] == "image/png"
    assert result["size_bytes"] == len(png_bytes)
    out = tmp_path / result["file"]
    assert out.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_image_download_rejects_non_image(tmp_path: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>not an image</html>", headers={"content-type": "text/html"}
        )

    tool = _downloader(transport=httpx.MockTransport(handler))
    result = await tool.execute({"url": "https://example.org/x.html"}, _ctx(tmp_path))
    assert result["success"] is False
    assert "image content type" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_image_download_traversal_output_rejected(tmp_path: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"\x89PNG\r\n\x1a\n" + b"\x00", headers={"content-type": "image/png"}
        )

    tool = _downloader(transport=httpx.MockTransport(handler))
    result = await tool.execute(
        {"url": "https://example.org/p.png", "output_path": "../escape.png"}, _ctx(tmp_path)
    )
    assert result["success"] is False
    assert not (tmp_path.parent / "escape.png").exists()


# ---------- web_image_search：fail-open + remote 双态 ----------


@pytest.mark.asyncio
async def test_image_search_fail_open_without_credentials(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("BANDI_CSE_API_KEY", raising=False)
    monkeypatch.delenv("BANDI_CSE_CX", raising=False)
    result = await _searcher().execute({"query": "示例图片"}, _ctx(tmp_path))
    assert result["success"] is True
    assert result["configured"] is False
    assert result["results"] == []
    assert "BANDI_CSE" in str(result.get("guide", ""))


@pytest.mark.asyncio
async def test_image_search_remote_with_credentials(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("BANDI_CSE_API_KEY", "test-key")
    monkeypatch.setenv("BANDI_CSE_CX", "test-cx")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("q") == "示例图片"
        assert request.url.params.get("cx") == "test-cx"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "title": "图片1",
                        "link": "https://cdn.example.org/a.png",
                        "mime": "image/png",
                    },
                    {
                        "title": "图片2",
                        "link": "http://insecure.example/b.png",
                        "mime": "image/png",
                    },
                ]
            },
        )

    searcher = _searcher(transport=httpx.MockTransport(handler))
    result = await searcher.execute({"query": "示例图片", "max_results": 5}, _ctx(tmp_path))
    assert result["success"] is True
    assert result["configured"] is True
    assert len(result["results"]) == 1  # http:// 非 https 被过滤
    assert result["results"][0]["link"].startswith("https://")


# ---------- web_login：凭证解析 + 公网 URL 校验（不涉 playwright 真实启动） ----------


def test_login_resolves_env_secret(monkeypatch: Any) -> None:
    from bandi.tools.web.login import _resolve_secret

    monkeypatch.setenv("BANDI_TEST_PW", "s3cret")
    assert _resolve_secret("env:BANDI_TEST_PW") == "s3cret"
    assert _resolve_secret("inline-password") == "inline-password"
    assert _resolve_secret("env:MISSING_VAR") == ""
    assert _resolve_secret(None) is None


@pytest.mark.asyncio
async def test_login_rejects_non_https() -> None:
    from bandi.tools.web import WebLoginTool

    tool = WebLoginTool()
    result = await tool.execute(
        {
            "url": "http://plain.example/login",
            "username_selector": "#u",
            "username": "u",
            "password_selector": "#p",
            "password": "p",
            "submit_selector": "button",
        },
        _ctx("."),
    )
    assert result["success"] is False
    assert "https" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_login_requires_credentials() -> None:
    from bandi.tools.web import WebLoginTool

    tool = WebLoginTool()
    result = await tool.execute(
        {
            "url": "https://example.org/login",
            "username_selector": "#u",
            "password_selector": "#p",
        },
        _ctx("."),
    )
    assert result["success"] is False
    assert "username/password" in str(result.get("error", ""))


# ---------- polite_http 共享库：fail-open robots + per-host 串行 ----------


@pytest.mark.asyncio
async def test_robots_fail_open_when_missing(tmp_path: Any) -> None:
    from bandi.tools.web._robots import assert_fetch_allowed, clear_robots_cache

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    clear_robots_cache()
    async with build_polite_client_for_test(transport=httpx.MockTransport(handler)) as client:
        await assert_fetch_allowed(client, "https://robotsless.example/page")  # 不抛错


def build_polite_client_for_test(**kwargs: Any) -> Any:
    from bandi.tools.web._polite_http import build_polite_client

    return build_polite_client(**kwargs)


@pytest.mark.asyncio
async def test_robots_disallow_raises(tmp_path: Any) -> None:
    from bandi.tools.web._robots import assert_fetch_allowed, clear_robots_cache

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("robots.txt"):
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /private/\n",
                headers={"content-type": "text/plain"},
            )
        return httpx.Response(200, text="ok")

    clear_robots_cache()
    async with build_polite_client_for_test(transport=httpx.MockTransport(handler)) as client:
        await assert_fetch_allowed(client, "https://example.org/public")
        with pytest.raises(ValueError, match="robots.txt disallows"):
            await assert_fetch_allowed(client, "https://example.org/private/x")


# ---------- 装配族（factory 注册 + gate fail-closed） ----------


def test_web_tools_registered_with_gates() -> None:
    from bandi.agent.factory import build_tool_registry
    from bandi.tools.base import ToolCategory

    registry = build_tool_registry(feature_flags={"canvas": True, "web": True})
    scraper = registry.get("web_scraper")
    download = registry.get("web_image_download")
    search = registry.get("web_image_search")
    login = registry.get("web_login")
    assert scraper is not None and scraper.category == ToolCategory.WEB
    assert download is not None and download.category == ToolCategory.WEB
    assert search is not None and search.category == ToolCategory.WEB
    assert login is not None and login.category == ToolCategory.WEB
    assert scraper.to_tool_meta()["capability_tags"] == ["network"]
    assert download.to_tool_meta()["capability_tags"] == ["network", "file_write"]
    assert search.to_tool_meta()["capability_tags"] == ["network"]

    closed = build_tool_registry(feature_flags={"canvas": True, "web": False})
    assert closed.get("web_scraper") is None
    assert closed.get("web_image_download") is None
    assert closed.get("web_image_search") is None
    assert closed.get("web_login") is None
