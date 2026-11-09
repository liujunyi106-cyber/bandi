# 功能：Agent 单轮执行入口（事件订阅/会话恢复/检查点恢复）
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bandi.memory.memory_manager import MemoryHierarchyManager
    from bandi.memory.memory_provider import MemoryManager

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.checkpoint import CheckpointStore
from bandi.agent.factory import (
    build_agent_context,
    build_mcp_broker,
    build_provider,
    build_tool_registry,
)
from bandi.agent.loop import AgentLoop
from bandi.agent.prompt_harness.builder import prompt_harness_from_config
from bandi.context.planner import ContextPlanner
from bandi.context.source import ContextSourceRegistry
from bandi.core.abort_controller import AbortController
from bandi.core.config import BandiConfig, default_cost_db_path
from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import Message, MessageRole
from bandi.core.stream_handler import StreamHandler
from bandi.core.user_interaction import UserInteraction
from bandi.eval.fairness_evaluator import FairnessEvaluator
from bandi.knowledge.citations import strip_generated_citation_block
from bandi.memory.session.store import SessionStore
from bandi.observability.metrics import MetricsCollector
from bandi.observability.write_failures import get_registry
from bandi.ontology.lookup import OntologyEntityLookup
from bandi.perception.pipeline import Dependencies as PerceptionDeps
from bandi.perception.pipeline import PerceptionPipeline
from bandi.skills.manager import SkillInjector, create_skill_registry

logger = logging.getLogger(__name__)

_active_contexts: dict[str, Any] = {}
_active_lock = threading.Lock()
_pending_injection_subs: dict[str, list] = {}


# 功能：本体写入工具集——本体写入轮的内容归本体库，不再自动直写长期记忆
_ONTOLOGY_WRITE_TOOLS = frozenset({"ontology_define", "ontology_assert", "ontology_retract"})


def _ontology_write_turn(ctx: Any) -> bool:
    # 功能：本用户轮是否含本体写入工具调用（写本体=只写本体，防双写）。
    # 从消息尾部反向扫描至最近的 USER 消息（用户轮边界）——工具结果进 messages
    # 后 ctx.tool_results 会被下一迭代清空，messages 是轮末唯一存续载体；
    # 上一用户轮的本体调用不误拦本轮（遇 USER 即停）。
    from bandi.core.message import MessageRole

    for msg in reversed(list(getattr(ctx, "messages", None) or [])):
        role = getattr(msg, "role", None)
        if role == MessageRole.USER:
            return False
        if (
            role == MessageRole.TOOL
            and str(getattr(msg, "name", "") or "") in _ONTOLOGY_WRITE_TOOLS
        ):
            return True
    return False


def _long_term_source_text(
    raw_input: str | None,
    final_message: str | None,
    explicit_remember: bool,
) -> str | None:
    """功能：选出本轮长期记忆内容源（只取用户输入），None 表示本轮不写。

    薄壳：实现见 `memory.decide.long_term_source_text`（判定只有一份）。保留此名是为了
    既有契约的导入路径稳定。`explicit_remember` 由调用方预先算好传入，避免重复判定。
    """
    from bandi.memory.decide import long_term_source_text

    text = (raw_input or "").strip()
    if not text:
        return None
    if explicit_remember:
        return text
    return long_term_source_text(text, final_message or "", explicit_remember=False) or None


def _assistant_persist_content(
    final_message: str | None, current_content: str | None
) -> str | None:
    """功能：选出本轮应落库的 assistant 正文（返回 None 表示不落库）。

    两个独立决策——「落什么」与「不落什么」：
    - 正常终态（final_message 为空或与模型输出一致）→ 落模型真实输出；
    - 异常终态（final_message 是系统生成的终止文案，与模型输出不同）→ 落
      current_content，系统文案本身不进历史（否则后续轮模型会模仿复读）；
    - 模型无任何输出时返回 None——不落空消息。

    异常终态仍要落 current_content：它已随content_delta 送达用户，丢掉等于
    用户说过的话在刷新后消失。
    """
    final_text = final_message or ""
    current_text = current_content or ""
    if final_text.strip() and final_text.strip() != current_text.strip():
        return current_text if current_text.strip() else None
    return final_text or current_text or None


def _assistant_already_persisted(
    store: Any, session_id: str, turn_index: int, content: str
) -> bool:
    """功能：判定本轮 assistant 回复是否已被 checkpoint 屏障补写进 store。

    根因：checkpoint 屏障的语义是「写 checkpoint 前把 ctx.messages
    里尚未落库的消息补写」（loop._flush_messages_before_checkpoint），而
    `checkpoint_interval=1` ⇒ 每轮都触发 ⇒ 轮末 `run_agent_turn` 再写一次就产生
    同 (turn_index, content) 的第二条。后果：会话历史膨胀、完成态耗时按轮聚合失真
    （表现为 UI 显示 0.0s）。

    口径与 loop 侧完全一致（turn_index + content），读失败一律视为「未落过」——
    宁可多写一条（可容忍），也不误跳过真实回复（丢历史不可逆）。
    """
    try:
        entries = store.get_branch(session_id, full=True)
    except Exception:  # noqa: BLE001 — 读失败不得阻断主链
        return False
    for entry in entries:
        if (
            entry.get("entry_type") == "message"
            and entry.get("role") == "assistant"
            and int(entry.get("turn_index") or 0) == int(turn_index)
            and (entry.get("content") or "") == content
        ):
            return True
    return False


async def _emit_terminal_reply_frame(
    handler: StreamHandler | None,
    session_id: str,
    message: str | None,
    streamed: str | None,
    reply: str,
) -> None:
    """功能：异常终态补帧（-3 修复）——TOOL_ERROR/超时等终止的 final_message 为系统
    生成、不经流式 content_delta 发出，此前用户当轮看不到任何终止说明（无最终回复）。
    此处补发一条 content_delta 让终止原因当轮可见；落库语义保持现状（系统文案不落库）。
    仅当 final_message 非空且与已流式内容不同（未发过）时补帧，帧失败不阻断收尾。"""
    if handler is None or not reply:
        return
    if not message or message == streamed:
        return
    try:
        await handler.on_content_delta(session_id, reply)
    except Exception:
        logger.warning("terminal reply frame emit failed", exc_info=True)


def register_pending_injection_sub(session_id: str, callback: Any) -> None:
    """SSE 流启动时预挂 USER_INPUT_INJECTED 回调（ctx 尚未注册）。"""
    with _active_lock:
        _pending_injection_subs.setdefault(session_id, []).append(callback)


async def flush_pending_injection_subs(session_id: str, bus: Any) -> None:
    """ctx 注册后把 pending 回调补挂到真实 bus（幂等：无 pending 即空操作）。"""
    from bandi.core.events import EventType

    with _active_lock:
        callbacks = _pending_injection_subs.pop(session_id, [])
    for cb in callbacks:
        try:
            await bus.subscribe(EventType.USER_INPUT_INJECTED, cb)
        except Exception:
            logger.exception("failed to attach pending injection sub for %s", session_id)


def discard_pending_injection_subs(session_id: str) -> None:
    """SSE 流异常终止时丢弃未消费的 pending 订阅（防泄漏）。"""
    with _active_lock:
        _pending_injection_subs.pop(session_id, None)


def _register_active_context(session_id: str, ctx: Any) -> None:
    with _active_lock:
        _active_contexts[session_id] = ctx
        count = len(_active_contexts)
    MetricsCollector().record_active_sessions(count)


def _unregister_active_context(session_id: str) -> None:
    with _active_lock:
        _active_contexts.pop(session_id, None)
        count = len(_active_contexts)
    MetricsCollector().record_active_sessions(count)


# non-blocking 认领：同 session 已有活跃 turn 时第二个调用方快速失败
# 防双击发送/双入口并发跑同一会话（消息历史交叉写入、checkpoint 互踩）。
_session_busy: set[str] = set()
_SESSION_LEASE_SECONDS = 300
_INSTANCE_ID = __import__("uuid").uuid4().hex


def _session_lease_meta(store: Any, session_id: str) -> dict | None:
    session = store.get_session(session_id)
    if session is None:
        return None
    raw = session.get("metadata") or {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        return dict(json.loads(str(raw)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _acquire_session_slot(
    session_id: str,
    store: Any | None = None,
    instance_id: str | None = None,
) -> bool:
    """
    调用方立即返回 SESSION_BUSY）。运行中指令变更走 注入（inject_user_input）。
    跨进程互斥——store 存在时以租约认领（有效租约且非本实例 → 拒绝）
    过期/无租约可认领；进程内 _session_busy 先行快速失败。"""
    inst = instance_id or _INSTANCE_ID
    with _active_lock:
        if session_id in _session_busy:
            return False
        if store is not None:
            try:
                metadata = _session_lease_meta(store, session_id)
                # 修改前快照作 CAS expected（他进程抢先认领时库值已变 → 失败）
                expected = dict(metadata or {})
                claim = (metadata or {}).get("execution_claim") or {}
                expires_at = claim.get("lease_expires_at")
                if expires_at:
                    from datetime import datetime

                    try:
                        expires = datetime.fromisoformat(str(expires_at))
                        now = datetime.now(expires.tzinfo) if expires.tzinfo else datetime.now()
                        if expires > now and claim.get("instance_id") != inst:
                            # 单内核部署：异实例租约 = 上一进程崩溃/重启的残留。
                            # 不接管则崩溃后 300s 内的重试全部 SESSION_BUSY
                            # ；本实例接管并继续写 claim。
                            logger.warning(
                                "stale lease from other instance on session %s — taking over",
                                session_id,
                            )
                    except (ValueError, TypeError):
                        pass
                new_claim = {
                    "instance_id": inst,
                    "claimed_at": __import__("datetime")
                    .datetime.now(__import__("datetime").UTC)
                    .isoformat(),
                    "lease_expires_at": (
                        __import__("datetime").datetime.now(__import__("datetime").UTC)
                        + __import__("datetime").timedelta(seconds=_SESSION_LEASE_SECONDS)
                    ).isoformat(),
                }
                metadata["execution_claim"] = new_claim
                # （库值已变）时 CAS 失败 → 认领失败（消除读-写竞态窗口）
                if not store.update_session_if_metadata(session_id, metadata, expected):
                    logger.warning("session lease CAS failed for %s (concurrent claim)", session_id)
                    return False
            except Exception:
                logger.warning("session lease acquire failed for %s", session_id, exc_info=True)
        _session_busy.add(session_id)
        return True


def _release_session_slot(
    session_id: str,
    store: Any | None = None,
    instance_id: str | None = None,
) -> None:
    """释放会话执行槽位（幂等：未认领/已释放均为无操作）。
    仅清除本实例租约（他实例租约不动，防误释放）。"""
    inst = instance_id or _INSTANCE_ID
    with _active_lock:
        _session_busy.discard(session_id)
    if store is not None:
        try:
            metadata = _session_lease_meta(store, session_id)
            claim = (metadata or {}).get("execution_claim") or {}
            if claim.get("instance_id") == inst:
                metadata.pop("execution_claim", None)
                store.update_session(session_id, metadata=json.dumps(metadata, ensure_ascii=False))
        except sqlite3.ProgrammingError:
            pass
        except Exception:
            logger.warning("session lease release failed for %s", session_id, exc_info=True)


def session_slot_busy(session_id: str, store: Any | None = None) -> bool:
    """会话是否正在执行——本进程活跃 ctx/槽位，或未过期的跨进程租约。

    revert/危险会话操作的前置互斥判定（原实现无任何检查，运行中截断历史会丢消息）。"""
    with _active_lock:
        if session_id in _session_busy or session_id in _active_contexts:
            return True
    if store is not None:
        try:
            metadata = _session_lease_meta(store, session_id) or {}
        except Exception:  # noqa: BLE001 — 存储异常按「不忙」处理（互斥检查不阻断业务）
            return False
        claim = metadata.get("execution_claim") or {}
        expires_at = claim.get("lease_expires_at")
        if expires_at:
            from datetime import datetime

            try:
                expires = datetime.fromisoformat(str(expires_at))
                now = datetime.now(expires.tzinfo) if expires.tzinfo else datetime.now()
                if expires > now:
                    return True
            except (TypeError, ValueError):
                return True
    return False


def _renew_session_lease(
    session_id: str,
    store: Any,
    instance_id: str | None = None,
) -> bool:
    """会话租约续期——仅持有者（instance 匹配且未过期）可续
    过期时间前移；非 owner/无租约/已过期返回 False（不得劫持他实例租约）。

    原实现只认领一次无续期：长 turn 超过租约后他进程可抢占
    导致同会话双跑（消息历史交叉写入、checkpoint 互踩）。"""
    inst = instance_id or _INSTANCE_ID
    from datetime import UTC, datetime, timedelta

    try:
        metadata = _session_lease_meta(store, session_id)
    except Exception:
        logger.warning("session lease renew read failed for %s", session_id, exc_info=True)
        return False
    if not metadata:
        return False
    expected = dict(metadata)
    claim = dict(metadata.get("execution_claim") or {})
    if claim.get("instance_id") != inst:
        return False
    expires_at = claim.get("lease_expires_at")
    if expires_at:
        try:
            expires = datetime.fromisoformat(str(expires_at))
            now = datetime.now(expires.tzinfo) if expires.tzinfo else datetime.now()
            if expires <= now:
                # 已过期——他进程可能已认领，续期失败（CAS 语义：不覆盖他人）
                return False
        except (TypeError, ValueError):
            pass  # 损坏时间戳按可续处理（与 acquire 的「损坏视为无租约」一致）
    claim["lease_expires_at"] = (
        datetime.now(UTC) + timedelta(seconds=_SESSION_LEASE_SECONDS)
    ).isoformat()
    metadata["execution_claim"] = claim
    try:
        return bool(store.update_session_if_metadata(session_id, metadata, expected))
    except Exception:
        logger.warning("session lease renew CAS failed for %s", session_id, exc_info=True)
        return False


async def _session_lease_keeper(session_id: str, store: Any) -> None:
    """长任务租约保活——按 1/3 周期续期；续期失败（租约已释放/
    被接管/存储异常）即退出，不打扰执行链。"""
    interval = max(30.0, _SESSION_LEASE_SECONDS / 3)
    try:
        while True:
            await asyncio.sleep(interval)
            if not _renew_session_lease(session_id, store):
                return
    except asyncio.CancelledError:
        raise


async def _stop_lease_keeper(task: Any) -> None:
    """停止保活任务（幂等——None/已完成均为空操作）。"""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except BaseException:  # noqa: BLE001 — CancelledError 属 BaseException
        pass


def inject_user_input(
    session_id: str, text: str
) -> bool:  # 功能：A3 实时转向——向活跃会话注入新指令
    with _active_lock:
        ctx = _active_contexts.get(session_id)
    if ctx is None:
        return False
    # 防转向文本「注入死 ctx 后静默丢失」
    if (
        getattr(ctx, "terminal_reason", None) is not None
        or str(getattr(ctx, "current_state", "")) == "terminated"
    ):
        return False
    ctx.inject_input(text)
    return True


def _safe_filename(session_id: str) -> str:
    """将 session_id 清洗为合法文件名（七-完整 ID 哈希防截断碰撞——
    公司 scoped id 前缀相同会写同一 JSONL；原始 session_id 由事件行 data 承载）。"""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16] or "session"


@dataclass
class AgentTurnResult:
    reply: str
    terminal_reason: str | None
    total_tokens: int
    turn_index: int
    session_id: str
    # 审批锚点：本轮因等待审批终止时，出问题的那一次工具调用（空 = 无审批）
    approval_tool_call_id: str = ""
    approval_tool_name: str = ""


def _subagent_kind_payload(event: Event) -> tuple[str, dict[str, Any]]:
    # 功能：子代理事件 → 嵌套过程帧 kind/payload（未知类型返回空 kind）
    d = event.data if isinstance(event.data, dict) else {}
    et = event.type
    if et == EventType.LLM_STREAM_EVENT:
        ev_type = str(d.get("type", ""))
        if ev_type == "thinking_delta":
            return "thinking", {"text": d.get("content", "")}
        if ev_type == "content_delta":
            return "content", {"text": d.get("content", "")}
        return "", {}
    if et == EventType.TOOL_INVOKED:
        return "tool_start", {
            "tool_name": d.get("tool_name", "?"),
            "tool_input": d.get("tool_input") or {},
            "tool_call_id": d.get("tool_call_id", ""),
        }
    if et == EventType.TOOL_COMPLETED:
        return "tool_result", {
            "tool_name": d.get("tool_name", "?"),
            "success": bool(d.get("success", True)),
            "output_preview": d.get("output_preview", ""),
            "duration_ms": d.get("duration_ms", 0),
            "tool_call_id": d.get("tool_call_id", ""),
            "citations": d.get("citations") or [],
        }
    if et == EventType.TOOL_ERROR:
        return "tool_result", {
            "tool_name": d.get("tool_name", "?"),
            "success": False,
            "output_preview": d.get("error_message", "Unknown error"),
            "duration_ms": d.get("duration_ms", 0),
            "tool_call_id": d.get("tool_call_id", ""),
        }
    if et == EventType.TOOL_PROGRESS:
        return "tool_progress", {
            "tool_name": d.get("tool_name", "?"),
            "progress_type": d.get("progress_type", ""),
            "data": d.get("data", {}),
        }
    if et == EventType.SKILL_INJECTED:
        return "skill", {"skills": [str(s) for s in (d.get("skills") or [])]}
    if et == EventType.COMPRESSION_COMPLETED:
        return "note", {
            "note_type": "compaction",
            "compaction": {
                "stage": d.get("stage", ""),
                "input_chars": d.get("input_chars", 0),
                "output_chars": d.get("output_chars", 0),
                "summary": d.get("summary", ""),
                "maintenance": bool(d.get("maintenance", False)),
            },
        }
    if et == EventType.MEMORY_WRITTEN:
        return "note", {
            "note_type": "memory_written",
            "channels": [str(c) for c in (d.get("channels") or [])],
        }
    return "", {}


class _HandlerBindings:
    """存储稳定的闭包引用，解决 bound method 身份比较失败的问题。"""

    def __init__(self, handler: StreamHandler, session_id: str) -> None:
        self.handler = handler
        self.session_id = session_id
        self.bindings: dict[EventType, Callable] = {}

    def _subagent_label(self, event: Event) -> dict[str, Any] | None:
        # 功能：子代理事件标签（带本会话归属校验；非子代理事件返回 None）
        if not isinstance(event.data, dict):
            return None
        label = event.data.get("subagent")
        if not isinstance(label, dict):
            return None
        if str(label.get("parent_session_id") or "") != self.session_id:
            return None
        return label

    async def _forward_subagent(self, event: Event) -> bool:
        # 功能：子代理过程归属转发（True=已被子代理通道消费，不得再落父流）
        label = self._subagent_label(event)
        if label is None:
            return False
        forward = getattr(self.handler, "on_subagent_progress", None)
        if forward is None:
            return True
        kind, payload = _subagent_kind_payload(event)
        if kind:
            await forward(label, kind, payload)
        return True

    def _owns(self, event: Event) -> bool:
        # 功能：会话归属过滤——子代理与父会话共享 bus，事件必须按会话隔离
        # （事件 session_id 由 EventBus 来源字段填充；data 形态兜底）
        eid = str(getattr(event, "session_id", "") or "")
        if not eid and event.data:
            eid = str(event.data.get("session_id", "") or "")
        return not eid or eid == self.session_id

    async def _on_stream_event(self, event: Event) -> None:
        if await self._forward_subagent(event):
            return
        if not self._owns(event):
            return
        if event.data is None:
            return
        ev_type = event.data.get("type", "")
        if ev_type == "content_delta":
            await self.handler.on_content_delta(
                self.session_id,
                event.data.get("content", ""),
            )
        elif ev_type == "thinking_delta":
            await self.handler.on_thinking(
                self.session_id,
                event.data.get("content", ""),
            )

    async def _on_tool_invoked(self, event: Event) -> None:
        if await self._forward_subagent(event):
            return
        if not self._owns(event):
            return
        if event.data is None:
            return
        d = event.data
        await self.handler.on_tool_call_start(
            self.session_id,
            d.get("tool_name", "?"),
            d.get("tool_input") or {},
            d.get("tool_call_id", ""),
        )

    async def _on_tool_completed(self, event: Event) -> None:
        if await self._forward_subagent(event):
            return
        if not self._owns(event):
            return
        if event.data is None:
            return
        d = event.data
        await self.handler.on_tool_result(
            self.session_id,
            d.get("tool_name", "?"),
            d.get("success", True),
            d.get("output_preview", ""),
            d.get("duration_ms", 0),
            d.get("tool_call_id", ""),
            citations=d.get("citations") or [],
        )

    async def _on_tool_error(self, event: Event) -> None:
        if await self._forward_subagent(event):
            return
        if not self._owns(event):
            return
        if event.data is None:
            return
        d = event.data
        await self.handler.on_tool_result(
            self.session_id,
            d.get("tool_name", "?"),
            False,
            d.get("error_message", "Unknown error"),
            d.get("duration_ms", 0),
            d.get("tool_call_id", ""),
        )

    async def _on_tool_progress(self, event: Event) -> None:
        if await self._forward_subagent(event):
            return
        if not self._owns(event):
            return
        if event.data is None:
            return
        d = event.data
        await self.handler.on_tool_progress(
            self.session_id,
            d.get("tool_name", "?"),
            d.get("progress_type", ""),
            d.get("data", {}),
        )

    async def _on_skill_injected(self, event: Event) -> None:
        if await self._forward_subagent(event):
            return
        if not self._owns(event):
            return
        if event.data is None:
            return
        await self.handler.on_skill_injected(
            self.session_id,
            list(event.data.get("skills", [])),
        )

    async def _on_compression_started(self, event: Event) -> None:
        # 功能：压缩开始 → 过程块状态行（仅真实摘要压缩；维护 micro 不显示）
        if await self._forward_subagent(event):
            return
        if not self._owns(event):
            return
        on_start = getattr(self.handler, "on_compaction_start", None)
        if callable(on_start):
            await on_start(self.session_id)
        else:
            await self.handler.on_note(self.session_id, "compaction")

    async def _on_compression(self, event: Event) -> None:
        # 功能：压缩完成 → 过程消息注记（维护档免费 micro 不占用户过程块）
        if await self._forward_subagent(event):
            return
        if not self._owns(event):
            return
        data = event.data or {}
        stage = str(data.get("stage") or "")
        if data.get("maintenance"):
            return
        detail = {
            "stage": stage,
            "input_chars": int(data.get("input_chars") or 0),
            "output_chars": int(data.get("output_chars") or 0),
            "summary": str(data.get("summary") or ""),
            "phase": "completed",
        }
        on_compaction = getattr(self.handler, "on_compaction", None)
        if callable(on_compaction):
            await on_compaction(self.session_id, detail)
        else:
            await self.handler.on_note(self.session_id, "compaction")

    async def _on_memory_written(self, event: Event) -> None:
        # 功能：记忆写入通道明细 → 过程块「记入记忆 xN」行
        if await self._forward_subagent(event):
            return
        if not self._owns(event):
            return
        if event.data is None:
            return
        # 过程块只展示用户可感知的长期/项目沉淀；working/episodic 是内部通道，
        # 寒暄等短回合只写 working 时不应出现「记入记忆」。
        channels = [
            str(c)
            for c in (event.data.get("channels") or [])
            if str(c) in {"long_term", "project_md"}
        ]
        if not channels:
            return
        await self.handler.on_memory_written(self.session_id, channels)

    async def _on_task_ledger(self, event: Event) -> None:
        # 功能：任务台账更新 → 前端会话任务条
        if await self._forward_subagent(event):
            return
        if not self._owns(event):
            return
        if event.data is None:
            return
        raw = event.data.get("task_ledger") or []
        items = [dict(it) for it in raw if isinstance(it, dict)]
        await self.handler.on_task_ledger(self.session_id, items)

    def bind(self) -> dict[EventType, Callable]:
        self.bindings = {
            EventType.LLM_STREAM_EVENT: self._on_stream_event,
            EventType.TOOL_INVOKED: self._on_tool_invoked,
            EventType.TOOL_COMPLETED: self._on_tool_completed,
            EventType.TOOL_ERROR: self._on_tool_error,
            EventType.TOOL_PROGRESS: self._on_tool_progress,
            EventType.SKILL_INJECTED: self._on_skill_injected,
            EventType.COMPRESSION_STARTED: self._on_compression_started,
            EventType.COMPRESSION_COMPLETED: self._on_compression,
            EventType.MEMORY_WRITTEN: self._on_memory_written,
            EventType.TASK_LEDGER_UPDATED: self._on_task_ledger,
        }
        return self.bindings


async def _subscribe_handler(
    bus: EventBus, handler: StreamHandler, session_id: str
) -> _HandlerBindings:
    bindings = _HandlerBindings(handler, session_id)
    for et, fn in bindings.bind().items():
        await bus.subscribe(et, fn)
    return bindings


async def _unsubscribe_handler(bus: EventBus, bindings: _HandlerBindings | None) -> None:
    if bindings is None:
        return
    for et, fn in bindings.bindings.items():
        await bus.unsubscribe(et, fn)


def _session_restore(
    session_id: str,
    store: SessionStore,
    ctx: ExecutionContext,
    checkpoint_store: CheckpointStore | None = None,
    max_history_messages: int = 0,
) -> None:
    # get_branch 默认已按最新 boundary 投影（summary + 物理 preserved
    # segment + 之后新增）。原始历史仍在 boundary 之前，get_full_branch 可审计。
    entries = store.get_branch(session_id)
    restored_messages: list[Message] = []
    boundary: dict[str, Any] | None = None
    has_segment_messages = False
    for entry in entries:
        etype = entry.get("entry_type")
        if etype in ("summary", "compact_boundary"):
            boundary = entry
        elif etype == "message":
            has_segment_messages = True
    snapshot = _boundary_active_messages(boundary)
    post_context = _boundary_post_context(boundary)
    if snapshot is not None and not has_segment_messages:
        # 旧数据兜底：boundary 还没有物化 preserved segment 时用快照回灌。
        # 快照只含非 SYSTEM 消息；摘要由 boundary 自身补回（SYSTEM 身份 prompt
        # 每轮重新组装，不落库）。
        from bandi.core.message import message_from_dict

        summary_content = str(boundary.get("content") or "") if boundary is not None else ""
        if summary_content:
            restored_messages.append(Message(role=MessageRole.SYSTEM, content=summary_content))
            # 摘要不是已装配的系统提示：下一轮仍需跑 perception 重建身份/技能/工具面。
            meta = getattr(ctx, "meta", None)
            if isinstance(meta, dict):
                meta["restored_summary"] = True
        if post_context:
            restored_messages.append(Message(role=MessageRole.SYSTEM, content=post_context))
        restored_messages.extend(message_from_dict(item) for item in snapshot)
    else:
        post_context_added = False
        for entry in entries:
            etype = entry.get("entry_type")
            if etype in ("summary", "compact_boundary"):
                content = str(entry.get("content") or "")
                if content:
                    restored_messages.append(Message(role=MessageRole.SYSTEM, content=content))
                    meta = getattr(ctx, "meta", None)
                    if isinstance(meta, dict):
                        meta["restored_summary"] = True
                if post_context and not post_context_added:
                    restored_messages.append(Message(role=MessageRole.SYSTEM, content=post_context))
                    post_context_added = True
            elif etype == "message":
                from bandi.core.message import message_from_entry

                restored_messages.append(message_from_entry(entry))
    # 恢复统一走 message 条目；CheckpointStore（agent/checkpoint.py 每步持久化）
    # 是独立链，走下方恢复
    if restored_messages:
        # 员工持久会话下的轻量裁剪（只作用于 message 历史；checkpoint
        # 恢复是任务内续跑语义，不裁）。「回灌」= 最近窗口 + working/记忆注入（感知链）。
        if max_history_messages > 0 and len(restored_messages) > max_history_messages:
            restored_messages = restored_messages[-max_history_messages:]
        # 窗口首条对齐 user/system——裁在半轮中间时，孤立的 assistant/tool
        # （其配对在窗口外）会让服务端拒收（tool 结果无对应调用）
        while restored_messages and restored_messages[0].role not in (
            MessageRole.USER,
            MessageRole.SYSTEM,
        ):
            restored_messages.pop(0)
        if restored_messages:
            ctx.messages = restored_messages

    if checkpoint_store:
        latest_cp = checkpoint_store.list_by_session(session_id)
        if latest_cp and latest_cp[-1].get("id"):
            cp = checkpoint_store.load(latest_cp[-1]["id"])
            if cp and cp.state_json:
                try:
                    state: dict = json.loads(cp.state_json)
                    _restore_checkpoint_state(ctx, state)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning("checkpoint state restore failed: %s", e)
                # 同一 command_id（崩溃重放幂等对齐，重放事件不再新建链）
                if cp.command_id:
                    from bandi.core.event_bus import set_event_context

                    set_event_context(command_id=cp.command_id)
            # （两阶段提交的分叉窗口：先 checkpoint 后 entry，中间崩溃）
            try:
                entries = store.list_checkpoint_entries(session_id)
                if not entries:
                    store.save_checkpoint_entry(
                        session_id,
                        turn_index=cp.turn_index if cp is not None else 0,
                    )
            except Exception:  # noqa: BLE001 — 自愈 best-effort 不阻断恢复
                pass


def _boundary_active_messages(
    entry: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    """读 boundary entry 的 active_messages 快照（旧数据恢复兜底）。"""
    if entry is None:
        return None
    raw_meta = entry.get("metadata")
    meta: dict[str, Any] = {}
    if isinstance(raw_meta, str) and raw_meta:
        try:
            parsed = json.loads(raw_meta)
            meta = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
    elif isinstance(raw_meta, dict):
        meta = raw_meta
    snap = meta.get("active_messages")
    if isinstance(snap, list) and snap:
        return [item for item in snap if isinstance(item, dict)]
    return None


def _boundary_post_context(entry: dict[str, Any] | None) -> str:
    """功能：从 boundary metadata 取压缩后技能/工具/计划回注文本"""
    if entry is None:
        return ""
    raw_meta = entry.get("metadata")
    meta: dict[str, Any] = {}
    if isinstance(raw_meta, str) and raw_meta:
        try:
            parsed = json.loads(raw_meta)
            meta = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
    elif isinstance(raw_meta, dict):
        meta = raw_meta
    return str(meta.get("post_compact_context") or "")


def _restore_checkpoint_state(ctx: ExecutionContext, state: dict) -> None:
    from bandi.agent.base import QueryState

    qs = QueryState(**{k: v for k, v in state.items() if k in QueryState.__dataclass_fields__})
    ctx.apply_query_state(qs)
    # （否则 on_compressing 首次压缩才建，fast_growth/防抖跨进程不连续）；
    # 旧格式无字段则保持 None，行为不变
    if getattr(qs, "compress_token_history", ()) or qs.compress_low_returns:
        from bandi.memory.compression_pipeline import CompressionPipeline

        pipeline = CompressionPipeline()
        pipeline.restore_state(tuple(qs.compress_token_history), qs.compress_low_returns)
        ctx.compress.pipeline = pipeline


# 功能：允许记入经历层的终态白名单——只有「正常完成」类回合构成可复用的经历事实。
# 其余终态的回复是错误串/半成品/中断标记，入记忆只会在 consolidation 阶段被
# 当作高价值情节晋升，污染长期召回（如 MODEL_ERROR 的 "all LLM candidates failed…"）。
# UNKNOWN 表示「无终态信息」而非「失败」，按既有行为放行。
_EPISODIC_ELIGIBLE_TERMINALS = frozenset(
    {
        "COMPLETED",
        "VERIFIED",
        "UNKNOWN",
    }
)

# 功能：回复里的「有产出」信号由记忆层统一提供（MEMORY_VALUE_SIGNALS 单一来源）。


def _long_term_scope_for(
    fallback_project: str | None,
    fallback_employee: str | None,
    memory_type: Any,
    *,
    project_id: str | None,
) -> tuple[str | None, str | None]:
    """功能：按**记忆性质**决定长期记忆落点（薄壳，实现见 memory.decide.scope_for）。

    保留此名是为了既有契约的导入路径稳定；判定逻辑只有一份，不在此重复实现。
    """
    from bandi.memory.decide import scope_for

    return scope_for(
        memory_type,
        fallback_project=fallback_project,
        fallback_employee=fallback_employee,
        project_id=project_id,
    )


def _turn_has_reusable_value(ctx: ExecutionContext, raw_input: str, final_message: str) -> bool:
    """功能：本轮是否留下可跨轮复用的东西——决定写不写经历与草稿。

    纯寒暄、探针、单轮闲聊（无工具、无结论、未显式要求记住）既不是经历事实，
    也不是需要跨轮保留的任务状态；每轮都写只会把审批变成流水账。
    """
    from bandi.memory.ingest_review import (
        MEMORY_VALUE_SIGNALS,
        has_explicit_remember,
        is_noise_memory,
    )

    text = str(raw_input or "").strip()
    if not text:
        return False
    if has_explicit_remember(text):
        return True
    if is_noise_memory(text) is not None:
        return False
    if getattr(ctx, "tool_results", None):
        return True
    reply = str(final_message or "")
    return any(signal in reply for signal in MEMORY_VALUE_SIGNALS)


# 功能：记忆类型 → 面向用户的人话标签（审批摘要用，不暴露内部枚举名）。
_MEMORY_TYPE_LABELS = {
    "business_fact": "事实",
    "preference": "偏好",
    "procedural": "步骤",
    "process_conclusion": "结论",
    "episodic": "经历",
}
# 功能：审批预览要跳过的引导句标记（问候/自我介绍/能力清单——不是要记住的内容）。
_PREVIEW_SKIP_MARKERS = ("你好", "您好", "老板", "我是", "帮你", "我能", "可以帮", "有什么可以")


def memory_preview(content: str, memory_type: str, *, max_chars: int = 120) -> str:
    """功能：待审批记忆的**提炼预览**——人话类型标签 + 事实主体。

    用户反馈：旧摘要直接截内容开头，展示的是「你好，老板！我是…」这类
    回复的寒暄/自我介绍，看不出**要记住什么**。此处跳过引导句后取正文主体。"""
    label = _MEMORY_TYPE_LABELS.get(str(memory_type), str(memory_type))
    lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
    body_lines: list[str] = []
    for line in lines:
        if (
            not body_lines
            and any(marker in line for marker in _PREVIEW_SKIP_MARKERS)
            and len(line) < 60
        ):
            continue
        body_lines.append(line)
    body = " ".join(body_lines).strip() or str(content or "").strip()
    return f"{label} · {body[:max_chars]}"


_COMPACTION_SUMMARY_PREFIX = "[上下文摘要]"
# working_memory 12 条精神；会话内越界由感知链的 working/记忆注入承载
_COMPANY_SESSION_HISTORY_MESSAGES = 12


def _filter_mcp_tools(registry: Any, allowed_servers: list[str]) -> Any:
    """功能：按任务级 MCP 服务器白名单复制工具视图，非 MCP 工具全部保留。"""
    allowed = {str(name).strip() for name in allowed_servers if str(name).strip()}

    def _keep(tool: Any) -> bool:
        name = str(getattr(tool, "name", ""))
        if not getattr(tool, "mcp_remote", False):
            # 受限任务不暴露全局 MCP 资源工具（它们跨 server 读写资源）
            return not name.startswith("mcp_")
        provenance = str(getattr(tool, "provenance", "") or "")
        server = provenance[4:] if provenance.startswith("mcp:") else ""
        return server in allowed or name in allowed

    return registry.filtered_copy(_keep)


def _build_tool_registry_with_config(
    entity_store: Any,
    bandi_config: BandiConfig,
    provider: Any,
    ledger: Any,
    triple_ontology_store: Any | None = None,
) -> Any:
    """构建带完整配置的 ToolRegistry（超时/限额 + subagent_provider）——
    run_agent_turn 与 run_agent 统一走此路径，防委托后公司链丢配置。"""
    t = bandi_config.tools
    return build_tool_registry(
        entity_store,
        triple_ontology_store=triple_ontology_store,
        git_timeout=t.git_timeout,
        web_search_timeout_tavily=t.web_search_timeout_tavily,
        web_search_timeout_exa=t.web_search_timeout_exa,
        web_search_timeout_ddg=t.web_search_timeout_ddg,
        web_search_circuit_fail_limit=t.web_search_circuit_fail_limit,
        web_search_circuit_reset_secs=t.web_search_circuit_reset_secs,
        bash_timeout_default=t.bash_timeout_default,
        bash_timeout_max=t.bash_timeout_max,
        python_exec_timeout_default=t.python_exec_timeout_default,
        python_exec_timeout_max=t.python_exec_timeout_max,
        python_exec_max_stdout_chars=t.python_exec_max_stdout_chars,
        python_exec_max_stderr_chars=t.python_exec_max_stderr_chars,
        browser_timeout_default=t.browser_timeout_default,
        browser_timeout_max=t.browser_timeout_max,
        browser_post_click_delay_ms=t.browser_post_click_delay_ms,
        browser_snapshot_preview_chars=t.browser_snapshot_preview_chars,
        browser_max_interactive_elements=t.browser_max_interactive_elements,
        browser_element_text_max_chars=t.browser_element_text_max_chars,
        web_fetch_timeout_default=t.web_fetch_timeout_default,
        web_fetch_timeout_max=t.web_fetch_timeout_max,
        web_fetch_max_bytes=t.web_fetch_max_bytes,
        web_fetch_fallback_preview_chars=t.web_fetch_fallback_preview_chars,
        web_fetch_allow_private_networks=t.web_fetch_allow_private_networks,
        web_search_max_results=t.web_search_max_results,
        subagent_provider=provider,
        task_ledger=ledger,
        feature_flags=t.feature_flags.tool_groups,
        subagent_max_concurrency=t.subagent_max_concurrency,
    )


def _build_memory_manager(
    db_dir: Path,
    project_root: str | None = None,
    entity_lookup: Any = None,
    project_id: str | None = None,
) -> MemoryManager:
    """构建持久化 MemoryManager（long_term + episodic + working + 项目级 entries 落盘）。

    project_root 会话时 entries/落项目 .bandi/memory/（agent Edit 可直编）；
    缺省落 db_dir 派生路径（MarkdownMemoryStore 内部 fallback）。
    entity_lookup 非空时注入 EntityGraphEnricher（只读增强）；缺省 None 零行为；
    project_id 透传增强器作用域（防跨项目实体泄漏）。"""
    from bandi.memory.enricher import EntityGraphEnricher
    from bandi.memory.episodic import EpisodicStore
    from bandi.memory.long_term import LONG_TERM_DECAY_HALF_LIFE_DAYS, LongTermMemory
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.working import ScratchpadStore

    db_dir.mkdir(parents=True, exist_ok=True)
    return MemoryManager(
        long_term=LongTermMemory(
            str(db_dir / "long_term.db"),
            decay_half_life_days=LONG_TERM_DECAY_HALF_LIFE_DAYS,
        ),
        episodic=EpisodicStore(str(db_dir / "episodic.db")),
        working=ScratchpadStore(str(db_dir / "working.db")),
        markdown_memory=MarkdownMemoryStore(db_dir, project_root=project_root),
        enricher=(
            EntityGraphEnricher(entity_lookup, project_id=project_id)
            if entity_lookup is not None
            else None
        ),
    )


def _resolve_skill_team_id(org_engine: Any | None, role_id: str) -> str:
    """功能：解析技能过滤团队——角色归属 team_id；无引擎/无角色/空归属 → ""（不过滤）。"""
    if org_engine is None:
        return ""
    getter = getattr(org_engine, "get_role", None)
    role = getter(str(role_id or "")) if getter is not None else None
    if role is None:
        return ""
    return str(getattr(role, "team_id", "") or "").strip()


def _build_skill_scope(org_engine: Any | None, role_id: str) -> dict[str, Any]:
    """功能：构造技能作用域（team_id + global_only）——同时供自动注入与
    skill_invoke / skill_discover 使用，避免两条路径口径不一致。"""
    team_id = _resolve_skill_team_id(org_engine, role_id)
    return {"team_id": team_id, "global_only": not team_id}


def _employee_bound_skill_names(employee_id: str) -> list[str]:
    """功能：员工档案绑定的技能清单（general_skill）——绑定即生效，注入侧
    不依赖触发词；档案不可读按无绑定处理（best-effort，不阻断执行）。"""
    who = str(employee_id or "").strip()
    if not who:
        return []
    try:
        from bandi.company.agent_profile import AgentProfileManager

        profile = AgentProfileManager().get_profile(who)
    except Exception:  # noqa: BLE001 — 档案不可读按无绑定处理
        return []
    if profile is None:
        return []
    refs: list[str] = []
    for item in getattr(profile, "resources", []) or []:
        if str(getattr(item, "resource_type", "")) != "general_skill":
            continue
        if str(getattr(item, "status", "active")) != "active":
            continue
        ref = str(getattr(item, "resource_id", "") or "").strip()
        if ref:
            refs.append(ref)
    return refs


def _employee_bound_tool_names(employee_id: str) -> list[str]:
    """功能：员工档案绑定的工具清单（resource_type=tool）——绑定即生效，收窄工具面。

    空清单 = 未配置 = 不收窄（opt-in，与 role_tool_tags 的空名单语义不同：那里空=全禁）。
    档案不可读按未绑定处理（best-effort，不阻断执行）。
    """
    who = str(employee_id or "").strip()
    if not who:
        return []
    try:
        from bandi.company.agent_profile import AgentProfileManager

        profile = AgentProfileManager().get_profile(who)
    except Exception:  # noqa: BLE001 — 档案不可读按无绑定处理
        return []
    if profile is None:
        return []
    names: list[str] = []
    for item in getattr(profile, "resources", []) or []:
        if str(getattr(item, "resource_type", "")) != "tool":
            continue
        if str(getattr(item, "status", "active")) != "active":
            continue
        ref = str(getattr(item, "resource_id", "") or "").strip()
        if ref:
            names.append(ref)
    return names


# 功能：进程级 MCP broker 缓存——同一配置只连接一次（不随每次 run 重拉子进程）
_MCP_BROKER_CACHE: dict[str, Any] = {}


def _mcp_servers_fingerprint(bandi_config: BandiConfig) -> str:
    servers = list(getattr(bandi_config.tools, "mcp_servers", []) or [])
    raw = json.dumps([s.model_dump() for s in servers], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def _acquire_mcp_broker(bandi_config: BandiConfig, tool_registry: Any) -> Any | None:
    """功能：按需获取 MCP broker——``tools.mcp_autostart`` 关闭或无 servers 时
    直接返回 None；开启时进程级缓存复用，每次 run 只把已连接 broker 的工具
    注册到当轮 registry，并记录 MCP 工具名供懒暴露选择。"""
    if not bool(getattr(bandi_config.tools, "mcp_autostart", False)):
        return None
    if not list(getattr(bandi_config.tools, "mcp_servers", []) or []):
        return None
    cache_key = _mcp_servers_fingerprint(bandi_config)
    before: set[str] = set()
    if tool_registry is not None:
        before = {t.name for t in tool_registry.list_tools()}
    broker = _MCP_BROKER_CACHE.get(cache_key)
    if broker is None:
        broker = await build_mcp_broker(bandi_config, tool_registry)
        if broker is None:
            return None
        _MCP_BROKER_CACHE[cache_key] = broker
    elif tool_registry is not None:
        await broker.register_all_tools(tool_registry)
        from bandi.tools.mcp_resources import register_mcp_resource_tools

        register_mcp_resource_tools(tool_registry, broker)
    if tool_registry is not None:
        after = {t.name for t in tool_registry.list_tools()}
        merged = set(getattr(broker, "mcp_tool_names", set()) or set()) | (after - before)
        broker.mcp_tool_names = sorted(merged)
    return broker


def _reset_mcp_broker_cache() -> None:
    """功能：清空 MCP broker 缓存（测试隔离用）。"""
    _MCP_BROKER_CACHE.clear()


def _build_hierarchy_manager(db_dir: Path, session_store: SessionStore) -> MemoryHierarchyManager:
    """构建层级记忆管理器：global SQLite + 项目级 Markdown + WorkItemStore 快照。

    项目级记忆按 project_id 派生独立目录（<db_dir>/projects/<pid>/），多项目互不串写。
    """
    from bandi.company.store import WorkItemStore
    from bandi.memory.global_store import GlobalMemoryStore
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.memory_manager import MemoryHierarchyManager

    db_dir.mkdir(parents=True, exist_ok=True)
    return MemoryHierarchyManager(
        global_store=GlobalMemoryStore(str(db_dir / "hierarchy_global.db")),
        markdown_memory=MarkdownMemoryStore(db_dir),
        work_item_store=WorkItemStore(str(db_dir / "hierarchy_work_items.db")),
        session_store=session_store,
        store_dir=db_dir,
    )


def _memory_subject(ctx: Any, raw_input: str) -> str:
    """功能：经历记忆的主题标签——公司派单用工作项标题（附描述摘要），其余链路沿用
    「用户: 输入前缀」。

    seat_executor 把任务契约模板拼在 raw_input 最前，直接截前 200 字会得到一批逐字
    相同的样板文本（无法区分任务，FTS 命中即噪声）；工作项标题才是稳定可区分的主题。
    装配链：run_agent_turn 取 work_item → `ctx.meta["work_item_title"]`（ExecutionContext
    无 work_item 字段，只经 meta 透传）。
    """
    meta = getattr(ctx, "meta", None) or {}
    title = str(meta.get("work_item_title") or "").strip()
    description = str(meta.get("work_item_description") or "").strip()
    if not title:
        work_item = getattr(ctx, "work_item", None)
        title = str(getattr(work_item, "title", "") or "").strip()
        if title and not description:
            description = str(getattr(work_item, "description", "") or "").strip()
    if title:
        if description:
            return f"任务: {title}（{_clip_sentence(description, 120)}）"
        return f"任务: {title}"
    return f"用户: {_clip_sentence(raw_input, 200)}"


def _episode_importance(ctx: Any) -> float:
    """
    base 0.4 + 工具错误轮 +0.2 + 异常终止 +0.3 + 用户实时转向 +0.1，clamp [0,1]。"""
    score = 0.4
    tool_results = getattr(ctx, "tool_results", None) or []
    if any(not r.success for r in tool_results):
        score += 0.2
    terminal = getattr(ctx, "terminal_reason", None)
    if terminal in (
        TerminalReason.ESCALATED,
        TerminalReason.PROMPT_TOO_LONG,
        TerminalReason.TOKEN_BUDGET_EXHAUSTED,
    ):
        score += 0.3
    if getattr(ctx, "injected_text", None):
        score += 0.1
    return min(score, 1.0)


_SENTENCE_BOUNDARIES = "。！？!?；;\n"


def _clip_sentence(text: str, max_chars: int) -> str:
    """功能：episodic 摘要文本按句边界裁剪——优先停在句末标点（不句中断裂）
    边界过靠前（<1/4 预算，整段为单个超长句）或无标点时退硬切加省略号
    （4.10/）。"""
    raw = (text or "").strip()
    if len(raw) <= max_chars:
        return raw
    head = raw[:max_chars]
    cut = max(head.rfind(ch) for ch in _SENTENCE_BOUNDARIES)
    if cut >= max_chars // 4:
        return head[: cut + 1]
    return head + "…"


def _compaction_importance(ctx: Any) -> float:
    """功能：压缩摘要动态 importance——保真缺失率越高越可疑（0.7+0.2*(1-ratio)）
    无保真报告回退 0.7 基线。"""
    meta = getattr(ctx, "meta", None) or {}
    stats = meta.get("last_compression_stats")
    ratio = getattr(stats, "fidelity_missing_ratio", None) if stats is not None else None
    if ratio is None:
        return 0.7
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        return 0.7
    return max(0.7, min(0.9, 0.7 + 0.2 * (1.0 - max(0.0, min(1.0, value)))))


@dataclass
class _MemoryRetryRefs:
    """功能：跨轮重试重放注入的当前轮 live 依赖（不绑定上一轮已关闭实例）。"""

    manager: Any
    hierarchy: Any | None = None

    def scope_for(self, channel: str) -> str:
        """功能：按通道解析当前轮目标范围标识——范围不匹配的 pending 不重放（防跨库误写）。"""
        if channel == "episodic":
            return _store_scope(getattr(self.manager, "episodic", None))
        if channel == "working":
            return _store_scope(getattr(self.manager, "working", None))
        if channel in ("long_term", "compaction_summary"):
            return _store_scope(getattr(self.manager, "long_term", None))
        if channel in ("hierarchy_event", "compaction_note"):
            return _hierarchy_scope(self.hierarchy)
        return ""


def _store_scope(store: Any) -> str:
    """功能：取 store 的稳定目标标识（SQLite 库路径）——属性缺失/魔法桩
    （非 str/Path 的桩属性）时回退 PRAGMA，均无则空（空 = 不过滤，退化为旧
    「不过滤」行为）。桩属性 repr 含对象地址且跨轮漂移，若照 str() 返回
    会让重试范围永不匹配、pending 永久 deferred。"""
    if store is None:
        return ""
    for attr in ("db_path", "_db_path"):
        value = getattr(store, attr, None)
        if isinstance(value, (str, Path)):
            if value:
                return str(value)
            continue
    conn = getattr(store, "_conn", None)
    if conn is not None:
        try:
            for row in conn.execute("PRAGMA database_list").fetchall():
                if len(row) >= 3 and str(row[1]) == "main" and row[2]:
                    return str(row[2])
        except Exception:  # noqa: BLE001 — 已关闭连接无路径可查，回退空范围
            pass
    return ""


def _hierarchy_scope(hierarchy: Any) -> str:
    """功能：层级记忆目标范围——store_dir 优先，其次全局事件库路径，皆无回退空。"""
    if hierarchy is None:
        return ""
    store_dir = getattr(hierarchy, "_store_dir", None)
    if store_dir:
        return str(store_dir)
    return _store_scope(getattr(hierarchy, "_global", None))


def _persist_working_memory(
    ctx: ExecutionContext,
    manager: Any,
    session_id: str,
    turn_index: int,
    registry: Any,
    retry_refs: Any,
    *,
    raw_input: str,
    final_message: str,
    terminal: str,
) -> bool:
    """功能：写工作记忆 turn 快照（普通/群聊/单聊共用）——仅在本轮有跨轮价值时写。"""
    working_store = getattr(manager, "working", None)
    if working_store is None or not raw_input.strip():
        return False
    if not _turn_has_reusable_value(ctx, raw_input, final_message):
        return False
    from bandi.memory.session_memory import build_turn_snapshot
    from bandi.memory.working import ToolInvocation
    from bandi.observability.write_failures import RetryDependencyMissingError

    tool_invocations = [
        ToolInvocation(
            name=r.tool_name,
            success=r.success,
            result_preview=(str(r.output) if r.output is not None else "")[:200],
            error=r.error,
            duration_ms=r.duration_ms,
            session_id=session_id,
            invocation_id=r.tool_call_id or None,
        )
        for r in ctx.tool_results or []
    ]
    working_key = f"turn:{turn_index}"
    meta = getattr(ctx, "meta", None)
    ledger = meta.get("task_ledger") if isinstance(meta, dict) else None
    working_value = build_turn_snapshot(
        raw_input=raw_input,
        final_message=final_message,
        terminal=terminal,
        messages=getattr(ctx, "messages", None) or [],
        turn_index=turn_index,
        ledger_items=list(getattr(ledger, "items", None) or []),
    )

    def _write_working_on(refs: Any) -> None:
        store = getattr(refs.manager, "working", None)
        if store is None:
            raise RetryDependencyMissingError("working store unavailable")
        for invocation in tool_invocations:
            store.append_tool(invocation)
        store.set(working_key, working_value, session_id=session_id)

    return _run_write(
        registry,
        "working",
        lambda: _write_working_on(retry_refs),
        retry=_write_working_on,
        scope=retry_refs.scope_for("working"),
    )


def _write_or_stage_long_term(
    *,
    registry: Any,
    retry_refs: _MemoryRetryRefs,
    store: Any,
    item: Any,
    project_id: str | None,
    employee_id: str | None,
    memory_type: str,
    explicit: bool = False,
    value_score: float = 0.0,
) -> str:
    """功能：长期记忆的唯一写入出口——冲突/语义去重/origin 门全过才直写，否则暂存。

    直写只对「用户显式记住」开放；其余通过价值审查的候选先进草稿池，
    草稿池超限时再按价值晋级到审批队列。
    返回 written（已落库）/ staged（进暂存区待批）/ skipped（重复或写入失败）。
    """
    from bandi.memory.merge import is_semantic_duplicate
    from bandi.memory.staging import (
        GATE_DRAFT,
        GATE_REVIEW,
        MemoryWriteGate,
        get_write_origin,
        mask_summary,
        promote_memory_drafts,
        stage_long_term_write,
    )
    from bandi.observability.write_failures import RetryDependencyMissingError

    conflict = False
    if store is not None:
        checker = getattr(store, "check_conflict", None)
        if callable(checker):
            try:
                conflict = bool(checker(item.id, item.content))
            except Exception as e:  # noqa: BLE001 — 检查失败保守暂存（不炸主链）
                logger.warning("long_term conflict check failed: %s", e)
                conflict = True
    duplicate = False
    if store is not None and not conflict:
        try:
            duplicate = is_semantic_duplicate(
                store,
                item.content,
                item.id,
                project_id=project_id,
                employee_id=employee_id,
            )
        except Exception as e:  # noqa: BLE001 — 去重失败不阻断写入
            logger.warning("long_term semantic dedup failed: %s", e)
    # 功能：已被记过的内容不再提审——既不直写也不进暂存区
    if duplicate:
        logger.debug("long_term ingest skipped (semantic duplicate of recent memory)")
        return "skipped"
    origin = get_write_origin()
    # 功能：只有用户显式「记住」才允许跳过草稿直写；其余候选一律先进草稿池
    if explicit and not conflict and MemoryWriteGate.evaluate(origin) == "allow":

        def _write_long_term_on(refs: _MemoryRetryRefs) -> None:
            target = getattr(refs.manager, "long_term", None)
            if target is None:
                raise RetryDependencyMissingError("long_term store unavailable")
            from bandi.memory.merge import dedupe_key, merge_on_write

            merged = merge_on_write(
                target,
                item,
                dedupe_key(item),
                project_id=project_id,
                employee_id=employee_id,
            )
            if merged is None:
                target.store(item, project_id=project_id, employee_id=employee_id)

        if _run_write(
            registry,
            "long_term",
            lambda: _write_long_term_on(retry_refs),
            retry=_write_long_term_on,
            scope=retry_refs.scope_for("long_term"),
        ):
            return "written"
        return "skipped"
    stage_long_term_write(
        item,
        project_id=project_id,
        employee_id=employee_id,
        action="store_long_term",
        summary=mask_summary(
            memory_preview(item.content, memory_type) + ("（conflict）" if conflict else "")
        ),
        origin=origin,
        conflict=conflict,
        gate=GATE_REVIEW if conflict else GATE_DRAFT,
        value_score=value_score,
    )
    promote_memory_drafts()
    return "staged"


async def _persist_memory(
    ctx: ExecutionContext,
    manager: Any,
    session_id: str,
    hierarchy: Any | None = None,
    project_id: str | None = None,
    employee_id: str | None = None,
) -> None:
    """轮次结束后将本轮结果写入记忆系统（episodic 经历层 + long_term + hierarchy 事件快照）。

    B5b 8.4：async 化以发 MEMORY_WRITTEN 事件（通道明细）+ hierarchy payload
    附 channels——此前除 memory_persist 外无法区分写通道。

    五通道写入改为 best-effort + 有界重试——失败经 `_run_write` 登记计数
    （不静默），带重试回调入队，下次进本函数开头先补一次；仍失败则丢弃并计数。
    """
    from bandi.memory.consolidation import ONTOLOGY_TURN_TAG
    from bandi.memory.episodic import Episode
    from bandi.memory.extraction import extract_memory_fields
    from bandi.memory.ingest_review import review_episodic_ingest
    from bandi.memory.long_term import MemoryItem
    from bandi.memory.staging import (
        MemoryWriteGate,
        get_write_origin,
        mask_summary,
        stage_write,
    )
    from bandi.observability.write_failures import RetryDependencyMissingError

    registry = get_registry()
    # 原闭包绑定上一轮 manager，生产每轮新建/轮末关闭 → 重试必打在已关闭连接上；
    # 第四轮修复：仅重放「目标范围匹配」项，A 库失败项不得写进 B 库
    retry_refs = _MemoryRetryRefs(manager=manager, hierarchy=hierarchy)
    registry.retry_pending(retry_refs, scope_of=retry_refs.scope_for)

    channels: list[str] = []
    raw_input = ctx.raw_input or ""
    turn_index = ctx.turn_index
    terminal = ctx.terminal_reason.name if ctx.terminal_reason else "UNKNOWN"
    final_message = ctx.final_message or ctx.current_content or ""

    # 功能：失败/未完成的回合不构成「经历事实」——其 final_message 是错误串或半成品
    # （如 MODEL_ERROR 的 "all LLM candidates failed…"），写进 episodic 会被后续
    # consolidation 当作高价值情节晋升进长期记忆，污染召回。
    terminal_ok = terminal in _EPISODIC_ELIGIBLE_TERMINALS

    group_conversation_id = str(
        (getattr(ctx, "meta", None) or {}).get("group_conversation_id") or ""
    ).strip()
    dm_conversation_id = str(
        (getattr(ctx, "meta", None) or {}).get("dm_conversation_id") or ""
    ).strip()
    scoped_conversation_id = group_conversation_id or dm_conversation_id
    if scoped_conversation_id:
        from bandi.company.group_chat_protocol import is_pass_output

        if is_pass_output(final_message):
            return
    if (
        scoped_conversation_id
        and raw_input.strip()
        and terminal_ok
        and _turn_has_reusable_value(ctx, raw_input, final_message)
    ):
        if group_conversation_id:
            from bandi.memory.group_scope import group_scope_id

            scope = group_scope_id(group_conversation_id)
            scope_source = "group_turn"
            scope_tag = "group"
            scope_prefix = "group"
        else:
            from bandi.memory.dm_scope import dm_scope_id

            scope = dm_scope_id(str(project_id or ""), dm_conversation_id)
            scope_source = "dm_turn"
            scope_tag = "dm"
            scope_prefix = "dm"
        episode_summary = (
            f"{_memory_subject(ctx, raw_input)} → 回答: {_clip_sentence(final_message, 400)}"
        )
        fields = extract_memory_fields(episode_summary, user_input=raw_input, source=scope_source)
        verdict = review_episodic_ingest(episode_summary)
        episodic = getattr(manager, "episodic", None)
        if episodic is not None and verdict.decision != "reject":
            try:
                episodic.record(
                    Episode(
                        episode_id=f"ep:{scope_prefix}:{session_id}:{turn_index}",
                        summary=episode_summary,
                        session_id=session_id,
                        project_id=scope,
                        employee_id=None,
                        user_input=raw_input[:500],
                        tags=[terminal, scope_tag, f"mt:{fields.memory_type.value}"]
                        + [f"ent:{name}" for name in fields.entities],
                        importance=_episode_importance(ctx),
                    )
                )
                channels.append("episodic")
            except Exception as exc:  # noqa: BLE001 — 作用域记忆写入失败不阻断回合
                logger.warning("%s episodic write failed: %s", scope_prefix, exc)
        from bandi.memory.decide import decide_long_term

        scoped_decision = decide_long_term(
            raw_input=raw_input,
            final_message=final_message,
            source=scope_source,
            fallback_project=scope,
            project_id=project_id,
        )
        long_term = getattr(manager, "long_term", None)
        if long_term is not None and scoped_decision.write:
            try:
                from bandi.memory.structured import build_structured

                fact_fields = extract_memory_fields(
                    scoped_decision.source_text, user_input=raw_input, source=scope_source
                )
                metadata: dict[str, Any] = {
                    "user_input": raw_input[:500],
                    "evidence": scoped_decision.source_text[:2000],
                    "description": fact_fields.render_description(scoped_decision.source_text),
                    **fact_fields.to_metadata(),
                }
                structured = build_structured(scoped_decision.source_text)
                if structured:
                    metadata["structured"] = structured
                status = _write_or_stage_long_term(
                    registry=registry,
                    retry_refs=retry_refs,
                    store=long_term,
                    item=MemoryItem(
                        item_id=f"{scope_prefix}:{session_id}:{turn_index}",
                        content=scoped_decision.content,
                        importance=0.6,
                        metadata=metadata,
                    ),
                    project_id=scoped_decision.project_id,
                    employee_id=scoped_decision.employee_id,
                    memory_type=fact_fields.memory_type.value,
                    explicit=bool(scoped_decision.explicit),
                    value_score=float(scoped_decision.value_score or 0.0),
                )
                if status == "written":
                    channels.append("long_term")
            except Exception as exc:  # noqa: BLE001 — 作用域记忆写入失败不阻断回合
                logger.warning("%s long-term write failed: %s", scope_prefix, exc)
        # markdown 只承载沉淀条目：不再每轮写「用户: X → 回答: Y」原文快照——
        # 注入时会被原样读回，等于把上一轮对话当记忆复读，并绕过噪声闸门与价值门。
        if hierarchy is not None and scoped_decision.write:
            try:
                from bandi.memory.memory_manager import SCOPE_PROJECT

                hierarchy.append_memory(SCOPE_PROJECT, scoped_decision.content, project_id=scope)
                channels.append("project_md")
            except Exception as exc:  # noqa: BLE001 — 作用域记忆写入失败不阻断回合
                logger.warning("%s markdown write failed: %s", scope_prefix, exc)
        if _persist_working_memory(
            ctx,
            manager,
            session_id,
            turn_index,
            registry,
            retry_refs,
            raw_input=raw_input,
            final_message=final_message,
            terminal=terminal,
        ):
            channels.append("working")
        await _emit_memory_written(ctx, session_id, turn_index, channels)
        return

    episodic_store = getattr(manager, "episodic", None)
    if (
        episodic_store is not None
        and raw_input.strip()
        and terminal_ok
        and _turn_has_reusable_value(ctx, raw_input, final_message)
    ):
        # 公司派单的 raw_input 以任务契约模板开头（seat_executor 把 contract/output_ctx
        # 拼在最前）——直接截前缀会让不同员工/不同任务的经历摘要逐字相同。有工作项时
        # 改用「任务标题 + 描述」作主题，保住可区分性。
        episode_summary = (
            f"{_memory_subject(ctx, raw_input)} → 回答: {_clip_sentence(final_message, 400)}"
        )
        fields = extract_memory_fields(episode_summary, user_input=raw_input, source="agent_turn")
        verdict = review_episodic_ingest(episode_summary)
        episode = Episode(
            # 不重复记录（原 uuid4() 每次新 id 致重复经历），对齐 long_term 稳定键先例
            episode_id=f"ep:{session_id}:{turn_index}",
            # 4.10：句边界裁剪替代字符硬切（不句中断裂）
            summary=episode_summary,
            session_id=session_id,
            project_id=project_id or None,
            employee_id=employee_id or None,
            user_input=raw_input[:500],
            tags=[terminal, f"mt:{fields.memory_type.value}"]
            + [f"ent:{name}" for name in fields.entities]
            + ([ONTOLOGY_TURN_TAG] if _ontology_write_turn(ctx) else []),
            importance=_episode_importance(ctx),
        )

        def _write_episodic_on(refs: _MemoryRetryRefs) -> None:
            store = getattr(refs.manager, "episodic", None)
            if store is None:
                raise RetryDependencyMissingError("episodic store unavailable")
            store.record(episode)

        def _stage_episodic() -> None:
            stage_write(
                action="record_episode",
                summary=mask_summary(
                    f"{fields.memory_type.value}/episodic: {episode_summary[:80]}"
                ),
                payload={
                    "target": "episodic",
                    "item": asdict(episode),
                    "draft": {"kind": "后台直写暂存", "target": "情景记忆"},
                },
                origin=get_write_origin(),
            )

        if verdict.decision == "reject":
            logger.debug("episodic ingest rejected (%s): %s", verdict.reason, episode.episode_id)
        elif verdict.decision == "stage" or MemoryWriteGate.evaluate(get_write_origin()) != "allow":
            _stage_episodic()
        elif _run_write(
            registry,
            "episodic",
            lambda: _write_episodic_on(retry_refs),
            retry=_write_episodic_on,
            scope=retry_refs.scope_for("episodic"),
        ):
            channels.append("episodic")

    if _persist_working_memory(
        ctx,
        manager,
        session_id,
        turn_index,
        registry,
        retry_refs,
        raw_input=raw_input,
        final_message=final_message,
        terminal=terminal,
    ):
        channels.append("working")

    if not final_message:
        await _emit_memory_written(ctx, session_id, turn_index, channels)
        return

    # events 表 + 查询入口；hierarchy 事件门面委托
    # global_store.append_event）
    if hierarchy is not None:

        def _write_hierarchy_event_on(refs: _MemoryRetryRefs) -> None:
            target = refs.hierarchy
            if target is None:
                raise RetryDependencyMissingError("hierarchy store unavailable")
            # event_id 按 (session, turn) 稳定生成——不传 event_id 时重复
            # persist（同 turn 恢复/重跑）会产生重复事件
            target.append_event(
                "memory_persist",
                {
                    "event_type": "agent_turn",
                    "summary": f"用户: {raw_input[:100]} → Agent: {final_message[:100]}",
                    "turn_index": turn_index,
                    "terminal_reason": terminal,
                    "session_id": session_id,
                    # B5b 8.4：写入通道明细（episodic/working/long_term/compaction）
                    "channels": channels,
                },
                event_id=f"memory_persist:{session_id}:{turn_index}",
            )

        _run_write(
            registry,
            "hierarchy_event",
            lambda: _write_hierarchy_event_on(retry_refs),
            retry=_write_hierarchy_event_on,
            scope=retry_refs.scope_for("hierarchy_event"),
        )

    # 直写只取用户输入两路：显式「记住…」或用户陈述可记事实/偏好——
    # 要记的是用户说过的事实，助手回复永不直写。判定走单一入口 decide_long_term。
    from bandi.memory.decide import decide_long_term
    from bandi.memory.ingest_review import has_explicit_remember

    explicit_remember = has_explicit_remember(raw_input)
    decision = decide_long_term(
        raw_input=raw_input,
        final_message=final_message,
        source="agent_turn",
        fallback_project=project_id,
        fallback_employee=employee_id,
        project_id=project_id,
    )
    source_text = decision.source_text
    # 本体写入轮：内容归本体库（结构化提取），不再自动直写长期记忆——
    # 防同一内容「本体一份 + 长期记忆一份」重复；显式「记住」是用户主动指令，保留
    if source_text and not explicit_remember and _ontology_write_turn(ctx):
        logger.debug("long_term direct write skipped (ontology write turn)")
        source_text = None
    if source_text:
        fields = extract_memory_fields(source_text, user_input=raw_input, source="agent_turn")
        if not decision.write:
            logger.debug("long_term ingest rejected (%s)", decision.reason)
        else:
            from bandi.memory.structured import build_structured

            structured = build_structured(source_text)
            long_term_metadata: dict[str, Any] = {
                "user_input": raw_input[:500],
                "evidence": source_text[:2000],
                "description": fields.render_description(source_text),
                **fields.to_metadata(),
            }
            if structured:
                long_term_metadata["structured"] = structured
            if explicit_remember:
                # 显式「记住…」落可持久化标记：衰减可降权，但遗忘必须豁免
                # （用户明说要记的东西不该被维护周期清掉）。
                long_term_metadata["is_explicit"] = True
            long_term_item = MemoryItem(
                # REPLACE 覆盖，不产生重复条目。
                # project/employee 隔离由存储维度列承担，不冗余进 id。
                item_id=f"turn:{session_id}:{turn_index}",
                # 主形态 = 结构化断言，正则切不出谓词时回退原句；
                # 原句留底 evidence —— 断言供检索与合并，原句供追溯与纠错。
                content=decision.content,
                importance=0.5,
                metadata=long_term_metadata,
            )
            # 功能：落点由 decide_long_term 按记忆性质给出（偏好→全局 / 结论→项目 / 其余→本域）
            if (
                _write_or_stage_long_term(
                    registry=registry,
                    retry_refs=retry_refs,
                    store=getattr(manager, "long_term", None),
                    item=long_term_item,
                    project_id=decision.project_id,
                    employee_id=decision.employee_id,
                    memory_type=fields.memory_type.value,
                    explicit=bool(decision.explicit),
                    value_score=float(decision.value_score or 0.0),
                )
                == "written"
            ):
                channels.append("long_term")

    try:
        # 防止单个项目海量 turn 记忆淘汰其他项目高价值记忆；全局 max_items 作
        # 兜底上限防无限增长（原全局 prune(1000) 一个项目就能挤掉别的项目）。
        manager.long_term.prune_partitioned(max_items=5000, per_partition_max=300)
    except Exception as e:  # noqa: BLE001
        logger.warning("long-term prune failed: %s", e)

    if hierarchy is not None and project_id and employee_id:
        _run_write(
            registry,
            "hierarchy_snapshot",
            lambda: _persist_hierarchy_snapshot(
                ctx, hierarchy, project_id, employee_id, session_id
            ),
        )

    # 不得被快照覆盖式 save 冲掉）；轮末扫描消息中的 [上下文摘要]（SM 与
    # pipeline 两路压缩产物同权），稳定键 REPLACE 幂等（同 turn 重跑不重复）；
    # importance 0.7 高优保证检索排序靠前。long_term 落库无条件（不依赖
    # hierarchy/维度）；纪要追加仅在维度齐备时执行
    summary_msg = next(
        (
            m
            for m in reversed(getattr(ctx, "messages", None) or [])
            if (m.content or "").startswith(_COMPACTION_SUMMARY_PREFIX)
        ),
        None,
    )
    if summary_msg is not None:
        summary_body = summary_msg.content[len(_COMPACTION_SUMMARY_PREFIX) :][:2000]
        # 4.9：内容指纹键——摘要消息跨轮常驻，turn 键会每轮重复落同一摘要；
        # 内容指纹使「同摘要 REPLACE、新摘要新增」语义成立（同一 turn 重跑亦幂等）
        fingerprint = hashlib.sha256(summary_body.encode("utf-8")).hexdigest()[:8]

        compaction_item = MemoryItem(
            item_id=f"compaction:{session_id}:{fingerprint}",
            content=summary_body,
            importance=_compaction_importance(ctx),
            metadata={"session_id": session_id, "turn_index": turn_index},
        )

        def _write_compaction_summary_on(refs: _MemoryRetryRefs) -> None:
            store = getattr(refs.manager, "long_term", None)
            if store is None:
                raise RetryDependencyMissingError("long_term store unavailable")
            store.store(
                compaction_item,
                project_id=project_id,
                employee_id=employee_id,
            )

        summary_ok = _run_write(
            registry,
            "compaction_summary",
            lambda: _write_compaction_summary_on(retry_refs),
            retry=_write_compaction_summary_on,
            scope=retry_refs.scope_for("compaction_summary"),
        )
        note_ok = False
        if hierarchy is not None and project_id and employee_id:
            compaction_note = f"[压缩纪要 turn {turn_index}]\n{summary_body[:800]}"

            def _write_compaction_note_on(refs: _MemoryRetryRefs) -> None:
                target = refs.hierarchy
                if target is None:
                    raise RetryDependencyMissingError("hierarchy store unavailable")
                target.append_memory(
                    "session",
                    compaction_note,
                    project_id=project_id,
                    session_id=session_id,
                    employee_id=employee_id,
                )

            note_ok = _run_write(
                registry,
                "compaction_note",
                lambda: _write_compaction_note_on(retry_refs),
                retry=_write_compaction_note_on,
                scope=retry_refs.scope_for("compaction_note"),
            )
        # 通道明细只记真实落库成功的通道` 语义）
        if summary_ok or note_ok:
            channels.append("compaction")
    # B5b 8.4：记忆写入事件（通道明细随事件流/trace 出网）
    await _emit_memory_written(ctx, session_id, turn_index, channels)


def _run_write(
    registry: Any,
    channel: str,
    fn: Callable[[], None],
    retry: Callable[[Any], None] | None = None,
    scope: str = "",
) -> bool:
    """功能：执行一次 best-effort 写入——失败登记计数（含重试回调）并返回 False。

    不抛错：记忆写入属非关键路径，失败不得阻断 Agent 主链（边界）。
    retry 给定时登记为「接收当前轮 live 依赖重放」的回调（防旧闭包绑定已关闭实例）；
    scope 为写入目标范围标识，重放时按范围过滤防跨库误写。
    """
    try:
        fn()
        return True
    except Exception as e:  # noqa: BLE001 — best-effort 通道失败不阻断主链
        registry.record(channel, e, retry=retry if retry is not None else fn, scope=scope)
        return False


def _persist_or_raise(registry: Any, channel: str, fn: Callable[[], None]) -> None:
    """功能：fail-closed 写入——checkpoint 前等关键落库失败必须抛出，不能静默。

    与 `_run_write` 的区别：这是恢复语义依赖的数据（缺了会导致 checkpoint 重放
    已执行的工具），因此失败登记后**继续抛出**（对照 dsh session-checkpoint-policy）。
    """
    try:
        fn()
    except Exception as e:
        registry.record(channel, e)
        logger.error("critical write failed [channel=%s]: %s", channel, e)
        raise


async def _emit_memory_written(
    ctx: ExecutionContext, session_id: str, turn_index: int, channels: list[str]
) -> None:
    # 功能：B5b 8.4——MEMORY_WRITTEN 事件发射（失败不阻断记忆主链）
    if not channels:
        return
    try:
        from bandi.observability.write_failures import get_registry

        await ctx.bus.emit(
            Event(
                type=EventType.MEMORY_WRITTEN,
                data={
                    "session_id": session_id,
                    "turn_index": turn_index,
                    "channels": channels,
                    "write_failures_total": get_registry().total,
                },
            )
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("memory written event emit failed: %s", e)


def _persist_tool_messages(ctx: ExecutionContext, store: SessionStore, session_id: str) -> None:
    """将本轮的 TOOL 结果消息落库（按 tool_call_id 幂等去重，恢复后不重复）。

    on_executing 已把工具结果 append 进 ctx.messages（reflecting 直接终止时
    工具结果仍进入对话上下文），这里只负责持久化。
    """
    existing = {
        e.get("tool_call_id")
        for e in store.get_branch(session_id, full=True)
        if e.get("entry_type") == "message" and e.get("role") == "tool"
    }
    for msg in ctx.messages:
        if msg.role == MessageRole.TOOL and msg.tool_call_id:
            if msg.tool_call_id not in existing:
                store.append_message(session_id, msg)
                existing.add(msg.tool_call_id)


def _persist_hierarchy_snapshot(
    ctx: ExecutionContext,
    hierarchy: Any,
    project_id: str,
    employee_id: str,
    session_id: str,
) -> None:
    """将本轮写入层级记忆：session 快照（本会话最终产出）+ employee 快照（最终产出）。

    该内容已由 working（turn:{t}）与 episodic（ep:{sid}:{t}）承载并经 B 通道注入
    hierarchy 再写同一回声 = 双写。session 快照改为承载「本会话产出」这一唯一价值
    （employee 快照为员工级最终产出，二者作用域不同：session 按会话、employee 按员工）。
    """
    final_message = ctx.final_message or ctx.current_content or ""
    if not final_message:
        return
    terminal = ctx.terminal_reason.name if getattr(ctx, "terminal_reason", None) else "UNKNOWN"
    if terminal not in _EPISODIC_ELIGIBLE_TERMINALS:
        return
    from bandi.memory.ingest_review import is_noise_memory

    if (
        is_noise_memory(final_message) is not None
        or is_noise_memory(ctx.raw_input or "") is not None
    ):
        return
    hierarchy.save_memory(
        "session",
        final_message[:2000],
        project_id=project_id,
        session_id=session_id,
        employee_id=employee_id,
    )
    hierarchy.save_memory(
        "employee",
        final_message[:2000],
        project_id=project_id,
        employee_id=employee_id,
    )


def resolve_initial_mode(bandi_config: BandiConfig | None, requested: str) -> str:
    """功能：会话初始模式解析——显式请求优先；缺省回落 agent.mode 配置
    （原死配置：配置有定义但从不被消费），配置缺失再回落 execute。"""
    if requested:
        return requested
    mode = str(getattr(getattr(bandi_config, "agent", None), "mode", "") or "")
    return mode or "execute"


_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif")


def _is_image_attachment(item: dict[str, Any]) -> bool:
    # 功能：按 MIME/扩展名判定图片附件。
    mime = str(item.get("mime") or "").lower()
    if mime.startswith("image/"):
        return True
    name = str(item.get("name") or item.get("path") or "").lower()
    return name.endswith(_IMAGE_EXTENSIONS)


def _format_attachment_notes(
    attachments: list[dict[str, Any]] | None, *, vision: bool = False
) -> str:
    """功能：附件注记段（名称 + 文本类正文；图片按模型能力说明）。"""
    if not attachments:
        return ""
    sections: list[str] = []
    for item in attachments:
        name = str(item.get("name") or item.get("upload_id") or "附件")
        text = str(item.get("text") or "").strip()
        if _is_image_attachment(item):
            sections.append(
                f"【图片】{name}"
                + (
                    "（已作为图像输入）"
                    if vision
                    else "（当前模型不支持图像输入，无法查看图片内容）"
                )
            )
        else:
            sections.append(f"【附件】{name}\n{text}" if text else f"【附件】{name}")
    return "\n\n".join(sections)


def _inject_attachment_notes(ctx: Any, attachments: list[dict[str, Any]]) -> None:
    """功能：附件注记挂 meta（loop 组装后统一拼入请求末条 user）——落库原文不变。"""
    images = [
        {
            "path": str(item.get("path") or ""),
            "mime": str(item.get("mime") or ""),
            "name": str(item.get("name") or ""),
        }
        for item in attachments
        if _is_image_attachment(item) and str(item.get("path") or "").strip()
    ]
    if images:
        ctx.meta["image_attachments"] = images
    provider = getattr(ctx, "provider", None)
    vision = (
        bool(getattr(provider, "supports_vision", False))
        and str(getattr(provider, "protocol", "openai")) == "openai"
    )
    notes = _format_attachment_notes(attachments, vision=vision)
    if not notes:
        return
    ctx.meta["attachment_notes"] = notes


async def run_agent_turn(
    user_input: str,
    mode: str = "",
    session_id: str | None = None,
    session_store: SessionStore | None = None,
    entity_store: Any | None = None,
    triple_ontology_store: Any | None = None,
    bandi_config: BandiConfig | None = None,
    stream_handler: StreamHandler | None = None,
    abort_controller: AbortController | None = None,
    append_user_message: bool = True,
    memory_manager: Any | None = None,
    hierarchy: Any | None = None,
    project_id: str | None = None,
    employee_id: str | None = None,
    group_conversation_id: str = "",
    dm_conversation_id: str = "",
    user_interaction: UserInteraction | None = None,
    work_item: Any | None = None,
    checkpoint_interval: int = 1,
    communication: Any | None = None,
    agent_role: str = "user",
    project_root: str | None = None,
    provider_config: dict[str, Any] | None = None,
    session_timeout_sec: int = 0,
    company_runtime: Any | None = None,
    role_tool_tags: list[str] | None = None,
    role_fs_scopes: list[str] | None = None,
    manager_capable: bool = True,
    skill_scope: dict[str, Any] | None = None,
    company_store: Any | None = None,
    company_thread_manager: Any | None = None,
    company_org_engine: Any | None = None,
    user_id: str = "",
    account_store: Any | None = None,
    relax_workspace_asks: bool = False,
    session_title: str | None = None,
    session_origin: str = "",
    attachments: list[dict[str, Any]] | None = None,
    mcp_allowed_servers: list[str] | None = None,
) -> AgentTurnResult:
    """统一的 Agent 执行入口。CLI/API/未来桌面端/公司链路（run_agent 委托）都调这个。

    职责：
      1. 创建/复用会话（session_id 给定但无记录时自动补建——公司链 scoped id）
      2. 建立 EventBus + 绑定 StreamHandler
      3. 构建 Provider / ToolRegistry / ExecutionContext / PerceptionPipeline / AgentLoop
      4. 运行 AgentLoop
      5. 保存 assistant + 工具轮次消息
      6. 返回 AgentTurnResult

    append_user_message=False 时调用方已预先写入用户消息（如 API chat 路由），避免重复落库。
    work_item 传入时启用 OwnershipMiddleware 并绑定当前工作项（公司模式越界写守卫）。
    checkpoint_interval 为 checkpoint 保存步长（与 AgentLoop 对齐）。

    hierarchy 传入 MemoryHierarchyManager 时：轮末写 session/employee 快照；且四层记忆
    （global/project/session/employee）经 ContextLoader 注入 system prompt。缺失 project_id 或
    employee_id 时仅跳过快照写入与员工层检索，不抛错。
    """
    if bandi_config is None:
        bandi_config = BandiConfig.load()

    # bandi_config.llm 同名字段（build_provider 前生效，下游全链一致）。
    # 污染调用方持有的 config 对象（多员工并发各传不同 llm_config = 数据竞争）
    #
    # `default_model`，而 `build_provider` 内部会再解析一次档案 ⇒ 显式传入的
    # provider_config.default_model 被档案**静默吞掉**（注入不存在的模型 id 仍成功回复）。
    # 修法：显式覆盖场景先让档案落位、再把 provider_config 压在档案之上，并在建 provider
    # 时走 `_build_provider_direct`（不再二次档案解析，与 judge/feedback 路径同先例）。
    _explicit_model = ""
    if provider_config:
        from bandi.llm.profiles import with_resolved_llm

        bandi_config = with_resolved_llm(bandi_config)
        updates = {
            key: value
            for key, value in provider_config.items()
            if value is not None and hasattr(bandi_config.llm, key)
        }
        if updates:
            new_llm = bandi_config.llm.model_copy(update=updates)
            bandi_config = bandi_config.model_copy(update={"llm": new_llm})
            _explicit_model = str(updates.get("default_model") or "").strip()

    db_path = Path.home() / ".bandi" / "data" / "bandi.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = session_store or SessionStore(str(db_path))
    # 不关闭（跨轮复用），自建 store 必须随本 turn 结束关闭连接
    store_owned = session_store is None

    if not user_input.strip() and attachments:
        # 纯图片消息：给模型一个可执行的最小文本锚点，图像块随后挂载。
        user_input = "请查看图片"
    # 初始模式解析：显式请求 > agent.mode 配置 > execute（config.agent.mode 接线）
    mode = resolve_initial_mode(bandi_config, mode)

    if not session_id:
        session_id = store.create_session(
            mode=mode,
            project_root=project_root,
            project_id=project_id,
            title=(session_title or "").strip()[:120],
            origin=(session_origin or "").strip() or None,
        )
    elif store.get_session(session_id) is None:
        store.create_session(
            session_id=session_id,
            mode=mode,
            project_root=project_root,
            project_id=project_id,
            title=(session_title or "").strip()[:120],
            origin=(session_origin or "").strip() or None,
        )

    # 落盘去 ~/.bandi/tmp、bash cwd 落 workspace）
    if project_root is None:
        _root_resolver = getattr(store, "session_project_root", None)
        if _root_resolver is not None:
            project_root = _root_resolver(session_id)

    # 被拒 turn 零写入零构建（旧位置在 span 内，被拒时幽灵用户消息已落库且
    # provider/ctx 全套白建）。同 session 并发快速失败，指令变更走 注入。
    if not _acquire_session_slot(session_id, store=store):
        if store_owned:
            store.close()
        return AgentTurnResult(
            reply="",
            terminal_reason="SESSION_BUSY",
            total_tokens=0,
            turn_index=0,
            session_id=session_id,
        )

    # 永久 SESSION_BUSY 直到重启）；store_owned 同步关闭（此段在外层 finally 之外）
    lease_keeper: Any = None
    if store is not None:
        lease_keeper = asyncio.create_task(_session_lease_keeper(session_id, store))
    bus: Any = None
    try:
        if append_user_message:
            user_msg = Message(role=MessageRole.USER, content=user_input)
            if attachments:
                user_msg.meta["attachments"] = [
                    {k: v for k, v in item.items() if k != "text"} for item in attachments
                ]
            store.append_message(session_id, user_msg)

        event_log_path = (
            Path.home() / ".bandi" / "logs" / f"events-{_safe_filename(session_id)}.jsonl"
        )
        bus = EventBus(persist_path=event_log_path)
        bus.enable_persistence()
        from bandi.observability.bridge import AuditChainBridge

        await AuditChainBridge(store).wire(bus)
        # 人工决策事件路由：Process 级 ApprovalStore 绑定本 run 的 bus（按会话）
        from bandi.tools.approval import get_approval_store

        get_approval_store().bind_event_bus(bus, session_id=session_id)
        bindings = (
            await _subscribe_handler(bus, stream_handler, session_id) if stream_handler else None
        )
    except BaseException:
        if bus is not None:
            try:
                bus.disable_persistence()
            except Exception:
                logger.warning("event bus cleanup after setup failure failed", exc_info=True)
        await _stop_lease_keeper(lease_keeper)
        _release_session_slot(session_id, store=store)
        if store_owned:
            store.close()
        raise

    logger_instance: Any = None
    cost_tracker: Any = None
    provider: Any = None
    prefetch_handle: Any = None
    ckpt_store: Any = None
    ctx: Any = None
    memory: Any = None
    hierarchy_owned = False
    try:
        if _explicit_model:
            # 显式模型：直建 provider（档案已在上方落位、显式值已压在其上）——
            # 再经 build_provider 会被档案二次覆盖，用户的模型选择又将失效
            from bandi.agent.factory import _build_provider_direct

            provider = _build_provider_direct(bandi_config)
        else:
            provider = build_provider(bandi_config)
        from bandi.tools.task_ledger import TaskLedger

        ledger = TaskLedger(
            config=bandi_config.tools.task_ledger,
            session_id=session_id,
            session_store=store,
            event_bus=bus,
        )
        ledger.restore()
        tool_registry = _build_tool_registry_with_config(
            entity_store, bandi_config, provider, ledger, triple_ontology_store
        )
        # 公司链路在此把 store/thread_manager/org_engine 后绑定注入；非公司链路 deps 全空
        # 不注入（工具保持 fail-closed）。经 registry.get 定位而非 import 解耦（工具面完整）。
        _spawn_tool = tool_registry.get("spawn_thread_task")
        if _spawn_tool is not None and hasattr(_spawn_tool, "bind_deps"):
            _spawn_tool.bind_deps(
                store=company_store,
                thread_manager=company_thread_manager,
                org_engine=company_org_engine,
                work_item=work_item,
            )
        for _manager_tool_name in ("manager_board_read", "modify_work_item", "delete_work_item"):
            _manager_tool = tool_registry.get(_manager_tool_name)
            if _manager_tool is not None and hasattr(_manager_tool, "bind_deps"):
                _manager_tool.bind_deps(store=company_store, org_engine=company_org_engine)
        for _collab_tool_name in ("ask_peer_and_wait", "route_work"):
            _collab_tool = tool_registry.get(_collab_tool_name)
            if _collab_tool is not None and hasattr(_collab_tool, "bind_company"):
                _collab_tool.bind_company(
                    store=company_store, work_item=work_item, org_engine=company_org_engine
                )
        _team_tool = tool_registry.get("team_create")
        if _team_tool is not None and hasattr(_team_tool, "bind_deps"):
            _team_tool.bind_deps(org_engine=company_org_engine)
        # 功能：MCP 默认不拉起（mcp_autostart 关闭时直接跳过）；开启时进程级复用
        # 功能：MCP 默认不拉起（mcp_autostart 关闭时直接跳过）；开启时进程级复用；
        # 显式要求的 MCP 工具名透传工具面（不被相关性裁剪掉）
        mcp_broker = await _acquire_mcp_broker(bandi_config, tool_registry)
        if mcp_allowed_servers:
            tool_registry = _filter_mcp_tools(tool_registry, mcp_allowed_servers)
        skill_scope = _build_skill_scope(company_org_engine, agent_role)
        ctx = build_agent_context(
            user_input=user_input,
            mode=mode,
            provider=provider,
            tool_registry=tool_registry,
            bus=bus,
            bandi_config=bandi_config,
            entity_store=entity_store,
            triple_ontology_store=triple_ontology_store,
            mcp_broker=mcp_broker,
            user_interaction=user_interaction,
            enable_ownership=work_item is not None,
            work_item=work_item,
            communication=communication,
            agent_role=agent_role,
            project_root=project_root,
            role_tool_tags=role_tool_tags,
            role_fs_scopes=role_fs_scopes,
            manager_capable=manager_capable,
            resource_tool_allowlist=_employee_bound_tool_names(str(employee_id or "")),
            extra_surface_tools=getattr(mcp_broker, "mcp_tool_names", None),
            skill_scope=skill_scope,
            abort_controller=abort_controller,
            user_id=user_id,
            account_store=account_store,
            relax_workspace_asks=relax_workspace_asks,
        )
        ctx.meta["session_id"] = session_id
        ctx.meta["group_conversation_id"] = str(group_conversation_id or "").strip()
        ctx.meta["dm_conversation_id"] = str(dm_conversation_id or "").strip()
        # 下游 LLM 调用据此禁用回落链，不可用必须报错（配错模型 / Key / 余额 / 网关）
        # 不得静默换模型继续回复。仅当 provider_config 真正带模型名时才标记。
        _explicit_model = str((provider_config or {}).get("default_model") or "").strip()
        if _explicit_model:
            ctx.meta["strict_model"] = _explicit_model
        if attachments:
            _inject_attachment_notes(ctx, attachments)
        # 经历记忆主题（公司派单）：raw_input 以任务契约模板开头，轮末写记忆时需要一个
        # 可区分的主题，否则不同员工/不同任务的经历摘要逐字相同。只落字符串，不进 checkpoint
        if work_item is not None:
            work_item_title = str(getattr(work_item, "title", "") or "").strip()
            if work_item_title:
                ctx.meta["work_item_title"] = work_item_title
                work_item_description = str(getattr(work_item, "description", "") or "").strip()
                if work_item_description:
                    ctx.meta["work_item_description"] = work_item_description
        if not ctx.meta.get("run_id"):
            from uuid import uuid4

            ctx.meta["run_id"] = uuid4().hex[:12]
        ctx.meta["task_ledger"] = ledger
        # on_executing 工具批边界 drain 共用）；测试经同键覆盖打桩
        from bandi.agent.steer import get_default_registry

        ctx.meta["steer_registry"] = get_default_registry()
        from bandi.observability.tracer import BandiTracer

        tracer = BandiTracer()
        ctx.meta["tracer"] = tracer

        from bandi.llm.model_catalog import ModelCatalog
        from bandi.observability.logger import Logger
        from bandi.observability.trace_explainer import TraceExplainer

        log_dir = Path.home() / ".bandi" / "logs"
        from bandi.safety.pii_detector import PIIDetector

        logger_instance = Logger(
            log_dir=log_dir,
            console_output=False,
            pii_detector=PIIDetector().sanitize,
        )
        # .1：把 stdlib 主链日志（loop / states / company 等）接进结构化管道。
        # 幂等——重复构建 deps 不会重复挂 handler；哪些条目留在文件里由 Logger 自身
        # 的 _level 决定，桥只负责「收得到」。
        from bandi.observability.stdlib_bridge import install_logger_bridge

        install_logger_bridge(logger_instance, logger_name="bandi")
        # budget_limit 透传 config 值 + 注入 ctx.meta 供 states._do_llm_call
        # 在 LLM 调用前检查（进程级共享 tracker，当日累计跨 run 生效）
        from bandi.llm.cost_tracker import get_shared_cost_tracker
        from bandi.observability.cost_store import get_shared_cost_store

        # 共享实例按 db 路径 keyed——重复构建 deps 不会重复开连接/队列。
        # 库路径单一来源（core.config.default_cost_db_path）——与 api 端点读取同一文件
        cost_store = get_shared_cost_store(default_cost_db_path())
        cost_tracker = get_shared_cost_tracker(
            ModelCatalog(), budget_limit=bandi_config.llm.budget_limit, store=cost_store
        )
        trace_explainer = TraceExplainer(event_bus=bus, session_store=store)

        from bandi.core.dependencies import Dependencies as CoreDeps

        deps = CoreDeps(
            bus=bus,
            config=bandi_config,
            cost_tracker=cost_tracker,
            session_store=store,
            logger=logger_instance,
            trace_explainer=trace_explainer,
        )
        from bandi.core.event_bus import bootstrap_event_bus

        await bootstrap_event_bus(bus, deps)

        context_registry = ContextSourceRegistry()
        memory = memory_manager or _build_memory_manager(
            Path.home() / ".bandi" / "data", project_root=project_root, project_id=project_id
        )
        ctx.meta["memory_manager"] = memory
        # cost_tracker 注入 ctx.meta——states._do_llm_call 调用前检查预算
        ctx.meta["cost_tracker"] = cost_tracker
        ctx.meta["session_store"] = store
        if memory is not None:
            from bandi.context.sources.memory_source import MemoryContextSource

            context_registry.register(
                "memory",
                MemoryContextSource(
                    memory,
                    project_id=None
                    if (group_conversation_id or dm_conversation_id)
                    else project_id,
                    employee_id=None
                    if (group_conversation_id or dm_conversation_id)
                    else employee_id,
                    conversation_id=group_conversation_id or None,
                    dm_conversation_id=dm_conversation_id or None,
                ),
            )
        if employee_id:
            from bandi.context.sources.company_source import EmployeeSopContextSource

            context_registry.register("employee_sop", EmployeeSopContextSource(employee_id))
        if company_runtime is not None:
            from bandi.context.sources.company_source import CompanyContextSource

            context_registry.register(
                "company", CompanyContextSource(company_runtime=company_runtime)
            )
            ctx.meta["team_id"] = _resolve_skill_team_id(company_org_engine, agent_role)
            ctx.meta["project_id"] = project_id or ""
            ctx.meta["employee_id"] = employee_id or ""

        if hierarchy is None and project_id:
            hierarchy = _build_hierarchy_manager(Path.home() / ".bandi" / "data", store)
            # 其内部 SQLite 连接（global/work_items），外部传入的不关
            hierarchy_owned = True

        if hierarchy is not None:
            ctx.meta["hierarchy_manager"] = hierarchy

        skill_registry = create_skill_registry()
        skill_team_id = skill_scope["team_id"]
        skill_injector = (
            SkillInjector(
                skill_registry,
                team_id=skill_team_id,
                global_only=not skill_team_id,
                # 绑定技能只参与可见域/目录，不常驻注入正文
                bound_names=_employee_bound_skill_names(employee_id or "") if employee_id else [],
            )
            if skill_registry.get_count() > 0
            else None
        )

        entity_lookup = (
            OntologyEntityLookup(triple_ontology_store)
            if triple_ontology_store is not None
            else None
        )
        if memory is not None:
            from bandi.memory.enricher import EntityGraphEnricher

            memory.enricher = (
                EntityGraphEnricher(entity_lookup, project_id=project_id)
                if entity_lookup is not None
                else None
            )
        pipeline = PerceptionPipeline(
            deps=PerceptionDeps(
                session_store=store,
                entity_lookup=entity_lookup,
                tool_registry=tool_registry,
                prompt_harness=prompt_harness_from_config(bandi_config),
                skill_injector=skill_injector,
                fairness_evaluator=FairnessEvaluator(),
                hierarchy=hierarchy,
                project_id=project_id,
                employee_id=employee_id,
                team_id=skill_team_id,
                group_conversation_id=group_conversation_id,
                dm_conversation_id=dm_conversation_id,
                triple_ontology=triple_ontology_store,
            ),
            context_registry=context_registry,
            context_planner=ContextPlanner(),
        )

        ckpt_store = CheckpointStore()
        loop = AgentLoop(
            max_iterations=bandi_config.agent.max_iterations,
            perception=pipeline,
            checkpoint_store=ckpt_store,
            checkpoint_interval=checkpoint_interval,
            abort_controller=abort_controller,
            session_store=store,
        )

        if session_id:
            _session_restore(
                session_id,
                store,
                ctx,
                checkpoint_store=ckpt_store,
                max_history_messages=(
                    _COMPANY_SESSION_HISTORY_MESSAGES if company_runtime is not None else 0
                ),
            )

        # 2.8：轮计自增已收敛到 AgentLoop.run 主循环入口（所有入口一致生效）；
        # last_active_ts 在本 turn 出口刷新，判定用上次出口值

        if bandi_config.prefetch.enabled and memory is not None:
            from bandi.core.message import message_to_dict
            from bandi.perception.prefetch import (
                MemoryPrefetchProvider,
                PrefetchHandle,
                build_prefetch_query,
            )

            msgs = [message_to_dict(m, protocol=ctx.provider.protocol) for m in ctx.messages]
            provider_impl = MemoryPrefetchProvider(
                memory,
                max_chars=bandi_config.prefetch.max_chars,
                focused_memory=bandi_config.prefetch.focused_memory,
                project_id=project_id,
                employee_id=employee_id,
                conversation_id=group_conversation_id or None,
                dm_conversation_id=dm_conversation_id or None,
            )
            prefetch_handle = PrefetchHandle(
                provider=provider_impl,
                query=build_prefetch_query(msgs),
                session_id=session_id,
                messages=msgs,
            ).start()
            ctx.meta["prefetch_handle"] = prefetch_handle

        with tracer.span(
            "agent_turn",
            attributes={"session_id": session_id, "mode": mode, "turn_index": ctx.turn_index},
        ):
            from bandi.core.event_bus import set_event_context

            _ev_ctx_token = set_event_context(
                session_id=session_id, trace_id=ctx.trace_id, source="agent_turn"
            )
            _register_active_context(session_id, ctx)
            await flush_pending_injection_subs(session_id, ctx.bus)
            try:
                # 置 TIMEOUT 终态（端点面启用随 Day 132/）
                terminal_reason = await loop.run(ctx, session_timeout_sec=session_timeout_sec)
            finally:
                from bandi.core.event_bus import reset_event_context

                reset_event_context(_ev_ctx_token)
                _unregister_active_context(session_id)
                _release_session_slot(session_id, store=store)
        import time

        ctx.compress.last_active_ts = time.time()
        from bandi.observability.write_failures import get_registry

        _persist_or_raise(
            get_registry(), "tool_messages", lambda: _persist_tool_messages(ctx, store, session_id)
        )
        try:
            await _persist_memory(
                ctx,
                memory,
                session_id,
                hierarchy=hierarchy,
                project_id=project_id,
                employee_id=employee_id,
            )
        except Exception as e:  # noqa: BLE001
            if logger_instance is not None:
                logger_instance.warning("memory persist failed: %s", e)
        cf = getattr(ctx, "content_filter", None)
        output_sanitizer = getattr(ctx, "output_sanitizer", None)

        def _guarded(text: str) -> str:
            """功能：对送往用户与落库的文本统一过安全过滤与净化。"""
            out = text
            if cf and out and cf.check(out).blocked:
                out = "内容已被安全护栏拦截"
            if output_sanitizer and out:
                out = output_sanitizer.sanitize(out)
            return out

        reply = strip_generated_citation_block(
            _guarded(ctx.final_message or ctx.current_content or "")
        )
        # 异常终态下发给用户的是系统终止文案，而模型真实输出仍需留存——
        # 两者分别过护栏，避免系统文案进历史、也避免真实输出绕过净化。
        persist_content = strip_generated_citation_block(
            _guarded(_assistant_persist_content(ctx.final_message, ctx.current_content) or "")
        )
        await _emit_terminal_reply_frame(
            stream_handler, session_id, ctx.final_message, ctx.current_content, reply
        )
        if persist_content:
            assistant_msg = Message(
                role=MessageRole.ASSISTANT, content=persist_content, turn_index=ctx.turn_index
            )
            # （loop._flush_messages_before_checkpoint，checkpoint_interval=1 ⇒ 每轮触发）
            # 已把 ctx.messages 里的 assistant 补写进 store，轮末此处再写就产生
            # 同 (turn_index, content) 的第二条 → 历史膨胀 + 完成态耗时按轮聚合失真
            # （UI 显示 0.0s）。去重口径与 loop 侧一致（turn_index + content）。
            if not _assistant_already_persisted(store, session_id, ctx.turn_index, persist_content):
                store.append_message(session_id, assistant_msg)
        elif logger_instance is not None:
            logger_instance.debug("assistant 落库跳过：%s 终态无模型真实输出", ctx.terminal_reason)
        # 中断上下文持久化：用户终止/网络波动属于中断现象，
        # 重试属于**新的一次请求**——但 LLM 需记得中断前的目标与过程。
        # SYSTEM 注记进会话历史，重试轮的上下文装配自然携带（不污染用户可见消息流）。
        if terminal_reason is not None and terminal_reason.name == "USER_INTERRUPT":
            note = (
                "【中断】上一次执行被用户终止。该轮目标尚未完成；"
                "重试时请基于上文已执行的过程继续完成目标，不要重复已完成的步骤。"
            )
            try:
                store.append_message(session_id, Message(role=MessageRole.SYSTEM, content=note))
            except Exception:  # noqa: BLE001 — 注记失败不阻断终态返回
                logger.warning("interrupt note persist failed: session=%s", session_id)
    finally:
        if prefetch_handle is not None:
            # memory.close 后访问 SQLite（静默丢结果）
            cancel_wait = getattr(prefetch_handle, "cancel_and_wait", None)
            if cancel_wait is not None:
                await cancel_wait()
            else:
                prefetch_handle.cancel()
        if hierarchy_owned and hierarchy is not None:
            close_hierarchy = getattr(hierarchy, "close", None)
            if close_hierarchy is not None:
                close_hierarchy()
        # 释放执行槽位必须先于 store.close()——二次释放是「幂等丢弃」兜底
        # 若在 close 后调用会读已关闭连接（sqlite3.ProgrammingError）导致
        # 租约清除失败（异常路径兜底失效）且每次 run 都打 release failed 警告。
        await _stop_lease_keeper(lease_keeper)
        _release_session_slot(session_id, store=store)
        if store_owned:
            store.close()
        await _unsubscribe_handler(bus, bindings)
        if logger_instance is not None:
            logger_instance.close()
        if provider is not None:
            await provider.close()
        # doc_classifier/pdf_research 复用缓存的 provider 在轮末统一释放
        try:
            from bandi.tools.doc._doc_utils import close_lazy_provider

            await close_lazy_provider()
        except Exception:  # noqa: BLE001 — 清理失败不阻断主流程
            pass
        if ckpt_store is not None:
            ckpt_store.close()
        # 关闭本次新建的记忆连接（long_term SQLite）——
        # memory 由调用方传入时不关闭（跨轮复用，所有权归调用方）
        if memory is not None and memory_manager is None:
            closer = getattr(memory, "close", None)
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        if ctx is not None:
            bt = getattr(ctx, "badcase_tracker", None)
            if bt is not None and hasattr(bt, "close"):
                bt.close()
        # 整个关闭（persist 写线程/文件句柄泄漏）。fallback 兼容仅有 close 的 fake bus。
        if bus is not None:
            disable_persistence = getattr(bus, "disable_persistence", None)
            if callable(disable_persistence):
                # 功能：清理异常（磁盘/文件句柄）不得替换 finally 中的原始异常
                try:
                    disable_persistence()
                except Exception as e:  # noqa: BLE001 — 兜底清理失败只告警
                    logger.warning("bus disable_persistence failed on cleanup: %s", e)
            else:
                close_bus = getattr(bus, "close", None)
                if close_bus is not None:
                    await close_bus()

        # 成本台账是后台线程异步落库，不刷则本轮终帧到达时 cost-detail 仍读不到
        # 本轮 token——前端右栏「Token 用量」会停在上一轮的数，切会话才刷新
        try:
            from bandi.observability.cost_store import get_shared_cost_store

            get_shared_cost_store(default_cost_db_path()).flush()
        except Exception:
            logger.warning("cost store flush failed on turn end", exc_info=True)

    return AgentTurnResult(
        reply=reply,
        terminal_reason=terminal_reason.name if terminal_reason else None,
        total_tokens=ctx.total_tokens,
        turn_index=ctx.turn_index,
        session_id=session_id,
        approval_tool_call_id=str(ctx.meta.get("approval_pending_tool_call_id") or ""),
        approval_tool_name=str(ctx.meta.get("approval_pending_tool_name") or ""),
    )
