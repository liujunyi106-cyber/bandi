from __future__ import annotations

import os
from pathlib import Path

import pytest

from bandi.tools.file_edit import FileEditTool
from bandi.tools.file_ops import FileReadTool, FileWriteTool
from bandi.tools.glob_tool import GlobTool
from bandi.tools.grep_tool import GrepTool

_WORKSPACE_ROOT = Path.home() / ".bandi" / "workspace"


@pytest.fixture(autouse=True)
def _sandboxed_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tests.test_tools.test_file_tools as ft_mod

    monkeypatch.setattr(ft_mod, "_WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    d = tmp_path / "_test_file_tools"
    d.mkdir(parents=True, exist_ok=True)
    (d / "alpha.py").write_text("print('hello')\nx = 1\n")
    (d / "beta.py").write_text("print('world')\ny = 2\n")
    (d / "data.json").write_text('{"key": "value"}\n')
    (d / "sub").mkdir(exist_ok=True)
    (d / "sub" / "nested.py").write_text("z = 3\n")
    return d


# ── GlobTool ──────────────────────────────────────────────────────────


class TestGlobTool:
    @pytest.mark.asyncio
    async def test_glob_all_py(self, tmp_dir: Path) -> None:
        tool = GlobTool()
        result = await tool.execute({"pattern": "**/*.py", "path": str(tmp_dir)})
        assert "3 file(s)" in result
        assert "alpha.py" in result
        assert "beta.py" in result
        assert "nested.py" in result

    @pytest.mark.asyncio
    async def test_glob_no_match(self, tmp_dir: Path) -> None:
        tool = GlobTool()
        result = await tool.execute({"pattern": "*.rs", "path": str(tmp_dir)})
        assert "no matches found" in result

    @pytest.mark.asyncio
    async def test_glob_limit(self, tmp_dir: Path) -> None:
        tool = GlobTool()
        result = await tool.execute({"pattern": "**/*.py", "path": str(tmp_dir), "limit": 1})
        lines = result.splitlines()
        assert len(lines) <= 3

    @pytest.mark.asyncio
    async def test_glob_path_not_found(self, tmp_path: Path) -> None:
        tool = GlobTool()
        with pytest.raises(FileNotFoundError):
            await tool.execute({"pattern": "*", "path": str(tmp_path / "missing_dir")})


# ── GrepTool ──────────────────────────────────────────────────────────


class TestGrepTool:
    @pytest.mark.asyncio
    async def test_grep_simple(self, tmp_dir: Path) -> None:
        tool = GrepTool()
        result = await tool.execute({"pattern": "print", "path": str(tmp_dir)})
        assert "2 match(es)" in result
        assert "alpha.py" in result
        assert "beta.py" in result

    @pytest.mark.asyncio
    async def test_grep_no_match(self, tmp_dir: Path) -> None:
        tool = GrepTool()
        result = await tool.execute({"pattern": "zzz_nonexistent", "path": str(tmp_dir)})
        assert "no matches found" in result

    @pytest.mark.asyncio
    async def test_grep_limit(self, tmp_dir: Path) -> None:
        tool = GrepTool()
        result = await tool.execute({"pattern": "^", "path": str(tmp_dir), "limit": 1})
        assert "1 match(es)" in result or "1+ match(es)" in result

    @pytest.mark.asyncio
    async def test_grep_with_include(self, tmp_dir: Path) -> None:
        tool = GrepTool()
        result = await tool.execute(
            {
                "pattern": "key",
                "path": str(tmp_dir),
                "include": "*.json",
            }
        )
        assert "match(es)" in result
        assert "data.json" in result

    @pytest.mark.asyncio
    async def test_grep_invalid_regex(self) -> None:
        tool = GrepTool()
        with pytest.raises(ValueError, match="invalid regex"):
            await tool.execute({"pattern": "[invalid", "path": "."})

    @pytest.mark.asyncio
    async def test_grep_path_not_found(self, tmp_path: Path) -> None:
        tool = GrepTool()
        with pytest.raises(FileNotFoundError):
            await tool.execute({"pattern": "x", "path": str(tmp_path / "missing_dir")})


# ── FileEditTool ──────────────────────────────────────────────────────


class TestFileEditTool:
    @pytest.mark.asyncio
    async def test_edit_replace(self, tmp_dir: Path) -> None:
        path = tmp_dir / "alpha.py"
        tool = FileEditTool()
        result = await tool.execute(
            {
                "file_path": str(path),
                "old_string": "print('hello')",
                "new_string": "print('hi')",
            }
        )
        assert "replaced 1 occurrence" in result
        assert path.read_text() == "print('hi')\nx = 1\n"

    @pytest.mark.asyncio
    async def test_edit_replace_all(self, tmp_dir: Path) -> None:
        path = tmp_dir / "repeat.txt"
        path.write_text("A\nA\nA\n")
        tool = FileEditTool()
        result = await tool.execute(
            {
                "file_path": str(path),
                "old_string": "A",
                "new_string": "B",
                "replace_all": True,
            }
        )
        assert "replaced 3 occurrence(s)" in result
        assert path.read_text() == "B\nB\nB\n"

    @pytest.mark.asyncio
    async def test_edit_old_string_not_found(self, tmp_dir: Path) -> None:
        path = tmp_dir / "alpha.py"
        tool = FileEditTool()
        with pytest.raises(ValueError, match="old_string not found"):
            await tool.execute(
                {
                    "file_path": str(path),
                    "old_string": "nonexistent_content_xyz",
                    "new_string": "foo",
                }
            )

    @pytest.mark.asyncio
    async def test_edit_file_not_found(self, tmp_path: Path) -> None:
        tool = FileEditTool()
        path = tmp_path / "_nonexistent_test_file_xyz.txt"
        with pytest.raises(FileNotFoundError):
            await tool.execute(
                {
                    "file_path": str(path),
                    "old_string": "x",
                    "new_string": "y",
                }
            )


# ── FileReadTool ────────────────────────────────────────────────────


class TestFileReadTool:
    @pytest.mark.asyncio
    async def test_read_file(self, tmp_dir: Path) -> None:
        tool = FileReadTool()
        path = tmp_dir / "alpha.py"
        result = await tool.execute({"path": str(path)})
        assert "print('hello')" in result
        assert "x = 1" in result

    @pytest.mark.asyncio
    async def test_read_with_offset(self, tmp_dir: Path) -> None:
        tool = FileReadTool()
        path = tmp_dir / "alpha.py"
        result = await tool.execute({"path": str(path), "offset": 2})
        assert "x = 1" in result
        assert "print('hello')" not in result

    @pytest.mark.asyncio
    async def test_read_with_limit(self, tmp_dir: Path) -> None:
        tool = FileReadTool()
        path = tmp_dir / "data.json"
        result = await tool.execute({"path": str(path), "limit": 1})
        assert "     1|" in result
        assert "(End of file - total 1 lines)" in result

    @pytest.mark.asyncio
    async def test_read_sensitive_path(self, tmp_dir: Path) -> None:
        tool = FileReadTool()
        env_file = tmp_dir / ".env"
        env_file.write_text("SECRET=1")
        with pytest.raises(PermissionError, match="access denied"):
            await tool.execute({"path": str(env_file)})

    @pytest.mark.asyncio
    async def test_read_sensitive_extension(self, tmp_dir: Path) -> None:
        tool = FileReadTool()
        key_file = tmp_dir / "key.pem"
        key_file.write_text("private key")
        with pytest.raises(PermissionError, match="access denied"):
            await tool.execute({"path": str(key_file)})

    @pytest.mark.asyncio
    async def test_read_directory_lists_entries(self, tmp_dir: Path) -> None:
        tool = FileReadTool()
        result = await tool.execute({"path": str(tmp_dir)})
        assert "alpha.py" in result
        assert "entries)" in result

    @pytest.mark.asyncio
    async def test_read_invalid_offset(self, tmp_dir: Path) -> None:
        tool = FileReadTool()
        path = tmp_dir / "alpha.py"
        with pytest.raises(ValueError, match="offset must be >= 1"):
            await tool.execute({"path": str(path), "offset": 0})

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_path: Path) -> None:
        tool = FileReadTool()
        with pytest.raises(FileNotFoundError):
            await tool.execute({"path": str(tmp_path / "_nonexistent_xyz_")})

    @pytest.mark.asyncio
    async def test_read_symlink_rejected(self, tmp_dir: Path) -> None:
        link_path = tmp_dir / "link_to_alpha.py"
        target = tmp_dir / "alpha.py"
        try:
            os.symlink(str(target), str(link_path))
        except (OSError, NotImplementedError):
            pytest.skip("cannot create symlink on this system")
        if not link_path.is_symlink():
            pytest.skip("symlink silently degraded; escape scenario not constructible")
        tool = FileReadTool()
        with pytest.raises(ValueError, match="symlink not allowed"):
            await tool.execute({"path": str(link_path)})
        link_path.unlink(missing_ok=True)


# ── FileWriteTool ───────────────────────────────────────────────────


class TestFileWriteTool:
    @pytest.mark.asyncio
    async def test_write_new_file(self, tmp_dir: Path) -> None:
        tool = FileWriteTool()
        path = tmp_dir / "new.txt"
        result = await tool.execute({"path": str(path), "content": "hello world"})
        assert "written" in result
        assert path.read_text() == "hello world"

    @pytest.mark.asyncio
    async def test_write_append(self, tmp_dir: Path) -> None:
        tool = FileWriteTool()
        path = tmp_dir / "append.txt"
        await tool.execute({"path": str(path), "content": "line1\n", "mode": "write"})
        await tool.execute({"path": str(path), "content": "line2\n", "mode": "append"})
        assert path.read_text() == "line1\nline2\n"

    @pytest.mark.asyncio
    async def test_write_sensitive_path(self, tmp_dir: Path) -> None:
        tool = FileWriteTool()
        with pytest.raises(PermissionError, match="access denied"):
            await tool.execute({"path": str(tmp_dir / ".env"), "content": "x"})

    @pytest.mark.asyncio
    async def test_write_overwrite(self, tmp_dir: Path) -> None:
        tool = FileWriteTool()
        path = tmp_dir / "overwrite.txt"
        await tool.execute({"path": str(path), "content": "first"})
        await tool.execute({"path": str(path), "content": "second"})
        assert path.read_text() == "second"
