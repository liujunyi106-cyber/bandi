# 功能：契约——tools 域复查加固：shell 写目标全参数 / git 项目根沙箱 /
# 浏览器 driver 泄漏 / 确认缓存 TTL（一次性授权不被放大为永久）。
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# ---------- shell 写目标全参数提取 ----------


def test_contract_shell_write_targets_all_operands() -> None:
    """契约（沙箱全参数·破坏者）：rm/mkdir/tee/truncate 等多目标命令的**全部**位置
    参数必须提取为写目标——修复前只检末位（`rm 越界/a.txt other.txt` 只查到
    other.txt，越界写目标漏检）。"""
    from bandi.tools.permissions_pipeline import extract_shell_write_targets

    targets = extract_shell_write_targets("rm C:/outside/a.txt other.txt")
    assert any("outside" in t for t in targets), f"越界目标必须被提取: {targets}"

    targets2 = extract_shell_write_targets("mkdir /tmp/ok /tmp/evil")
    assert len([t for t in targets2 if "/tmp/" in t]) == 2, f"mkdir 全部目标: {targets2}"


def test_contract_shell_write_targets_cp_last_operand_regression() -> None:
    """回归：cp/mv 目标仍为末位（多源 cp 语义）——修复不得放宽到全参数。"""
    from bandi.tools.permissions_pipeline import extract_shell_write_targets

    targets = extract_shell_write_targets("cp a.txt b.txt dest/")
    assert targets == ["dest/"], f"cp 目标应为末位，实际 {targets}"


# ---------- git 工具项目根沙箱 ----------


@pytest.mark.asyncio
async def test_contract_git_ops_project_root_allowed(tmp_path: Path) -> None:
    """契约（项目根可用·破坏者）：project_root 内的 git 仓库必须可用——修复前
    resolve_within_workspace 拒绝项目根（BUG-J 同族漏网），真实项目上
    git_status/diff/commit 两个分支都不可用。"""
    import subprocess

    from bandi.tools.git_ops import _run_git

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    _, err, code = await _run_git(
        ["status", "--short"], cwd=str(tmp_path), project_root=str(tmp_path)
    )
    assert code == 0, f"project_root 内 git 必须可用: {err!r}"


@pytest.mark.asyncio
async def test_contract_git_ops_outside_path_rejected(tmp_path: Path) -> None:
    """契约（沙箱保持）：project_root 之外的路径仍必须被拒（修复不得放开沙箱）。"""
    from bandi.tools.git_ops import _run_git

    with pytest.raises(ValueError):
        await _run_git(["status"], cwd="C:/Windows", project_root=str(tmp_path))


# ---------- 浏览器 driver 泄漏 ----------


@pytest.mark.asyncio
async def test_contract_browser_driver_cleaned_on_launch_failure(monkeypatch) -> None:
    """契约（资源泄漏·破坏者）：chromium 两次 launch 全失败时必须回收 playwright
    driver——修复前 pw 从不 stop（每次失败泄漏一个 node 子进程）。"""
    stopped = {"n": 0}

    class _Chromium:
        async def launch(self, **kwargs: Any) -> Any:
            raise RuntimeError("no browser")

    class _PW:
        def __init__(self) -> None:
            self.chromium = _Chromium()

        async def stop(self) -> None:
            stopped["n"] += 1

    def _fake_async_playwright() -> Any:
        class _Ctx:
            async def start(self) -> Any:
                return _PW()

        return _Ctx()

    import playwright.async_api as pwa

    monkeypatch.setattr(pwa, "async_playwright", _fake_async_playwright)

    from bandi.tools.browser import BrowserRuntime

    runtime = BrowserRuntime()
    with pytest.raises(RuntimeError):
        await runtime.get_page()
    assert stopped["n"] == 1, "双 launch 失败路径必须 stop driver（防进程泄漏）"


# ---------- 确认缓存 TTL（一次性授权语义） ----------


@pytest.mark.asyncio
async def test_contract_confirm_cache_expires(monkeypatch, tmp_path: Path) -> None:
    """契约（一次性授权·破坏者）：确认缓存必须 TTL 过期——修复前 set 无期限
    用户「Allow once」被放大为进程级永久免问（同 tool+参数永不重问）。"""
    import bandi.tools.permissions_pipeline as pp
    from bandi.core.message import ToolCall
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    pipeline = PermissionsPipeline(project_root=str(tmp_path))
    pipeline.cache_approved("bash", {"command": "ls -la"})
    tc = ToolCall(id="c1", name="bash", arguments={"command": "ls -la"})

    v1 = await pipeline._check_ui(tc, None)
    assert v1.allow is True, "TTL 有效期内缓存必须命中（re-issue 穿透）"

    real_time = pp.time.time
    monkeypatch.setattr(pp.time, "time", lambda: real_time() + 3600)
    v2 = await pipeline._check_ui(tc, None)
    assert v2.allow is False, "确认缓存过期后不得继续放行（一次性授权不得永久化）"
