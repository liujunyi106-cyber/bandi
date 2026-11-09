# 功能：工具中间件/权限管道行为契约（多视角审计补强 ）
# 契约原则：权限管道 7 层顺序不变式 + 中间件 fail-closed 不变式——
# 破坏者视角覆盖「绕过面」（缺 pipeline/无确认通道/非目标错误不得误重试）。
from __future__ import annotations

import asyncio

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.pipeline import ToolContext


def _call(name: str = "read", arguments: dict | None = None) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments or {})


async def _ok_result(ctx: ToolContext, call: ToolCall) -> ToolResult:
    return ToolResult(tool_call_id=call.id, tool_name=call.name, success=True, output="ok")


async def _fail_result(ctx: ToolContext, call: ToolCall, error: str = "boom") -> ToolResult:
    return ToolResult(
        tool_call_id=call.id, tool_name=call.name, success=False, output="", error=error
    )


# ---------------------------------------------------------------------------
# 1) PermissionsPipeline 全链路 7 层顺序不变式
# ---------------------------------------------------------------------------


def test_contract_permissions_mode_ask_treated_as_execute() -> None:
    """契约 1（双模式化反转）：mode=ask 不再是合法模式——权限层不得因 ask
    触发特殊拒绝，行为与 execute 一致。"""
    from bandi.core.message import ToolCall
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    pipeline = PermissionsPipeline()
    for name in ("read", "web_search", "grep"):
        tc = ToolCall(id="t", name=name, arguments={})
        v_ask = asyncio.run(pipeline.check(tc, None, mode="ask"))
        v_execute = asyncio.run(pipeline.check(tc, None, mode="execute"))
        assert v_ask.allow == v_execute.allow, f"ask 不得触发 {name} 的模式层拒绝"


def test_contract_permissions_mode_plan_whitelist() -> None:
    """契约 2：mode=plan 只允许白名单工具——bash/write 等写盘工具必须拒绝；
    read/glob/grep 等只读放行；todo_write（计划产物台账）放行。

    行为变更：计划模式的核心产物即结构化计划台账——
    todo_write 并入 PLAN_MODE_TOOLS；此前被一并拒绝致「计划只能写在正文」。
    写盘类（write/file_write/bash/python_exec）仍硬拒（破坏者不得放松）。"""
    from bandi.core.message import ToolCall
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    pipeline = PermissionsPipeline()
    for allowed in (
        "read",
        "web_search",
        "web_fetch",
        "ontology_query",
        "todo_read",
        "todo_write",
        "glob",
        "grep",
    ):
        tc = ToolCall(id="t", name=allowed, arguments={})
        verdict = asyncio.run(pipeline.check(tc, None, mode="plan"))
        assert verdict.allow is True, f"plan 模式 {allowed} 必须放行"

    for denied in ("bash", "write", "file_write", "python_exec"):
        tc = ToolCall(id="t", name=denied, arguments={})
        verdict = asyncio.run(pipeline.check(tc, None, mode="plan"))
        assert verdict.allow is False, f"plan 模式 {denied} 必须拒绝"


def test_contract_permissions_deny_list_first() -> None:
    """契约 3：deny 名单优先于一切——deny 的工具即使 bypass 名单也拒绝
    （破坏者视角：工具同时在 deny 与 bypass 时不得被 bypass 放行）。"""
    from bandi.core.message import ToolCall
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    pipeline = PermissionsPipeline(
        deny_tools={"read"},
        bypass_tools={"read"},  # 同一工具双登记
    )
    tc = ToolCall(id="t", name="read", arguments={})
    verdict = asyncio.run(pipeline.check(tc, None, mode="execute"))
    assert verdict.allow is False, "deny 名单必须优先于 bypass 名单"


def test_contract_permissions_no_ui_denies_ask_tool() -> None:
    """契约 4：ask 类工具（bash/write）无确认通道时必须拒绝（fail-closed）——
    headless 环境（无 user_interaction）请求确认 = 拒绝，绝不静默放行。"""
    from bandi.core.message import ToolCall
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    pipeline = PermissionsPipeline(user_interaction=None)
    for name in ("bash", "write"):
        tc = ToolCall(id="t", name=name, arguments={})
        verdict = asyncio.run(pipeline.check(tc, None, mode="execute"))
        assert verdict.allow is False, f"{name} 无确认通道必须拒绝（fail-closed）"
        assert verdict.reason, f"{name} 拒绝必须有原因"


def test_contract_permissions_sensitive_path_case_insensitive_full() -> None:
    """契约 5（族级）：敏感路径拦截必须大小写不敏感 + 全参数名族覆盖——
    .ssh/.env/.git 大写变体 + path/cwd/file_path/target/dest 任一参数名携带
    敏感路径都必须拒绝（破坏者视角：换参数名或换大小写不得绕过）。"""
    from bandi.core.message import ToolCall
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    pipeline = PermissionsPipeline()
    arg_names = ("path", "cwd", "file_path", "target", "dest", "output_dir", "src")
    sensitive = ("C:/Users/x/.SSH/id_rsa", "C:/Users/x/.ENV", "C:/Users/x/.GIT/config")
    for arg in arg_names:
        for spath in sensitive:
            tc = ToolCall(id="t", name="write", arguments={arg: spath})
            verdict = asyncio.run(pipeline.check(tc, None, mode="execute"))
            assert verdict.allow is False, (
                f"参数 {arg} 携带敏感路径 {spath} 必须拒绝（大小写不敏感）"
            )


def test_contract_permissions_shell_dangerous_pattern_denied() -> None:
    """契约 6：bash 命令含危险管道/注入模式（| sh / ` / $( ）必须拒绝——
    即使命令前缀不在 deny_commands 名单（纵深防御）。"""
    from bandi.core.message import ToolCall
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    pipeline = PermissionsPipeline()
    for cmd in ("echo hi | sh", "cat f | bash", "echo `ls`", "x=$(rm -rf /)"):
        tc = ToolCall(id="t", name="bash", arguments={"command": cmd})
        verdict = asyncio.run(pipeline.check(tc, None, mode="execute"))
        assert verdict.allow is False, f"危险 shell 模式必须拒绝: {cmd!r}"


# ---------------------------------------------------------------------------
# 2) 中间件 fail-closed 不变式（族级 parametrize）
# ---------------------------------------------------------------------------


def _rate_limit_tool_pipeline(max_calls: int) -> tuple:
    from bandi.tools.middleware.ratelimit_mw import RateLimitMiddleware, SlidingWindowRateLimiter
    from bandi.tools.pipeline import ToolPipeline

    pipeline = ToolPipeline()
    pipeline.add(RateLimitMiddleware(SlidingWindowRateLimiter(max_calls, 60)))
    return pipeline


async def _run_seq(pipeline, call: ToolCall, n: int) -> list[ToolResult]:
    results = []
    for _ in range(n):
        results.append(await pipeline.execute(ToolContext(), call, _ok_result))
    return results


def test_contract_ratelimit_fails_closed_after_window() -> None:
    """契约 7：限流中间件在窗口内超限必须拒绝（fail-closed）——
    第 N+1 次调用不得静默放行；拒绝必须含 retry 语义原因。"""
    pipeline = _rate_limit_tool_pipeline(2)
    call = _call("read")

    async def _run() -> None:
        results = await _run_seq(pipeline, call, 4)
        blocked = [r for r in results if not r.success]
        assert len(blocked) == 2, f"第 3-4 次必须被限流拒绝: {[r.error for r in blocked]}"
        assert "Rate limited" in (blocked[0].error or ""), (
            f"拒绝原因必须含 Rate limited: {blocked[0].error!r}"
        )

    asyncio.run(_run())


def test_contract_ratelimit_key_scoped_per_user() -> None:
    """契约 8：限流键必须按 user_id+tool 隔离——用户 A 超限不影响用户 B
    （破坏者视角：不同用户共享限流键会误伤/被拖累）。"""
    from bandi.tools.middleware.ratelimit_mw import RateLimitMiddleware, SlidingWindowRateLimiter
    from bandi.tools.pipeline import ToolPipeline

    pipeline = ToolPipeline()
    pipeline.add(RateLimitMiddleware(SlidingWindowRateLimiter(2, 60)))

    async def _run() -> None:
        call = _call("read")
        # 用户 A 打满窗口
        for _ in range(2):
            await pipeline.execute(ToolContext(user_id="a"), call, _ok_result)
        third = await pipeline.execute(ToolContext(user_id="a"), call, _ok_result)
        assert not third.success, "用户 A 第 3 次必须被限流"
        # 用户 B 不受影响
        first_b = await pipeline.execute(ToolContext(user_id="b"), call, _ok_result)
        assert first_b.success, "用户 B 首次必须放行（限流键按 user 隔离）"

    asyncio.run(_run())


def test_contract_retry_does_not_retry_non_retryable() -> None:
    """契约 9：RetryMiddleware 对非重试错误（validation/invalid/permission denied）
    不得重试——重试非瞬时错误浪费调用且放大副作用（破坏者视角）。"""
    from bandi.tools.middleware.retry_mw import RetryMiddleware
    from bandi.tools.pipeline import ToolPipeline

    pipeline = ToolPipeline()
    pipeline.add(RetryMiddleware(max_retries=3, base_delay=0.01))
    calls = {"n": 0}

    async def _validation_fail(ctx: ToolContext, call: ToolCall) -> ToolResult:
        calls["n"] += 1
        return await _fail_result(ctx, call, "validation error: bad input")

    async def _run() -> None:
        result = await pipeline.execute(ToolContext(), _call("validate"), _validation_fail)
        assert not result.success, "非重试错误必须失败"
        assert calls["n"] == 1, f"非重试错误不得重试，实际调用 {calls['n']} 次"

    asyncio.run(_run())


def test_contract_retry_retries_transient_then_recovers() -> None:
    """契约 10：RetryMiddleware 对瞬时失败必须重试并在成功后返回成功——
    重试上限内不得放弃（前 2 次失败第 3 次成功）。"""
    from bandi.tools.middleware.retry_mw import RetryMiddleware
    from bandi.tools.pipeline import ToolPipeline

    pipeline = ToolPipeline()
    pipeline.add(RetryMiddleware(max_retries=3, base_delay=0.01))
    calls = {"n": 0}

    async def _flaky(ctx: ToolContext, call: ToolCall) -> ToolResult:
        calls["n"] += 1
        if calls["n"] < 3:
            return await _fail_result(ctx, call, "transient")
        return await _ok_result(ctx, call)

    async def _run() -> None:
        class _ReadOnlyStub:
            name = "flaky"
            is_read_only = True
            is_idempotent = False

        result = await pipeline.execute(
            ToolContext(extra={"tool": _ReadOnlyStub()}), _call("flaky"), _flaky
        )
        assert result.success, f"瞬时失败重试后必须成功: {result.error}"
        assert calls["n"] >= 3, f"必须重试至成功，实际调用 {calls['n']} 次"

    asyncio.run(_run())


def test_contract_mcp_retry_only_for_connection_errors() -> None:
    """合并后契约 11：RetryMiddleware 只对连接类错误重试——
    非 MCP 连接错误（业务失败）不得重试；重试期间不得死循环。"""
    from bandi.tools.middleware.retry_mw import RetryMiddleware
    from bandi.tools.pipeline import ToolPipeline

    class _Broker:
        pass

    pipeline = ToolPipeline()
    pipeline.add(RetryMiddleware(max_retries=2, base_delay=0.0, mcp_broker=_Broker()))
    calls = {"n": 0}

    async def _busy_fail(ctx: ToolContext, call: ToolCall) -> ToolResult:
        calls["n"] += 1
        return await _fail_result(ctx, call, "invalid response format")

    async def _run() -> None:
        result = await pipeline.execute(ToolContext(), _call("mcp_tool"), _busy_fail)
        assert not result.success, "业务错误必须失败"
        assert calls["n"] == 1, f"非连接错误不得重试，实际调用 {calls['n']} 次"

    asyncio.run(_run())


def test_contract_mcp_retry_bounded_on_connection_error() -> None:
    """合并后契约 12：MCP 连接错误重试必须有界——max_retries=2 时最多 3 次调用
    （1 原始 + 2 重试），不得无限重试。"""
    from bandi.tools.middleware.retry_mw import RetryMiddleware
    from bandi.tools.pipeline import ToolPipeline

    class _Broker:
        pass

    pipeline = ToolPipeline()
    pipeline.add(RetryMiddleware(max_retries=2, base_delay=0.0, mcp_broker=_Broker()))
    calls = {"n": 0}

    async def _conn_fail(ctx: ToolContext, call: ToolCall) -> ToolResult:
        calls["n"] += 1
        return await _fail_result(ctx, call, "mcp connection lost")

    async def _run() -> None:
        result = await pipeline.execute(ToolContext(), _call("mcp_tool"), _conn_fail)
        assert not result.success, "连接持续失败必须最终失败"
        assert calls["n"] == 3, f"重试必须有界（1+2），实际调用 {calls['n']} 次"

    asyncio.run(_run())


def test_contract_permission_middleware_no_pipeline_allows() -> None:
    """契约 13：PermissionMiddleware 无 pipeline（非公司链路）必须放行——
    但这是「无守卫配置」语义，有 pipeline 时必须拦截（见契约 14）。"""
    from bandi.tools.middleware.permission_mw import PermissionMiddleware

    mw = PermissionMiddleware(pipeline=None)

    async def _run() -> None:
        result = await mw.before_execute(ToolContext(), _call("read"))
        assert result.allow, "无 pipeline 时必须放行（未配置守卫不拦截）"

    asyncio.run(_run())


def test_contract_permission_middleware_denies_via_pipeline() -> None:
    """契约 14：PermissionMiddleware 有 pipeline 时必须经 pipeline 拦截——
    拒绝 verdict 必须传递为 allow=False + 原 reason（生产路径接线）。"""
    from bandi.core.message import ToolCall
    from bandi.tools.middleware.permission_mw import PermissionMiddleware
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    pipeline = PermissionsPipeline(deny_tools={"read"})
    mw = PermissionMiddleware(pipeline=pipeline)

    async def _run() -> None:
        ctx = ToolContext(mode="execute")
        call = ToolCall(id="c1", name="read", arguments={})
        result = await mw.before_execute(ctx, call)
        assert not result.allow, "pipeline 拒绝必须传递"
        assert result.reason and "denied" in result.reason.lower(), (
            f"拒绝原因必须含 denied: {result.reason}"
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 3) _acquisition_guard_violation 生产路径契约（data acquisition 守卫）
# ---------------------------------------------------------------------------


def _acq_task(projection: str = "data_acquisition", role: str = "acquisition_specialist") -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        id="wi-1",
        title="采集视频素材",
        description="下载宣传片到工作区",
        assignee_role=role,
        metadata={
            "execution_mode": "company_mode",
            # canonical 投影 key（work_item_identity 契约）——
            # 生产 WorkItem 由 mark_work_item_projection 写入该 key
            "work_item_projection_id": projection,
            "work_item_role_id": role,
            "workspace_manifest": {
                "root_path": r"C:\acq_workspace",
                "reserved_paths": {"work": r"C:\acq_workspace\work"},
            },
        },
    )


def test_contract_acquisition_guard_non_projection_allows() -> None:
    """契约 15：非 data_acquisition 投影的 bash 命令必须放行——
    普通项目 bash（非采集）不受采集越界守卫拦截（守卫只限采集投影）。"""
    from bandi.tools.middleware.ownership_mw import _acquisition_guard_violation

    call = ToolCall(
        id="c1",
        name="bash",
        arguments={"command": "curl -o C:/tmp/out.bin https://example.com/f.bin", "cwd": "."},
    )
    work_item = _acq_task(projection="normal_project", role="worker")
    assert _acquisition_guard_violation(call, work_item) is None, (
        "非采集投影必须放行（守卫只限 data_acquisition 投影）"
    )


def test_contract_acquisition_guard_non_shell_tool_allows() -> None:
    """契约 16：非 bash/python_exec 工具必须放行——read/write/其他工具
    不受采集守卫检查（守卫只检查 bash/python_exec 两个 shell 工具）。"""
    from bandi.tools.middleware.ownership_mw import _acquisition_guard_violation

    for tool_name in ("read", "write", "file_write", "grep"):
        call = ToolCall(id="c1", name=tool_name, arguments={"command": "curl ..."})
        assert _acquisition_guard_violation(call, _acq_task()) is None, (
            f"非 shell 工具 {tool_name} 必须放行"
        )


def test_contract_acquisition_guard_escapes_workspace_denied() -> None:
    """契约 17（破坏者视角）：采集投影 bash 命令把下载写到工作区外必须拦截——
    curl -o C:/outside/evil.bin（目标不在 workspace_manifest.root_path 内）必须
    返回越界原因；拦截原因必须含 acquisition/workspace 语义。"""
    from bandi.tools.middleware.ownership_mw import _acquisition_guard_violation

    call = ToolCall(
        id="c1",
        name="bash",
        arguments={
            "command": "curl -o C:/outside/evil.bin https://example.com/evil.bin",
            "cwd": ".",
        },
    )
    violation = _acquisition_guard_violation(call, _acq_task())
    assert violation is not None, "越界下载必须拦截"
    assert "workspace" in violation.lower(), f"拦截原因必须含 workspace: {violation}"


def test_contract_acquisition_guard_in_workspace_allows() -> None:
    """契约 18：采集投影 bash 下载到工作区内必须放行——
    curl -o C:/acq_workspace/work/out.bin 在 reserved work 目录内不拦截。"""
    from bandi.tools.middleware.ownership_mw import _acquisition_guard_violation

    call = ToolCall(
        id="c1",
        name="bash",
        arguments={
            "command": "curl -o C:/acq_workspace/work/out.bin https://example.com/out.bin",
            "cwd": ".",
        },
    )
    assert _acquisition_guard_violation(call, _acq_task()) is None, "工作区内下载必须放行"


def test_contract_acquisition_guard_non_http_url_denied() -> None:
    """契约 19：采集命令含非 http(s) URL（如 file:// 本地路径穿越）必须拦截——
    采集只允许 http(s) 下载源，file:/// 或其他协议必须拒绝（防本地文件读取越界）。"""
    from bandi.tools.middleware.ownership_mw import _acquisition_guard_violation

    call = ToolCall(
        id="c1",
        name="bash",
        arguments={
            "command": "curl -o C:/acq_workspace/work/evil.bin file:///etc/passwd",
            "cwd": ".",
        },
    )
    violation = _acquisition_guard_violation(call, _acq_task())
    assert violation is not None, "file:// 非 http 源必须拦截"
