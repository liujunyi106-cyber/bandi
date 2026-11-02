"""
契约族：MCP 上下文注入与取消接线（五-建议补齐）。

背景：MCP 五维元数据中 needs_context（需要 session/project 上下文）与
cancellable（支持取消）此前只有声明无行为。本契约钉接线：

- needs_context=True 的工具 execute 必须能从 context.extra 拿到 session_id
  与 project_root（executor 构造 extra 时强制注入）；
- cancellable=True 的工具在 abort 控制器已触发时不得继续执行（abort 竞速
  取消），cancellable=False 工具不受影响（等待完成）。

本契约钉的行为族（生产调用路径）：
- `tools/executor.py::_run_and_record`（extra 注入 + abort 竞速）
- `tools/mcp_client.py::_make_mcp_execute`（MCP 工具 execute 接收 context）
- `core/abort_controller.py::AbortController`（取消信号源）

断言「最终产物」：needs_context 工具执行收到 extra.session_id/project_root；
cancellable 工具 abort 后执行返回失败（工具 execute 未被调用）；不可取消
工具 abort 后仍执行完成。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from bandi.core.abort_controller import AbortController
from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry

_SEEN_EXTRA: list[dict] = []


class _NeedsCtxTool(BaseTool):
    name = "ctx_probe"
    description = "需要上下文探针"
    category = ToolCategory.INTEGRATION
    needs_context = True

    async def execute(self, params, context=None) -> str:  # type: ignore[override]
        _SEEN_EXTRA.append(dict(context.extra or {}))
        return "ok"


class _CancellableTool(BaseTool):
    name = "cancel_probe"
    description = "可取消探针"
    category = ToolCategory.INTEGRATION
    cancellable = True

    async def execute(self, params, context=None) -> str:  # type: ignore[override]
        await asyncio.sleep(5)
        return "late"


class _NonCancellableTool(BaseTool):
    name = "stubborn_probe"
    description = "不可取消探针"
    category = ToolCategory.INTEGRATION

    async def execute(self, params, context=None) -> str:  # type: ignore[override]
        return "done"


def _ex(tool: BaseTool, controller: AbortController | None = None) -> ToolExecutor:
    reg = ToolRegistry()
    reg.register_instance(tool)
    return ToolExecutor(registry=reg, abort_controller=controller)


def test_contract_needs_context_injects_session_and_project() -> None:
    """needs_context=True 工具：execute 必须收到 extra.session_id + project_root。"""
    tool = _NeedsCtxTool()
    ex = _ex(tool)
    ex.bind_session_meta({"session_id": "s-ctx", "mode_manager": None, "project_root": "/proj"})
    asyncio.run(ex.execute([ToolCall(id="c1", name="ctx_probe", arguments={})]))
    extra = _SEEN_EXTRA[-1]
    assert extra.get("session_id") == "s-ctx", f"extra 必须注入 session_id，实际 {extra}"
    assert extra.get("project_root") == "/proj", "extra 必须注入 project_root"


def test_contract_cancellable_tool_aborts_without_reexec() -> None:
    """cancellable=True 工具 + abort 已触发：不得执行工具（竞速取消）。"""
    tool = _CancellableTool()
    tool.execute = AsyncMock(side_effect=AssertionError("cancellable 工具不应被调用"))  # type: ignore[method-assign]
    controller = AbortController()
    controller.abort("user_interrupt")
    ex = _ex(tool, controller)
    (result,) = asyncio.run(ex.execute([ToolCall(id="c2", name="cancel_probe", arguments={})]))
    assert result.success is False, "abort 后 cancellable 工具必须失败"
    assert "abort" in (result.error or "").lower() or result.should_abort


def test_contract_non_cancellable_not_started_after_abort() -> None:
    """cancellable=False 工具 + abort 已触发：
    不得启动——原「等待语义」仅保留给已进入执行中的调用（执行中不打断的
    对照契约见 `test_contract_local_tool_cancel.py`）。"""
    tool = _NonCancellableTool()
    controller = AbortController()
    controller.abort("user_interrupt")
    ex = _ex(tool, controller)
    (result,) = asyncio.run(ex.execute([ToolCall(id="c3", name="stubborn_probe", arguments={})]))
    assert result.success is False, "abort 后不可取消工具也不得启动"
    assert "aborted" in (result.error or "").lower(), f"实际 {result.error!r}"
