"""
契约族：审批门判定与真实权限判定必须同语义（permissions_override="ask" 的审批暂停）。

背景（本次全量审查发现）：
- `tools/permissions_pipeline.py::PermissionsPipeline.check()` 在 Layer 1 之前先处理
  `tool.permissions_override`：`"deny"` → 拒；`"ask"` → `_check_override_ask()` → 末段
  `_check_ui()`（headless 无通道 → **拒绝**）。
- 而 `executor._approval_gate()` 的触发条件走 `needs_approval()`，后者此前只委托
  `_check_ask_list()`（只看 ask_tools 名单 + risk_level ∈ {high, critical}），**不看
  override**。
- 后果：`apply_patch`(medium/ask)、`edit`(medium/ask)、`git_commit`(medium/ask) 等
  `permissions_override="ask"` 且风险级非高危的工具，在 headless/API 下
  `needs_approval=False` → **审批暂停门永不触发** → 落到 `check()` 的 `_check_ui()`
  被**硬拒**（"requires approval but no confirmation channel"）。
  即：审批暂停（AWAITING_APPROVAL）这一头等能力，对自己声明「需询问」的工具完全失效。

契约钉的行为族：
- `PermissionsPipeline.needs_approval` 必须覆盖 `check()` 会走 ASK 的全部成因
- `executor._approval_gate` 对真实工具（生产类，非桩）必须触发 APPROVAL_REQUIRED
- 破坏者：bypass 工具 / plan 模式 / 非 ask 工具不得被误拦
"""

from __future__ import annotations

from typing import Any

import pytest

from bandi.core.message import ToolCall
from bandi.tools.apply_patch import ApplyPatchTool
from bandi.tools.file_edit import FileEditTool
from bandi.tools.git_ops import GitCommitTool
from bandi.tools.permissions_pipeline import PermissionsPipeline


def _real_ask_tools() -> list[Any]:
    """生产工具类实例（非桩）——契约必须钉真实工具形态，防止桩掩盖风险级/override。"""
    return [ApplyPatchTool(), FileEditTool(), GitCommitTool()]


def test_override_ask_tools_are_not_in_default_ask_list() -> None:
    """前提钉死：这些工具**不在**默认 ask_tools 名单里，否则本契约失去意义。"""
    from bandi.core.config import PermissionsConfig

    ask_list = set(PermissionsConfig().ask_tools)
    for tool in _real_ask_tools():
        assert tool.permissions_override == "ask"
        assert tool.risk_level not in ("high", "critical"), (
            f"{tool.name} 风险级已升高危——本契约前提失效，需重新评估"
        )
        assert tool.name not in ask_list, f"{tool.name} 已在 ask_tools，契约前提失效"


def test_needs_approval_covers_override_ask_tools() -> None:
    """核心断言：override="ask" 的工具必须被判为需审批（否则审批门永不触发）。"""
    pp = PermissionsPipeline()
    for tool in _real_ask_tools():
        tc = ToolCall(id="t1", name=tool.name, arguments={})
        assert pp.needs_approval(tc, tool, "execute") is True, (
            f"{tool.name} override=ask 但 needs_approval=False → 审批暂停不可达，"
            "headless 下会被硬拒而非挂起待批"
        )


def test_needs_approval_is_single_source_with_check_ask_layer() -> None:
    """needs_approval 必须恒等于 Layer 2 ask 判定（含 override=ask）——防再次漂移。

    plan 模式是显式短路（只读白名单另行把关），故 plan 下恒不询问不算漂移。
    """
    pp = PermissionsPipeline()
    for tool in _real_ask_tools():
        tc = ToolCall(id="t1", name=tool.name, arguments={})
        for mode in ("execute", "plan"):
            assert (
                pp.needs_approval(tc, tool, mode) == pp._check_ask_list(tc, tool, mode).ask_user
            ), f"drift on {tool.name}/{mode}"
        assert pp.needs_approval(tc, tool, "execute") is True
        assert pp.needs_approval(tc, tool, "plan") is False, "plan 模式不得询问"


@pytest.mark.asyncio
async def test_headless_gate_pauses_override_ask_tool(monkeypatch: Any) -> None:
    """端到端：headless 下真实 override=ask 工具必须挂起待批，而非被硬拒。"""
    from bandi.tools.approval import get_approval_store, reset_approval_store
    from bandi.tools.executor import ToolExecutor

    reset_approval_store()
    monkeypatch.setattr("bandi.tools.executor.ToolExecutor.__init__", lambda self, *a, **k: None)
    ex = ToolExecutor()
    ex._permissions_pipeline = PermissionsPipeline()  # headless：user_interaction=None
    ex._mode = "execute"
    ex._session_meta = {"session_id": "sess-override"}

    tool = ApplyPatchTool()
    tc = ToolCall(id="t-ap", name=tool.name, arguments={"patch": "*** Begin Patch\n*** End Patch"})
    gate = await ex._approval_gate(tc, tool)
    assert gate is not None, "headless override=ask 工具必须进入审批门（而非直接硬拒）"
    assert (gate.error or "").startswith("APPROVAL_REQUIRED:")
    assert get_approval_store().get_pending("sess-override") is not None
    reset_approval_store()


def test_bypass_and_plan_mode_not_regressed() -> None:
    """破坏者：bypass 工具与 plan 模式不得被本修复误拦。"""
    from bandi.tools.file_ops import FileReadTool

    pp = PermissionsPipeline()
    read_tool = FileReadTool()
    assert (
        pp.needs_approval(ToolCall(id="r", name=read_tool.name, arguments={}), read_tool, "execute")
        is False
    )

    # bypass 名单工具即使是 override=ask 形态也不应触发（bypass 优先）
    class _BypassAskTool:
        name = "read"
        risk_level = "medium"
        permissions_override = "ask"

    assert (
        pp.needs_approval(ToolCall(id="b", name="read", arguments={}), _BypassAskTool(), "execute")
        is False
    )
    assert (
        pp.needs_approval(
            ToolCall(id="p", name="apply_patch", arguments={}), ApplyPatchTool(), "plan"
        )
        is False
    )
