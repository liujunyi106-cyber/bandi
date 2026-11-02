from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType

_MAX_ITEMS_DEFAULT = 24
_ACTIVE_FORM_KEYS = ("active_form", "activeForm")


@dataclass
class TaskLedgerConfig:  # 功能：任务台账配置（启停/上限/双持久化开关/事件开关）
    enabled: bool = True
    max_items: int = _MAX_ITEMS_DEFAULT
    persist_to_session: bool = True
    persist_to_work_item: bool = True
    emit_runtime_events: bool = True


def normalize_todo_status(value: Any) -> str:
    status = str(value or "pending").strip().lower()
    if status in {"done", "completed", "complete", "finished"}:
        return "completed"
    if status in {"in_progress", "in-progress", "active", "current"}:
        return "in_progress"
    return "pending"


def normalize_todos(todos_payload: Any) -> list[dict[str, Any]]:
    if not isinstance(todos_payload, list):
        return []
    normalized: list[dict[str, Any]] = []
    has_active = False
    for index, raw in enumerate(todos_payload, start=1):
        if not isinstance(raw, dict):
            continue
        status = normalize_todo_status(raw.get("status"))
        if status == "in_progress":
            if has_active:
                status = "pending"
            else:
                has_active = True
        title = (
            str(raw.get("title") or "").strip()
            or str(raw.get("content") or "").strip()
            or str(raw.get("active_form") or raw.get("activeForm") or "").strip()
            or f"todo-{index}"
        )
        active_form = (
            str(raw.get(_ACTIVE_FORM_KEYS[0]) or raw.get(_ACTIVE_FORM_KEYS[1]) or title).strip()
            or title
        )
        content = str(raw.get("content") or title).strip() or title
        normalized.append(
            {
                "id": str(raw.get("id") or index),
                "title": title,
                "content": content,
                "active_form": active_form,
                "status": status,
            }
        )
    return normalized


def render_todos(todo_state: list[dict[str, Any]]) -> str:
    if not todo_state:
        return "(no TODO items)"
    status_icons = {
        "pending": "[ ]",
        "in_progress": "[>]",
        "completed": "[x]",
        "done": "[x]",
    }
    return "\n".join(
        f"{status_icons.get(str(item.get('status', 'pending')), '[ ]')} "
        f"{str(item.get('title') or item.get('content') or item.get('id', 'todo'))}"
        for item in todo_state
    )


class TaskLedger:  # 功能：任务台账——todo_write 更新内存态，persist 双写 session/work_item 并发射事件
    def __init__(
        self,
        config: TaskLedgerConfig | None = None,
        session_id: str | None = None,
        session_store: Any | None = None,
        work_item_provider: Callable[[], Any] | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._config = config or TaskLedgerConfig()
        self._session_id = session_id
        self._session_store = session_store
        self._work_item_provider = work_item_provider
        self._event_bus = event_bus
        self.items: list[dict[str, Any]] = []

    def restore(self) -> None:
        if not self._config.enabled:
            return
        ledger = self._load_session_ledger()
        if ledger is None:
            ledger = self._load_work_item_ledger()
        self.items = normalize_todos(ledger) if isinstance(ledger, list) else []

    def apply_todos(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                return {"error": f"Invalid todo JSON: {exc}", "success": False}
        self.items = normalize_todos(payload)[: self._max_items()]
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "success": True,
            "result": {
                "todos": list(self.items),
                "rendered": render_todos(self.items),
                "task_ledger": list(self.items),
            },
        }

    async def persist(self) -> None:
        if not self._config.enabled:
            return
        normalized = list(self.items[: self._max_items()])
        if self._config.persist_to_session and self._session_store is not None and self._session_id:
            await asyncio.to_thread(self._persist_session, normalized)
        if self._config.persist_to_work_item and self._work_item_provider is not None:
            self._persist_work_item(normalized)
        if self._config.emit_runtime_events and self._event_bus is not None:
            await self._event_bus.emit(
                Event(
                    type=EventType.TASK_LEDGER_UPDATED,
                    # _HandlerBindings._owns 会 fail-open 广播到本 bus 全部订阅者）
                    data={
                        "task_ledger": list(normalized),
                        "item_count": len(normalized),
                        "session_id": str(self._session_id or ""),
                    },
                )
            )

    def _max_items(self) -> int:
        return max(1, int(self._config.max_items or _MAX_ITEMS_DEFAULT))

    def _load_session_ledger(self) -> list[dict[str, Any]] | None:
        if (
            not self._config.persist_to_session
            or self._session_store is None
            or not self._session_id
        ):
            return None
        session = self._session_store.get_session(self._session_id)
        if session is None:
            return None
        meta = _safe_meta(session.get("metadata"))
        return meta.get("task_ledger")

    def _load_work_item_ledger(self) -> list[dict[str, Any]] | None:
        if not self._config.persist_to_work_item or self._work_item_provider is None:
            return None
        work_item = self._work_item_provider()
        if work_item is None:
            return None
        runtime_meta = getattr(work_item, "runtime_metadata", None) or {}
        return runtime_meta.get("task_ledger")

    def _persist_session(self, normalized: list[dict[str, Any]]) -> None:
        session = self._session_store.get_session(self._session_id)
        meta = _safe_meta(session.get("metadata")) if session else {}
        meta["task_ledger"] = list(normalized)
        self._session_store.update_session(
            self._session_id, metadata=json.dumps(meta, ensure_ascii=False)
        )

    def _persist_work_item(self, normalized: list[dict[str, Any]]) -> None:
        work_item = self._work_item_provider()
        if work_item is None:
            return
        runtime_meta = dict(getattr(work_item, "runtime_metadata", None) or {})
        runtime_meta["task_ledger"] = list(normalized)
        work_item.runtime_metadata = runtime_meta


def _safe_meta(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw)
        return dict(parsed) if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
