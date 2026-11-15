# 功能：P0-3 审批回执契约——服务端不可变请求 + 回执幂等 + 一次性消费。
# 钉行为：同一 approval 的二次执行（网络失败/重开页面后的 re-issue）必须返回
# 同一结果而不重放副作用；改参数视为另一次调用（P0-6 已由 once 参数级承担）。
from __future__ import annotations

import asyncio
from typing import Any

from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


class _CountingTool(BaseTool):
    # 用 ask 名单内的名称（write）让「未授权的新参数」确实进入审批门，
    # 从而能区分「回执命中」与「重新审批」两条路径。
    name = "write"
    description = "回执探针（每次真实执行自增）"
    category = ToolCategory.UTIL
    calls = 0

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:  # type: ignore[override]
        type(self).calls += 1
        return f"executed-{type(self).calls}"


def _make_executor(session_id: str, idempotency_store: Any = None) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register_instance(_CountingTool())
    executor = ToolExecutor(registry=registry, idempotency_store=idempotency_store)
    executor.bind_session_meta({"session_id": session_id})
    return executor


def test_same_approval_executes_once_and_returns_same_receipt() -> None:
    """契约：同 approval 二次执行返回同一回执，工具只真实执行一次。"""
    from bandi.tools.approval import get_approval_store, params_digest, reset_approval_store

    reset_approval_store()
    _CountingTool.calls = 0
    try:
        args = {"path": "a.txt"}
        store = get_approval_store()
        # 模拟用户批准「写 A.txt」这一条具体调用
        store.grant(
            "s-receipt",
            _CountingTool.name,
            params_digest=params_digest(args),
            scope="once",
        )
        executor = _make_executor("s-receipt")

        async def _run() -> tuple[Any, Any]:
            first = (
                await executor.execute(
                    [ToolCall(id="c1", name=_CountingTool.name, arguments=dict(args))]
                )
            )[0]
            second = (
                await executor.execute(
                    [ToolCall(id="c2", name=_CountingTool.name, arguments=dict(args))]
                )
            )[0]
            return first, second

        first, second = asyncio.run(_run())

        assert first.success is True and second.success is True
        assert _CountingTool.calls == 1, "同 approval 二次执行不得重放副作用"
        assert first.output == second.output, "二次执行必须返回同一回执结果"
        assert second.tool_call_id == "c2", "回执返回需重映射到当前 tool_call_id"
    finally:
        reset_approval_store()


def test_receipt_persists_across_executor_instances(tmp_path) -> None:
    """契约：回执落幂等存储——换 executor 实例（模拟重开页面/进程重启）仍命中。"""
    from bandi.tools.approval import get_approval_store, params_digest, reset_approval_store
    from bandi.tools.idempotency import IdempotencyStore

    reset_approval_store()
    _CountingTool.calls = 0
    try:
        args = {"path": "c.txt"}
        store = get_approval_store()
        store.grant(
            "s-receipt-3",
            _CountingTool.name,
            params_digest=params_digest(args),
            scope="once",
        )
        idem = IdempotencyStore(str(tmp_path / "idem"))
        first = _make_executor("s-receipt-3", idempotency_store=idem)
        second = _make_executor("s-receipt-3", idempotency_store=idem)  # 新实例，同存储

        async def _run() -> tuple[Any, Any]:
            a = (
                await first.execute(
                    [ToolCall(id="c1", name=_CountingTool.name, arguments=dict(args))]
                )
            )[0]
            b = (
                await second.execute(
                    [ToolCall(id="c2", name=_CountingTool.name, arguments=dict(args))]
                )
            )[0]
            return a, b

        a, b = asyncio.run(_run())

        assert a.success is True and b.success is True
        assert _CountingTool.calls == 1, "跨 executor 实例（重开页面）不得重放副作用"
        assert a.output == b.output
    finally:
        reset_approval_store()


def test_session_grant_repeats_execution_not_receipt() -> None:
    """契约：session 授权=本会话信任该工具——重复同参调用必须真实执行（不回执）。"""
    from bandi.tools.approval import get_approval_store, reset_approval_store

    reset_approval_store()
    _CountingTool.calls = 0
    try:
        args = {"path": "d.txt"}
        store = get_approval_store()
        store.grant("s-session", _CountingTool.name, params_digest="", scope="session")
        executor = _make_executor("s-session")

        async def _run() -> tuple[Any, Any]:
            a = (
                await executor.execute(
                    [ToolCall(id="c1", name=_CountingTool.name, arguments=dict(args))]
                )
            )[0]
            b = (
                await executor.execute(
                    [ToolCall(id="c2", name=_CountingTool.name, arguments=dict(args))]
                )
            )[0]
            return a, b

        a, b = asyncio.run(_run())

        assert a.success is True and b.success is True
        assert _CountingTool.calls == 2, "session 授权的重复调用必须真实执行（不得被回执吞掉）"
    finally:
        reset_approval_store()


def test_different_args_are_a_new_call_not_a_receipt_hit() -> None:
    """契约：改参数视为另一次调用——不会命中旧回执（配合 P0-6 once 参数级）。"""
    from bandi.tools.approval import get_approval_store, params_digest, reset_approval_store

    reset_approval_store()
    _CountingTool.calls = 0
    try:
        approved_args = {"path": "a.txt"}
        store = get_approval_store()
        store.grant(
            "s-receipt-2",
            _CountingTool.name,
            params_digest=params_digest(approved_args),
            scope="once",
        )
        executor = _make_executor("s-receipt-2")

        async def _run() -> tuple[Any, Any]:
            first = (
                await executor.execute(
                    [ToolCall(id="c1", name=_CountingTool.name, arguments=dict(approved_args))]
                )
            )[0]
            # 未获批准的新参数 → 应重新走审批（这里断言不会拿到旧回执）
            other = (
                await executor.execute(
                    [ToolCall(id="c2", name=_CountingTool.name, arguments={"path": "b.txt"})]
                )
            )[0]
            return first, other

        first, other = asyncio.run(_run())

        assert first.success is True
        assert other.success is False, "改参数后未授权应进入审批而非复用旧回执"
        assert _CountingTool.calls == 1, "未授权的新调用不得真实执行"
    finally:
        reset_approval_store()
