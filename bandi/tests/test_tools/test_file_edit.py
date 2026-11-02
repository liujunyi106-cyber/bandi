from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bandi.tools.file_edit import FileEditTool


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    return tmp_path


class TestFileEditTool:
    @pytest.mark.asyncio
    async def test_replace_single(self, workspace: Path) -> None:
        fp = workspace / "test.txt"
        fp.write_text("hello world\nsecond line\nhello again")
        tool = FileEditTool()
        result = await tool.execute(
            {
                "file_path": str(fp),
                "old_string": "hello world",
                "new_string": "hi world",
            }
        )
        assert "replaced 1 occurrence(s)" in result
        assert fp.read_text("utf-8") == "hi world\nsecond line\nhello again"

    @pytest.mark.asyncio
    async def test_replace_all(self, workspace: Path) -> None:
        fp = workspace / "test.txt"
        fp.write_text("hello world\nsecond line\nhello again")
        tool = FileEditTool()
        result = await tool.execute(
            {
                "file_path": str(fp),
                "old_string": "hello",
                "new_string": "hi",
                "replace_all": True,
            }
        )
        assert "replaced 2 occurrence(s)" in result
        assert fp.read_text("utf-8") == "hi world\nsecond line\nhi again"

    @pytest.mark.asyncio
    async def test_old_string_not_found(self, workspace: Path) -> None:
        fp = workspace / "test.txt"
        fp.write_text("hello world")
        tool = FileEditTool()
        with pytest.raises(ValueError, match="old_string not found"):
            await tool.execute(
                {
                    "file_path": str(fp),
                    "old_string": "nonexistent text",
                    "new_string": "whatever",
                }
            )

    @pytest.mark.asyncio
    async def test_file_not_found(self, workspace: Path) -> None:
        tool = FileEditTool()
        with pytest.raises(FileNotFoundError):
            await tool.execute(
                {
                    "file_path": str(workspace / "nonexistent.txt"),
                    "old_string": "hello",
                    "new_string": "hi",
                }
            )

    @pytest.mark.asyncio
    async def test_path_outside_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        safe_root = Path(tempfile.mkdtemp())
        monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(safe_root))
        tool = FileEditTool()
        outside = tmp_path / "outside.txt"
        outside.write_text("test")
        with pytest.raises(ValueError, match="outside workspace"):
            await tool.execute(
                {
                    "file_path": str(outside),
                    "old_string": "test",
                    "new_string": "changed",
                }
            )

    @pytest.mark.asyncio
    async def test_prefix_sibling_directory_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        safe_root = Path(tempfile.mkdtemp())
        monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(safe_root))
        sibling = Path(str(safe_root) + "2")
        sibling.mkdir(parents=True, exist_ok=True)
        fp = sibling / "pwn.txt"
        fp.write_text("pwned")
        tool = FileEditTool()
        with pytest.raises(ValueError, match="outside workspace"):
            await tool.execute(
                {
                    "file_path": str(fp),
                    "old_string": "pwned",
                    "new_string": "owned",
                }
            )

    @pytest.mark.asyncio
    async def test_replace_relative_path(self, workspace: Path) -> None:
        fp = workspace / "test.txt"
        fp.write_text("hello")
        tool = FileEditTool()
        result = await tool.execute(
            {
                "file_path": "test.txt",
                "old_string": "hello",
                "new_string": "bye",
            }
        )
        assert "replaced 1 occurrence(s)" in result

    @pytest.mark.asyncio
    async def test_sensitive_file_rejected(self, workspace: Path) -> None:
        """edit 不得改写敏感路径（.env/credentials/.key 等）。"""
        env = workspace / ".env"
        env.write_text("API_KEY=secret")
        tool = FileEditTool()
        with pytest.raises(ValueError, match="sensitive"):
            await tool.execute(
                {
                    "file_path": str(env),
                    "old_string": "API_KEY=secret",
                    "new_string": "API_KEY=hacked",
                }
            )
        assert env.read_text("utf-8") == "API_KEY=secret", "敏感文件不得被改写"

    @pytest.mark.asyncio
    async def test_sensitive_extension_rejected(self, workspace: Path) -> None:
        key = workspace / "server.key"
        key.write_text("PRIVATE")
        tool = FileEditTool()
        with pytest.raises(ValueError, match="sensitive"):
            await tool.execute(
                {
                    "file_path": str(key),
                    "old_string": "PRIVATE",
                    "new_string": "PUBLIC",
                }
            )
