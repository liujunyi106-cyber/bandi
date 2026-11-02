# 功能：契约沙箱须放行用户日常目录与应用数据目录
#
# 产品预期：桌面端形态下
# C 盘），普通会话/项目会话都应能正常读写桌面文件。
#
# 行为不变式（钉行为，不钉实现）：
# 1. 应用数据目录 ~/.bandi 可解析——系统提示引导的偏好文件必须可达
# 2. 桌面/文档/下载可解析
#   3. 放行范围可由 env 显式扩展（桌面端部署到自定义盘时用）
#   4. 不放行任何系统目录（破坏者视角：沙箱缺口不得扩大到 Windows/Program Files）
#   5. 用户目录与项目根、workspace 构成并集，不互相取代
#   6. role_fs_scopes 窄化仍然生效（角色边界不被放行绕过）

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from bandi.tools import fs_tool
from bandi.tools.fs_tool import resolve_within_workspace, user_scope_roots


@pytest.fixture
def user_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """功能：在隔离家目录下建出应用数据目录与桌面/文档/下载，并隔离 workspace 根。"""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    for name in (".bandi", "Desktop", "Documents", "Downloads"):
        (home / name).mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BANDI_FS_USER_ROOTS", raising=False)
    monkeypatch.setattr(fs_tool, "_WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


class TestUserScopeRoots:
    def test_includes_app_data_dir(self, user_dirs: Path) -> None:
        """功能：~/.bandi 必须进放行根——提示词引导的偏好文件在此。"""
        roots = {Path(p).resolve() for p in user_scope_roots()}

        assert (user_dirs / ".bandi").resolve() in roots

    def test_includes_desktop_documents_downloads(self, user_dirs: Path) -> None:
        """功能：桌面/文档/下载必须进放行根。"""
        roots = {Path(p).resolve() for p in user_scope_roots()}

        for name in ("Desktop", "Documents", "Downloads"):
            assert (user_dirs / name).resolve() in roots

    def test_skips_missing_dirs(self, user_dirs: Path) -> None:
        """功能：不存在的目录不进放行根（避免解析噪声与无谓 is_relative_to 开销）。"""
        import shutil

        shutil.rmtree(user_dirs / "Downloads")

        roots = {Path(p).resolve() for p in user_scope_roots()}

        assert (user_dirs / "Downloads").resolve() not in roots

    def test_env_override_extends_roots(self, user_dirs: Path, monkeypatch: Any) -> None:
        """功能：env 可显式追加自定义根（桌面端装到 D 盘时配置自定义数据/工作目录）。"""
        extra = user_dirs / "D盘项目"
        extra.mkdir()
        monkeypatch.setenv("BANDI_FS_USER_ROOTS", str(extra))

        roots = {Path(p).resolve() for p in user_scope_roots()}

        assert extra.resolve() in roots

    def test_env_override_accepts_multiple(self, user_dirs: Path, monkeypatch: Any) -> None:
        """功能：env 多根按 os.pathsep 分隔。"""
        a = user_dirs / "A"
        b = user_dirs / "B"
        a.mkdir()
        b.mkdir()
        monkeypatch.setenv("BANDI_FS_USER_ROOTS", os.pathsep.join([str(a), str(b)]))

        roots = {Path(p).resolve() for p in user_scope_roots()}

        assert {a.resolve(), b.resolve()} <= roots


class TestUserPathsResolvable:
    def test_app_data_preferences_resolvable(self, user_dirs: Path) -> None:
        """功能：端到端——`~/.bandi/preferences.md` 经沙箱解析成功（修复前越界拒绝）。"""
        got = resolve_within_workspace("~/.bandi/preferences.md")

        assert got == (user_dirs / ".bandi" / "preferences.md").resolve()

    def test_desktop_file_resolvable(self, user_dirs: Path) -> None:
        """功能：端到端——桌面文件可读写解析。"""
        got = resolve_within_workspace("~/Desktop/menu.txt")

        assert got == (user_dirs / "Desktop" / "menu.txt").resolve()

    def test_absolute_desktop_path_resolvable(self, user_dirs: Path) -> None:
        """功能：绝对桌面路径同样放行（用户/模型常给绝对路径）。"""
        target = user_dirs / "Desktop" / "plan.txt"

        assert resolve_within_workspace(str(target)) == target.resolve()


class TestSandboxNotWeakened:
    def test_system_dirs_still_rejected(self, user_dirs: Path) -> None:
        """功能：破坏者视角——放行用户目录不得顺带放开系统目录。"""
        for forbidden in ("C:/Windows/System32/drivers/etc/hosts", "C:/Program Files/app"):
            with pytest.raises(ValueError, match="path outside workspace"):
                resolve_within_workspace(forbidden)

    def test_other_user_dir_still_rejected(self, user_dirs: Path) -> None:
        """功能：家目录之外的任意目录仍拒绝（放行是枚举白名单，不是整个盘符）。"""
        outside = user_dirs.parent / "别人的目录"
        outside.mkdir(exist_ok=True)

        with pytest.raises(ValueError, match="path outside workspace"):
            resolve_within_workspace(str(outside / "x.txt"))

    def test_workspace_still_allowed(self, user_dirs: Path) -> None:
        """功能：workspace 仍是合法根（放行是并集，不是取代）。"""
        got = resolve_within_workspace("docs/a.md")

        assert got == (user_dirs.parent / "workspace" / "docs" / "a.md").resolve()

    def test_role_fs_scopes_still_narrow_user_dirs(self, user_dirs: Path) -> None:
        """功能：破坏者视角——role_fs_scopes 窄化对用户目录同样生效。"""
        with pytest.raises(ValueError, match="path outside role fs scopes"):
            resolve_within_workspace("~/Desktop/menu.txt", role_fs_scopes=["~/Documents"])


class TestProjectRootUnion:
    def test_project_root_unioned_with_user_dirs(self, user_dirs: Path, tmp_path: Path) -> None:
        """功能：项目会话的项目根与用户目录并存（项目文件 + 桌面都能碰）。"""
        project = tmp_path / "proj"
        project.mkdir()
        (project / "fixtures").mkdir()

        project_hit = resolve_within_workspace(
            "fixtures/sales.csv", extra_roots=[str(project)], base=str(project)
        )
        desktop_hit = resolve_within_workspace("~/Desktop/a.txt")

        assert project_hit == (project / "fixtures" / "sales.csv").resolve()
        assert desktop_hit == (user_dirs / "Desktop" / "a.txt").resolve()
