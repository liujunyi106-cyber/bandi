# 功能：契约——公司调度常驻驱动。
#
# 背景：API/桌面进程懒构造 CompanyDispatcher 且**不启动 run_forever**——前端派单
# startup 起后台 run_forever，shutdown 取消；pytest 环境强制关（测试不得起
# 调度循环写真实库）；BANDI_COMPANY_AUTORUN 显式可关。
from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

# 注意：`from bandi.api import app` 会被包 __init__ 的 re-export 遮蔽（拿到
# FastAPI 对象）——必须取模块本身（与 test_contract_b2_project_files 同口径）。
app_mod = importlib.import_module("bandi.api.app")


class TestAutorunGate:
    def test_disabled_under_pytest(self) -> None:
        assert app_mod._company_autorun_enabled({}, in_pytest=True) is False

    def test_default_enabled(self) -> None:
        assert app_mod._company_autorun_enabled({}, in_pytest=False) is True

    def test_env_off_variants(self) -> None:
        for value in ("0", "false", "no", "off", "FALSE"):
            assert (
                app_mod._company_autorun_enabled({"BANDI_COMPANY_AUTORUN": value}, in_pytest=False)
                is False
            ), value

    def test_env_on_variants(self) -> None:
        for value in ("1", "true", "yes", "on"):
            assert (
                app_mod._company_autorun_enabled({"BANDI_COMPANY_AUTORUN": value}, in_pytest=False)
                is True
            ), value


class _StubDispatcher:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = False

    async def run_forever(self, running: Any, **kwargs: Any) -> None:
        self.started.set()
        try:
            while running():
                await asyncio.sleep(0.01)
        finally:
            self.stopped = True


@pytest.mark.asyncio
async def test_startup_starts_loop_and_shutdown_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubDispatcher()
    monkeypatch.setattr(app_mod, "_get_company_dispatcher", lambda: stub)
    monkeypatch.setattr(app_mod, "_company_autorun_enabled", lambda *a, **k: True)

    await app_mod._startup_company_autorun()
    await asyncio.wait_for(stub.started.wait(), timeout=2)
    assert app_mod._company_loop_task is not None

    await app_mod._shutdown_company_autorun()
    assert stub.stopped is True
    assert app_mod._company_loop_task is None


@pytest.mark.asyncio
async def test_startup_noop_when_gate_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubDispatcher()
    monkeypatch.setattr(app_mod, "_get_company_dispatcher", lambda: stub)
    monkeypatch.setattr(app_mod, "_company_autorun_enabled", lambda *a, **k: False)

    await app_mod._startup_company_autorun()
    await asyncio.sleep(0.05)
    assert not stub.started.is_set()
    assert app_mod._company_loop_task is None
