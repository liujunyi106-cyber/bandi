# 功能：员工执行过程广播（thread_progress 帧数据源）——StreamHandler 实现把
# run_agent_turn 的过程帧转成前端事件，经装配的公司 WS 广播回调实时推送
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class EmployeeProgressHandler:
    """把员工执行过程转发为 thread_progress 事件（实现 core StreamHandler Protocol）。

    事件结构与前端会话帧同构（event.type/data，复用前端归并逻辑）；
    归属字段 project_id/thread_id/role_id 逐帧携带（前端按项目/员工过滤）；
    广播为 best-effort——回调异常不中断员工执行。
    事件同时累积快照（`snapshot()`）供回写持久化——刷新后过程块可回看。
    """

    # 过程事件快照上限——超长执行只保留前 N 条（防线程消息/会话条目膨胀）
    _MAX_EVENTS = 300

    def __init__(
        self,
        callback: Callable[[dict[str, Any]], None],
        *,
        project_id: str = "",
        thread_id: str = "",
        role_id: str = "",
        artifact_store: Any | None = None,
        work_item_id: str = "",
    ) -> None:
        self._callback = callback
        self._project_id = project_id
        self._thread_id = thread_id
        self._role_id = role_id
        # 写类工具成功即登记产物（右栏「产物」数据源）——员工产物落其执行会
        # 话（role-session），项目聚合面（project_artifacts 前缀扫描）据此可见；
        # work_item_id 供回复下方交付物卡片精确关联。
        self._artifact_store = artifact_store
        self._work_item_id = work_item_id
        self._running_inputs: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []

    def snapshot(self) -> list[dict[str, Any]]:
        """功能：本次执行过程事件快照（回写持久化——刷新后过程块可回看）。"""
        return [dict(item) for item in self._events]

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        event = {"type": event_type, "data": data}
        if len(self._events) < self._MAX_EVENTS:
            self._events.append(event)
        frame = {
            "type": "thread_progress",
            "data": {
                "project_id": self._project_id,
                "thread_id": self._thread_id,
                "role_id": self._role_id,
                "work_item_id": self._work_item_id,
                "event": event,
            },
        }
        try:
            self._callback(frame)
        except Exception as exc:  # noqa: BLE001 — 广播失败不中断员工执行
            logger.debug("employee progress broadcast failed: %s", exc)

    async def on_started(self, session_id: str, work_item_id: str = "") -> None:
        """功能：执行开始即广播（早于首个 LLM token）——前端立即显示员工头像/正在执行。"""
        self._emit("started", {"work_item_id": work_item_id})

    async def on_content_delta(self, session_id: str, delta: str) -> None:
        self._emit("content_delta", {"text": delta})

    async def on_thinking(self, session_id: str, delta: str) -> None:
        self._emit("thinking_delta", {"text": delta})

    async def on_tool_call_start(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        tool_call_id: str = "",
    ) -> None:
        if tool_call_id:
            self._running_inputs[tool_call_id] = dict(tool_input or {})
        self._emit(
            "tool_call_start",
            {"tool_name": tool_name, "tool_input": tool_input or {}, "tool_call_id": tool_call_id},
        )

    async def on_tool_result(
        self,
        session_id: str,
        tool_name: str,
        success: bool,
        output_preview: str,
        duration_ms: int,
        tool_call_id: str = "",
        citations: list[dict[str, Any]] | None = None,
    ) -> None:
        self._emit(
            "tool_result",
            {
                "tool_name": tool_name,
                "success": success,
                "output_preview": output_preview,
                "duration_ms": duration_ms,
                "tool_call_id": tool_call_id,
                "citations": citations or [],
            },
        )
        if success and self._artifact_store is not None:
            from bandi.core.process_recorder import _WRITE_TOOLS, register_write_artifact

            if tool_name in _WRITE_TOOLS:
                tool_input = self._running_inputs.pop(tool_call_id, {}) if tool_call_id else {}
                register_write_artifact(
                    self._artifact_store,
                    session_id,
                    tool_name,
                    tool_input,
                    work_item_id=self._work_item_id,
                )

    async def on_tool_progress(
        self, session_id: str, tool_name: str, progress_type: str, data: dict[str, Any]
    ) -> None:
        self._emit(
            "tool_progress",
            {"tool_name": tool_name, "progress_type": progress_type, "data": data},
        )

    async def on_skill_injected(self, session_id: str, skills: list[str]) -> None:
        self._emit("skill_injected", {"skills": list(skills)})

    async def on_note(self, session_id: str, note_type: str) -> None:
        self._emit("note", {"note_type": note_type})

    async def on_compaction_start(self, session_id: str) -> None:
        self._emit("note", {"note_type": "compaction", "phase": "started"})

    async def on_compaction(self, session_id: str, detail: dict[str, Any]) -> None:
        self._emit(
            "note",
            {
                "note_type": "compaction",
                "phase": "completed",
                "compaction": dict(detail),
            },
        )

    async def on_memory_written(self, session_id: str, channels: list[str]) -> None:
        self._emit("memory_written", {"channels": list(channels), "count": len(channels)})

    async def on_task_ledger(self, session_id: str, items: list[dict[str, Any]]) -> None:
        self._emit("task_ledger", {"items": list(items), "count": len(items)})

    async def on_done(
        self,
        session_id: str,
        terminal_reason: str,
        total_tokens: int,
        turn_index: int,
        duration_ms: float = 0.0,
        steps: int = 0,
    ) -> None:
        self._emit(
            "done",
            {
                "terminal_reason": terminal_reason,
                "total_tokens": total_tokens,
                "turn_index": turn_index,
                "duration_ms": int(duration_ms or 0),
                "steps": int(steps or 0),
            },
        )

    async def on_error(self, session_id: str, error: str) -> None:
        self._emit("error", {"message": error})
