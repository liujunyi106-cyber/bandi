# 功能：契约 — 高危写操作执行前目标文件快照（轻量兜底：快照/还原/风险门槛/执行器接线）
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from bandi.core.message import ToolCall
from bandi.safety.write_snapshot import (
    create_snapshot,
    extract_target_paths,
    restore_snapshot,
    snapshot_worthy,
)
from bandi.tools.executor import ToolExecutor


class TestCreateSnapshot:
    def test_copies_existing_files_and_writes_manifest(self, tmp_path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "sub"
        b.mkdir()
        (b / "b.txt").write_text("AAA", encoding="utf-8")
        a.write_text("BBB", encoding="utf-8")
        missing = tmp_path / "nope.txt"

        batch = create_snapshot(
            "file_write", [a, b / "b.txt", missing], snapshot_root=tmp_path / "snaps"
        )

        assert batch is not None
        manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        files = manifest["files"]
        assert len(files) == 2
        assert set(files.values()) == {str(a), str(b / "b.txt")}
        for key, orig in files.items():
            assert (batch / key).read_text(encoding="utf-8") == Path(orig).read_text(
                encoding="utf-8"
            )

    def test_returns_none_when_no_existing_targets(self, tmp_path) -> None:
        batch = create_snapshot(
            "file_write", [tmp_path / "ghost.txt"], snapshot_root=tmp_path / "snaps"
        )
        assert batch is None


class TestRestoreSnapshot:
    def test_restores_original_content(self, tmp_path) -> None:
        target = tmp_path / "data.txt"
        target.write_text("ORIGINAL", encoding="utf-8")
        batch = create_snapshot("file_write", [target], snapshot_root=tmp_path / "snaps")
        assert batch is not None
        target.write_text("TAMPERED", encoding="utf-8")

        restored = restore_snapshot(batch)

        assert restored == [target]
        assert target.read_text(encoding="utf-8") == "ORIGINAL"


class TestSnapshotWorthy:
    def test_risk_gate(self) -> None:
        assert snapshot_worthy("apply_patch", {}, False) is True
        assert snapshot_worthy("file_write", {"path": "x"}, False) is True
        # 只读工具永不快照（file_read 虽登记 MEDIUM，但读语义无快照价值）
        assert snapshot_worthy("file_read", {"path": "x"}, True) is False
        assert snapshot_worthy("read", {"path": "x"}, True) is False
        # 参数关键词升级：bash 带 force → CRITICAL
        assert snapshot_worthy("bash", {"command": "rm --force x"}, False) is True


class TestExtractTargetPaths:
    def test_absolute_and_relative_paths(self, tmp_path) -> None:
        existing = tmp_path / "t.txt"
        existing.write_text("x", encoding="utf-8")
        targets = extract_target_paths(
            {"path": str(existing), "other": str(tmp_path / "ghost.txt")},
            base_dir=tmp_path,
        )
        assert targets == [existing]

    def test_apply_patch_update_file_header(self, tmp_path) -> None:
        target = tmp_path / "code.py"
        target.write_text("x", encoding="utf-8")
        patch = f"*** Begin Patch\n*** Update File: {target}\n@@\n"
        targets = extract_target_paths({"patch": patch}, base_dir=tmp_path)
        assert targets == [target]


def _executor(tool: Any, snapshot_root: str) -> ToolExecutor:
    registry = Mock()
    registry.get.return_value = tool
    # 放行管道桩：绕开默认审批门（快照语义在执行前一刻验证，与审批无关）
    verdict = Mock()
    verdict.allow = True
    pipeline = Mock()
    pipeline.check = AsyncMock(return_value=verdict)
    pipeline.check_mode_available = Mock(return_value=verdict)
    return ToolExecutor(
        registry=registry, snapshot_root=snapshot_root, permissions_pipeline=pipeline
    )


def _tool(name: str, read_only: bool, target: Any) -> Any:
    class _T:
        pass

    t = _T()
    t.name = name
    t.is_concurrency_safe = True
    t.external_content = False
    t.cache_ttl = 0
    t.permissions_override = None
    t.risk_level = "high"
    t.resource_type = "file"
    t.is_read_only = read_only
    t.validate_params = lambda params: []
    t.executed = False

    async def execute(params: dict) -> str:
        t.executed = True
        return "ok"

    t.execute = execute
    return t


def _call(name: str, arguments: dict) -> ToolCall:
    return ToolCall(id="tc-1", name=name, arguments=arguments)


class TestExecutorWiring:
    @pytest.mark.asyncio
    async def test_write_tool_snapshots_target_before_execution(
        self, tmp_path, monkeypatch
    ) -> None:
        target = tmp_path / "victim.txt"
        target.write_text("BEFORE", encoding="utf-8")
        snap_root = tmp_path / "snaps"
        tool = _tool("file_write", False, target)

        async def execute(params: dict) -> str:
            target.write_text("AFTER", encoding="utf-8")
            tool.executed = True
            return "ok"

        tool.execute = execute
        executor = _executor(tool, str(snap_root))
        results = await executor.execute([_call("file_write", {"path": str(target)})])

        assert results[0].success is True
        batches = list(snap_root.iterdir())
        assert len(batches) == 1
        manifest = json.loads((batches[0] / "manifest.json").read_text(encoding="utf-8"))
        (orig,) = manifest["files"].values()
        assert orig == str(target)
        # 快照内容是执行前状态
        assert (batches[0] / list(manifest["files"])[0]).read_text(encoding="utf-8") == "BEFORE"

    @pytest.mark.asyncio
    async def test_read_only_tool_creates_no_snapshot(self, tmp_path) -> None:
        target = tmp_path / "r.txt"
        target.write_text("x", encoding="utf-8")
        snap_root = tmp_path / "snaps"
        executor = _executor(_tool("file_read", True, target), str(snap_root))
        results = await executor.execute([_call("file_read", {"path": str(target)})])
        assert results[0].success is True
        assert not snap_root.exists()

    @pytest.mark.asyncio
    async def test_snapshot_failure_does_not_block_execution(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "w.txt"
        target.write_text("x", encoding="utf-8")
        tool = _tool("file_write", False, target)

        def boom(*args: Any, **kwargs: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("bandi.safety.write_snapshot.create_snapshot", boom)
        executor = _executor(tool, str(tmp_path / "snaps"))
        results = await executor.execute([_call("file_write", {"path": str(target)})])
        assert results[0].success is True
        assert tool.executed is True
