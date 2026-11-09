"""
契约族：plan 白名单跨入口一致性（六-）。

背景：plan 工具白名单曾有多处定义漂移风险（ModeManager / PermissionsPipeline /
registry / API / CLI / subagent）。审计要求全部入口引用同一来源
`ToolRegistry.PLAN_MODE_TOOLS`。

本契约钉的行为族（全部生产入口）：
- `agent/mode_manager.py::ModeManager.filter_tools`（工具枚举过滤）
- `tools/permissions_pipeline.py::PermissionsPipeline.check`（plan 拒绝写工具）
- `tools/registry.py::ToolRegistry.list_schemas`（schema 枚举过滤）

断言「最终产物」：白名单内工具（read）在全部入口 plan 模式下可见/可执行；
白名单外写工具（write）在全部入口 plan 模式下不可见/被拒——**跨入口零漂移**。
"""

from __future__ import annotations

import asyncio

from bandi.agent.mode_manager import ModeManager
from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.permissions_pipeline import PermissionsPipeline
from bandi.tools.registry import ToolRegistry

_IN = "read"  # 白名单成员
_OUT = "write"  # 白名单外写工具


def test_contract_plan_whitelist_consistent_across_entries() -> None:
    """三入口 plan 判定一致：白名单内放行、白名单外拦截，零漂移。"""
    assert _IN in ToolRegistry.PLAN_MODE_TOOLS and _OUT not in ToolRegistry.PLAN_MODE_TOOLS

    # 入口 1：ModeManager.filter_tools
    mm = ModeManager(initial_mode="plan")
    kept = mm.filter_tools([_IN, _OUT])
    assert kept == [_IN], f"mode_manager plan 过滤漂移：{kept}"

    # 入口 2：PermissionsPipeline.check（plan 态跳过 ask，但写工具仍被 mode 拒）
    pipeline = PermissionsPipeline()

    class _T(BaseTool):
        name = "x"
        description = ""
        category = ToolCategory.UTIL

        async def execute(self, params, context=None):
            return "ok"

    tool_in = _T()
    tool_in.name = _IN
    tool_out = _T()
    tool_out.name = _OUT
    v_in = asyncio.run(pipeline.check(ToolCall(id="a", name=_IN, arguments={}), tool_in, "plan"))
    v_out = asyncio.run(pipeline.check(ToolCall(id="b", name=_OUT, arguments={}), tool_out, "plan"))
    assert v_in.allow is True, f"plan 白名单成员必须允许，reason={v_in.reason}"
    assert v_out.allow is False, "plan 白名单外写工具必须拒绝"

    # 入口 3：registry.list_schemas（schema 枚举过滤）
    reg = ToolRegistry()
    reg.register_instance(tool_in)
    reg.register_instance(tool_out)
    schemas = reg.list_schemas(mode="plan")
    names = [s["function"]["name"] for s in schemas]
    assert _IN in names and _OUT not in names, f"registry plan 枚举漂移：{names}"
