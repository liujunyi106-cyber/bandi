# 功能：轨迹解释（TraceExplainer 生成步骤说明）
from __future__ import annotations

import json
from dataclasses import dataclass

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.memory.session.store import SessionStore


@dataclass
class ExplanationStep:
    turn: int
    action: str
    detail: str
    timestamp: float = 0.0


class TraceExplainer:
    _MAX_DETAIL_CHARS = 200
    # B5c 8.8：每会话轨迹步数上界（超限丢最旧）——原 _steps 只增不减，长跑内存增长
    _MAX_STEPS_PER_SESSION = 500

    def __init__(self, event_bus: EventBus | None, session_store: SessionStore) -> None:
        # event_bus=None = 只读观测模式（如 explain 端点），仅用 list_checkpoints 读侧
        self._bus = event_bus
        self._store = session_store
        self._steps: dict[str, list[ExplanationStep]] = {}

    def _event_session(self, event: Event) -> str:
        # 功能：RF-4——会话键统一取 event.session_id（EventBus 装配时自动填充）
        # 再退 data 内字段；生产 VERDICT/COMPRESSION 事件 data 无 session_id
        return event.session_id or str(event.data.get("session_id") or "_default")

    def _append_step(self, sid: str, step: ExplanationStep) -> None:
        # 功能：B5c 8.8——统一入口 + 每会话上界（丢最旧保近期）
        bucket = self._steps.setdefault(sid, [])
        bucket.append(step)
        overflow = len(bucket) - self._MAX_STEPS_PER_SESSION
        if overflow > 0:
            del bucket[:overflow]

    async def start(self) -> None:
        if self._bus is None:
            return
        await self._bus.subscribe(EventType.LLM_RESPONSE, self._on_llm_response)
        await self._bus.subscribe(EventType.TOOL_INVOKED, self._on_tool_invoked)
        await self._bus.subscribe(EventType.TOOL_COMPLETED, self._on_tool_completed)
        # B5c 8.8：轨迹面扩展（状态迁移/验证裁决/压缩/审批——原仅 3 类）
        await self._bus.subscribe(EventType.STATE_CHANGED, self._on_state_changed)
        await self._bus.subscribe(EventType.VERDICT_ISSUED, self._on_verdict)
        await self._bus.subscribe(EventType.COMPRESSION_COMPLETED, self._on_compression)
        await self._bus.subscribe(EventType.APPROVAL_REQUESTED, self._on_approval)

    async def _on_state_changed(self, event: Event) -> None:
        sid = self._event_session(event)
        self._append_step(
            sid,
            ExplanationStep(
                turn=event.data.get("turn", 0),
                action="state_changed",
                detail=f"{event.data.get('from', '?')} → {event.data.get('to', '?')}",
                timestamp=event.timestamp,
            ),
        )

    async def _on_verdict(self, event: Event) -> None:
        sid = self._event_session(event)
        self._append_step(
            sid,
            ExplanationStep(
                turn=event.data.get("turn", 0),
                action="verdict_issued",
                detail=str(event.data.get("action", "?"))[: self._MAX_DETAIL_CHARS],
                timestamp=event.timestamp,
            ),
        )

    async def _on_compression(self, event: Event) -> None:
        sid = self._event_session(event)
        self._append_step(
            sid,
            ExplanationStep(
                turn=event.data.get("turn", 0),
                action="compressed",
                detail=f"stage={event.data.get('stage', '?')} "
                f"msgs {event.data.get('input_messages', '?')}→{event.data.get('output_messages', '?')}",
                timestamp=event.timestamp,
            ),
        )

    async def _on_approval(self, event: Event) -> None:
        sid = self._event_session(event)
        self._append_step(
            sid,
            ExplanationStep(
                turn=event.data.get("turn", 0),
                action="approval_requested",
                detail=str(event.data.get("tool_name", "?"))[: self._MAX_DETAIL_CHARS],
                timestamp=event.timestamp,
            ),
        )

    async def _on_llm_response(self, event: Event) -> None:
        # 事件 data 无 content（PII 收窄设计）——用 provider/model/usage 构建
        sid = self._event_session(event)
        turn = event.data.get("turn", 0)
        provider = event.data.get("provider", "?")
        model = event.data.get("model", "?")
        usage = event.data.get("usage", {}) or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        self._append_step(
            sid,
            ExplanationStep(
                turn=turn,
                action="llm_decided",
                detail=f"{provider}/{model} (in={prompt_tokens}, out={completion_tokens})",
                timestamp=event.timestamp,
            ),
        )

    async def _on_tool_invoked(self, event: Event) -> None:
        sid = self._event_session(event)
        turn = event.data.get("turn", 0)
        tool_name = event.data.get("tool_name", "?")
        tool_call_id = event.data.get("tool_call_id", "")
        self._append_step(
            sid,
            ExplanationStep(
                turn=turn,
                action="tool_called",
                detail=f"{tool_name} ({tool_call_id})",
                timestamp=event.timestamp,
            ),
        )

    async def _on_tool_completed(self, event: Event) -> None:
        sid = self._event_session(event)
        turn = event.data.get("turn", 0)
        tool_name = event.data.get("tool_name", "?")
        result = event.data.get("output_preview", "") or ""
        self._append_step(
            sid,
            ExplanationStep(
                turn=turn,
                action="result_observed",
                detail=f"{tool_name} → {str(result)[: self._MAX_DETAIL_CHARS]}",
                timestamp=event.timestamp,
            ),
        )

    def get_steps(
        self, session_id: str = "_default", at: str | None = None
    ) -> list[ExplanationStep]:
        return list(self._collect_steps(session_id, at))

    def explain(self, session_id: str = "_default", at: str | None = None) -> str:
        steps = self._collect_steps(session_id, at)
        if not steps:
            return "（无执行轨迹）"
        lines: list[str] = []
        for s in steps:
            label = {
                "llm_decided": "LLM 决策",
                "tool_called": "调用工具",
                "result_observed": "工具返回",
                "user_asked": "用户提问",
                "state_changed": "状态迁移",
                "verdict_issued": "验证裁决",
                "compressed": "上下文压缩",
                "approval_requested": "审批请求",
            }.get(s.action, s.action)
            lines.append(f"[轮次{s.turn}] {label}: {s.detail}")
        return "\n".join(lines)

    def _collect_steps(self, session_id: str, at: str | None) -> list[ExplanationStep]:
        # 事件内存优先（信息更全，含 result_observed）；空则从持久化重建（跨进程可用）
        memory = self._steps.get(session_id, [])
        if memory and at is None:
            return list(memory)
        return self.rebuild_steps(session_id, at)

    def rebuild_steps(self, session_id: str, at: str | None = None) -> list[ExplanationStep]:
        # 从 session_store 重建轨迹（用户提问→工具调用→LLM 决策 单实现，explain 端点共用）
        if self._store is None:
            return []
        try:
            entries = self._store.resolved_branch(session_id, at=at)
        except ValueError:
            return []
        steps: list[ExplanationStep] = []
        for e in entries:
            role = e.get("role", "")
            content = e.get("content", "")
            metadata = e.get("metadata", "")
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
            turn = e.get("turn_index", 0)
            if role == "user" and content:
                steps.append(
                    ExplanationStep(
                        turn=turn,
                        action="user_asked",
                        detail=str(content)[: self._MAX_DETAIL_CHARS],
                    )
                )
            tool_calls = metadata.get("tool_calls", []) if isinstance(metadata, dict) else []
            for tc in tool_calls:
                steps.append(
                    ExplanationStep(
                        turn=turn,
                        action="tool_called",
                        detail=f"{tc.get('name', '?')}({tc.get('arguments', {})})",
                    )
                )
            if role == "assistant" and content:
                steps.append(
                    ExplanationStep(
                        turn=turn,
                        action="llm_decided",
                        detail=str(content)[: self._MAX_DETAIL_CHARS],
                    )
                )
        return steps

    def list_checkpoints(self, session_id: str = "_default") -> list[dict]:
        """从 session_store 读取 checkpoint entries，生成时间旅行导航列表。"""
        if self._store is None:
            return []
        return self._store.list_checkpoint_entries(session_id)
