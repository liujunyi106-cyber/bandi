"""功能：契约 — 写路径保真收口（.5：write 主工具 + doc 族共享 fs_write 一套）

钉的行为：
- `write` 工具覆盖写：目标已有 UTF-8 BOM 时**保留 BOM**（不得静默丢——下游解析差异）；
- `write` 工具：存在 read_state 记录（已读）且文件被外部修改（mtime 前进 + 哈希不同）→
  **拒绝写入**（MODIFIED_SINCE_READ——防覆盖用户手动修改）；
- 原子性由 fs_write.atomic_write 统一承载（tmp + os.replace）。

注：doc 族（text/markdown/html/config）本轮同批收口为 atomic_write；
其行为回归由既有 doc 测试保证，本契约钉 write 工具的两项新增承诺。
"""

from __future__ import annotations

import codecs
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from bandi.tools.file_ops import FileWriteTool


def _tool() -> FileWriteTool:
    return FileWriteTool()


def _context(read_state: dict | None = None, root: Path | None = None) -> SimpleNamespace:
    extra: dict = {"read_state": read_state or {}}
    if root is not None:
        extra["project_root"] = str(root)
    return SimpleNamespace(extra=extra)


class TestWriteBomPreserved:
    @pytest.mark.asyncio
    async def test_existing_bom_is_preserved(self, tmp_path: Path) -> None:
        target = tmp_path / "bom.txt"
        target.write_bytes(codecs.BOM_UTF8 + "旧内容".encode())
        tool = _tool()
        await tool.execute(
            {"path": str(target), "content": "新内容", "encoding": "utf-8"},
            _context(root=tmp_path),
        )
        raw = target.read_bytes()
        assert raw.startswith(codecs.BOM_UTF8), "覆盖写必须保留原有 UTF-8 BOM"
        assert raw[len(codecs.BOM_UTF8) :].decode("utf-8") == "新内容"

    @pytest.mark.asyncio
    async def test_no_bom_stays_no_bom(self, tmp_path: Path) -> None:
        target = tmp_path / "plain.txt"
        tool = _tool()
        await tool.execute(
            {"path": str(target), "content": "内容", "encoding": "utf-8"}, _context(root=tmp_path)
        )
        assert not target.read_bytes().startswith(codecs.BOM_UTF8)


class TestWriteExternalModificationGuard:
    @pytest.mark.asyncio
    async def test_rejects_when_file_modified_after_read(self, tmp_path: Path) -> None:
        target = tmp_path / "guard.txt"
        target.write_bytes("原始".encode())
        stat = target.stat()
        read_state = {
            "k1": {
                "resolved_path": str(target),
                "mtime": stat.st_mtime,
                "content_sha256": "deadbeef" * 8,
                "partial": False,
                "read_at": "2026-10-06T00:00:00",
            }
        }
        time.sleep(0.05)
        target.write_bytes("用户手动改过".encode())
        tool = _tool()
        with pytest.raises(ValueError, match="modified since read"):
            await tool.execute(
                {"path": str(target), "content": "agent 覆盖", "encoding": "utf-8"},
                _context(read_state, root=tmp_path),
            )
        assert target.read_text(encoding="utf-8") == "用户手动改过", "被拒后不得落笔"

    @pytest.mark.asyncio
    async def test_allows_when_hash_matches_after_touch(self, tmp_path: Path) -> None:
        """云同步/杀软只改 mtime 而内容未变 → 放行（哈希一致）。"""
        import hashlib

        target = tmp_path / "touch.txt"
        payload = "同内容".encode()
        target.write_bytes(payload)
        stat = target.stat()
        read_state = {
            "k1": {
                "resolved_path": str(target),
                "mtime": stat.st_mtime,
                "content_sha256": hashlib.sha256(payload).hexdigest(),
                "partial": False,
                "read_at": "2026-10-06T00:00:00",
            }
        }
        time.sleep(0.05)
        target.write_bytes(payload)  # 重写同内容 → mtime 前进、哈希一致
        tool = _tool()
        await tool.execute(
            {"path": str(target), "content": "新内容", "encoding": "utf-8"},
            _context(read_state, root=tmp_path),
        )
        assert target.read_text(encoding="utf-8") == "新内容"

    @pytest.mark.asyncio
    async def test_no_read_state_no_guard(self, tmp_path: Path) -> None:
        target = tmp_path / "noread.txt"
        target.write_bytes(b"x")
        tool = _tool()
        await tool.execute(
            {"path": str(target), "content": "y", "encoding": "utf-8"},
            _context(root=tmp_path),
        )
        assert target.read_text(encoding="utf-8") == "y"
