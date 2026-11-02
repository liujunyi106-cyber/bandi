"""
重试层 = RetryMiddleware 单层（factory.build_tool_pipeline 装配
   MCPConnectionMiddleware 已合并移除）
禁重试判定 = ctx.extra["tool"]（executor.py:389 注入）元数据 + result.meta.error_class
executor except 分类 = tools/errors.py 两异常类型 → meta.error_class / should_abort
dict-error（_extract_dict_error→_fail_result）确定性失败不重试
"""

from typing import Any

import pytest

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.base import BaseTool, RetryPolicy
from bandi.tools.middleware.retry_mw import NON_RETRYABLE_TOOL_NAMES, RetryMiddleware
from bandi.tools.pipeline import ToolPipeline


def _call(name: str = "t1") -> ToolCall:
    return ToolCall(id=f"id-{name}", name=name, arguments={})


class _FlakyRun:
    """计数型 run_tool 回调——前 fail_n 次失败，之后成功；支持注入 meta/error_class。"""

    def __init__(
        self,
        fail_n: int = 99,
        error: str = "boom",
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.calls = 0
        self._fail_n = fail_n
        self._error = error
        self._meta = meta

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
            )
        return ToolResult(tool_call_id=call.id, tool_name=call.name, success=True, output="ok")


def _pipeline(max_retries: int = 2, broker: Any = None) -> ToolPipeline:
    p = ToolPipeline()
    p.add(RetryMiddleware(max_retries=max_retries, base_delay=0.0, mcp_broker=broker))
    return p


def _ctx(tool: Any = None) -> Any:
    from bandi.tools.pipeline import ToolContext

    extra = {"tool": tool} if tool is not None else {}
    return ToolContext(extra=extra)


class _ToolStub:
    def __init__(
        self,
        name: str,
        *,
        destructive: bool = False,
        readonly: bool = False,
        retry_policy: RetryPolicy | None = None,
        retryable: bool | None = None,
    ) -> None:
        self.name = name
        self.is_destructive = destructive
        self.is_concurrency_safe = readonly
        self.is_read_only = readonly
        self.risk_level = "low"
        self.permissions_override = None
        self.retry_policy = retry_policy
        self.retryable = retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(NON_RETRYABLE_TOOL_NAMES))
async def test_non_idempotent_tools_never_retried(tool_name: str) -> None:
    """非幂等名单成员失败不得重试（防 git commit 重放/rm 重放等副作用放大）。"""
    run = _FlakyRun()
    pipeline = _pipeline(max_retries=2)
    ctx = _ctx(_ToolStub(tool_name))
    result = await pipeline.execute(ctx, _call(tool_name), run)
    assert result.success is False
    assert run.calls == 1, f"{tool_name} 失败被重试 {run.calls - 1} 次——副作用工具禁止重试"


@pytest.mark.asyncio
async def test_destructive_flag_blocks_retry() -> None:
    """is_destructive=True 即使不在名单也禁重试（元数据优先于名单）。"""
    run = _FlakyRun()
    pipeline = _pipeline(max_retries=2)
    ctx = _ctx(_ToolStub("custom_writer", destructive=True))
    await pipeline.execute(ctx, _call("custom_writer"), run)
    assert run.calls == 1


@pytest.mark.asyncio
async def test_readonly_tool_failure_still_retried() -> None:
    """回归保护：只读安全工具失败仍按 max_retries 重试（原重试能力不回退）。"""
    run = _FlakyRun(fail_n=2)
    pipeline = _pipeline(max_retries=2)
    ctx = _ctx(_ToolStub("web_search", readonly=True))
    result = await pipeline.execute(ctx, _call("web_search"), run)
    assert result.success is True
    assert run.calls == 3, "1 初次 + 2 重试"


@pytest.mark.asyncio
async def test_mcp_connection_error_single_layer_with_health_check() -> None:
    """MCP 连接错误由单一层处理——健康检查触发一次、总执行次数收敛为
    1+max_retries（不再与 MCP 层级联放大），且不再依赖第二套中间件。"""
    from bandi.agent.factory import build_tool_pipeline  # noqa: F401 — 装配面在 factory 内核验

    class _Broker:
        def __init__(self) -> None:
            self.health_checks = 0

        async def check_client(self, tool_name: str) -> None:
            self.health_checks += 1

    broker = _Broker()
    run = _FlakyRun(fail_n=1, error="MCP connection unavailable")
    pipeline = _pipeline(max_retries=2, broker=broker)
    ctx = _ctx(_ToolStub("mcp_grep", readonly=True))
    result = await pipeline.execute(ctx, _call("mcp_grep"), run)
    assert result.success is True
    assert run.calls == 2, f"单层语义总次数应为 1+1，实际 {run.calls}"
    assert broker.health_checks >= 1, "连接类错误必须触发健康检查"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(NON_RETRYABLE_TOOL_NAMES))
async def test_connection_keyword_does_not_bypass_explicit_non_retryable(tool_name: str) -> None:
    """高危防护：连接豁免（.5）不得绕过显式声明——声明 retryable=False
    的工具即使错误文本命中 connection/mcp/transport 关键词也只执行 1 次。
    （历史 fail-open：bash 声明禁重试，但错误含 'connection' 即被重放=副作用放大）"""
    from bandi.tools.base import RetryPolicy

    run = _FlakyRun(error="MCP connection unavailable transport reconnect")
    pipeline = _pipeline(max_retries=2)
    ctx = _ctx(_ToolStub(tool_name, retry_policy=RetryPolicy(retryable=False)))
    await pipeline.execute(ctx, _call(tool_name), run)
    assert run.calls == 1, f"{tool_name} 声明禁重试却被连接关键词豁免 {run.calls - 1} 次"


@pytest.mark.asyncio
async def test_connection_keyword_does_not_bypass_retryable_false() -> None:
    """同族 2：显式 retryable=False 的 MCP 工具同受保护——连接豁免只对未声明的
    只读/安全工具放行，不得覆盖声明反向后门。"""
    run = _FlakyRun(error="MCP connection lost, reconnecting")
    pipeline = _pipeline(max_retries=2)
    ctx = _ctx(_ToolStub("search_grep", retryable=False))
    await pipeline.execute(ctx, _call("search_grep"), run)
    assert run.calls == 1, "retryable=False 被连接关键词豁免重试"


@pytest.mark.asyncio
async def test_timeout_readonly_tool_still_retried() -> None:
    """超时语义：只读/未声明安全工具超时仍是传输层失败→可重试（保持恢复能力）。"""
    run = _FlakyRun(fail_n=1, error="operation timed out", meta={"error_class": "timeout"})
    pipeline = _pipeline(max_retries=2)
    ctx = _ctx(_ToolStub("web_fetch", readonly=True))
    result = await pipeline.execute(ctx, _call("web_fetch"), run)
    assert result.success is True
    assert run.calls == 2, "只读工具超时应重试恢复"


@pytest.mark.asyncio
async def test_timeout_undeclared_write_tool_not_retried() -> None:
    """超时语义：未声明的写/副作用工具超时禁重试——超时可能已半执行
    重放会放大副作用。此处刻意用「普通未声明工具」验证 timeout 分支本身生效
    （若用 destructive=True 会先被 destructive 分支拦截，契约将失效）。"""
    run = _FlakyRun(error="timed out after 30s", meta={"error_class": "timeout"})
    pipeline = _pipeline(max_retries=2)
    ctx = _ctx(_ToolStub("data_worker"))
    await pipeline.execute(ctx, _call("data_worker"), run)
    assert run.calls == 1, "普通未声明工具超时被重试——半执行重放风险"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(NON_RETRYABLE_TOOL_NAMES))
async def test_timeout_never_retries_declared_side_effect_tools(tool_name: str) -> None:
    """同族：declared 副作用工具（名单成员）超时一律 calls==1。"""
    from bandi.tools.base import RetryPolicy

    run = _FlakyRun(error="timed out", meta={"error_class": "timeout"})
    pipeline = _pipeline(max_retries=2)
    ctx = _ctx(_ToolStub(tool_name, retry_policy=RetryPolicy(retryable=False)))
    await pipeline.execute(ctx, _call(tool_name), run)
    assert run.calls == 1


@pytest.mark.asyncio
async def test_dict_error_failure_not_retried() -> None:
    """残余：dict-error（{"error":...}）是确定性业务失败——executor 置
    success=False 且标 non_retryable，重试层不得重放。"""
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.registry import ToolRegistry

    class _DictErrTool(BaseTool):
        name = "collab_probe"
        description = "probe"
        enabled = True
        tags = ["util"]

        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
            self.calls += 1
            return {"error": "业务校验失败"}

    registry = ToolRegistry()
    tool = _DictErrTool()
    registry.register_instance(tool)
    executor = ToolExecutor(
        registry,
        permissions_pipeline=None,
        tool_pipeline=_pipeline(max_retries=2),
    )
    results = await executor.execute([_call("collab_probe")])
    assert results[0].success is False
    assert results[0].meta.get("error_class") == "non_retryable"
    assert tool.calls == 1, "dict-error 确定性失败不得重试"


@pytest.mark.asyncio
async def test_non_retryable_error_type_not_retried() -> None:
    """
    不重试（类型判断优先于字符串关键词猜测）。"""
    from bandi.tools.errors import NonRetryableToolError
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.registry import ToolRegistry

    class _NonRetryTool(BaseTool):
        name = "strict_probe"
        description = "probe"
        enabled = True
        tags = ["util"]

        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, params: dict[str, Any], context: Any = None) -> str:
            self.calls += 1
            raise NonRetryableToolError("参数不支持")

    registry = ToolRegistry()
    tool = _NonRetryTool()
    registry.register_instance(tool)
    executor = ToolExecutor(registry, tool_pipeline=_pipeline(max_retries=2))
    results = await executor.execute([_call("strict_probe")])
    assert results[0].success is False and results[0].should_abort is False
    assert results[0].meta.get("error_class") == "non_retryable"
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_aborted_error_sets_should_abort() -> None:
    from bandi.tools.errors import ToolAbortedError
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.registry import ToolRegistry

    class _AbortTool(BaseTool):
        name = "abort_probe"
        description = "probe"
        enabled = True
        tags = ["util"]

        async def execute(self, params: dict[str, Any], context: Any = None) -> str:
            raise ToolAbortedError("用户中止了该操作")

    registry = ToolRegistry()
    registry.register_instance(_AbortTool())
    executor = ToolExecutor(registry, tool_pipeline=_pipeline(max_retries=2))
    results = await executor.execute([_call("abort_probe")])
    assert results[0].success is False
    assert results[0].should_abort is True
    assert results[0].meta.get("error_class") == "aborted"


@pytest.mark.asyncio
async def test_approval_deny_not_retried() -> None:
    """审批拒绝文案（approval.py 'Do not retry'）不得再重试询问/执行。
    工具名必须取名单外只读工具——否则名单判定先行，marker 路径永不被独立验证。"""
    run = _FlakyRun(error="user denied the operation. Do not retry this action")
    pipeline = _pipeline(max_retries=2)
    ctx = _ctx(_ToolStub("web_search", readonly=True))
    await pipeline.execute(ctx, _call("web_search"), run)
    assert run.calls == 1, "用户拒绝后重试 = 无视用户决定"


def test_non_retryable_names_are_real_registered_tools(tmp_path: Any) -> None:
    """
    名单写模块文件名而非注册名（如 file_edit/git_ops）= 死条目
    真实高危写工具（edit/git_commit）漏防 → commit/edit 重放风险。"""
    from bandi.agent.factory import build_tool_registry
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore

    store = OntologyStore(open_ontology(str(tmp_path / "o.db")))
    registered = {t.name for t in build_tool_registry(triple_ontology_store=store).list_tools()}
    missing = sorted(NON_RETRYABLE_TOOL_NAMES - registered)
    assert not missing, (
        f"名单死条目（registry 无此注册名，禁重试形同虚设）: {missing}；"
        f"registry 实有: {sorted(registered)}"
    )
