# 功能：可观测性桥接（MetricsBridge 打通工具/代理指标）
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from bandi.core.events import EventType
from bandi.observability.metrics import MetricsCollector

_MS_PER_SEC = 1000


class MetricsBridge:
    """功能：EventBus 事件 → MetricsCollector 的观测接线桥（LLM/工具/回合/会话计数）"""

    def __init__(self, metrics: MetricsCollector | None = None) -> None:
        self._metrics = metrics or MetricsCollector()
        self._llm_start: dict[str, float] = {}
        self._tool_start: dict[str, float] = {}

    @property
    def metrics(self) -> MetricsCollector:
        return self._metrics

    @staticmethod
    def _llm_key(event: object) -> str:
        # 键=会话或 provider:model——并发请求计时互不串扰（工具侧同款字典模式）
        data = event.data if hasattr(event, "data") else {}
        return str(data.get("session_id") or f"{data.get('provider', '')}:{data.get('model', '')}")

    async def on_llm_request(self, event: object) -> None:
        self._llm_start[self._llm_key(event)] = time.monotonic()

    async def on_llm_response(self, event: object) -> None:
        data = event.data if hasattr(event, "data") else {}
        provider = data.get("provider", "unknown")
        model = data.get("model", "unknown")
        usage = data.get("usage", {}) or {}
        duration = self._llm_duration(event)
        self._metrics.record_llm_call(
            provider=provider,
            model=model,
            status="success",
            prompt_tokens=usage.get("prompt_tokens", 0) or 0,
            completion_tokens=usage.get("completion_tokens", 0) or 0,
            duration=duration,
        )

    async def on_llm_error(self, event: object) -> None:
        data = event.data if hasattr(event, "data") else {}
        provider = data.get("provider", "unknown")
        model = data.get("model", "unknown")
        self._metrics.record_llm_call(
            provider=provider,
            model=model,
            status="error",
            prompt_tokens=0,
            completion_tokens=0,
            duration=self._llm_duration(event),
        )

    async def on_tool_invoked(self, event: object) -> None:
        data = event.data if hasattr(event, "data") else {}
        tool = data.get("tool_name", "unknown")
        # 工具 duration 串扰（先完成者覆盖后完成者的计时）
        call_id = data.get("tool_call_id", "") or tool
        self._tool_start[call_id] = time.monotonic()

    async def on_tool_completed(self, event: object) -> None:
        data = event.data if hasattr(event, "data") else {}
        tool = data.get("tool_name", "unknown")
        call_id = data.get("tool_call_id", "") or tool
        duration_ms = data.get("duration_ms")
        start = self._tool_start.pop(call_id, None)  # HB-P2（批 3）：弹出防无界累积
        if duration_ms is None:
            duration_ms = (
                time.monotonic() - (start if start is not None else time.monotonic())
            ) * _MS_PER_SEC
        self._metrics.record_tool_call(tool, duration_ms / _MS_PER_SEC, success=True)

    async def on_tool_error(self, event: object) -> None:
        data = event.data if hasattr(event, "data") else {}
        tool = data.get("tool_name", "unknown")
        call_id = data.get("tool_call_id", "") or tool
        duration_ms = data.get("duration_ms")
        start = self._tool_start.pop(call_id, None)  # HB-P2（批 3）：弹出防无界累积
        if duration_ms is None:
            duration_ms = (
                time.monotonic() - (start if start is not None else time.monotonic())
            ) * _MS_PER_SEC
        self._metrics.record_tool_call(tool, duration_ms / _MS_PER_SEC, success=False)

    async def on_terminated(self, event: object) -> None:
        data = event.data if hasattr(event, "data") else {}
        self._metrics.record_agent_turn(data.get("terminal_reason", "unknown"))

    async def on_domain_event(self, event: object) -> None:
        # 功能：B5b 8.7——领域事件统一桥接（VERDICT/REWORK/安全/PII/降级/审批/
        # 压缩/记忆写入/状态迁移 → metrics 通用域计数）
        etype = getattr(getattr(event, "type", None), "name", "unknown")
        data = event.data if hasattr(event, "data") else {}
        outcome = str(
            data.get("action")
            or data.get("stage")
            or data.get("decision")
            or data.get("terminal_reason")
            or ""
        )
        self._metrics.record_domain_event(etype, outcome)

    def _llm_duration(self, event: object) -> float:
        start = self._llm_start.pop(self._llm_key(event), None)
        if start is None:
            return 0.0
        return time.monotonic() - start

    async def wire(self, bus: Any) -> None:
        for event_type, handler in [
            (EventType.LLM_REQUEST, self.on_llm_request),
            (EventType.LLM_RESPONSE, self.on_llm_response),
            (EventType.LLM_ERROR, self.on_llm_error),
            (EventType.TOOL_INVOKED, self.on_tool_invoked),
            (EventType.TOOL_COMPLETED, self.on_tool_completed),
            (EventType.TOOL_ERROR, self.on_tool_error),
            (EventType.TERMINATED, self.on_terminated),
            # B5b 8.7：覆盖扩展（领域事件观测面）
            (EventType.VERDICT_ISSUED, self.on_domain_event),
            (EventType.REWORK_REQUESTED, self.on_domain_event),
            (EventType.SAFETY_GUARD_TRIGGERED, self.on_domain_event),
            (EventType.PII_DETECTED, self.on_domain_event),
            (EventType.DEGRADATION, self.on_domain_event),
            (EventType.STATE_CHANGED, self.on_domain_event),
            (EventType.APPROVAL_REQUESTED, self.on_domain_event),
            (EventType.APPROVAL_GRANTED, self.on_domain_event),
            (EventType.APPROVAL_DENIED, self.on_domain_event),
            (EventType.COMPRESSION_COMPLETED, self.on_domain_event),
            (EventType.MEMORY_WRITTEN, self.on_domain_event),
        ]:
            await bus.subscribe(event_type, handler)


class AuditChainBridge:
    """功能：B5b 8.6b——关键事件（工具错误/安全拦截/审批拒绝）落防篡改审计
    哈希链（此前 `record_event` 唯一调用点在 CLI，生产关键事件不入链）。"""

    _AUDITED: tuple[EventType, ...] = (
        EventType.TOOL_ERROR,
        EventType.SAFETY_GUARD_TRIGGERED,
        EventType.APPROVAL_DENIED,
    )

    def __init__(self, session_store: Any, chain_path: Path | None = None) -> None:
        from bandi.observability.audit_reporter import AuditReporter

        self._reporter = AuditReporter(session_store, chain_path=chain_path)

    @property
    def reporter(self) -> Any:
        return self._reporter

    async def on_audited_event(self, event: object) -> None:
        data = event.data if hasattr(event, "data") else {}
        etype = getattr(getattr(event, "type", None), "name", "unknown")
        try:
            self._reporter.record_event({"event_type": etype, "data": dict(data)})
        except Exception:  # noqa: BLE001 — 审计落链失败不阻断主链
            pass

    async def wire(self, bus: Any) -> None:
        for event_type in self._AUDITED:
            await bus.subscribe(event_type, self.on_audited_event)
