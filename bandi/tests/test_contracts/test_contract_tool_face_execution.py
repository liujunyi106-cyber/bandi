"""契约族：工具执行面必须与 schema 面同源收窄（绑定 / 管理者 / 会话作用域）。

本契约钉的行为：
- `tools/executor.py::ToolExecutor._role_gate`：员工工具绑定外调用直接拒绝；
- 无派发面角色调用管理者专属工具直接拒绝；
- `perception/prefetch.py::MemoryPrefetchProvider`：群聊/单聊维度透传到 search_unified。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from bandi.core.message import ToolCall
from bandi.tools.executor import ToolExecutor


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name
        self.is_concurrency_safe = True
        self.is_idempotent = True
        self.is_read_only = True
        self.tags = ["util"]
        self.cache_ttl = 0.0
        self.enabled = True
        self.risk_level = "low"
        self.permissions_override = None
        self.parameters: dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, params: dict, **kwargs: Any) -> str:
        return f"{self.name}:ok"

    def validate_params(self, params: dict) -> list[str]:
        return []


def _executor(**kwargs: Any) -> tuple[ToolExecutor, MagicMock]:
    registry = MagicMock()
    registry.get.side_effect = lambda name: _Tool(name)
    return ToolExecutor(registry=registry, **kwargs), registry


@pytest.mark.asyncio
async def test_bound_tool_face_denies_unbound_execution() -> None:
    """绑定只给了 read——模型凭空点名 write 必须被拒（schema 面不可见的执行面同源）。"""
    executor, _ = _executor(bound_tool_names=["read"])
    results = await executor.execute(
        [
            ToolCall(id="1", name="read", arguments={}),
            ToolCall(id="2", name="write", arguments={}),
        ]
    )
    by_name = {item.tool_name: item for item in results}
    assert by_name["read"].success is True
    assert by_name["write"].success is False
    assert "not bound" in (by_name["write"].error or "")


@pytest.mark.asyncio
async def test_manager_only_tool_denied_without_dispatch_face() -> None:
    """无下级员工的执行面不得调用 delete_work_item（schema 面已扣的同源门禁）。"""
    executor, _ = _executor(manager_capable=False)
    results = await executor.execute([ToolCall(id="1", name="delete_work_item", arguments={})])
    assert results[0].success is False
    assert "manager-capable" in (results[0].error or "")


class _UnifiedMemory:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search_unified(self, query: str, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        return []


def test_prefetch_forwards_conversation_scope() -> None:
    """群聊/DM 轮：prefetch 必须把会话维度透传给统一检索，不能只带项目/员工。"""
    from bandi.perception.prefetch import MemoryPrefetchProvider

    memory = _UnifiedMemory()
    provider = MemoryPrefetchProvider(
        memory,
        project_id="p1",
        employee_id="e1",
        conversation_id="g1",
        dm_conversation_id="d1",
    )
    asyncio.run(provider("q", "s1", []))
    assert memory.calls
    call = memory.calls[0]
    assert call["conversation_id"] == "g1"
    assert call["dm_conversation_id"] == "d1"
