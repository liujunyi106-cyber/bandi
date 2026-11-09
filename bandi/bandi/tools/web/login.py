# 功能：WebLoginTool — Playwright 自动化登录（env 门控凭证 + 会话持久化到工作区）
from __future__ import annotations

import os
from typing import Any

from bandi.tools.base import ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool
from bandi.tools.web.browser_pool import BrowserPool


def _resolve_secret(raw: str | None) -> str | None:
    # 功能：凭证解析——`env:VAR` 从环境变量取（推荐，不落盘明文）；否则原样返回
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.startswith("env:"):
        return os.environ.get(s[4:], "")
    return s


class WebLoginTool(FsTool):
    external_content: bool = True
    name: str = "web_login"
    description: str = (
        "Automate a login on a public web application using Playwright. Fills "
        "username/password into the given CSS selectors and submits. Credentials "
        "may be passed inline or referenced as `env:VAR_NAME` to avoid embedding "
        "secrets in tool arguments. Optionally saves the authenticated session "
        "cookie jar into the workspace so later browser tool calls stay logged in."
    )
    category: ToolCategory = ToolCategory.WEB
    feature_group: str = "web"
    capability_tags: list[str] = [
        "network",
        "file_write",
    ]
    risk_level: str = "high"
    is_concurrency_safe: bool = False
    is_destructive: bool = False
    is_idempotent: bool = False
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["url", "username_selector", "password_selector", "submit_selector"],
        "additionalProperties": False,
        "properties": {
            "url": {"type": "string", "description": "Public login page URL."},
            "username_selector": {
                "type": "string",
                "description": "CSS selector for username input.",
            },
            "username": {
                "type": "string",
                "description": "Username/email, or `env:VAR_NAME` to read from the environment.",
            },
            "password_selector": {
                "type": "string",
                "description": "CSS selector for password input.",
            },
            "password": {
                "type": "string",
                "description": "Password, or `env:VAR_NAME` to read from the environment.",
            },
            "submit_selector": {
                "type": "string",
                "description": "CSS selector for the submit button.",
            },
            "wait_after_submit_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": 30000,
                "default": 3000,
                "description": "Milliseconds to wait after clicking submit.",
            },
            "save_session": {
                "type": "boolean",
                "default": False,
                "description": "Persist the authenticated cookie jar in the workspace.",
            },
        },
    }

    def __init__(self, *, max_pages: int = 2, timeout_ms: int = 30000) -> None:
        self._max_pages = max_pages
        self._timeout_ms = timeout_ms

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        url = str(params.get("url") or "").strip()
        user_sel = str(params.get("username_selector") or "").strip()
        pass_sel = str(params.get("password_selector") or "").strip()
        submit_sel = str(params.get("submit_selector") or "").strip()
        if not (url and url.startswith("https://")):
            return {"success": False, "error": "url must be a public https:// page"}
        username = _resolve_secret(params.get("username"))
        password = _resolve_secret(params.get("password"))
        if not username or not password:
            return {
                "success": False,
                "error": "username/password required (inline or env:VAR_NAME)",
            }
        wait_ms = min(int(params.get("wait_after_submit_ms", 3000)), 30000)
        save_session = bool(params.get("save_session", False))

        from bandi.tools.browser import BrowserTool

        try:
            await BrowserTool._assert_navigation_url(url)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        pool = BrowserPool.get_instance(max_pages=self._max_pages, timeout_ms=self._timeout_ms)
        page = await pool.acquire()
        try:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=min(self._timeout_ms, 60000)
            )
            await page.fill(user_sel, username, timeout=min(self._timeout_ms, 60000))
            await page.fill(pass_sel, password, timeout=min(self._timeout_ms, 60000))
            await page.click(submit_sel, timeout=min(self._timeout_ms, 60000))
            await page.wait_for_timeout(wait_ms)
            final_url = page.url
            title = await page.title()
            storage = None
            if save_session:
                storage = await page.context.storage_state()
                cookies = storage.get("cookies") or []
                origin = url.split("://", 1)[1].split("/", 1)[0]
                rel = f"sessions/{origin}.session.json"
                path = self._resolve_output(rel, context)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    _dump_session({c["name"]: c["value"] for c in cookies if c.get("value")}),
                    encoding="utf-8",
                )
            return {
                "success": True,
                "final_url": final_url,
                "title": title,
                "logged_in": True,
                "session_saved": bool(save_session and storage),
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"login failed: {exc}"}
        finally:
            await pool.release(page)


def _dump_session(cookies: dict[str, str]) -> str:
    # 功能：将 cookie 名值对序列化为紧凑 JSON（不含口令；名字到值），不落明文口令
    import json

    return json.dumps(
        {"cookies": cookies, "note": "session cookie jar (no passwords stored)"}, ensure_ascii=False
    )
