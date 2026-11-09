# 功能：契约——子进程能力（Windows SelectorEventLoop 不支持 spawn 的转译与自检）
# 背景：uvicorn 在 `reload or workers>1` 时显式使用 SelectorEventLoop（Windows）
# asyncio.create_subprocess_exec 抛**无参 NotImplementedError（str 为空）**——
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from bandi.tools.subprocess_tree import (
    spawn_managed_subprocess,
    subprocess_capability_warning,
)


class TestSelectorLoopSpawnTranslated:
    def test_selector_loop_spawn_raises_actionable_error(self) -> None:
        """契约（破坏者）：SelectorEventLoop 下 spawn 必须抛**可行动错误**
        （非空消息 + 指引），不得抛出空消息的 NotImplementedError。"""
        if sys.platform != "win32":
            pytest.skip("windows-only behavior")
        previous = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        try:
            with pytest.raises(Exception) as excinfo:
                asyncio.run(spawn_managed_subprocess(sys.executable, "-c", "print(1)"))
        finally:
            asyncio.set_event_loop_policy(previous)
        message = str(excinfo.value)
        assert message, "错误消息不得为空（原无参 NotImplementedError str 为空）"
        assert "SelectorEventLoop" in message or "reload" in message, (
            f"错误必须给出可行动指引（事件循环/--reload）：{message!r}"
        )


class TestCapabilityWarning:
    def test_selector_loop_returns_warning(self) -> None:
        """契约：Windows SelectorEventLoop → 返回非空告警（启动自检数据源）。"""
        if sys.platform != "win32":
            pytest.skip("windows-only behavior")
        loop = asyncio.SelectorEventLoop()
        try:
            warning = subprocess_capability_warning(loop)
        finally:
            loop.close()
        assert warning, "SelectorEventLoop 必须给出告警"
        assert "reload" in warning, f"告警须含可行动指引：{warning!r}"

    def test_proactor_loop_returns_none(self) -> None:
        """契约（对照组）：ProactorEventLoop 支持子进程 → 无告警。"""
        if sys.platform != "win32":
            pytest.skip("windows-only behavior")
        loop = asyncio.ProactorEventLoop()
        try:
            assert subprocess_capability_warning(loop) is None
        finally:
            loop.close()

    def test_proactor_spawn_still_works(self) -> None:
        """契约（对照组，端到端）：Proactor 下 spawn 正常跑通（防转译误伤正常路径）。"""
        if sys.platform != "win32":
            pytest.skip("windows-only behavior")

        async def run() -> str:
            proc = await spawn_managed_subprocess(
                sys.executable,
                "-c",
                "print('cap-ok')",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            return out.decode("utf-8", errors="replace").strip()

        assert asyncio.run(run()) == "cap-ok"


class TestProjectScripts:
    def test_dev_api_default_without_reload(self) -> None:
        """契约：默认 dev:api 必须无 --reload（Selector 循环会让 spawn 工具全灭）；
        热重载场景由显式 dev:api:reload 提供（工具链受限已在启动告警）。"""
        pkg = Path(__file__).resolve().parents[3] / "frontend" / "package.json"
        import json

        scripts = json.loads(pkg.read_text(encoding="utf-8"))["scripts"]
        assert "--reload" not in scripts["dev:api"], "dev:api 默认不得带 --reload"
        assert "--reload" in scripts.get("dev:api:reload", ""), (
            "须保留显式 dev:api:reload 供热重载场景"
        )
