from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bandi.tools.git_ops import GitCommitTool, GitDiffTool, GitStatusTool

GIT_AVAILABLE = shutil.which("git") is not None


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    import bandi.tools.fs_tool as fs_mod

    d = tmp_path / "repo"
    d.mkdir()
    monkeypatch.setattr(fs_mod, "_WORKSPACE_ROOT", tmp_path)
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
    import subprocess

    subprocess.run(["git", "init"], cwd=str(d), capture_output=True, check=False)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(d),
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tester"], cwd=str(d), capture_output=True, check=False
    )
    (d / "readme.md").write_text("# test\n")
    subprocess.run(["git", "add", "-A"], cwd=str(d), capture_output=True, check=False)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(d), capture_output=True, check=False)
    return str(d)


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not found on PATH")
class TestGitCwdSandbox:
    @pytest.mark.asyncio
    async def test_cwd_outside_workspace_rejected(self, tmp_path: Path) -> None:
        """cwd 指向工作区外必须拒绝（防信息泄漏/任意仓库提交）。"""
        import bandi.tools.fs_tool as fs_mod

        outside = tmp_path / "outside"
        outside.mkdir()
        # 工作区 = tmp_path/ws，outside 在其外
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(fs_mod, "_WORKSPACE_ROOT", ws)
        monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(ws))
        try:
            tool = GitStatusTool()
            with pytest.raises(ValueError, match="outside workspace"):
                await tool.execute({"cwd": str(outside)})
        finally:
            monkeypatch.undo()


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not found on PATH")
class TestGitStatusTool:
    @pytest.mark.asyncio
    async def test_clean_repo(self, git_repo: str) -> None:
        tool = GitStatusTool()
        result = await tool.execute({"cwd": git_repo})
        assert "(clean)" in result

    @pytest.mark.asyncio
    async def test_dirty_repo(self, git_repo: str) -> None:
        Path(git_repo, "new.txt").write_text("hello")
        tool = GitStatusTool()
        result = await tool.execute({"cwd": git_repo})
        assert "new.txt" in result


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not found on PATH")
class TestGitDiffTool:
    @pytest.mark.asyncio
    async def test_no_changes(self, git_repo: str) -> None:
        tool = GitDiffTool()
        result = await tool.execute({"cwd": git_repo})
        assert "(no changes)" in result

    @pytest.mark.asyncio
    async def test_has_diff(self, git_repo: str) -> None:
        Path(git_repo, "readme.md").write_text("# changed\n")
        tool = GitDiffTool()
        result = await tool.execute({"cwd": git_repo})
        assert "changed" in result or "+" in result


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not found on PATH")
class TestGitCommitTool:
    @pytest.mark.asyncio
    async def test_commit_success(self, git_repo: str) -> None:
        Path(git_repo, "file.txt").write_text("new")
        tool = GitCommitTool()
        result = await tool.execute({"cwd": git_repo, "message": "add file.txt"})
        assert "committed" in result

    @pytest.mark.asyncio
    async def test_commit_empty(self, git_repo: str) -> None:
        tool = GitCommitTool()
        with pytest.raises(RuntimeError):
            await tool.execute({"cwd": git_repo, "message": "nothing"})
