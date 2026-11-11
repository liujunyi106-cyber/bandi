# 功能：契约——浏览器沙箱默认开启（默认启动参数不得含 --no-sandbox）
#
# 背景：`tools/browser.py`（BrowserRuntime，2 处 launch）与 `tools/web/browser_pool.py`
# "--disable-setuid-sandbox"]`——默认禁用 Chromium 沙箱，网页内容逃逸即可触达宿主。
#
# 契约钉死：
# 1. 默认（未设逃生开关）启动参数不含任何禁沙箱参数；
# 2. 仅显式 `BANDI_BROWSER_NO_SANDBOX=1/true/yes/on` 才放开（CI/容器逃生口）；
# 3. 两个生产 launch 入口（BrowserRuntime / BrowserPool，含 chrome 缺失回退路径）
# 都经同一参数构造点，默认沙箱在真实调用面生效。
from __future__ import annotations

import sys
import types
from typing import Any

import pytest

_NO_SANDBOX_FLAGS = ("--no-sandbox", "--disable-setuid-sandbox")


def _enable_flag(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("BANDI_BROWSER_NO_SANDBOX", value)


def _clear_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BANDI_BROWSER_NO_SANDBOX", raising=False)


# ── 参数构造点（默认/开关/假值）────────────────────────────────────────


def test_launch_args_default_excludes_no_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约（默认沙箱）：未设逃生开关时启动参数为空（Chromium 默认沙箱保持开启）。"""
    from bandi.tools.web.browser_pool import browser_launch_args

    _clear_flag(monkeypatch)
    args = browser_launch_args()
    for flag in _NO_SANDBOX_FLAGS:
        assert flag not in args, f"默认启动参数不得含 {flag}：{args}"


def test_launch_args_escape_hatch_enables_no_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约（逃生口）：显式开关打开才放开沙箱（CI/容器可能需要）。"""
    from bandi.tools.web.browser_pool import browser_launch_args

    for value in ("1", "true", "YES", "on"):
        _enable_flag(monkeypatch, value)
        args = browser_launch_args()
        for flag in _NO_SANDBOX_FLAGS:
            assert flag in args, f"BANDI_BROWSER_NO_SANDBOX={value} 时应含 {flag}：{args}"
    _clear_flag(monkeypatch)
    for flag in _NO_SANDBOX_FLAGS:
        assert flag not in browser_launch_args(), "开关移除后必须回落默认沙箱"


@pytest.mark.parametrize("value", ["0", "false", "off", "", "no"])
def test_launch_args_falsey_values_keep_sandbox(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """破坏者契约：假值/空值不得被当成开启（防误配静默禁沙箱）。"""
    from bandi.tools.web.browser_pool import browser_launch_args

    _enable_flag(monkeypatch, value)
    args = browser_launch_args()
    for flag in _NO_SANDBOX_FLAGS:
        assert flag not in args, f"开关值 {value!r} 不得放开沙箱：{args}"


# ── 假 Playwright：在真实 launch 调用面断言参数 ────────────────────────


class _FakePage:
    def set_default_timeout(self, timeout_ms: int) -> None:
        pass

    async def close(self) -> None:
        pass


class _FakeBrowser:
    async def new_page(self) -> _FakePage:
        return _FakePage()

    async def close(self) -> None:
        pass


class _FakeChromium:
    def __init__(self, calls: list[dict[str, Any]], *, fail_first: bool) -> None:
        self._calls = calls
        self._fail_first = fail_first

    async def launch(self, **kwargs: Any) -> _FakeBrowser:
        self._calls.append(kwargs)
        if self._fail_first and len(self._calls) == 1:
            raise RuntimeError("system chrome unavailable")
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self, calls: list[dict[str, Any]], *, fail_first: bool) -> None:
        self.chromium = _FakeChromium(calls, fail_first=fail_first)

    async def stop(self) -> None:
        pass


class _FakeDriver:
    def __init__(self, calls: list[dict[str, Any]], *, fail_first: bool) -> None:
        self._calls = calls
        self._fail_first = fail_first

    async def start(self) -> _FakePlaywright:
        return _FakePlaywright(self._calls, fail_first=self._fail_first)


def _install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]], *, fail_first: bool = False
) -> None:
    module = types.ModuleType("playwright.async_api")
    module.async_playwright = lambda: _FakeDriver(calls, fail_first=fail_first)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)


@pytest.mark.asyncio
async def test_browser_runtime_launches_without_no_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """契约（生产入口 browser.py）：BrowserRuntime 默认 launch 参数不含禁沙箱标志。"""
    from bandi.tools.browser import BrowserRuntime

    _clear_flag(monkeypatch)
    calls: list[dict[str, Any]] = []
    _install_fake_playwright(monkeypatch, calls)
    runtime = BrowserRuntime()
    await runtime.get_page()
    await runtime.close()

    assert calls, "必须真实调用 chromium.launch"
    assert calls[0].get("channel") == "chrome"
    for flag in _NO_SANDBOX_FLAGS:
        assert flag not in calls[0]["args"], f"BrowserRuntime 默认不得含 {flag}"


@pytest.mark.asyncio
async def test_browser_runtime_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """接线契约（browser.py）：逃生开关打开时 BrowserRuntime 才带禁沙箱参数。"""
    from bandi.tools.browser import BrowserRuntime

    _enable_flag(monkeypatch, "1")
    calls: list[dict[str, Any]] = []
    _install_fake_playwright(monkeypatch, calls)
    runtime = BrowserRuntime()
    await runtime.get_page()
    await runtime.close()

    for flag in _NO_SANDBOX_FLAGS:
        assert flag in calls[0]["args"], f"开关打开时应含 {flag}"


@pytest.mark.asyncio
async def test_browser_runtime_fallback_keeps_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约（生产回退路径，同族）：系统 chrome 缺失时回退 launch 与
    BrowserPool 同构造点——回退路径同样不得含禁沙箱标志，且不得带 channel。"""
    from bandi.tools.browser import BrowserRuntime

    _clear_flag(monkeypatch)
    calls: list[dict[str, Any]] = []
    _install_fake_playwright(monkeypatch, calls, fail_first=True)
    runtime = BrowserRuntime()
    await runtime.get_page()
    await runtime.close()

    assert len(calls) == 2, "首启失败必须走回退 launch（两条路径都要覆盖）"
    assert calls[0].get("channel") == "chrome"
    assert "channel" not in calls[1], "回退路径不得沿用 chrome channel"
    for kwargs in calls:
        for flag in _NO_SANDBOX_FLAGS:
            assert flag not in kwargs["args"], f"回退路径不得含 {flag}：{kwargs}"


@pytest.mark.asyncio
async def test_browser_pool_launch_defaults_to_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约（生产入口 browser_pool.py）：BrowserPool 两条 launch 路径默认均不含
    禁沙箱标志（含系统 chrome 缺失回退路径）。"""
    from bandi.tools.web.browser_pool import BrowserPool

    _clear_flag(monkeypatch)
    calls: list[dict[str, Any]] = []
    _install_fake_playwright(monkeypatch, calls, fail_first=True)
    pool = BrowserPool()
    await pool._launch()

    assert len(calls) == 2, "首启失败必须走回退 launch（两条路径都要覆盖）"
    for kwargs in calls:
        for flag in _NO_SANDBOX_FLAGS:
            assert flag not in kwargs["args"], f"回退路径不得含 {flag}：{kwargs}"


@pytest.mark.asyncio
async def test_browser_pool_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """接线契约（browser_pool.py）：逃生开关打开时 BrowserPool 才带禁沙箱参数。"""
    from bandi.tools.web.browser_pool import BrowserPool

    _enable_flag(monkeypatch, "true")
    calls: list[dict[str, Any]] = []
    _install_fake_playwright(monkeypatch, calls)
    pool = BrowserPool()
    await pool._launch()

    for kwargs in calls:
        for flag in _NO_SANDBOX_FLAGS:
            assert flag in kwargs["args"], f"开关打开时应含 {flag}"
