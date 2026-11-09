"""契约：工具面工件在「闲聊裁剪」与「Delta 增量」之间的边界。

两件事必须同时成立：
1. 闲聊轮裁掉全量工具面（token 优化——同一份工具面已经经 `tools` 参数发过一遍）
2. 工具面跨轮只发增量（Delta 契约——重复工具不再以文本重注入）

冲突点：闲聊裁剪的前提是「注册表里存在 tool_search 这个按需检索入口」。前提
不成立时若照裁，本轮什么都不投递，Delta 基线随之断掉——下一轮整份工具面被当成
新工件重发，模型看到从未见过的工具列表却被标成增量。
"""

from __future__ import annotations

from typing import Any

import pytest

from bandi.company.output_contract import (
    ARTIFACT_TYPE_TOOL_SURFACE,
    RUNTIME_ARTIFACT_DELTA_HEADER,
    RUNTIME_ARTIFACT_HEADER,
    changed_artifacts,
)
from bandi.perception.context_assembler import ContextAssembler
from bandi.tools.base import BaseTool


class _Echo(BaseTool):
    name: str = "echo"
    description: str = "回声工具"

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        return "echo"


class _Search(BaseTool):
    name: str = "tool_search"
    description: str = "按需检索工具入口"

    async def execute(self, params: dict[str, Any], context: Any = None) -> list[str]:
        return []


class _Write(BaseTool):
    name: str = "write"
    description: str = "写文件工具"

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        return "written"


def _assembler(*tools: BaseTool) -> ContextAssembler:
    from bandi.tools.registry import ToolRegistry

    reg = ToolRegistry()
    for t in tools:
        reg.register_instance(t)
    return ContextAssembler(tool_registry=reg)


class TestChitchatTrim:
    def test_trims_to_search_entry_when_present(self) -> None:
        # 纯寒暄才裁剪；能力询问（「你能做什么」）不裁——见 capability_honesty 契约
        asm = _assembler(_Echo(), _Search(), _Write())
        art = asm._build_tool_surface_artifact(None, None, "你好")
        assert art is not None
        assert "tool_search" in art.content
        assert "echo" not in art.content, "寒暄轮不应把全量工具面以文本重发"

    def test_capability_query_not_trimmed(self) -> None:
        """破坏者视角：能力询问若也被裁，模型只能据实报告最小面 = 谎报能力。"""
        asm = _assembler(_Echo(), _Search(), _Write())
        art = asm._build_tool_surface_artifact(None, None, "你能做什么")
        assert art is not None
        assert "echo" in art.content

    def test_falls_back_to_full_when_no_search_entry(self) -> None:
        """破坏者视角：注册表没有 tool_search 时，裁剪会把本轮投递清空。"""
        asm = _assembler(_Echo(), _Write())
        art = asm._build_tool_surface_artifact(None, None, "你能做什么")
        assert art is not None, "裁成空会让 Delta 基线断掉"
        assert "echo" in art.content


class TestDeltaBaseline:
    def test_second_turn_artifact_is_delta(self) -> None:
        """首轮投递过工具面 → 次轮工具集变化必须标为增量而非全量重发。"""
        asm = _assembler(_Echo(), _Write())
        first = asm._build_tool_surface_artifact(None, None, "写个脚本")
        assert first is not None
        prev = {ARTIFACT_TYPE_TOOL_SURFACE: "stale-hash"}
        pairs = changed_artifacts([first], prev)
        assert pairs[0][1] is True, "已有基线且内容变化 → 应标增量"
        assert pairs[0][0].artifact_type == ARTIFACT_TYPE_TOOL_SURFACE

    def test_no_baseline_is_full_not_delta(self) -> None:
        """首轮没有基线时标全量——模型从没见过这份工具面。"""
        asm = _assembler(_Echo(), _Write())
        art = asm._build_tool_surface_artifact(None, None, "写个脚本")
        pairs = changed_artifacts([art], {})
        assert pairs[0][1] is False

    def test_headers_differ(self) -> None:
        assert RUNTIME_ARTIFACT_DELTA_HEADER != RUNTIME_ARTIFACT_HEADER


class TestSurfaceToolsParity:
    def test_artifact_lists_only_surface_tools(self) -> None:
        """团队与普通会话同口径：文本工具面 = 相关性裁剪后的 active tools。"""
        asm = _assembler(_Echo(), _Search(), _Write())
        art = asm._build_tool_surface_artifact(None, None, "写个脚本", surface_tools=["write"])
        assert art is not None
        assert "write" in art.content
        assert "echo" not in art.content and "tool_search" not in art.content

    def test_current_tool_names_follows_surface_tools(self) -> None:
        asm = _assembler(_Echo(), _Write())
        assert asm.current_tool_names(None, ["write"]) == ["write"]


class TestChitchatThenRealTurn:
    def test_delta_survives_chitchat_first_turn(self) -> None:
        """端到端口径：闲聊首轮（有检索入口）→ 次轮新增工具，仍走增量。"""
        # 首轮：注册表含 tool_search，闲聊裁剪投递检索入口
        turn1 = _assembler(_Echo(), _Search(), _Write())
        art1 = turn1._build_tool_surface_artifact(None, None, "你好")
        assert art1 is not None
        known = ["tool_search"]
        # 次轮：非闲聊，新增工具相对已知集合仍是增量
        turn2 = _assembler(_Echo(), _Search(), _Write())
        art2 = turn2._build_tool_surface_artifact(None, known, "写个脚本")
        assert art2 is not None
        assert "write" in art2.content
        assert "tool_search" not in art2.content, "已投递过的工具不重复文本注入"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
