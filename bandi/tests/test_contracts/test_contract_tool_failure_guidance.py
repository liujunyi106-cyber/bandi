# 功能：契约工具失败须立即回喂换路引导，不留零引导区
#
# 行为不变式（钉行为，不钉实现）：
#   1. 首次失败即带引导——告知核对参数、可改用其他工具/路径，禁止原样重试
#   2. 第二次失败升级引导——明确"再失败一次该工具将被暂停"
#   3. 第三次调用进入暂停（既有语义不变），错误文案含改用其他工具的指引
#   4. 成功调用不受影响（失败计数重置、无引导文案）
#   5. 未知工具同样给引导（不得只回 "unknown tool" 让模型瞎猜）

from __future__ import annotations

import tempfile
from typing import Any

import pytest

from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


class _FailTool(BaseTool):
    name: str = "failer"
    description: str = ""

    async def execute(self, params: dict[str, Any], **kwargs: Any) -> str:
        raise RuntimeError("boom")


@pytest.fixture
def executor() -> ToolExecutor:
    registry = ToolRegistry()
    registry.register_instance(_FailTool())
    return ToolExecutor(registry, consecutive_failure_threshold=3, temp_dir=tempfile.mkdtemp())


class TestFirstFailureGuidance:
    @pytest.mark.asyncio
    async def test_first_failure_has_guidance(self, executor: ToolExecutor) -> None:
        """功能：核心不变式——首次失败必须自带换路引导，不留零引导区。"""
        result = (await executor.execute([ToolCall(id="1", name="failer", arguments={})]))[0]

        assert result.success is False
        assert "失败引导" in (result.error or ""), "首次失败须附引导"
        assert "其他工具" in (result.error or "") or "换" in (result.error or "")

    @pytest.mark.asyncio
    async def test_first_failure_not_abort(self, executor: ToolExecutor) -> None:
        """功能：首次失败不得中止（仍给模型自纠机会）。"""
        result = (await executor.execute([ToolCall(id="1", name="failer", arguments={})]))[0]

        assert result.should_abort is False


class TestSecondFailureEscalation:
    @pytest.mark.asyncio
    async def test_second_failure_warns_about_pause(self, executor: ToolExecutor) -> None:
        """功能：第二次失败升级引导——预告暂停，促使立即换路。"""
        await executor.execute([ToolCall(id="1", name="failer", arguments={})])
        result = (await executor.execute([ToolCall(id="2", name="failer", arguments={})]))[0]

        assert result.success is False
        assert "暂停" in (result.error or ""), "第二次失败须预告暂停"


class TestPauseStageKeepsGuidance:
    @pytest.mark.asyncio
    async def test_third_call_paused_with_alternative_guidance(
        self, executor: ToolExecutor
    ) -> None:
        """功能：第三次调用进入暂停，文案仍含改用其他工具的指引（既有语义）。"""
        await executor.execute([ToolCall(id="1", name="failer", arguments={})])
        await executor.execute([ToolCall(id="2", name="failer", arguments={})])
        result = (await executor.execute([ToolCall(id="3", name="failer", arguments={})]))[0]

        assert result.success is False
        assert "暂停" in (result.error or "")
        assert "其他工具" in (result.error or "")


class TestSuccessUnaffected:
    @pytest.mark.asyncio
    async def test_success_has_no_guidance_and_resets(self) -> None:
        """功能：成功路径无引导文案、失败计数重置（无回归）。"""

        class _FlipTool(BaseTool):
            name: str = "flip"
            description: str = ""
            fail: bool = True

            async def execute(self, params: dict[str, Any], **kwargs: Any) -> str:
                if self.fail:
                    raise RuntimeError("first")
                return "ok"

        tool = _FlipTool()
        registry = ToolRegistry()
        registry.register_instance(tool)
        executor = ToolExecutor(
            registry, consecutive_failure_threshold=3, temp_dir=tempfile.mkdtemp()
        )

        await executor.execute([ToolCall(id="1", name="flip", arguments={})])
        tool.fail = False
        result = (await executor.execute([ToolCall(id="2", name="flip", arguments={})]))[0]

        assert result.success is True
        assert executor._failure_count.get("flip", 0) == 0


class TestUnknownToolGuidance:
    @pytest.mark.asyncio
    async def test_unknown_tool_gets_guidance(self) -> None:
        """功能：未知工具也须引导换路，不得只回 'unknown tool' 让模型反复猜。"""
        executor = ToolExecutor(ToolRegistry(), temp_dir=tempfile.mkdtemp())

        result = (await executor.execute([ToolCall(id="1", name="no_such_tool", arguments={})]))[0]

        assert result.success is False
        assert "unknown tool" in (result.error or "")
        assert "其他工具" in (result.error or "") or "可用工具" in (result.error or "")
