# 功能：应用控制面路径守卫契约——工具不得读写 ~/.bandi 配置/审批/组织/数据
from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest

from bandi.tools.doc.archive_manager import ArchiveManagerTool
from bandi.tools.doc.config_file_tool import ConfigFileTool
from bandi.tools.fs_tool import sensitive_path_reason
from bandi.tools.memory_tools import MemoryForgetTool
from bandi.tools.util.folder_tool import FolderOperationsTool


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".bandi" / "company").mkdir(parents=True)
    (home / ".bandi" / "workspace" / "proj").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_control_plane_paths_are_sensitive(fake_home: Path) -> None:
    root = fake_home / ".bandi"
    assert sensitive_path_reason(root / "mcp.json")
    assert sensitive_path_reason(root / "config.yaml")
    assert sensitive_path_reason(root / "approvals.json")
    assert sensitive_path_reason(root / "company" / "org.json")
    assert sensitive_path_reason(root / "data" / "bandi.db")
    assert sensitive_path_reason(root / "workspace" / "proj" / "a.txt") is None


def test_config_file_write_control_plane_is_rejected(fake_home: Path) -> None:
    tool = ConfigFileTool()
    with pytest.raises(ValueError, match="sensitive path rejected"):
        asyncio.run(
            tool._op_write(
                {
                    "file_path": str(fake_home / ".bandi" / "mcp.json"),
                    "format": "json",
                    "data": {"mcpServers": {"x": {"command": "calc"}}},
                }
            )
        )


def test_folder_delete_control_plane_is_rejected(fake_home: Path) -> None:
    tool = FolderOperationsTool()
    with pytest.raises(ValueError, match="sensitive path rejected"):
        asyncio.run(
            tool._op_delete(
                {"folder": str(fake_home / ".bandi"), "recursive": True},
                None,
            )
        )


def test_archive_extract_sensitive_member_is_rejected(fake_home: Path) -> None:
    downloads = fake_home / "Downloads"
    downloads.mkdir()
    archive = downloads / "payload.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(".git/config", "[core]\n\tfsmonitor = calc.exe\n")
    tool = ArchiveManagerTool()
    with pytest.raises(ValueError, match="sensitive archive member"):
        asyncio.run(
            tool._op_extract(
                {
                    "archive_path": str(archive),
                    "output_dir": str(downloads / "out"),
                }
            )
        )


def test_memory_forget_project_entry_rejects_path_traversal(fake_home: Path) -> None:
    from bandi.memory.markdown_memory import MarkdownMemoryStore

    store = MarkdownMemoryStore(fake_home / "mem", project_root=str(fake_home / "proj"))
    outside = fake_home / "MEMORY.md"
    outside.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid entry id"):
        MemoryForgetTool._forget_project_entry(store, "../../MEMORY")
    assert outside.exists()


def test_folder_operations_requires_approval() -> None:
    tool = FolderOperationsTool()
    assert tool.permissions_override == "ask"
    assert tool.is_destructive is True
    assert tool.risk_level == "high"
