# 功能：web 工具单测——schema/校验/选择器解析/host 限速串行/重试（深行为契约覆盖）
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.web import WebImageDownloadTool, WebImageSearchTool, WebLoginTool, WebScraperTool
from bandi.tools.web._polite_http import build_polite_client, host_request_gate, polite_get
from bandi.tools.web.scraper import parse_css_selector


def _ctx(tmp_path: Any, **extra: Any) -> ToolPermissionContext:
    merged: dict[str, Any] = {"project_root": str(tmp_path)}
    merged.update(extra)
    return ToolPermissionContext(user_id="unit-test", role="tester", mode="execute", extra=merged)


def test_web_tools_meta_capabilities() -> None:
    assert WebScraperTool().category == ToolCategory.WEB
    assert WebScraperTool().capability_tags == [ToolCapability.NETWORK.value]
    assert WebImageDownloadTool().capability_tags == [
        ToolCapability.NETWORK.value,
        ToolCapability.FILE_WRITE.value,
    ]
    assert WebLoginTool().capability_tags == [
        ToolCapability.NETWORK.value,
        ToolCapability.FILE_WRITE.value,
    ]
    for tool in (WebScraperTool(), WebImageDownloadTool(), WebImageSearchTool(), WebLoginTool()):
        assert tool.feature_group == "web"


def test_parser_accepts_tag_class_id_attr() -> None:
    assert parse_css_selector("h1") is not None
    assert parse_css_selector("h1.title") is not None
    assert parse_css_selector(".item") is not None
    assert parse_css_selector("#intro") is not None
    assert parse_css_selector("a[href]") is not None
    assert parse_css_selector("a[href=https://x.example]") is not None
    s = parse_css_selector("h1.title.big")
    assert s is not None and s.tag == "h1" and s.classes == ("title", "big")
    assert parse_css_selector("div > span") is None
    assert parse_css_selector("") is None


def test_scraper_params_validation() -> None:
    tool = WebScraperTool()
    assert tool.validate_params({})
    assert tool.validate_params({"selectors": {"h": "h1"}})  # 缺 url
    assert tool.validate_params(
        {"url": "https://a.com", "selectors": {"h": "h1"}, "bogus": 1}
    )  # 多余键


def test_scraper_schema_no_additional() -> None:
    schema = WebScraperTool().parameters
    assert schema.get("additionalProperties") is False
    assert {"url", "selectors", "selector", "multiple"} <= set(schema["properties"])


@pytest.mark.asyncio
async def test_host_request_gate_serializes_same_host() -> None:
    order: list[int] = []

    async def worker(n: int) -> None:
        async with host_request_gate("https://a.example/x", 100, 0):
            order.append(n)
            await asyncio.sleep(0.05)

    await asyncio.gather(worker(1), worker(2))
    assert order == [1, 2]


@pytest.mark.asyncio
async def test_host_request_gate_allows_different_hosts_in_parallel() -> None:
    order: list[int] = []

    async def worker(n: int) -> None:
        async with host_request_gate(f"https://host{n}.example/x", 100, 0):
            order.append(n)
            await asyncio.sleep(0.05)

    await asyncio.gather(worker(1), worker(2))
    assert order == [1, 2]


@pytest.mark.asyncio
async def test_polite_get_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, text="ok")

    async with build_polite_client(timeout=5, transport=httpx.MockTransport(handler)) as client:
        resp = await polite_get(
            client, "https://example.org/page", max_retries=1, retry_backoff_base_ms=1
        )
    assert resp.status_code == 200
    assert calls["n"] == 2


def test_image_search_params_max_results_bounds() -> None:
    schema = WebImageSearchTool().parameters
    assert schema["properties"]["max_results"]["maximum"] == 10
    assert schema["properties"]["max_results"]["minimum"] == 1
    assert schema["required"] == ["query"]


def test_login_schema_selectors_required() -> None:
    schema = WebLoginTool().parameters
    assert {"url", "username_selector", "password_selector", "submit_selector"} <= set(
        schema["required"]
    )
    assert "username" in schema["properties"] and "password" in schema["properties"]
