# 功能：契约——安全加固可做项：Archive 输出标外部内容（防间接注入）
# 应用库连接 hardened PRAGMA（secure_delete=ON / trusted_schema=OFF）。
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest

from bandi.core.message import ToolCall
from bandi.tools.doc.archive_manager import ArchiveManagerTool
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


def _mk_executor(*tools: Any) -> ToolExecutor:
    reg = ToolRegistry()
    for t in tools:
        reg.register_instance(t)
    return ToolExecutor(registry=reg, execution_timeout=15)


class TestArchiveExternalMarking:
    def test_archive_tool_declares_external_content(self) -> None:
        assert ArchiveManagerTool().external_content is True, (
            "压缩包内容来自外部文件，须声明 external_content（executor 包装）"
        )

    @pytest.mark.asyncio
    async def test_archive_list_output_wrapped_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """生产链路：executor 执行 archive list，输出必须带外部内容边界。"""
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
        archive = tmp_path / "a.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("note.txt", "hello")
        ex = _mk_executor(ArchiveManagerTool())
        results = await ex.execute(
            [
                ToolCall(
                    id="a1",
                    name="archive_manager",
                    arguments={"operation": "list", "archive_path": str(archive)},
                )
            ]
        )
        result = results[0]
        assert result.success is True, f"archive list 应成功：{result.error}"
        assert "<<<EXTERNAL_UNTRUSTED_CONTENT" in (result.output or ""), (
            f"输出必须被外部内容边界包裹：{(result.output or '')[:200]}"
        )


class TestAppDbHardenedPragmas:
    def test_app_db_connection_hardened(self, tmp_path: Path) -> None:
        from bandi.utils.db import connect_app_db

        conn = connect_app_db(tmp_path / "app.db")
        try:
            assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
            assert conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        finally:
            conn.close()

    def test_plain_connect_not_forced_hardened(self, tmp_path: Path) -> None:
        """破坏者对照：外部数据源连接不得被强制加固（行为零变化）。"""
        from bandi.utils.db import connect

        conn = connect(tmp_path / "ext.db")
        try:
            assert conn.execute("PRAGMA trusted_schema").fetchone()[0] == 1
        finally:
            conn.close()

    def test_reopen_existing_db_still_hardened(self, tmp_path: Path) -> None:
        """现存库再次打开同样生效（PRAGMA 按连接施加）。"""
        from bandi.utils.db import connect_app_db

        path = tmp_path / "app.db"
        first = connect_app_db(path)
        first.execute("CREATE TABLE t (x INTEGER)")
        first.close()
        second = connect_app_db(path)
        try:
            assert second.execute("PRAGMA secure_delete").fetchone()[0] == 1
        finally:
            second.close()
