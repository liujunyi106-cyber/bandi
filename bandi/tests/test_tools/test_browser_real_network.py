# 功能：BrowserTool 真实浏览器集成测试（依赖 playwright 1.61 + chromium 1228 + 外网）
# 环境缺浏览器或外网不可达时跳过；就绪则真实导航并断言最终产物（功能验证纪律）
from __future__ import annotations

import socket
from pathlib import Path

import pytest

from bandi.tools.browser import BrowserTool

_MS_PLAYWRIGHT = Path.home() / "AppData" / "Local" / "ms-playwright"
_BROWSER_OK = (_MS_PLAYWRIGHT / "chromium_headless_shell-1228").is_dir()


def _network_ok(host: str = "example.com", port: int = 443) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not (_BROWSER_OK and _network_ok()),
    reason="playwright chromium 1228 未安装或外网不可达（真实依赖测试）",
)


class TestBrowserRealNetwork:
    @pytest.mark.asyncio
    async def test_real_navigate_example_domain(self) -> None:
        tool = BrowserTool()
        snap = await tool.execute({"action": "navigate", "url": "https://example.com"})
        assert "Example Domain" in snap, "Title 必须正确"
        assert "https://example.com" in snap, "URL 必须正确"
        assert "documentation examples" in snap, "2026 新版正文必须被抓取"

    @pytest.mark.asyncio
    async def test_real_navigate_chinese_site(self) -> None:
        tool = BrowserTool()
        snap = await tool.execute({"action": "navigate", "url": "https://www.baidu.com"})
        assert "百度" in snap, f"中文页面返回必须正确，实际 {snap[:80]}"

    @pytest.mark.asyncio
    async def test_real_lifecycle_snapshot_close(self) -> None:
        tool = BrowserTool()
        snap1 = await tool.execute({"action": "navigate", "url": "https://example.com"})
        snap2 = await tool.execute({"action": "snapshot"})
        assert "Example Domain" in snap1 and "Example Domain" in snap2, "快照复用同一页面"
        r = await tool.execute({"action": "close"})
        assert "closed" in r, f"close 必须成功，实际 {r}"
