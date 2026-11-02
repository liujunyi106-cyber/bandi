# 功能：追踪器（BandiTracer 跨度采集 + 空实现）
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bandi.core.events import Event

OPENTELEMETRY_AVAILABLE = False
try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    OPENTELEMETRY_AVAILABLE = True
except ImportError:
    pass


class _NullSpan:
    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def end(self) -> None:
        pass

    def __enter__(self) -> _NullSpan:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class _NullTracer:
    @contextlib.contextmanager
    def start_as_current_span(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> Iterator[_NullSpan]:
        yield _NullSpan()


class BandiTracer:
    def __init__(
        self,
        service_name: str = "bandi",
        otlp_endpoint: str | None = None,
    ) -> None:
        self._service_name = service_name
        if OPENTELEMETRY_AVAILABLE:
            provider = TracerProvider()
            if otlp_endpoint:
                provider.add_span_processor(
                    SimpleSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
                )
            else:
                provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer(service_name)
        else:
            self._tracer = _NullTracer()

    @contextlib.contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
        with self._tracer.start_as_current_span(name, attributes=attributes) as span:
            yield span

    def start_as_current_span(self, name: str, attributes: dict[str, Any] | None = None) -> Any:
        # 功能：裸启动 span（OTelEventBusBridge 栈式配对用，非上下文管理器场景）
        return self._tracer.start_as_current_span(name, attributes=attributes)

    @property
    def current_trace_id(self) -> str:
        if not OPENTELEMETRY_AVAILABLE:
            return ""
        span = trace.get_current_span()
        ctx = span.get_span_context() if hasattr(span, "get_span_context") else None
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, "032x")
        return ""


_TRACER_SINGLETON: BandiTracer | None = None


def get_shared_tracer() -> BandiTracer:
    """功能：进程级共享 BandiTracer 单例——代码层 span 与事件 bridge
    共用同一 TracerProvider，事件 span 经 OTel 上下文自动嵌套在代码 span 下。"""
    global _TRACER_SINGLETON
    if _TRACER_SINGLETON is None:
        _TRACER_SINGLETON = BandiTracer()
    return _TRACER_SINGLETON


def generate_trace_id() -> str:
    tid = get_shared_tracer().current_trace_id
    if tid:
        return tid
    return __import__("uuid").uuid4().hex


class OTelEventBusBridge:
    _SPAN_MAP: dict[str, str] = {
        "LLM_REQUEST": "llm.request",
        "TOOL_INVOKED": "tool.invoke",
        "STATE_CHANGED": "agent.state_change",
    }

    _END_TO_START_KEY: dict[str, str] = {
        "LLM_RESPONSE": "LLM_REQUEST",
        "TOOL_COMPLETED": "TOOL_INVOKED",
        "TERMINATED": "STATE_CHANGED",
        "LLM_ERROR": "LLM_REQUEST",
        "TOOL_ERROR": "TOOL_INVOKED",
    }

    def __init__(self, tracer: BandiTracer | None = None) -> None:
        self._tracer = tracer or get_shared_tracer()
        self._stacks: dict[str, list[Any]] = {}
        # 防异常路径（无 RESPONSE/ERROR）span 永不 end）
        self._span_sessions: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def end_spans_for_session(self, session_id: str) -> int:
        # 功能：B5c 8.9——关闭指定会话所有残留 span（TERMINATED 兜底），返回关闭数
        closed = 0
        for active_key in list(self._stacks):
            if self._span_sessions.get(active_key) != session_id:
                continue
            stack = self._stacks.pop(active_key)
            self._span_sessions.pop(active_key, None)
            for cm in stack:
                try:
                    cm.__exit__(None, None, None)
                    closed += 1
                except Exception:  # noqa: BLE001 — 关闭失败不阻断
                    pass
        return closed

    @staticmethod
    def _correlation_key(event: Event) -> str:
        # 同 turn 多次 LLM 请求（截断恢复循环），tool_call_id 区分并行工具
        data = event.data
        sid = event.session_id or str(data.get("session_id") or "")
        turn = event.turn_id or str(data.get("turn", data.get("turn_index", "")))
        call = str(data.get("tool_call_id") or "")
        return f"{sid}:{turn}:{event.command_id}:{call}"

    async def handle_event(self, event: Event) -> None:
        key = event.type.name
        correlation = self._correlation_key(event)
        async with self._lock:
            if key == "TERMINATED":
                # 无 RESPONSE/ERROR 时 span 永不 end 的泄漏修复）
                sid = event.session_id or str(event.data.get("session_id") or "")
                if sid:
                    self.end_spans_for_session(sid)
            start_key = self._END_TO_START_KEY.get(key)
            if start_key is not None:
                active_key = f"{start_key}:{correlation}"
                stack = self._stacks.get(active_key)
                if stack:
                    cm = stack.pop()
                    if not stack:
                        self._stacks.pop(active_key, None)
                        self._span_sessions.pop(active_key, None)
                    cm.__exit__(None, None, None)
                return

            name = self._SPAN_MAP.get(key)
            if name is None:
                return
            cm = self._tracer.start_as_current_span(name)
            if cm is None:
                return
            span = cm.__enter__()
            for k, v in event.data.items():
                if isinstance(v, (str, int, float, bool)):
                    span.set_attribute(k, str(v))
            span.set_attribute("trace_id", event.trace_id or "")
            span.set_attribute("command_id", event.command_id or "")
            active_key = f"{key}:{correlation}"
            self._stacks.setdefault(active_key, []).append(cm)
            self._span_sessions[active_key] = event.session_id or str(
                event.data.get("session_id") or ""
            )
