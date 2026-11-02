"""
契约族：跨进程持久化恢复重试（残余，TMC-收尾）。

背景：RetryPolicy.cross_process_resume 此前仅元数据。本契约落地：
声明 cross_process_resume=True 的工具——① 幂等键改为**确定性**（tool+args 哈希
跨进程可重算，进程 A 中断后进程 B 可定位同一操作）；② 执行前查持久化
IdempotencyStore（命中即返回先前结果，不重复执行）；③ 执行成功后写入。

未声明（默认）工具保持调用实例级 uuid key（新调用新 key，防外部去重吞真实
再次执行）——两种语义由工具作者声明选择。

本契约钉的行为族（生产调用路径）：
- `tools/idempotency.py::IdempotencyStore`（文件持久化 get/set）
- `tools/executor.py::_run_and_record`（key 两段式 + 查/写 store）
- `tools/base.py::RetryPolicy.cross_process_resume`（开关）

断言「最终产物」：同 args 跨"进程"（两个 executor 实例共享 store）确定性 key
相同；A 执行后 B 命中 store 返回结果且工具 execute 不被再次调用。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, RetryPolicy, ToolCategory
from bandi.tools.executor import ToolExecutor
from bandi.tools.idempotency import IdempotencyStore
from bandi.tools.registry import ToolRegistry

_RESUME_ARG = {"op": "deploy"}


class _ResumeTool(BaseTool):
    name = "resume_op"
    description = "跨进程恢复探针"
    category = ToolCategory.UTIL
    retry_policy = RetryPolicy(retryable=False, cross_process_resume=True)

    async def execute(self, params: dict, context=None) -> str:  # type: ignore[override]
        return "deployed:v1"


class _PlainTool(BaseTool):
    name = "plain_op"
    description = "普通工具探针"
    category = ToolCategory.UTIL

    async def execute(self, params: dict, context=None) -> str:  # type: ignore[override]
        return "ok"


def _mk_executor(
    tool: BaseTool,
    store: IdempotencyStore,
    project_root: str | None = None,
    pipeline: Any | None = None,
) -> ToolExecutor:
    reg = ToolRegistry()
    reg.register_instance(tool)
    return ToolExecutor(
        registry=reg,
        idempotency_store=store,
        project_root=project_root,
        permissions_pipeline=pipeline,
    )


def test_contract_resume_key_deterministic_across_executors(tmp_path) -> None:
    """cross_process_resume 工具：同 args 跨 executor 实例（模拟跨进程）必须同 key。"""
    store = IdempotencyStore(tmp_path)
    a = _mk_executor(_ResumeTool(), store)
    b = _mk_executor(_ResumeTool(), store)
    key_a = a._idempotency_key_for(
        _ResumeTool(), ToolCall(id="x1", name="resume_op", arguments=_RESUME_ARG)
    )
    key_b = b._idempotency_key_for(
        _ResumeTool(), ToolCall(id="x2", name="resume_op", arguments=_RESUME_ARG)
    )
    assert key_a == key_b, f"跨进程恢复 key 必须确定性（进程 B 可重算），{key_a!r} != {key_b!r}"


def test_contract_resume_reuses_persisted_result_without_reexec(tmp_path) -> None:
    """A 执行成功后 B（同 key）必须命中 store 返回结果，工具 execute 不得再次调用。"""
    store = IdempotencyStore(tmp_path)
    tool_a = _ResumeTool()
    tool_b = _ResumeTool()
    ex_a = _mk_executor(tool_a, store)
    ex_b = _mk_executor(tool_b, store)
    tool_b.execute = AsyncMock(return_value="deployed:v1")  # type: ignore[method-assign]

    r1 = asyncio.run(ex_a.execute([ToolCall(id="a1", name="resume_op", arguments=_RESUME_ARG)]))[0]
    r2 = asyncio.run(ex_b.execute([ToolCall(id="b1", name="resume_op", arguments=_RESUME_ARG)]))[0]
    assert r1.success is True and "deployed:v1" in str(r1.output)
    assert r2.success is True and "deployed:v1" in str(r2.output), (
        f"B 必须返回持久化结果，实际 {r2.output!r}"
    )
    assert tool_b.execute.await_count == 0, (
        f"跨进程恢复不得重新执行工具（副作用重放），await_count={tool_b.execute.await_count}"
    )


def test_contract_plain_tool_unaffected_by_store(tmp_path) -> None:
    """未声明 cross_process_resume 的工具：新调用新 key，store 不拦截。"""
    store = IdempotencyStore(tmp_path)
    tool = _PlainTool()
    ex = _mk_executor(tool, store)
    r1 = asyncio.run(ex.execute([ToolCall(id="p1", name="plain_op", arguments={"x": 1})]))[0]
    assert r1.success is True and "ok" in str(r1.output)


def test_contract_resume_key_scoped_by_project_root(tmp_path) -> None:
    """跨进程恢复 key 必须含 project_root 隔离——不同运行环境同参数算出
    不同 key（防单跑结果污染全量跑：全局 store 命中旧结果吞真实写入）。"""
    store = IdempotencyStore(tmp_path)
    a = _mk_executor(_ResumeTool(), store, project_root=str(tmp_path / "proj_a"))
    b = _mk_executor(_ResumeTool(), store, project_root=str(tmp_path / "proj_b"))
    key_a = a._idempotency_key_for(
        _ResumeTool(), ToolCall(id="x1", name="resume_op", arguments=_RESUME_ARG)
    )
    key_b = b._idempotency_key_for(
        _ResumeTool(), ToolCall(id="x2", name="resume_op", arguments=_RESUME_ARG)
    )
    assert key_a != key_b, f"不同 project_root 必须不同 key（运行环境隔离），{key_a!r} == {key_b!r}"


def test_contract_resume_key_same_within_project(tmp_path) -> None:
    """同一 project_root 内同参数 key 仍确定性相同——跨进程恢复语义不受影响。"""
    store = IdempotencyStore(tmp_path)
    proj = str(tmp_path / "proj")
    a = _mk_executor(_ResumeTool(), store, project_root=proj)
    b = _mk_executor(_ResumeTool(), store, project_root=proj)
    key_a = a._idempotency_key_for(
        _ResumeTool(), ToolCall(id="x1", name="resume_op", arguments=_RESUME_ARG)
    )
    key_b = b._idempotency_key_for(
        _ResumeTool(), ToolCall(id="x2", name="resume_op", arguments=_RESUME_ARG)
    )
    assert key_a == key_b, f"同项目内恢复 key 必须稳定，{key_a!r} != {key_b!r}"


def test_contract_resume_exec_not_swallowed_across_projects(tmp_path) -> None:
    """进程级端到端：A 项目执行成功入全局 store 后，B 项目同参数同内容
    必须真实执行并落盘 B 目录——不得命中 store 跳过写入（根因复现场景）。"""
    from bandi.core.user_interaction import UserInteraction
    from bandi.tools.file_ops import FileWriteTool
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    class _AutoApprove(UserInteraction):
        async def ask_permission(self, tool_name: str, args: dict[str, Any], risk: str) -> bool:
            return True

    store = IdempotencyStore(tmp_path)
    pipeline = PermissionsPipeline(user_interaction=_AutoApprove())
    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()

    tool = FileWriteTool()
    ex_a = _mk_executor(tool, store, project_root=str(proj_a), pipeline=pipeline)
    r1 = asyncio.run(
        ex_a.execute(
            [ToolCall(id="a1", name="write", arguments={"path": "alpha.txt", "content": "A"})],
        )
    )[0]
    assert r1.success is True, f"A 项目写入失败: {r1.error}"

    ex_b = _mk_executor(tool, store, project_root=str(proj_b), pipeline=pipeline)
    r2 = asyncio.run(
        ex_b.execute(
            [ToolCall(id="b1", name="write", arguments={"path": "alpha.txt", "content": "A"})],
        )
    )[0]
    assert r2.success is True, f"B 项目执行失败: {r2.error}"
    assert (proj_a / "alpha.txt").read_text(encoding="utf-8") == "A"
    assert (proj_b / "alpha.txt").read_text(encoding="utf-8") == "A", (
        f"B 项目写入被幂等 store 误命中吞掉（S9/S10 根因）：{proj_b / 'alpha.txt'} 不存在"
    )


def test_contract_write_append_not_resumed(tmp_path) -> None:
    """契约（5.2 破坏者）：write 的 append 模式不得参与跨进程恢复——同参
    append 重复调用必须真实追加（此前被幂等 store 命中吞掉，内容丢失）。"""
    from bandi.core.user_interaction import UserInteraction
    from bandi.tools.file_ops import FileWriteTool
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    class _AutoApprove(UserInteraction):
        async def ask_permission(self, tool_name: str, args: dict[str, Any], risk: str) -> bool:
            return True

    store = IdempotencyStore(tmp_path)
    pipeline = PermissionsPipeline(user_interaction=_AutoApprove())
    tool = FileWriteTool()
    ex = _mk_executor(tool, store, project_root=str(tmp_path), pipeline=pipeline)
    args = {"path": "append_log.txt", "content": "A", "mode": "append"}
    r1 = asyncio.run(ex.execute([ToolCall(id="p1", name="write", arguments=args)]))[0]
    r2 = asyncio.run(ex.execute([ToolCall(id="p2", name="write", arguments=dict(args))]))[0]
    assert r1.success is True and r2.success is True
    content = (tmp_path / "append_log.txt").read_text(encoding="utf-8")
    assert content == "AA", f"append 重复调用必须真实追加（实际 {content!r}——被幂等吞）"


def test_contract_append_resume_eligibility_flag() -> None:
    """契约（5.2）：工具级 resume_eligible 钩子——append 不参与、write 参与。"""
    from bandi.tools.file_ops import FileWriteTool

    tool = FileWriteTool()
    assert tool.resume_eligible({"mode": "append", "path": "x", "content": "y"}) is False
    assert tool.resume_eligible({"mode": "write", "path": "x", "content": "y"}) is True
    assert tool.resume_eligible({"path": "x", "content": "y"}) is True
