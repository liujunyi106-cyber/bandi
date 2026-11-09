# 功能：BrowserPool 共享库 — Playwright 浏览器实例池（懒加载 + 页面上限 + 生命周期）
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# 沙箱逃生开关：默认关闭（Chromium 默认沙箱保持开启）；CI/容器环境无
_NO_SANDBOX_ENV = "BANDI_BROWSER_NO_SANDBOX"


def no_sandbox_enabled() -> bool:
    # 功能：浏览器沙箱逃生开关读取——仅显式真值才禁用沙箱；假值/空值一律保持沙箱
    value = os.environ.get(_NO_SANDBOX_ENV, "").strip().lower()
    return value in ("1", "true", "yes", "on")


def browser_launch_args() -> list[str]:
    # 功能：Chromium 启动参数单一构造点——默认不含任何禁沙箱参数
    if no_sandbox_enabled():
        return ["--no-sandbox", "--disable-setuid-sandbox"]
    return []


def chromium_launch_kwargs(
    *, channel: str | None = None, user_data_dir: str | None = None
) -> dict[str, Any]:
    # 功能：chromium.launch 参数字典（browser.py / browser_pool.py 共用同一构造点）
    kwargs: dict[str, Any] = {"headless": True, "args": browser_launch_args()}
    if channel:
        kwargs["channel"] = channel
    if user_data_dir:
        kwargs["user_data_dir"] = user_data_dir
    return kwargs


def browser_profile_dir(extra: dict[str, Any] | None) -> str | None:
    """每员工独立持久化浏览器 profile（登录态/缓存不跨员工）。

    仅员工会话（extra.employee_id + employee_workspace 齐备）启用；个人助理
    会话返回 None，保持既有「临时浏览器会话」行为。
    """
    data = extra or {}
    if not str(data.get("employee_id") or "").strip():
        return None
    workspace = str(data.get("employee_workspace") or "").strip()
    if not workspace:
        return None
    root = Path(workspace) / "browser-profile"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return str(root)


class BrowserPool:
    # 功能：Playwright 浏览器连接池——懒加载单浏览器 + 并发页面上限（默认最大 5 页）
    _instance: BrowserPool | None = None

    def __init__(self, max_pages: int = 5, *, timeout_ms: int = 30000) -> None:
        self._max_pages = max(1, max_pages)
        self._timeout_ms = timeout_ms
        self._sem: asyncio.Semaphore | None = None
        self._browser: Any | None = None
        self._pw: Any | None = None
        self._pages: list[Any] = []
        self._launch_error: BaseException | None = None

    @classmethod
    def get_instance(cls, *, max_pages: int = 5, timeout_ms: int = 30000) -> BrowserPool:
        # 功能：单例获取；参数仅首次生效（close() 后重建新实例）
        if cls._instance is None:
            cls._instance = cls(max_pages=max_pages, timeout_ms=timeout_ms)
        return cls._instance

    async def _launch(self) -> None:
        # 功能：懒加载 playwright + 启动 chromium（优先系统 chrome，回退自带）
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(
                **chromium_launch_kwargs(channel="chrome")
            )
        except Exception:  # noqa: BLE001 - 无系统 Chrome 时回退 playwright 自带 chromium
            self._browser = await self._pw.chromium.launch(**chromium_launch_kwargs())

    async def _new_page(self) -> Any:
        if self._browser is None:
            await self._launch()
        assert self._browser is not None
        page: Any = await self._browser.new_page()
        page.set_default_timeout(self._timeout_ms)
        return page

    async def acquire(self) -> Any:
        # 功能：获取一个页面（超上限则等待）；调用方负责 finally 归还
        if self._sem is None:
            # 惰性创建：绑定首次 acquire 的事件循环（get_instance 可能早于 loop 调用）
            self._sem = asyncio.Semaphore(max(1, self._max_pages))
        await self._sem.acquire()
        # 建页失败（浏览器启动失败/崩溃/new_page 超时）必须归还信号量——否则每失败
        # 一次泄漏一个 permit，累计 max_pages 次后 acquire 永久阻塞（无超时的
        # Semaphore.acquire，浏览器工具全链挂死）。
        try:
            page = await self._new_page()
        except BaseException:
            self._sem.release()
            raise
        self._pages.append(page)
        return page

    async def release(self, page: Any) -> None:
        # 功能：归还并关闭页面，释放容量
        try:
            await page.close()
        except Exception:  # noqa: BLE001 - 页面已关闭则忽略
            pass
        self._pages = [p for p in self._pages if p is not page]
        if self._sem is not None:
            self._sem.release()

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Any]:
        # 功能：页面上下文管理器——acquire 后自动归还
        page = await self.acquire()
        try:
            yield page
        finally:
            await self.release(page)

    async def close(self) -> None:
        # 功能：关闭全部页面 + 浏览器 + playwright 实例
        for page in list(self._pages):
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
        self._pages = []
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
        BrowserPool._instance = None
