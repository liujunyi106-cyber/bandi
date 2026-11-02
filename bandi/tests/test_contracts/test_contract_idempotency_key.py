"""
契约族：工具执行 idempotency key 调用实例级语义（残余 TMC-）。

背景：第一版实现用 (tool_name, args) 确定性哈希——重试能复用，但会把
「用户再次明确发起的同参调用」误判为同一次（外部 Idempotency-Key 去重会
吞掉第二次真实执行）。正确语义：**一次逻辑调用一个 key**——同一 ToolCall
实例（含重试）复用同一 key；不同批次的新调用必须新 key。

本契约钉的行为族（生产调用路径）：
- `tools/executor.py::_run_and_record`（extra 构造 + _idem_keys 记账）
- `tools/base.py::ToolPermissionContext.extra`（透传载体）

断言「最终产物」：工具 execute 收到 context.extra.idempotency_key；
同一调用重试复用同一 key；不同批次的同参调用 key 必须不同。
"""

from __future__ import annotations

import asyncio

from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry

_SEEN_KEYS: list[str] = []


class _KeyCaptureTool(BaseTool):
    name = "key_probe"
    description = "幂等键捕获探针"
    category = ToolCategory.UTIL

    async def execute(self, params: dict, context: ToolPermissionContext | None = None) -> str:  # type: ignore[override]
        extra = context.extra if context is not None else None
        _SEEN_KEYS.append((extra or {}).get("idempotency_key", "<missing>"))
        return "ok"


def _run(tool: BaseTool, args: dict, ex: ToolExecutor | None = None) -> ToolExecutor:
    if ex is None:
        reg = ToolRegistry()
        reg.register_instance(tool)
        ex = ToolExecutor(registry=reg)
    asyncio.run(ex.execute([ToolCall(id="c1", name=tool.name, arguments=args)]))
    return ex


def test_contract_idempotency_key_injected_into_extra() -> None:
    """工具 execute 必须收到 context.extra.idempotency_key（非缺失非空）。"""
    tool = _KeyCaptureTool()
    _run(tool, {"x": 1})
    key = _SEEN_KEYS[-1]
    assert key != "<missing>" and key, f"idempotency_key 必须注入 extra，实际 {key!r}"


def test_contract_idempotency_key_new_call_gets_new_key() -> None:
    """不同批次的新调用（同参数）必须产生不同 key——外部去重不得吞掉真实再次执行。"""
    tool = _KeyCaptureTool()
    reg = ToolRegistry()
    reg.register_instance(tool)
    ex = ToolExecutor(registry=reg)
    # 批次 1
    asyncio.run(ex.execute([ToolCall(id="c1", name="key_probe", arguments={"x": 1})]))
    # 批次 2：同参新调用（新 ToolCall id）
    asyncio.run(ex.execute([ToolCall(id="c2", name="key_probe", arguments={"x": 1})]))
    assert _SEEN_KEYS[-1] != _SEEN_KEYS[-2], (
        f"新调用必须新 key（同参跨批），{_SEEN_KEYS[-2]!r} == {_SEEN_KEYS[-1]!r}"
    )


def test_contract_idempotency_key_retry_reuses_same_key() -> None:
    """同一 ToolCall 实例批内重试必须复用同一 key（防重试重复副作用）；
    5.5 起批次结束后释放记账——重试语义限定在批内（生产重试即批内重入）。"""
    tool = _KeyCaptureTool()
    reg = ToolRegistry()
    reg.register_instance(tool)
    ex = ToolExecutor(registry=reg)
    tc = ToolCall(id="retry-c1", name="key_probe", arguments={"x": 1})
    asyncio.run(ex._run_and_record(tc, tool))
    first = _SEEN_KEYS[-1]
    asyncio.run(ex._run_and_record(tc, tool))
    second = _SEEN_KEYS[-1]
    assert first == second, f"批内重试必须复用同一 key，{first!r} != {second!r}"
