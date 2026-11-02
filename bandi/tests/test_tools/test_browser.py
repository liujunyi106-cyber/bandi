from __future__ import annotations

import pytest

from bandi.tools.base import ToolDependency
from bandi.tools.browser import BrowserRuntime, BrowserTool
from bandi.tools.dependency_checker import DependencyChecker

pytestmark = pytest.mark.heavy


class TestBrowserTool:
    @pytest.mark.asyncio
    async def test_dependency_check(self) -> None:
        checker = DependencyChecker()
        deps = [ToolDependency(type="python_package", name="playwright", help_hint="")]
        missing = checker.describe_missing(deps)
        assert len(missing) == 0, f"playwright not available: {missing}"

    @pytest.mark.asyncio
    async def test_risk_level(self) -> None:
        tool = BrowserTool()
        assert tool.risk_level == "medium"
        assert tool.is_concurrency_safe is False
        assert tool.name == "browser"

    @pytest.mark.asyncio
    async def test_unknown_action(self) -> None:
        tool = BrowserTool()
        result = await tool.execute({"action": "invalid"})
        assert "unknown action" in result

    @pytest.mark.asyncio
    async def test_navigate_missing_url(self) -> None:
        tool = BrowserTool()
        result = await tool.execute({"action": "navigate"})
        assert "url is required" in result

    @pytest.mark.asyncio
    async def test_navigate_private_ip_rejected(self) -> None:
        """浏览器导航到私网地址必须拒绝（SSRF 防护，对齐 web_fetch）。"""
        tool = BrowserTool()
        with pytest.raises(ValueError, match="private|blocked"):
            await tool.execute({"action": "navigate", "url": "http://127.0.0.1:8000/"})

    @pytest.mark.asyncio
    async def test_navigate_localhost_rejected(self) -> None:
        tool = BrowserTool()
        with pytest.raises(ValueError, match="private|blocked"):
            await tool.execute({"action": "navigate", "url": "http://localhost:8000/"})

    @pytest.mark.asyncio
    async def test_navigate_file_scheme_rejected(self) -> None:
        """file:// 导航必须拒绝（禁止读取本地任意文件）。"""
        tool = BrowserTool()
        with pytest.raises(ValueError, match="file|blocked"):
            await tool.execute({"action": "navigate", "url": "file:///etc/passwd"})

    @pytest.mark.asyncio
    async def test_click_missing_selector(self) -> None:
        tool = BrowserTool()
        result = await tool.execute({"action": "click"})
        assert "selector is required" in result

    @pytest.mark.asyncio
    async def test_type_missing_params(self) -> None:
        tool = BrowserTool()
        result = await tool.execute({"action": "type"})
        assert "selector is required" in result

    @pytest.mark.asyncio
    async def test_screenshot_returns_path(self) -> None:
        tool = BrowserTool()
        result = await tool.execute({"action": "screenshot"})
        assert "error" not in result
        assert ".bandi" in result

    @pytest.mark.asyncio
    async def test_screenshot_paths_unique_per_call(self) -> None:
        """多会话/多次截图不得互相覆盖（路径必须唯一化）。"""
        tool = BrowserTool()
        first = await tool.execute({"action": "screenshot"})
        second = await tool.execute({"action": "screenshot"})
        assert "error" not in first and "error" not in second
        assert first != second

    @pytest.mark.asyncio
    async def test_scroll_no_params(self) -> None:
        tool = BrowserTool()
        result = await tool.execute({"action": "scroll"})
        assert "error" not in result
        assert "scrolled" in result

    @pytest.mark.asyncio
    async def test_select_option_missing_selector(self) -> None:
        tool = BrowserTool()
        result = await tool.execute({"action": "select_option"})
        assert "selector is required" in result

    @pytest.mark.asyncio
    async def test_select_option_missing_value(self) -> None:
        tool = BrowserTool()
        result = await tool.execute({"action": "select_option", "selector": "#s"})
        assert "value is required" in result

    @pytest.mark.asyncio
    async def test_close_returns_ok(self) -> None:
        tool = BrowserTool()
        result = await tool.execute({"action": "close"})
        assert "browser closed" in result

    @pytest.mark.asyncio
    async def test_back_missing_params(self) -> None:
        tool = BrowserTool()
        result = await tool.execute({"action": "back"})
        assert "error" not in result


class TestBrowserRuntime:
    @pytest.mark.asyncio
    async def test_close_without_start(self) -> None:
        rt = BrowserRuntime()
        await rt.close()
