# 功能：BLOB 堆积上限契约（对应 #11：单会话 blob 数量必须受控，防止磁盘写爆）
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.tools.util.tool_argument_blob import (
    ToolArgumentBlobTool,
)


@pytest.fixture()
def blob_tool(tmp_path: Path) -> ToolArgumentBlobTool:
    return ToolArgumentBlobTool(data_dir=tmp_path)


async def _create(blob_tool: ToolArgumentBlobTool) -> dict[str, Any]:
    return await blob_tool.execute({"action": "create"}, context=None)


@pytest.mark.asyncio
async def test_blob_boundary_rejects_overflow(blob_tool: ToolArgumentBlobTool) -> None:
    """达到上限后第 N+1 次 create 必须被拒绝（ok=False + 原因说明）。"""
    limit = 3
    with patch("bandi.tools.util.tool_argument_blob._MAX_BLOBS_PER_SESSION", limit):
        for _ in range(limit):
            res = await _create(blob_tool)
            assert res.get("ok") is not True or "blob_id" in res, f"前 {limit} 次必须成功: {res}"
        overflow = await _create(blob_tool)
        assert overflow.get("ok") is False
        assert "too many blobs" in overflow.get("error", "").lower()


@pytest.mark.asyncio
async def test_blob_boundary_exactly_at_limit(blob_tool: ToolArgumentBlobTool) -> None:
    """恰好达到上限的最后一次 create 应成功（边界：>= 拒绝，所以 次成功 + N 次成功）。"""
    limit = 3
    with patch("bandi.tools.util.tool_argument_blob._MAX_BLOBS_PER_SESSION", limit):
        results = []
        for _ in range(limit):
            results.append(await _create(blob_tool))
        assert all("blob_id" in r for r in results), f"前 {limit} 次必须全成功: {results}"
        assert results[-1].get("ok") is True or "blob_id" in results[-1]


@pytest.mark.asyncio
async def test_blob_boundary_limit_1_isolation() -> None:
    """极限测试：limit=1 时第 2 次 create 必须被拒，且第 1 次不受影响。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        tool = ToolArgumentBlobTool(data_dir=Path(tmp))
        with patch("bandi.tools.util.tool_argument_blob._MAX_BLOBS_PER_SESSION", 1):
            first = await _create(tool)
            assert "blob_id" in first, f"第 1 次必须成功: {first}"
            second = await _create(tool)
            assert second.get("ok") is False
            assert "too many blobs" in second.get("error", "").lower()
