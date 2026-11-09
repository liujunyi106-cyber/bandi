# 功能：契约fs 工具层必须展开家目录符号 `~`
#
# 行为不变式（钉行为，不钉实现）：
#   1. `~` 开头路径按家目录展开，不得当成字面目录名拼在 workspace 后面
#   2. 展开后若落在 allowed roots 之外，仍走越界拒绝（不得因展开绕过沙箱）
#   3. 非 `~` 路径的既有语义不变（相对落workspace、越界绝对路径被拒）

from __future__ import annotations

from pathlib import Path

import pytest

from bandi.tools import fs_tool
from bandi.tools.fs_tool import resolve_within_workspace


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """功能：家目录与工作区根都指向 tmp，彻底隔离真实用户目录与真实工作区。

    须同时设USERPROFILE（expanduser 读它）与模块级 _WORKSPACE_ROOT
    （resolve_within_workspace 读模块常量，不读 workspace_root() 函数）。
    """
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.setattr(fs_tool, "_WORKSPACE_ROOT", workspace)
    return home, workspace


class TestTildeExpansion:
    def test_tilde_resolves_to_home_not_workspace(self, isolated: tuple[Path, Path]) -> None:
        """功能：`~/x` 展开到家目录，不得拼成 workspace/~/x。

        修复前实得...\\workspace\\~\\notes.txt（file not found）。
        """
        home, _ = isolated

        got = resolve_within_workspace("~/notes.txt", extra_roots=[str(home)])

        assert got == (home / "notes.txt").resolve()
        assert "~" not in str(got)

    def test_tilde_nested_path_expands(self, isolated: tuple[Path, Path]) -> None:
        """功能：多级 `~` 路径（~/.bandi/preferences.md）同样整体展开。"""
        home, _ = isolated

        got = resolve_within_workspace("~/.bandi/preferences.md", extra_roots=[str(home)])

        assert got == (home / ".bandi" / "preferences.md").resolve()

    def test_tilde_outside_allowed_still_rejected(self, isolated: tuple[Path, Path]) -> None:
        """功能：破坏者视角——展开不构成越界豁免，落在 allowed 外仍须拒绝。"""
        home, _ = isolated
        outside = home / "elsewhere"
        outside.mkdir()

        with pytest.raises(ValueError, match="path outside workspace"):
            resolve_within_workspace("~/notes.txt", extra_roots=[str(outside)])

    def test_tilde_without_extra_roots_rejected(self, isolated: tuple[Path, Path]) -> None:
        """功能：家目录默认不在 allowed roots → 报越界而非拼接后的假路径。"""
        with pytest.raises(ValueError) as exc:
            resolve_within_workspace("~/notes.txt")

        assert "path outside workspace" in str(exc.value)


class TestNonTildeBehaviorUnchanged:
    def test_relative_path_still_under_workspace(self, isolated: tuple[Path, Path]) -> None:
        """功能：普通相对路径语义不变，仍落workspace。"""
        _, workspace = isolated

        got = resolve_within_workspace("docs/a.md")

        assert got == (workspace / "docs" / "a.md").resolve()

    def test_absolute_outside_rejected(self, isolated: tuple[Path, Path]) -> None:
        """功能：越界绝对路径仍被拒绝（无回归）。"""
        with pytest.raises(ValueError, match="path outside workspace"):
            resolve_within_workspace("C:/Windows/System32/drivers")
