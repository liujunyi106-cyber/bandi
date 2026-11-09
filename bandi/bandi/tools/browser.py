# 功能：浏览器工具（BrowserTool 网页抓取 + 运行时管理）
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from bandi.tools.base import (
    BaseTool,
    RetryPolicy,
    ToolCategory,
    ToolDependency,
    ToolPermissionContext,
)

logger = logging.getLogger(__name__)

_MS_PER_SEC = 1000


class BrowserTool(BaseTool):
    external_content: bool = True
    name: str = "browser"
    description: str = (
        "Automate a web browser: navigate, click, type, snapshot, screenshot, scroll, "
        "wait, select_option, back, or close."
    )
    category: ToolCategory = ToolCategory.WEB
    risk_level: str = "medium"
    permissions_override: str = "ask"
    permission_action: str = "execute"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)
    # 需要身份上下文以派生每员工独立浏览器 profile
    needs_context: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["action"],
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "navigate",
                    "click",
                    "type",
                    "snapshot",
                    "screenshot",
                    "scroll",
                    "wait",
                    "select_option",
                    "back",
                    "close",
                ],
                "description": "Action to perform",
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to (required for navigate)",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector for target element (required for click/type/select_option)",
            },
            "text": {
                "type": "string",
                "description": "Text to type or wait for",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 30, max 120)",
                "default": 30,
            },
            "x": {
                "type": "integer",
                "description": "Horizontal scroll pixels (for scroll action)",
            },
            "y": {
                "type": "integer",
                "description": "Vertical scroll pixels (for scroll action)",
            },
            "value": {
                "type": "string",
                "description": "Option value to select (for select_option action)",
            },
        },
    }
    is_concurrency_safe: bool = False
    dependencies: list[ToolDependency] = [
        ToolDependency(
            type="python_package",
            name="playwright",
            help_hint="pip install playwright && playwright install chromium",
        ),
    ]

    _runtime: BrowserRuntime | None = None

    @staticmethod
    async def _assert_navigation_url(url: str) -> None:
        """SSRF 防护：浏览器导航目标必须为公网 http(s)，拒绝 file/data 与私网地址。"""
        from urllib.parse import urlparse

        from bandi.tools.web_fetch import _assert_public_url

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"blocked navigation scheme: {parsed.scheme!r} (http/https only)")
        await _assert_public_url(url)

    def __init__(
        self,
        timeout_default: int = 30,
        timeout_max: int = 120,
        post_click_delay_ms: int = 500,
        snapshot_preview_chars: int = 3000,
        max_interactive_elements: int = 50,
        element_text_max_chars: int = 60,
    ):
        self._runtime: BrowserRuntime | None = None
        self._timeout_default = timeout_default
        self._timeout_max = timeout_max
        self._post_click_delay_ms = post_click_delay_ms
        self._snapshot_preview_chars = snapshot_preview_chars
        self._max_interactive_elements = max_interactive_elements
        self._element_text_max_chars = element_text_max_chars

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        action = params["action"]
        timeout = max(1, min(int(params.get("timeout", self._timeout_default)), self._timeout_max))

        if action == "close":
            if self._runtime:
                await self._runtime.close()
                self._runtime = None
            return "browser closed"

        if self._runtime is None:
            from bandi.tools.web.browser_pool import browser_profile_dir

            # 员工会话用独立持久化 profile；个人助理回退临时会话
            self._runtime = BrowserRuntime(
                profile_dir=browser_profile_dir(getattr(context, "extra", None))
            )
        page = await self._runtime.get_page()

        if action == "navigate":
            url = params.get("url", "")
            if not url:
                return "error: url is required for navigate"
            await self._assert_navigation_url(url)
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout * _MS_PER_SEC)
            return await self._snapshot(page)

        if action == "click":
            sel = params.get("selector", "")
            if not sel:
                return "error: selector is required for click"
            await page.click(sel, timeout=timeout * _MS_PER_SEC)
            await page.wait_for_timeout(self._post_click_delay_ms)
            return f"clicked '{sel}'"

        if action == "type":
            sel = params.get("selector", "")
            txt = params.get("text", "")
            if not sel:
                return "error: selector is required for type"
            if not txt:
                return "error: text is required for type"
            await page.fill(sel, txt, timeout=timeout * _MS_PER_SEC)
            preview = (
                txt[: self._element_text_max_chars] + "..."
                if len(txt) > self._element_text_max_chars
                else txt
            )
            return f"typed '{preview}' into '{sel}'"

        if action == "snapshot":
            return await self._snapshot(page)

        if action == "screenshot":
            tmp_dir = Path.home() / ".bandi" / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            out = tmp_dir / f"browser_screenshot_{uuid.uuid4().hex[:8]}.png"
            await page.screenshot(path=str(out), full_page=True)
            return f"screenshot saved to {out}"

        if action == "scroll":
            x = params.get("x", 0)
            y = params.get("y", 0)
            await page.evaluate(f"window.scrollBy({x}, {y})")
            return f"scrolled by ({x}, {y})"

        if action == "wait":
            sec = max(1, min(int(params.get("timeout", self._timeout_default)), self._timeout_max))
            txt = params.get("text", "")
            if txt:
                await page.wait_for_selector(f"text={txt}", timeout=sec * _MS_PER_SEC)
                return f"waited for text '{txt}'"
            await page.wait_for_timeout(sec * _MS_PER_SEC)
            return f"waited {sec}s"

        if action == "select_option":
            sel = params.get("selector", "")
            val = params.get("value", "")
            if not sel:
                return "error: selector is required for select_option"
            if not val:
                return "error: value is required for select_option"
            await page.select_option(sel, value=val, timeout=timeout * _MS_PER_SEC)
            return f"selected '{val}' in '{sel}'"

        if action == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=timeout * _MS_PER_SEC)
            return await self._snapshot(page)

        return f"error: unknown action '{action}'"

    async def _snapshot(self, page: object) -> str:
        title = await page.title()
        url = page.url
        text = await page.evaluate("document.body.innerText")
        interactive = await page.evaluate(
            f"""() => {{
                const els = document.querySelectorAll('a[href], button, input, textarea, select, [role="button"], [onclick]');
                return Array.from(els).slice(0, {self._max_interactive_elements}).map(el => {{
                    const tag = el.tagName.toLowerCase();
                    const type = el.type || '';
                    const id = el.id ? '#' + el.id : '';
                    const cls = Array.from(el.classList).slice(0, 2).map(c => '.' + c).join('');
                    const name = el.getAttribute('name') ? '[name="' + el.getAttribute('name') + '"]' : '';
                    const text = (el.textContent || '').trim().slice(0, {self._element_text_max_chars});
                    const placeholder = el.getAttribute('placeholder') || '';
                    const aria = el.getAttribute('aria-label') || '';
                    const href = el.getAttribute('href') || '';
                    const selectors = tag + id + cls + name;
                    return {{ tag, type, selectors, text, placeholder, aria, href }};
                }});
            }}"""
        )
        lines = [f"Title: {title}", f"URL: {url}", ""]
        text_preview = text[: self._snapshot_preview_chars]
        lines.append(text_preview)
        if len(text) > self._snapshot_preview_chars:
            lines.append("... (truncated)")
        lines.extend(["", "--- Interactive elements ---"])
        for el in interactive:
            label = el["text"] or el["placeholder"] or el["aria"] or el["href"][:40]
            lines.append(f"  <{el['tag']}>{label}  → {el['selectors']}")
        return "\n".join(lines)


class BrowserRuntime:
    def __init__(self, profile_dir: str | None = None) -> None:
        # 复用同一浏览器会话/页面（导航状态污染、会话交叉）
        self._instance: object | None = None
        self._page: object | None = None
        # 员工独立持久化 profile 目录（None=临时会话，既有行为）
        self._profile_dir = profile_dir

    async def get_page(self) -> object:
        if self._page is not None:
            return self._page
        from playwright.async_api import async_playwright

        from bandi.tools.web.browser_pool import chromium_launch_kwargs

        if self._instance is None:
            pw = await async_playwright().start()
            if self._profile_dir:
                # 每员工独立持久化上下文——登录态/缓存不跨员工共享
                try:
                    holder = await pw.chromium.launch_persistent_context(
                        user_data_dir=self._profile_dir,
                        **chromium_launch_kwargs(channel="chrome"),
                    )
                except Exception:  # noqa: BLE001 - 无系统 Chrome 时回退自带 chromium
                    try:
                        holder = await pw.chromium.launch_persistent_context(
                            user_data_dir=self._profile_dir,
                            **chromium_launch_kwargs(),
                        )
                    except Exception:
                        # 两次 launch 全失败：driver 必须回收（防 node 子进程泄漏）
                        await pw.stop()
                        raise
                self._instance = (pw, holder)
            else:
                try:
                    browser = await pw.chromium.launch(**chromium_launch_kwargs(channel="chrome"))
                except Exception:  # noqa: BLE001 - 无系统 Chrome 时回退 playwright 自带 chromium
                    try:
                        browser = await pw.chromium.launch(**chromium_launch_kwargs())
                    except Exception:
                        await pw.stop()
                        raise
                self._instance = (pw, browser)
        _, holder = self._instance
        if self._profile_dir:
            pages = getattr(holder, "pages", None) or []
            self._page = pages[0] if pages else await holder.new_page()
        else:
            self._page = await holder.new_page()
        self._page.set_default_timeout(30000)
        return self._page

    async def close(self) -> None:
        if self._page is not None:
            await self._page.close()
            self._page = None
        if self._instance is not None:
            pw, holder = self._instance
            await holder.close()
            await pw.stop()
            self._instance = None
