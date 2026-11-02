# 功能：指标收集（MetricsCollector；prometheus_client 缺失时降级 NullCollector）
from __future__ import annotations

from typing import Any

PROMETHEUS_AVAILABLE = False
try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest

    PROMETHEUS_AVAILABLE = True
except ImportError:
    pass


class _NullCollector:
    def labels(self, **kwargs: str) -> _NullCollector:
        return self

    def inc(self, amount: float = 1) -> None:
        pass

    def observe(self, amount: float) -> None:
        pass

    def set(self, value: float) -> None:
        pass


class MetricsCollector:
    _instance: MetricsCollector | None = None

    def __new__(cls) -> MetricsCollector:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._domain_event_counts: dict[str, int] = {}
        if PROMETHEUS_AVAILABLE:
            self._domain_events = Counter(
                "bandi_domain_events_total",
                "Domain events by kind/outcome",
                ["kind", "outcome"],
            )
            self._agent_turns = Counter(
                "bandi_agent_turns_total",
                "Total agent turns",
                ["terminal_reason"],
            )
            self._llm_calls = Counter(
                "bandi_llm_calls_total",
                "LLM API calls",
                ["provider", "model", "status"],
            )
            self._tool_calls = Counter(
                "bandi_tool_calls_total",
                "Tool executions",
                ["tool", "status"],
            )
            self._token_usage = Counter(
                "bandi_token_usage_total",
                "Token usage",
                ["type"],
            )
            self._tool_latency = Histogram(
                "bandi_tool_latency_seconds",
                "Tool execution latency",
                ["tool"],
                buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30],
            )
            self._llm_latency = Histogram(
                "bandi_llm_latency_seconds",
                "LLM call latency",
                ["provider"],
                buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60],
            )
            self._active_sessions = Gauge(
                "bandi_active_sessions",
                "Currently active sessions",
            )
        else:
            self._domain_events = _NullCollector()
            self._agent_turns = _NullCollector()
            self._llm_calls = _NullCollector()
            self._tool_calls = _NullCollector()
            self._token_usage = _NullCollector()
            self._tool_latency = _NullCollector()
            self._llm_latency = _NullCollector()
            self._active_sessions = _NullCollector()

    def record_agent_turn(self, terminal_reason: str) -> None:
        self._agent_turns.labels(terminal_reason=terminal_reason).inc()

    def record_llm_call(
        self,
        provider: str,
        model: str,
        status: str,
        prompt_tokens: int,
        completion_tokens: int,
        duration: float,
    ) -> None:
        self._llm_calls.labels(provider=provider, model=model, status=status).inc()
        if prompt_tokens > 0:
            self._token_usage.labels(type="prompt").inc(prompt_tokens)
        if completion_tokens > 0:
            self._token_usage.labels(type="completion").inc(completion_tokens)
        self._llm_latency.labels(provider=provider).observe(duration)

    def record_tool_call(self, tool: str, duration: float, success: bool) -> None:
        status = "success" if success else "failure"
        self._tool_calls.labels(tool=tool, status=status).inc()
        self._tool_latency.labels(tool=tool).observe(duration)

    def record_active_sessions(self, count: int) -> None:
        self._active_sessions.set(count)

    def record_domain_event(self, kind: str, outcome: str = "") -> None:
        # 功能：B5b 8.7——领域事件通用计数（VERDICT/REWORK/安全拦截/PII/降级/
        # 审批/压缩/记忆写入等桥接覆盖；prometheus 可选，dict 恒维护供断言）
        key = f"{kind}:{outcome}" if outcome else kind
        self._domain_event_counts[key] = self._domain_event_counts.get(key, 0) + 1
        if PROMETHEUS_AVAILABLE:
            self._domain_events.labels(kind=kind, outcome=outcome).inc()

    @property
    def domain_event_counts(self) -> dict[str, int]:
        return dict(self._domain_event_counts)

    def expose_metrics(self, app: Any) -> None:
        if not PROMETHEUS_AVAILABLE:
            return
        from fastapi import Response

        @app.get("/metrics")
        async def metrics() -> Response:
            return Response(content=generate_latest(), media_type="text/plain")
