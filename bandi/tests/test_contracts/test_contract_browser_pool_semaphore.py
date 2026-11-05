"""
契约族：BrowserPool 信号量生命周期（建页失败不得泄漏 permit）。

背景（本次全量审查发现，阻塞类）：
- `tools/web/browser_pool.py::acquire()` 先 `await self._sem.acquire()` 再
  `await self._new_page()`。`_new_page()` 会触发懒加载 `_launch()`（Playwright 启动
  chromium，无系统 Chrome 时还要回退重试）——启动失败/崩溃/new_page 超时都会抛出。
- 抛出路径上没有 `release()`：每失败一次泄漏一个 permit。累计 `max_pages`（默认 5）
  次失败后，`Semaphore.acquire()` 无超时且永不返回 → 浏览器工具全链**永久挂死**。
- 调用方 `tools/web/login.py` 的 acquire 在 try 之外，也无法兜底归还。

契约钉的行为：失败必须归还 permit（池恢复满容量），后续 acquire 可正常取得页面。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bandi.tools.web.browser_pool import BrowserPool


def _make_pool(max_pages: int = 2) -> BrowserPool:
    pool = BrowserPool(max_pages=max_pages)
    pool._sem = asyncio.Semaphore(max(1, max_pages))
    return pool


@pytest.mark.asyncio
async def test_acquire_releases_permit_when_page_creation_fails(monkeypatch: Any) -> None:
    """建页抛错必须归还 permit——否则容量被永久吞掉。"""
    pool = _make_pool(max_pages=2)

    async def _boom() -> Any:
        raise RuntimeError("chromium launch failed")

    monkeypatch.setattr(pool, "_new_page", _boom)

    for _ in range(5):  # 远超 max_pages 次失败
        with pytest.raises(RuntimeError):
            await pool.acquire()

    assert pool._sem is not None
    assert pool._sem._value == 2, (
        f"建页失败泄漏 permit：剩余容量 {pool._sem._value} != 2（累计 max_pages 次后 acquire 将永久阻塞）"
    )
    assert pool._pages == [], "失败路径不得登记半成品页面"


@pytest.mark.asyncio
async def test_pool_usable_after_repeated_failures(monkeypatch: Any) -> None:
    """破坏者：连续失败后正常建页必须仍能取到（不会因耗尽而挂死）。"""
    pool = _make_pool(max_pages=1)

    async def _boom() -> Any:
        raise RuntimeError("launch failed")

    monkeypatch.setattr(pool, "_new_page", _boom)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await pool.acquire()

    sentinel = object()

    async def _ok() -> Any:
        return sentinel

    monkeypatch.setattr(pool, "_new_page", _ok)
    page = await asyncio.wait_for(pool.acquire(), timeout=2.0)
    assert page is sentinel


@pytest.mark.asyncio
async def test_acquisition_cancellation_does_not_leak(monkeypatch: Any) -> None:
    """取消（CancelledError 属 BaseException）同样不得泄漏 permit。"""
    pool = _make_pool(max_pages=1)

    async def _cancelled() -> Any:
        raise asyncio.CancelledError()

    monkeypatch.setattr(pool, "_new_page", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        await pool.acquire()

    assert pool._sem is not None and pool._sem._value == 1
