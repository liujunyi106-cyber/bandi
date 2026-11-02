"""
  plan 白名单：exit_plan_mode 在 ToolRegistry.PLAN_MODE_TOOLS 单一来源中
     ModeManager.filter_tools 放行
  批准切换：plan 态 + confirm_tool 批准 → mode_manager 切 execute
  fail-closed：无确认通道/用户拒绝 → 保持 plan 不切换
  on_executing 接线：工具批后 mode 变化 → ctx.active_tools 刷新全量 +
     meta["mode"] 同步（写类工具下轮可达）

破坏者断言：非 plan 态调用不重复切换；headless 静默拒绝。
"""

from typing import Any

import pytest

from bandi.core.message import ToolCall

# ── plan 白名单 ────────────────────────────────────────────────────────────


def test_exit_plan_mode_in_plan_allowlist() -> None:
    """契约：白名单单一来源含 exit_plan_mode，filter_tools 放行。"""
    from bandi.agent.mode_manager import ModeManager
    from bandi.tools.registry import ToolRegistry

    assert "exit_plan_mode" in ToolRegistry.PLAN_MODE_TOOLS
    allowed = ModeManager(initial_mode="plan").filter_tools(
        ["read", "grep", "exit_plan_mode", "bash", "write"]
    )
    assert "exit_plan_mode" in allowed and "bash" not in allowed


# ── P1b plan 白名单只读性守卫（u-fix ）──────────────────────────────────────


def _build_production_registry() -> Any:
    """构建生产工具注册表（entity_store=None 时 entity_search 条件缺席可跳过）。"""
    import asyncio

    from bandi.core.config import BandiConfig

    async def _build() -> Any:
        cfg = BandiConfig.load()
        t = cfg.tools
        from bandi.agent.factory import build_tool_registry

        return build_tool_registry(
            None,
            git_timeout=t.git_timeout,
            web_search_timeout_tavily=t.web_search_timeout_tavily,
            web_search_timeout_exa=t.web_search_timeout_exa,
            web_search_timeout_ddg=t.web_search_timeout_ddg,
            bash_timeout_default=t.bash_timeout_default,
        )

    return asyncio.run(_build())


def test_plan_allowlist_members_never_bypass_approval() -> None:
    """契约（P1b/守卫）：_check_ask_list 与 needs_approval 对 plan 跳过询问层
    其安全前提 = 白名单全员非写盘/破坏/高危/ask-override 工具——任一成员腐化
    （如混入写类工具）即静默旁路用户确认，此不变式必须机制守卫。"""
    from bandi.tools.permissions_pipeline import plan_mode_tool_violations
    from bandi.tools.registry import ToolRegistry

    registry = _build_production_registry()
    violations = plan_mode_tool_violations(registry)
    assert violations == [], f"PLAN_MODE_TOOLS 出现需审批形态的腐化成员: {violations}"

    # 名单存在性：守卫针对的是真实白名单，防名单被清空后守卫空转
    assert len(ToolRegistry.PLAN_MODE_TOOLS) >= 5, "白名单被清空会使 plan 模式失去可用只读工具面"


def test_plan_allowlist_guard_catches_corrupted_member() -> None:
    """变异自检：注入一个破坏性工具到临时注册表后守卫必须报违规——"""
    from bandi.tools.base import BaseTool
    from bandi.tools.permissions_pipeline import plan_mode_tool_violations
    from bandi.tools.registry import ToolRegistry

    class _CorruptedTool(BaseTool):
        name = "fake_writer"
        description = "corruption probe"
        risk_level = "low"
        is_destructive = True

        async def execute(self, params: dict[str, Any], context: Any = None) -> str:
            return ""

    registry = ToolRegistry()
    registry.register_instance(_CorruptedTool())
    # 把腐化工具塞进白名单副本语义：直接以 registry 全集校验时按名单过滤
    # 故此处用 monkeypatch 名单验证守卫对名单内容的敏感性
    original = ToolRegistry.PLAN_MODE_TOOLS
    try:
        ToolRegistry.PLAN_MODE_TOOLS = (*original, "fake_writer")
        violations = plan_mode_tool_violations(registry)
        assert any("fake_writer" in v for v in violations), "破坏性工具混入白名单必须被守卫捕获"
    finally:
        ToolRegistry.PLAN_MODE_TOOLS = original


# ── /工具行为 ────────────────────────────────────────────────────────────


class _ModeManagerStub:
    def __init__(self, initial: str = "plan") -> None:
        self.mode = initial

    def switch(self, new: str) -> str:
        old = self.mode
        self.mode = new
        return old

    def filter_tools(self, tool_names: list) -> list:
        from bandi.tools.registry import ToolRegistry

        if self.mode == "plan":
            return [t for t in tool_names if t in ToolRegistry.PLAN_MODE_TOOLS]
        return list(tool_names)


class _ConfirmPipelineStub:
    def __init__(self, granted: bool) -> None:
        self._granted = granted
        self.asked = False

    async def confirm_tool(self, **kw: Any) -> bool:
        self.asked = True
        return self._granted


@pytest.mark.asyncio
async def test_approved_switch_to_execute() -> None:
    """契约：plan 态经确认通道批准 → mode 切 execute。"""
    from bandi.tools.base import ToolPermissionContext
    from bandi.tools.exit_plan_mode import ExitPlanModeTool

    mm = _ModeManagerStub("plan")
    pp = _ConfirmPipelineStub(granted=True)
    tool = ExitPlanModeTool()

    result = await tool.execute(
        {}, context=ToolPermissionContext(extra={"mode_manager": mm}, permissions_pipeline=pp)
    )

    assert result["switched"] is True
    assert mm.mode == "execute"
    assert pp.asked, "必须经过用户确认通道"


@pytest.mark.asyncio
async def test_denied_or_headless_stays_in_plan() -> None:
    """契约（+破坏者）：拒绝与 headless（无管道）均保持 plan。"""
    from bandi.tools.base import ToolPermissionContext
    from bandi.tools.exit_plan_mode import ExitPlanModeTool

    tool = ExitPlanModeTool()

    mm_deny = _ModeManagerStub("plan")
    r_deny = await tool.execute(
        {},
        context=ToolPermissionContext(
            extra={"mode_manager": mm_deny}, permissions_pipeline=_ConfirmPipelineStub(False)
        ),
    )
    assert r_deny["switched"] is False and mm_deny.mode == "plan"

    mm_headless = _ModeManagerStub("plan")
    r_headless = await tool.execute(
        {}, context=ToolPermissionContext(extra={"mode_manager": mm_headless})
    )
    assert r_headless["switched"] is False and mm_headless.mode == "plan", (
        "headless 无确认通道必须 fail-closed 保持 plan"
    )

    mm_exec = _ModeManagerStub("execute")
    r_exec = await tool.execute({}, context=ToolPermissionContext(extra={"mode_manager": mm_exec}))
    assert r_exec["switched"] is False and mm_exec.mode == "execute", "非 plan 态不得切换"

    r_unbound = await tool.execute({}, context=None)
    assert "error" in r_unbound, "standalone 无绑定必须报错而非静默"


# ── on_executing 接线 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_executing_refreshes_active_tools_after_mode_switch() -> None:
    """契约：批准切 execute 后当轮刷新 ctx.active_tools 为全量 + meta 同步。"""
    from types import SimpleNamespace

    from bandi.agent import states as states_mod
    from bandi.core.message import Message, MessageRole, ToolResult

    class _RegistryStub:
        def list_tools(self) -> list:
            return [SimpleNamespace(name=n) for n in ("read", "grep", "bash", "write")]

        def list_schemas(self, mode=None, allowed_tools=None) -> list:
            names = [
                n
                for n in ("read", "grep", "bash", "write")
                if allowed_tools is None or n in allowed_tools
            ]
            return [{"name": n} for n in names]

    class _Executor:
        def __init__(self) -> None:
            self.registry = _RegistryStub()
            self.bound_meta: dict | None = None

        def bind_session_meta(self, meta: dict) -> None:
            self.bound_meta = meta

        def set_runtime_snapshot(self, snap: dict) -> None:
            pass

        async def execute(self, calls: list) -> list:
            return [
                ToolResult(
                    tool_call_id=tc.id, tool_name=tc.name, output="ok", success=True, duration_ms=1
                )
                for tc in calls
            ]

    class _Bus:
        async def emit(self, *a: Any, **k: Any) -> None:
            return None

    mm = _ModeManagerStub("plan")
    executor = _Executor()
    cfg_agent = SimpleNamespace(context_window=1000)
    ctx = SimpleNamespace(
        terminal_reason=None,
        turn_index=1,
        current_tool_calls=[ToolCall(id="c1", name="exit_plan_mode", arguments={})],
        tool_executor=executor,
        meta={"tracer": None, "task_ledger": None, "mode": "plan", "mode_manager": mm},
        session_id="s-plan",
        messages=[Message(role=MessageRole.USER, content="q")],
        tool_results=None,
        cot_trace=[],
        loop_detection_window=5,
        tool_sequence_hashes=[],
        config=cfg_agent,
        consumed_tokens=0,
        total_tokens=0,
        active_tools=[{"name": "read"}],
        compress=SimpleNamespace(pipeline=None),
        provider=SimpleNamespace(model_name="m"),
        bus=_Bus(),
    )
    # exit_plan_mode 工具执行体由真实工具完成（此处 executor 直接返回 ok）
    # 模拟其副作用：mode_manager 已被批准切换
    mm.switch("execute")

    await states_mod.on_executing(ctx)

    assert executor.bound_meta is not None, "session meta 必须绑定到 executor"
    assert ctx.meta["mode"] == "execute", "meta mode 必须同步"
    names = {s.get("name") for s in ctx.active_tools}
    assert names == {"read", "grep", "bash", "write"}, (
        f"active_tools 必须刷新为全量（实际 {names}）"
    )
