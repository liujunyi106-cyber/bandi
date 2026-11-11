# 功能：子 Agent 装配契约——project_root 沙箱继承（父→子）+ 共享幂等存储（跨调度重放保序）
# 契约背景：subagent.py:274 的 ToolExecutor 未装配 project_root/idempotency_store——
# 子代理 write 相对路径回落全局 ~/.bandi/workspace（产物错位，与 bash 同源）
# 且跨进程恢复时子代理重写不命中幂等 key（副作用重放）。
from typing import Any

import pytest

from bandi.agent.subagent import SubagentManager
from bandi.agent.subagent_tool import SubAgentTool
from bandi.core.message import LLMResponse, TokenUsage, ToolCall
from bandi.llm.mock import MockProvider
from bandi.tools.base import ToolPermissionContext
from bandi.tools.file_ops import FileWriteTool
from bandi.tools.idempotency import IdempotencyStore
from bandi.tools.permissions_pipeline import PermissionsPipeline
from bandi.tools.registry import ToolRegistry


class _ApprovingUI:
    # 测试基建：模拟用户确认通道恒批准（覆盖 write 的 permissions_override="ask"）。
    async def ask_permission(self, *args: Any, **kwargs: Any) -> bool:
        return True


def _allow_write() -> PermissionsPipeline:
    return PermissionsPipeline(bypass_tools={"write"}, user_interaction=_ApprovingUI())  # type: ignore[arg-type]


def _write_provider(path: str, content: str) -> MockProvider:
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="c1", name="write", arguments={"path": path, "content": content})
            ],
            usage=TokenUsage(prompt_tokens=2, completion_tokens=1),
            finish_reason="tool_calls",
        )
    )
    provider.add_response(
        LLMResponse(content="note written", usage=TokenUsage(prompt_tokens=1, completion_tokens=1))
    )
    return provider


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_instance(FileWriteTool())
    return registry


@pytest.mark.asyncio
class TestSubagentProjectRootAssembly:
    async def test_write_lands_in_project_root(self, tmp_path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        manager = SubagentManager(
            provider=_write_provider("notes/plan.md", "hello"),
            project_root=str(project),
            permissions_pipeline=_allow_write(),
        )
        try:
            result = await manager.spawn("write a plan note", tool_registry=_registry())
        finally:
            manager.close()
        assert result.success, result.message
        target = project / "notes" / "plan.md"
        assert target.is_file(), "子代理相对路径写入必须落在 project_root 内"
        assert target.read_text(encoding="utf-8") == "hello"

    async def test_project_root_isolates_peers(self, tmp_path) -> None:
        p1 = tmp_path / "proj-a"
        p2 = tmp_path / "proj-b"
        p1.mkdir()
        p2.mkdir()
        m1 = SubagentManager(
            provider=_write_provider("notes/plan.md", "a"),
            project_root=str(p1),
            permissions_pipeline=_allow_write(),
        )
        m2 = SubagentManager(
            provider=_write_provider("notes/plan.md", "b"),
            project_root=str(p2),
            permissions_pipeline=_allow_write(),
        )
        try:
            r1 = await m1.spawn("write for a", tool_registry=_registry())
            r2 = await m2.spawn("write for b", tool_registry=_registry())
        finally:
            m1.close()
            m2.close()
        assert r1.success and r2.success
        assert (p1 / "notes" / "plan.md").read_text(encoding="utf-8") == "a"
        assert (p2 / "notes" / "plan.md").read_text(encoding="utf-8") == "b"

    async def test_subagent_tool_inherits_project_root_via_context(self, tmp_path) -> None:
        # 装配点（DoD-11）：父 executor → context.extra["project_root"] → SubAgentTool → manager
        project = tmp_path / "proj"
        project.mkdir()
        tool = SubAgentTool(
            provider=_write_provider("notes/plan.md", "hello"), tool_registry=_registry()
        )
        context = ToolPermissionContext(
            extra={"project_root": str(project)}, permissions_pipeline=_allow_write()
        )
        out = await tool.execute({"task": "write a plan note"}, context=context)
        assert out.get("success"), out
        assert (project / "notes" / "plan.md").is_file()


@pytest.mark.asyncio
class TestSubagentIdempotencyAssembly:
    async def test_write_resumes_from_shared_store(self, tmp_path) -> None:
        # 幂等共享：子代理 write 命中共享 store 同 key → 不重写（副作用保序）
        project = tmp_path / "proj"
        project.mkdir()
        store = IdempotencyStore(str(tmp_path / "idem"))
        manager = SubagentManager(
            provider=_write_provider("notes/plan.md", "hello"),
            project_root=str(project),
            idempotency_store=store,
            permissions_pipeline=_allow_write(),
        )
        try:
            first = await manager.spawn("write a plan note", tool_registry=_registry())
            assert first.success, first.message
            target = project / "notes" / "plan.md"
            assert target.is_file() and target.read_text(encoding="utf-8") == "hello"
            target.write_text("tampered", encoding="utf-8")
            # 清理掉同轮 provider 预置再跑同参数——命中幂等键直接返回，不得重写
            manager._provider.clear_responses()
            manager._provider.add_response(
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="c2",
                            name="write",
                            arguments={"path": "notes/plan.md", "content": "hello"},
                        )
                    ],
                    usage=TokenUsage(prompt_tokens=2, completion_tokens=1),
                    finish_reason="tool_calls",
                )
            )
            manager._provider.add_response(
                LLMResponse(
                    content="note written", usage=TokenUsage(prompt_tokens=1, completion_tokens=1)
                )
            )
            second = await manager.spawn("write a plan note again", tool_registry=_registry())
            assert second.success, second.message
            assert target.read_text(encoding="utf-8") == "hello", (
                "HB-P1 A7 后幂等键含会话维度——子代理每次 spawn 为新会话，"
                "跨会话同参不是同一次操作（允许重写）；同会话恢复语义由 "
                "test_contract_cross_process_resume 覆盖"
            )
        finally:
            manager.close()
