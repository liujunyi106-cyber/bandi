from __future__ import annotations

from pathlib import Path

import pytest

from bandi.tools.list_dir import ListDirTool


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", root)
    return root


@pytest.fixture
def tmp_tree(workspace_root: Path) -> str:
    d = workspace_root
    (d / "alpha.txt").write_text("a")
    (d / "beta.txt").write_text("b")
    (d / "sub").mkdir()
    (d / "sub" / "nested.py").write_text("x")
    return str(d)


class TestListDirTool:
    @pytest.mark.asyncio
    async def test_list_current(self, tmp_tree: str) -> None:
        tool = ListDirTool()
        result = await tool.execute({"path": tmp_tree})
        assert "alpha.txt" in result
        assert "beta.txt" in result
        assert "sub/" in result

    @pytest.mark.asyncio
    async def test_list_with_depth(self, tmp_tree: str) -> None:
        tool = ListDirTool()
        result = await tool.execute({"path": tmp_tree, "depth": 2})
        assert "nested.py" in result

    @pytest.mark.asyncio
    async def test_list_limit(self, tmp_tree: str) -> None:
        tool = ListDirTool()
        result = await tool.execute({"path": tmp_tree, "limit": 1})
        lines = result.splitlines()
        assert "truncated" in result or len(lines) <= 4

    @pytest.mark.asyncio
    async def test_path_not_found(self, workspace_root: Path) -> None:
        tool = ListDirTool()
        with pytest.raises(FileNotFoundError):
            await tool.execute({"path": str(workspace_root / "missing_dir")})

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, workspace_root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("x")
        tool = ListDirTool()
        with pytest.raises(ValueError, match="outside workspace"):
            await tool.execute({"path": str(outside)})

    @pytest.mark.asyncio
    async def test_list_dir_symlink_dir_not_recursed(
        self, workspace_root: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "hidden_secret.py").write_text("x")
        (workspace_root / "sub").mkdir()
        (workspace_root / "sub" / "ok.py").write_text("x")
        try:
            (workspace_root / "link_out").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            import sys as _sys

            if _sys.platform != "win32":
                pytest.skip("symlink not supported on this platform")
            import _winapi

            try:
                _winapi.CreateJunction(str(outside), str(workspace_root / "link_out"))
            except (OSError, NotImplementedError):
                pytest.skip("directory link not supported on this platform")
        tool = ListDirTool()
        result = await tool.execute({"path": str(workspace_root), "depth": 2})
        assert "ok.py" in result
        assert "hidden_secret.py" not in result, "symlink 目录不得递归列出外部内容"
