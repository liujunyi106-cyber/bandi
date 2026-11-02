"""
工具级显式重试策略契约（day-stage5）：
- 决策链 = run 级否决（error_class/should_abort/deny 文案）→ 工具显式 retry_policy 权威
  → MCP 动态 retryable 开关 → legacy 回退（is_destructive/名单/error 关键词）
- 显式 retry_policy 声明可重试时，max_retries/backoff_seconds 必须生效
- 8 个副作用工具必须声明 retry_policy=RetryPolicy(retryable=False)
"""

from __future__ import annotations

from typing import Any

import pytest

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.middleware.retry_mw import RetryMiddleware
from bandi.tools.pipeline import ToolPipeline


def _call(name: str = "t1") -> ToolCall:
    return ToolCall(id=f"id-{name}", name=name, arguments={})


class _FlakyRun:
    """计数型 run_tool 回调——前 fail_n 次失败，之后成功；支持注入 meta/should_abort。"""

    def __init__(
        self,
        fail_n: int = 99,
        error: str = "server error",
        meta: dict[str, Any] | None = None,
        should_abort: bool = False,
    ) -> None:
        self.calls = 0
        self._fail_n = fail_n
        self._error = error
        self._meta = meta
        self._should_abort = should_abort

    async def __call__(self, ctx: Any, call: ToolCall) -> ToolResult:
        self.calls += 1
        if self.calls <= self._fail_n:
            return ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                success=False,
                output="",
                error=self._error,
                meta=dict(self._meta) if self._meta else None,
                should_abort=self._should_abort,
            )
        return ToolResult(tool_call_id=call.id, tool_name=call.name, success=True, output="ok")


def _pipeline(max_retries: int = 2) -> ToolPipeline:
    p = ToolPipeline()
    p.add(RetryMiddleware(max_retries=max_retries, base_delay=0.0))
    return p


def _ctx(tool: Any = None) -> Any:
    from bandi.tools.pipeline import ToolContext

    extra = {"tool": tool} if tool is not None else {}
    return ToolContext(extra=extra)


class _ToolStub:
    """legacy 类工具桩——基础属性；选填 retry_policy/retryable 显式声明。"""

    def __init__(
        self,
        name: str,
        *,
        destructive: bool = False,
        readonly: bool = False,
        retry_policy: Any = None,
        retryable: Any = None,
    ) -> None:
        self.name = name
        self.is_destructive = destructive
        self.is_read_only = readonly
        self.is_idempotent = False
        self.is_concurrency_safe = readonly
        self.risk_level = "low"
        self.permissions_override = None
        self.retry_policy = retry_policy
        self.retryable = retryable


def _new_retry_policy_class(
    retryable: bool, max_retries: int = 2, backoff_seconds: float = 0.0
) -> Any:
    from bandi.tools.base import RetryPolicy

    return RetryPolicy(
        retryable=retryable, max_retries=max_retries, backoff_seconds=backoff_seconds
    )


class TestSideEffectToolsDeclareNonRetryable:
    """DoD-2 硬指标：副作用工具必须声明 retry_policy.retryable=False（参数化覆盖全部成员）。"""

    _TOOLS: list[tuple[type, str]] = [
        ("BashTool", "bash"),
        ("FileWriteTool", "write"),
        ("FileEditTool", "edit"),
        ("ApplyPatchTool", "apply_patch"),
        ("GitCommitTool", "git_commit"),
        ("PythonExecTool", "python_exec"),
        ("BrowserTool", "browser"),
    ]

    @pytest.mark.parametrize(
        ("cls_name", "tool_name"),
        _TOOLS,
        ids=[name for name, _ in _TOOLS],
    )
    def test_tool_declares_non_retryable_policy(self, cls_name: str, tool_name: str) -> None:
        from bandi.tools import (
            ApplyPatchTool,
            BashTool,
            BrowserTool,
            FileEditTool,
            FileWriteTool,
            GitCommitTool,
            PythonExecTool,
        )
        from bandi.tools.base import RetryPolicy

        cls = {
            "BashTool": BashTool,
            "FileWriteTool": FileWriteTool,
            "FileEditTool": FileEditTool,
            "ApplyPatchTool": ApplyPatchTool,
            "GitCommitTool": GitCommitTool,
            "PythonExecTool": PythonExecTool,
            "BrowserTool": BrowserTool,
        }[cls_name]

        policy = getattr(cls, "retry_policy", None)
        assert policy is not None, f"{tool_name} 必须声明 retry_policy（不能依赖静态名单）"
        assert isinstance(policy, RetryPolicy)
        assert policy.retryable is False, f"{tool_name} 副作用工具必须声明 retryable=False"

    def test_ontology_retract_declares_non_retryable_policy(self, tmp_path: Any) -> None:
        """本体撤回是破坏性写——适配器须声明 non-retryable（防重放重复删事实）。"""
        from bandi.ontology.spec import open_ontology
        from bandi.ontology.tools import OntologyToolContext
        from bandi.ontology.triple_store import OntologyStore
        from bandi.tools.base import RetryPolicy
        from bandi.tools.ontology_tool_adapter import build_ontology_tools

        store = OntologyStore(open_ontology(str(tmp_path / "o.db")))
        tools = {t.name: t for t in build_ontology_tools(OntologyToolContext(store))}
        policy = getattr(tools["ontology_retract"], "retry_policy", None)
        assert isinstance(policy, RetryPolicy), "ontology_retract 必须声明 retry_policy"
        assert policy.retryable is False, "ontology_retract 破坏性工具必须声明 retryable=False"


class TestExplicitRetryPolicyAuthority:
    """显式 retry_policy 权威：声明决定是否重试，独立于 is_destructive/静态名单。"""

    @pytest.mark.asyncio
    async def test_retryable_false_blocks_retry_for_safe_tool(self) -> None:
        """名单外、非 destructive 的安全工具一旦显式声明 retryable=False 也禁重试。"""
        policy = _new_retry_policy_class(retryable=False)
        run = _FlakyRun()
        pipeline = _pipeline()
        ctx = _ctx(_ToolStub("custom_safe", readonly=True, retry_policy=policy))
        await pipeline.execute(ctx, _call("custom_safe"), run)
        assert run.calls == 1, "显式 retry_policy.retryable=False 必须禁重试"

    @pytest.mark.asyncio
    async def test_retryable_true_retries_with_policy_attempts(self) -> None:
        """显式 retryable=True 用 policy.max_retries 重试，直到成功。"""
        policy = _new_retry_policy_class(retryable=True, max_retries=2)
        run = _FlakyRun(fail_n=2)
        pipeline = _pipeline()
        ctx = _ctx(_ToolStub("custom_safe", readonly=True, retry_policy=policy))
        result = await pipeline.execute(ctx, _call("custom_safe"), run)
        assert result.success is True
        assert run.calls == 3, "1 初次 + policy.max_retries=2 重试"

    @pytest.mark.asyncio
    async def test_retryable_true_exhausts_policy_max_retries(self) -> None:
        """持续失败时总调用数 = 1 + policy.max_retries（参数生效）。"""
        policy = _new_retry_policy_class(retryable=True, max_retries=1)
        run = _FlakyRun(fail_n=99)
        pipeline = _pipeline()
        ctx = _ctx(_ToolStub("custom_safe", readonly=True, retry_policy=policy))
        result = await pipeline.execute(ctx, _call("custom_safe"), run)
        assert result.success is False
        assert run.calls == 2, "1 初次 + 1 重试（policy.max_retries=1）"

    @pytest.mark.asyncio
    async def test_max_retries_zero_disables_retry(self) -> None:
        """max_retries=0 边界：声明可重试但重试次数为 0 = 仅初次。"""
        policy = _new_retry_policy_class(retryable=True, max_retries=0)
        run = _FlakyRun()
        pipeline = _pipeline()
        ctx = _ctx(_ToolStub("custom_safe", readonly=True, retry_policy=policy))
        await pipeline.execute(ctx, _call("custom_safe"), run)
        assert run.calls == 1


class TestRunLevelNegationsBeatExplicitPolicy:
    """破坏者视角：run 级硬否决必须优先于显式 retryable=True（防止用户否决被声明绕过）。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("meta", "should_abort", "error"),
        [
            ({"error_class": "aborted"}, False, "用户中止了该操作"),
            ({"error_class": "non_retryable"}, False, "业务校验失败"),
            (None, True, "doomed"),
            (None, False, "user denied the operation. Do not retry this action"),
        ],
        ids=["aborted", "non_retryable", "should_abort", "deny_marker"],
    )
    async def test_run_level_negation_blocks_retry(
        self, meta: dict[str, Any] | None, should_abort: bool, error: str
    ) -> None:
        policy = _new_retry_policy_class(retryable=True, max_retries=3)
        run = _FlakyRun(fail_n=99, error=error, meta=meta, should_abort=should_abort)
        pipeline = _pipeline()
        ctx = _ctx(_ToolStub("custom_safe", readonly=True, retry_policy=policy))
        await pipeline.execute(ctx, _call("custom_safe"), run)
        assert run.calls == 1, (
            f"run 级否决({meta or should_abort or error})不得被显式 retryable=True 绕过"
        )


class TestMCPRetryableSwitchConsumed:
    """的 MCP 动态工具 retryable 属性（bool 类属性）必须被重试层消费。"""

    def _mcp_tool(self, retryable: bool, destructive: bool = False) -> Any:
        tool = _ToolStub("srv_probe", readonly=not destructive, retryable=retryable)
        tool.is_destructive = destructive
        return tool

    @pytest.mark.asyncio
    async def test_mcp_retryable_false_blocks_retry(self) -> None:
        run = _FlakyRun()
        pipeline = _pipeline()
        ctx = _ctx(self._mcp_tool(retryable=False))
        await pipeline.execute(ctx, _call("srv_probe"), run)
        assert run.calls == 1, "MCP retryable=False 必须禁重试"

    @pytest.mark.asyncio
    async def test_mcp_retryable_true_allows_retry(self) -> None:
        run = _FlakyRun(fail_n=1)
        pipeline = _pipeline()
        ctx = _ctx(self._mcp_tool(retryable=True))
        result = await pipeline.execute(ctx, _call("srv_probe"), run)
        assert result.success is True
        assert run.calls == 2, "MCP retryable=True 允许重试到成功"

    @pytest.mark.asyncio
    async def test_mcp_retryable_false_blocks_connection_retry(self) -> None:
        """MCP retryable=False 是声明权威——连接关键词不得绕过（高危 fail-open 收敛：
        错误文本可伪造 connection/unavailable/transport，若放行则副作用工具可被重放）。
        连接型瞬时故障的自动恢复仅保留给未声明/只读安全工具。"""
        run = _FlakyRun(fail_n=1, error="MCP connection unavailable")
        pipeline = _pipeline()
        ctx = _ctx(self._mcp_tool(retryable=False, destructive=True))
        await pipeline.execute(ctx, _call("srv_probe"), run)
        assert run.calls == 1, "retryable=False 的 MCP 工具连接错误也禁重试"


class TestCrossProcessResumeIsMetadataOnly:
    """cross_process_resume 仅是元数据开关（本版不做 idempotency key）
    设定 True 不得改变本进程重试行为，也不得引入任何副作用去重。"""

    def test_field_exists_and_defaults_false(self) -> None:
        from bandi.tools.base import RetryPolicy

        policy = RetryPolicy(retryable=True)
        assert policy.cross_process_resume is False
        assert hasattr(policy, "cross_process_resume")

    @pytest.mark.asyncio
    async def test_cross_process_resume_true_does_not_gate_retry(self) -> None:
        from bandi.tools.base import RetryPolicy

        policy = RetryPolicy(
            retryable=True, max_retries=1, backoff_seconds=0.0, cross_process_resume=True
        )
        run = _FlakyRun(fail_n=1)
        pipeline = _pipeline()
        ctx = _ctx(_ToolStub("custom_safe", readonly=True, retry_policy=policy))
        result = await pipeline.execute(ctx, _call("custom_safe"), run)
        assert result.success is True
        assert run.calls == 2


class TestExplicitPolicyProductionRoute:
    """DoD-12：显式 retry_policy 经生产装配链（executor + registry + pipeline）逐层透传。"""

    @pytest.mark.asyncio
    async def test_production_executor_respects_explicit_policy(self) -> None:
        from bandi.tools.base import BaseTool, RetryPolicy
        from bandi.tools.executor import ToolExecutor
        from bandi.tools.registry import ToolRegistry

        class _SideEffectTool(BaseTool):
            name = "custom_mutate"
            description = "probe"
            enabled = True
            tags = ["util"]
            retry_policy = RetryPolicy(retryable=False, max_retries=3, backoff_seconds=0.0)

            def __init__(self) -> None:
                self.calls = 0

            async def execute(self, params: dict[str, Any], context: Any = None) -> str:
                self.calls += 1
                raise RuntimeError("transient mutation failure")

        registry = ToolRegistry()
        tool = _SideEffectTool()
        registry.register_instance(tool)
        executor = ToolExecutor(registry, tool_pipeline=_pipeline(max_retries=3))
        results = await executor.execute([_call("custom_mutate")])
        assert results[0].success is False
        assert tool.calls == 1, (
            "生产链路必须遵守显式 retry_policy.retryable=False（防御 RuntimeError 归类混淆）"
        )


class TestLegacyFallbackUnchanged:
    """无显式声明时 legacy 回退行为保持不动（防止声明式改造破坏既有静态名单）。"""

    @pytest.mark.asyncio
    async def test_legacy_list_blocks_retry(self) -> None:
        from bandi.tools.middleware.retry_mw import NON_RETRYABLE_TOOL_NAMES

        run = _FlakyRun()
        pipeline = _pipeline()
        name = sorted(NON_RETRYABLE_TOOL_NAMES)[0]
        ctx = _ctx(_ToolStub(name))  # 无 retry_policy / 无 retryable → legacy
        await pipeline.execute(ctx, _call(name), run)
        assert run.calls == 1

    @pytest.mark.asyncio
    async def test_legacy_readonly_still_retried(self) -> None:
        run = _FlakyRun(fail_n=2)
        pipeline = _pipeline()
        ctx = _ctx(_ToolStub("web_search", readonly=True))
        result = await pipeline.execute(ctx, _call("web_search"), run)
        assert result.success is True
        assert run.calls == 3
