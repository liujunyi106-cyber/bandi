"""收口：blob 大参数代理生产接线契约——
executor 执行前把 `blob:<id>` 引用参数还原为真实内容（读后即焚）。"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry
from bandi.tools.util.tool_argument_blob import ToolArgumentBlobTool


class _EchoTool(BaseTool):
    name: str = "echo"
    description: str = "echo content back"
    category: ToolCategory = ToolCategory.UTIL
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, params: dict[str, Any], context=None) -> dict[str, Any]:
        return {"success": True, "saw": params.get("content", "")}


@pytest.mark.asyncio
async def test_contract_executor_resolves_blob_argument(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool)
    registry.register(ToolArgumentBlobTool)
    blob_tool = ToolArgumentBlobTool()
    executor = ToolExecutor(registry=registry)
    executor.bind_session_meta({"session_id": "sess-blob", "blob_data_dir": str(tmp_path)})

    ctx = ToolPermissionContext(
        user_id="tester",
        extra={"session_id": "sess-blob", "blob_data_dir": str(tmp_path)},
    )
    # 写入 blob（append + finalize）
    content = "大参数正文 " * 500
    blob_id = uuid.uuid4().hex  # [a-f0-9]{8,64}
    created = await blob_tool.execute({"action": "create"}, ctx)
    assert created.get("ok") is True, created
    blob_id = created.get("blob_id") or blob_id
    start = await blob_tool.execute(
        {"action": "append", "blob_id": blob_id, "chunk": content[:100]}, ctx
    )
    assert start.get("ok") is True, start
    fin = await blob_tool.execute({"action": "finalize", "blob_id": blob_id}, ctx)
    assert fin.get("ok") is True, fin

    # 经 executor 执行：blob 引用参数被还原为真实内容
    from bandi.core.message import ToolCall

    result = await executor.execute(
        [ToolCall(id="t1", name="echo", arguments={"content": f"blob:{blob_id}"})]
    )
    assert result[0].success, result[0].output
    assert content[:100] in str(result[0].output), "下游工具必须拿到真实内容"

    # 读后即焚：二次还原抛 ValueError
    with pytest.raises(ValueError):
        await asyncio.wait_for(
            __import__(
                "bandi.tools.util.tool_argument_blob", fromlist=["resolve_blob_text"]
            ).resolve_blob_text(ctx, blob_id),
            timeout=5,
        )
