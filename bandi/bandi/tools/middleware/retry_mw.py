from __future__ import annotations

import asyncio
import logging
from typing import Any

from bandi.core.events import Event, EventType
from bandi.core.message import ToolCall, ToolResult
from bandi.tools.base import RetryPolicy
from bandi.tools.pipeline import MiddlewareResult, ToolContext, ToolMiddleware
from bandi.utils.backoff import compute_backoff_delay

logger = logging.getLogger(__name__)

# 成员必须是 registry 实际注册名（契约 test_non_retryable_names_are_real_registered_tools 钉住）
NON_RETRYABLE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "bash",
        "write",
        "edit",
        "apply_patch",
        "git_commit",
        "python_exec",
        "ontology_retract",
        "browser",
    }
)

# 审批拒绝/用户否决类文案（approval.py 'Do not retry' 语义）同样禁止重试
_DENIED_MARKERS = ("user denied", "do not retry", "denied by approval policy")


class RetryMiddleware(ToolMiddleware):
    def __init__(
        self,
        max_retries: int = 2,
        base_delay: float = 1.0,
        mcp_broker: Any | None = None,
        # 退避抖动 + 上限封顶
        max_delay: float = 30.0,
        jitter: bool = True,
    ) -> None:
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._jitter = jitter
        # 消除双层级联重试（最坏 1×(1+2) 放大）
        self._mcp_broker = mcp_broker

    async def before_execute(self, ctx: ToolContext, call: ToolCall) -> MiddlewareResult:
        return MiddlewareResult(allow=True)

    async def after_execute(
        self, ctx: ToolContext, call: ToolCall, result: ToolResult
    ) -> ToolResult:
        if result.success or ctx._retrying:
            return result

        is_mcp_connection = self._is_mcp_connection_error(result.error)
        policy = self._resolve_retry_policy(ctx, result, is_mcp_connection)
        if policy is None:
            return result

        ctx._retrying = True
        try:
            for attempt in range(policy.max_retries):
                # 公共退避函数（jitter + cap）——与 LLM 层 circuit_breaker 单一实现
                # 首次延迟 = backoff_seconds（旧公式 2**(attempt+1) 首跳 2× 的语义收敛）
                delay = compute_backoff_delay(
                    policy.backoff_seconds,
                    backoff_factor=2.0,
                    attempt=attempt,
                    max_delay=self._max_delay,
                    jitter=self._jitter,
                )
                await asyncio.sleep(delay)
                await self._emit_retry_progress(ctx, call, attempt + 1, policy.max_retries, delay)
                if is_mcp_connection and self._mcp_broker is not None:
                    await self._maybe_health_check(call)
                result = await ctx._pipeline_execute(ctx, call)
                if result.success:
                    break
        finally:
            ctx._retrying = False
        return result

    def _resolve_retry_policy(
        self, ctx: ToolContext, result: ToolResult, is_mcp_connection: bool
    ) -> RetryPolicy | None:
        """决策链（v2，修复）：运行级硬否决 → 显式 retry_policy 权威
        → MCP retryable 开关 → legacy 名单/destructive → timeout 传输级语义
        → MCP 连接豁免 → 错误关键词。None = 禁重试。

        修复点：连接豁免从「先于一切声明」降到工具声明之后——显式
        retryable=False/名单成员的错误文本命中 connection/mcp/transport 关键词时
        不再被豁免重放（fail-open 收敛）。timeout（error_class="timeout"）视为传输级
        失败但可能已半执行：只读/未声明安全工具放行重试，写/副作用工具禁重试。"""
        meta = getattr(result, "meta", None) or {}
        if meta.get("error_class") in ("non_retryable", "aborted"):
            return None
        if result.should_abort:
            return None
        error = result.error or ""
        lowered = error.lower()
        if any(marker in lowered for marker in _DENIED_MARKERS):
            return None

        tool = (ctx.extra or {}).get("tool")
        if tool is not None:
            policy = getattr(tool, "retry_policy", None)
            if policy is not None:
                # retryable=True 用本策略的 max_retries/backoff_seconds
                if not policy.retryable:
                    logger.warning("tool %s declares non-retryable; skip auto-retry", tool.name)
                    return None
                return policy
            mcp_retryable = getattr(tool, "retryable", None)
            if mcp_retryable is False:
                logger.warning("MCP tool %s retryable=False; skip auto-retry", tool.name or "?")
                return None
            if getattr(tool, "is_destructive", False):
                return None
            # 不得落入下方 fail-closed 兜底
            if mcp_retryable is True:
                return RetryPolicy(
                    retryable=True, max_retries=self._max_retries, backoff_seconds=self._base_delay
                )
            if getattr(tool, "name", "") in NON_RETRYABLE_TOOL_NAMES:
                logger.warning("non-idempotent tool %s failed; skip auto-retry", tool.name)
                return None

        # 其余（尤其写/副作用工具）禁重试，避免半执行重放放大
        if meta.get("error_class") == "timeout":
            explicitly_retryable = tool is not None and getattr(tool, "retryable", None) is True
            is_read_only = bool(getattr(tool, "is_read_only", False)) if tool else True
            if not (is_read_only or explicitly_retryable):
                return None

        # DNS/名称解析类瞬时故障：请求大概率未达对端（无副作用），放宽重试并放大
        # 退避（次数 +2、起步退避 ≥1.5s）——避免网络抖动把可恢复失败变成最终失败
        if self._is_dns_error(error):
            return RetryPolicy(
                retryable=True,
                max_retries=self._max_retries + 2,
                backoff_seconds=max(self._base_delay * 3.0, 1.5),
            )
        if is_mcp_connection:
            return RetryPolicy(
                retryable=True, max_retries=self._max_retries, backoff_seconds=self._base_delay
            )
        if self._is_non_retryable(error):
            return None
        safe_to_retry = bool(getattr(tool, "is_read_only", False)) or bool(
            getattr(tool, "is_idempotent", False)
        )
        if not safe_to_retry:
            logger.warning(
                "undeclared non-readonly tool %s failed; skip auto-retry (fail-closed)",
                getattr(tool, "name", "?"),
            )
            return None
        return RetryPolicy(
            retryable=True, max_retries=self._max_retries, backoff_seconds=self._base_delay
        )

    async def _emit_retry_progress(
        self, ctx: ToolContext, call: ToolCall, attempt: int, max_retries: int, delay: float
    ) -> None:
        """功能：重试进度事件（TOOL_PROGRESS · progress_type=retry）——前端重试条数据源；
        ctx 未桥接 event_bus 时静默跳过。"""
        bus = (ctx.extra or {}).get("event_bus")
        if bus is None:
            return
        await bus.emit(
            Event(
                type=EventType.TOOL_PROGRESS,
                data={
                    "session_id": (ctx.extra or {}).get("session_id", ""),
                    "tool_name": call.name,
                    "tool_call_id": call.id,
                    "progress_type": "retry",
                    "data": {
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "delay_seconds": round(delay, 2),
                    },
                },
            )
        )

    async def _maybe_health_check(self, call: ToolCall) -> None:
        checker = getattr(self._mcp_broker, "check_client", None) if self._mcp_broker else None
        if checker is None:
            return
        try:
            result = checker(call.name)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:  # noqa: BLE001 — 健康检查失败不阻断重试链
            logger.debug("mcp health check failed: %s", e)

    def _is_dns_error(self, error: str | None) -> bool:
        if not error:
            return False
        error_lower = error.lower()
        keywords = [
            "dns",
            "getaddrinfo",
            "name or service not known",
            "name resolution",
            "temporary failure",
        ]
        return any(kw in error_lower for kw in keywords)

    def _is_mcp_connection_error(self, error: str | None) -> bool:
        if not error:
            return False
        error_lower = error.lower()
        keywords = ["mcp", "connection", "unavailable", "transport", "reconnect"]
        return any(kw in error_lower for kw in keywords)

    def _is_non_retryable(self, error: str | None) -> bool:
        if not error:
            return False
        keywords = ["validation", "invalid", "not found", "permission denied"]
        return any(kw in error.lower() for kw in keywords)
