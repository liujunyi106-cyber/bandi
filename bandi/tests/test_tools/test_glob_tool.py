from __future__ import annotations

from pathlib import Path

import pytest

from bandi.tools.glob_tool import GlobTool


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", root)
    return root


@pytest.fixture
def tmp_tree(workspace_root: Path) -> str:
    d = workspace_root
    (d / "alpha.py").write_text("x")
    (d / "beta.py").write_text("y")
    (d / "sub").mkdir()
    (d / "sub" / "nested.py").write_text("z")
    (d / "sub" / "data.txt").write_text("w")
    return str(d)


class TestGlobTool:
    @pytest.mark.asyncio
    async def test_glob_simple(self, tmp_tree: str) -> None:
        tool = GlobTool()
        result = await tool.execute({"pattern": "*.py", "path": tmp_tree})
        assert "alpha.py" in result
        assert "beta.py" in result
        assert "nested.py" not in result

    @pytest.mark.asyncio
    async def test_glob_recursive(self, tmp_tree: str) -> None:
        tool = GlobTool()
        result = await tool.execute({"pattern": "**/*.py", "path": tmp_tree})
        assert "nested.py" in result

    @pytest.mark.asyncio
    async def test_glob_no_matches(self, tmp_tree: str) -> None:
        tool = GlobTool()
        result = await tool.execute({"pattern": "*.rs", "path": tmp_tree})
        assert "no matches found" in result

    @pytest.mark.asyncio
    async def test_glob_path_not_found(self, workspace_root: Path) -> None:
        tool = GlobTool()
        with pytest.raises(FileNotFoundError):
            await tool.execute({"pattern": "*.py", "path": str(workspace_root / "missing_dir")})

    @pytest.mark.asyncio
    async def test_glob_path_traversal_rejected(self, workspace_root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "evil.py").write_text("x")
        tool = GlobTool()
        with pytest.raises(ValueError, match="outside workspace"):
            await tool.execute({"pattern": "*.py", "path": str(outside)})

    @pytest.mark.asyncio
    async def test_glob_absolute_pattern_outside_workspace_rejected(
        self, workspace_root: Path, tmp_path: Path
    ) -> None:
        """绝对路径 pattern 指向工作区外必须拒绝（不得枚举任意目录）。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "evil.py").write_text("x")
        tool = GlobTool()
        with pytest.raises(ValueError, match="outside workspace"):
            await tool.execute({"pattern": str(outside / "*.py"), "path": str(workspace_root)})

    @pytest.mark.asyncio
    async def test_glob_relative_pattern_escape_rejected(
        self, workspace_root: Path, tmp_path: Path
    ) -> None:
        """相对 pattern 含 ../ 逃逸到工作区外必须拒绝。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "evil.py").write_text("x")
        tool = GlobTool()
        with pytest.raises(ValueError, match="outside workspace"):
            await tool.execute({"pattern": "../outside/*.py", "path": str(workspace_root)})

    @pytest.mark.asyncio
    async def test_glob_absolute_pattern_inside_workspace_allowed(
        self, workspace_root: Path
    ) -> None:
        """绝对路径 pattern 指向工作区内正常匹配。"""
        (workspace_root / "alpha.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(
            {"pattern": str(workspace_root / "*.py"), "path": str(workspace_root)}
        )
        assert "alpha.py" in result

    @pytest.mark.asyncio
    async def test_glob_symlink_escape_skipped_not_fatal(
        self, workspace_root: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "evil.py").write_text("x")
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
        (workspace_root / "alpha.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute({"pattern": "**/*.py", "path": str(workspace_root)})
        assert "alpha.py" in result, "工作区内匹配必须保留"
        assert "evil.py" not in result, "链接目标不得进入结果"
