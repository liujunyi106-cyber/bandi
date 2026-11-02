# 功能：AgentLoop 主循环（感知注入 + 状态机驱动 + 安全组件接线）
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bandi.agent.base import ExecutionContext, RecoveryAction, TerminalReason
from bandi.agent.deps import get_query_deps
from bandi.core.abort_controller import AbortController
from bandi.core.config import AgentConfig
from bandi.core.message import Message, MessageRole
from bandi.observability.context import bind_log_context, set_log_context
from bandi.utils.errors import error_text

if TYPE_CHECKING:
    from bandi.agent.checkpoint import CheckpointStore
    from bandi.safety.content_filter import ContentFilter
    from bandi.safety.output_sanitizer import OutputSanitizer
    from bandi.safety.pii_detector import PIIDetector
    from bandi.safety.prompt_guard import PromptGuard
from bandi.agent.states import (
    RECOVERY_SAFE_READONLY_TOOLS,
    on_compressing,
    on_executing,
    on_init,
    on_observing,
    on_parsing,
    on_reflecting,
    on_thinking,
    readonly_tool_kind,
)
from bandi.agent.states import (
    RECOVERY_UNSAFE_READONLY_TOOLS as RECOVERY_UNSAFE_READONLY_TOOLS,
)
from bandi.agent.transitions import build_default_machine
from bandi.core.events import Event, EventType

logger = logging.getLogger(__name__)

TOOL_OUTCOME_UNKNOWN_TEXT = "该工具可能已执行，结果未知，请先核实外部状态或询问用户，不要盲目重试。"

# 功能：连续多少次同一工具失败后插入改路提示——3 次足以说明当前参数形状不被接受。
_REPEATED_FAILURE_THRESHOLD = 3

REPEATED_FAILURE_TEXT = (
    "[系统提示] 工具 {tool} 已连续失败 {times} 次，继续用同样的方式调用只会继续失败。\n"
    "请不要再重复调用它：改用其他工具、换一条路径，或基于已有信息直接回答用户。"
)


def _persist_plan_file(ctx: ExecutionContext) -> None:
    # 功能：plan 模式终态落盘计划文件——台账渲染写入
    # `~/.bandi/plans/plan-<session8>.md`（同会话覆盖；非 plan/无台账/写盘失败静默跳过）
    try:
        if str(ctx.meta.get("mode") or "") != "plan":
            return
        ledger = ctx.meta.get("task_ledger")
        items = list(getattr(ledger, "items", None) or [])
        if not items:
            return
        from bandi.tools.task_ledger import render_todos

        session_id = str(ctx.meta.get("session_id") or ctx.session_id or "default")
        plan_dir = Path.home() / ".bandi" / "plans"
        plan_dir.mkdir(parents=True, exist_ok=True)
        target = plan_dir / f"plan-{session_id[:8]}.md"
        content = (
            "# 执行计划（plan 模式）\n\n"
            f"- 会话：{session_id}\n"
            f"- 生成时间：{datetime.now(UTC).isoformat()}\n\n"
            f"{render_todos(items)}\n"
        )
        target.write_text(content, encoding="utf-8")
        logger.info("plan file persisted: %s", target)
    except Exception as e:  # noqa: BLE001 — 产物落盘不得阻断主链
        logger.debug("plan file persist failed: %s", e)


# 共用单一来源）；此处 re-export 供既有调用方/契约导入。


def _is_replay_safe_readonly(name: str, registry: Any) -> bool:
    """功能：恢复重放安全判定——只读工具才安全（fail-closed：未声明/查询不到
    → 视为副作用）。registry 元数据（is_read_only）与 executor 去重同源的
    只读维度一致；注意与 executor 去重口径并非全等：executor 去重资格是
    is_read_only or is_idempotent 且 dedup_scope != none，本判定只看 is_read_only
    （幂等写工具重放仍会真实执行副作用，不得视为安全）。无 registry 元数据时回落
    显式只读集合；元数据只读但恢复不安全的切模式工具（enter/exit_plan_mode）
    优先判不安全。"""
    kind = readonly_tool_kind(name, registry)
    if kind is not None:
        return kind
    return name in RECOVERY_SAFE_READONLY_TOOLS


def partition_recovery_tool_calls(
    calls: list[Any] | None,
    *,
    dispatched_ids: set[str] | None = None,
    executed_ids: set[str] | None = None,
    registry: Any = None,
) -> tuple[list[Any], list[Any]]:
    """功能：拆分恢复时的挂起工具调用——(可安全重放, 需合成未知结果)。

    判定（全部 fail-closed）：
    ⓪ **已有结果（`executed_ids`）→ 既不重放也不标 unknown**——按批次粗判会把这批
       里**已经执行完**的调用也归入 unknown，与实际状态不符（复核 🟡-9）；
    ① 只读调用 → 可重放（无副作用；口径 = registry is_read_only 且不在恢复
       不安全子集，与 executor 批内去重「is_read_only or is_idempotent」口径
       不同——幂等写工具重放仍有真实副作用，不得安全重放）；
    ② 副作用调用：派发记录含该 id → 已启动、结果未知 → unknown；派发记录存在且
       不含该 id（明确未启动）→ 可重放（首次真实执行，保持恢复续跑语义）；
       **派发记录缺失（旧格式快照 / provenance 未知）→ 保守判 unknown**
       （不得把「没记录」当「没启动」——执行中崩溃的旧快照会因此重放副作用）；
       空 id 无法匹配派发记录 → unknown（无法证明未启动，不重放）。
    """
    replayable: list[Any] = []
    unknown: list[Any] = []
    done = executed_ids or set()
    for call in calls or []:
        call_id = str(getattr(call, "id", "") or "")
        if call_id and call_id in done:
            continue
        name = str(getattr(call, "name", "") or "")
        if _is_replay_safe_readonly(name, registry):
            replayable.append(call)
            continue
        if not call_id:
            unknown.append(call)
            continue
        if dispatched_ids is None or call_id in dispatched_ids:
            unknown.append(call)
            continue
        replayable.append(call)
    return replayable, unknown


def executed_tool_call_ids(ctx: Any) -> set[str]:
    """功能：从上下文收集**本轮已有结果**的工具调用 id（本批结果 + 本轮落库 TOOL 消息）。

    两块都要看：`tool_results` 装本轮刚算完、尚未落库的结果；`messages` 装
    checkpoint 恢复回来的历史 TOOL 消息。历史 TOOL 消息只认当前轮
    （`turn_index == ctx.turn_index`）：Google 适配器每轮 `fc_{name}` 计数重置会
    跨轮复用同 id，收全历史会把旧轮结果误当本轮已执行（恢复时既不重放也不注入
    unknown，静默跳过）。`tool_results` 由 loop 在单轮内使用，视作当前轮。
    """
    ids: set[str] = set()
    for result in getattr(ctx, "tool_results", None) or []:
        call_id = str(getattr(result, "tool_call_id", "") or "")
        if call_id:
            ids.add(call_id)
    current_turn = int(getattr(ctx, "turn_index", 0) or 0)
    for msg in getattr(ctx, "messages", None) or []:
        if getattr(msg, "role", None) != MessageRole.TOOL:
            continue
        msg_turn = getattr(msg, "turn_index", None)
        # 🟢-3：轮次缺失/None 的历史 TOOL 消息无法证明属于本轮——跳过，不得
        # `or 0` 兜底到首轮（turn_index==0 时会把跨轮同 id 结果误当本轮已执行）
        if msg_turn is None or int(msg_turn) != current_turn:
            continue
        call_id = str(getattr(msg, "tool_call_id", "") or "")
        if call_id:
            ids.add(call_id)
    return ids


def _take_pending_injection(ctx: Any) -> str | None:
    """功能：取走待消费注入文本——优先 ctx 原子方法（take_injected_input，锁内
    取走并清空，并发 push 不丢）；无该方法的桩 ctx 回落字段读取（契约桩兼容）。"""
    taker = getattr(ctx, "take_injected_input", None)
    if callable(taker):
        return taker()
    text = getattr(ctx, "injected_input", None)
    if text is None or getattr(ctx, "_injected_consumed", False):
        return None
    ctx.injected_input = None
    ctx._injected_consumed = True
    return text


def _steer_invalidation_note() -> str:
    """功能：转向声明——需求中途修改时旧的冲突约束失效（投影「最新指令」节引用）。"""
    return "用户已在中途修改需求：此前与最新一条用户指令冲突的旧约束一律作废，以最新指令为准。"


def _mark_dispatched_tool_calls(ctx: ExecutionContext) -> None:
    """功能：执行前派发记录——本批工具调用 id 并集写入 meta（跨 checkpoint
    序列化），供恢复分区区分「已启动结果未知」与「未启动可重放」。必须在
    executing 快照之前调用：快照先于任何工具执行是恢复不重放副作用的充分条件。"""
    dispatched = set(ctx.meta.get("dispatched_tool_call_ids") or set())
    dispatched.update(
        str(getattr(tc, "id", "") or "") for tc in (ctx.current_tool_calls or []) if tc is not None
    )
    ctx.meta["dispatched_tool_call_ids"] = {i for i in dispatched if i}


def _assistant_tool_segment_end(messages: list[Any], call_id: str) -> int | None:
    """功能：定位声明 call_id 的 assistant 消息之后「连续 TOOL 段」的插入位——
    与 tool_message_guard 的 stub 插位；找不到返回 None（调用方回落 append）。"""
    for idx, msg in enumerate(messages):
        calls = getattr(msg, "tool_calls", None) or []
        if msg.role != MessageRole.ASSISTANT or not calls:
            continue
        if not any(str(getattr(tc, "id", "") or "") == call_id for tc in calls):
            continue
        end = idx + 1
        while end < len(messages) and messages[end].role == MessageRole.TOOL:
            end += 1
        return end
    return None


class AgentLoop:  # 功能：Agent 主循环（状态机驱动的思考→工具执行→观察→反思的迭代流程）
    def __init__(
        self,
        max_iterations: int | None = None,
        prompt_guard: PromptGuard | None = None,
        content_filter: ContentFilter | None = None,
        pii_detector: PIIDetector | None = None,
        output_sanitizer: OutputSanitizer | None = None,
        perception: object | None = None,
        checkpoint_store: CheckpointStore | None = None,
        checkpoint_interval: int = 1,
        abort_controller: AbortController | None = None,
        session_store: Any = None,
    ):
        self.state_machine = build_default_machine()
        # 有效哨兵，统一由 run() 消费点做唯一解析（车由 run() 一处担责，三装配
        # 点 + 直接库调用行为一致，见 run() 注释）
        self._max_iterations = max_iterations
        self._handlers: dict[str, Callable] = {
            "init": on_init,
            "thinking": on_thinking,
            "parsing": on_parsing,
            "executing": on_executing,
            "observing": on_observing,
            "reflecting": on_reflecting,
            "compressing": on_compressing,
        }
        self._perception = perception
        self._prompt_guard = prompt_guard
        self._content_filter = content_filter
        self._pii_detector = pii_detector
        self._output_sanitizer = output_sanitizer
        self._checkpoint_store = checkpoint_store
        self._session_store = session_store
        self._checkpoint_interval = (
            checkpoint_interval if checkpoint_interval and checkpoint_interval > 0 else 0
        )
        self._abort_controller = abort_controller
        self._session_timeout_sec = 0

    def _wire_safety(self, ctx: ExecutionContext) -> None:
        if ctx.prompt_guard is not None:
            return
        from bandi.safety import create_safety_modules

        bandi_config = ctx.meta.get("bandi_config")
        if bandi_config is None:
            from bandi.core.config import BandiConfig

            bandi_config = BandiConfig.load()
        if not bandi_config.safety.enabled:
            # 三护栏无条件装配；下游消费点均已判空，None 安全）
            return
        guard, content, pii, sanitizer = create_safety_modules(bandi_config, ctx.bus)
        ctx.prompt_guard = guard
        ctx.content_filter = content
        ctx.pii_detector = pii
        ctx.output_sanitizer = sanitizer

    async def _run_perception(
        self, ctx: ExecutionContext
    ) -> None:  # 功能：可选感知层，处理 raw_input 并注入上下文
        if self._perception is None or not ctx.raw_input:
            return
        if (
            ctx.messages
            and ctx.messages[0].role == MessageRole.SYSTEM
            and not ctx.meta.get("restored_summary")
        ):
            # system 已由历史恢复携带指令覆盖语义，避免 plan 前缀写 execute 语义漂移
            await self._inject_mode_directive(ctx)
            await self._inject_frame_state(ctx)
            _notes = str(ctx.meta.get("attachment_notes") or "")
            if _notes and not ctx.meta.get("attachment_notes_applied"):
                for _m in reversed(ctx.messages):
                    _role = _m.role.value if isinstance(_m.role, MessageRole) else _m.role
                    if _role == "user":
                        _m.content = f"{_m.content or ''}\n\n{_notes}"
                        break
                ctx.meta["attachment_notes_applied"] = True
            return
        # 功能：工具面文本与 tools 参数同口径——用相关性裁剪后的 active tools；
        # 空列表回退 None（空列表会裁成空工具面并断掉增量基线）
        from bandi.tools.selection import schema_name as _schema_name

        _surface_tools = [
            _schema_name(s) for s in (ctx.active_tools or []) if _schema_name(s)
        ] or None
        result = await self._perception.process(
            ctx.raw_input,
            session_id=ctx.meta.get("session_id"),
            session_mode=str(ctx.meta.get("mode", "execute")),
            surface_tools=_surface_tools,
        )
        if not result.messages:
            return
        if ctx.messages:
            system_messages = [m for m in result.messages if m.role == MessageRole.SYSTEM]
            # 功能：易变注入（记忆召回等）随 user 侧下发——合并时必须保留，
            # 否则有历史的会话只剩 system，注入内容整体丢失
            injected = [
                m
                for m in result.messages
                if m.role != MessageRole.SYSTEM
                and bool((getattr(m, "meta", None) or {}).get("injected_context"))
            ]
            merged = [
                m
                for m in ctx.messages
                if not (getattr(m, "meta", None) or {}).get("injected_context")
            ]
            merged = system_messages + merged
            if injected:
                insert_at = len(merged)
                for index in range(len(merged) - 1, -1, -1):
                    if merged[index].role == MessageRole.USER:
                        insert_at = index
                        break
                merged[insert_at:insert_at] = injected
            ctx.messages = merged
        else:
            ctx.messages = result.messages
        ctx.meta["perception"] = result
        _notes = str(ctx.meta.get("attachment_notes") or "")
        if _notes and not ctx.meta.get("attachment_notes_applied"):
            for _m in reversed(ctx.messages):
                _role = _m.role.value if isinstance(_m.role, MessageRole) else _m.role
                if _role == "user":
                    _m.content = f"{_m.content or ''}\n\n{_notes}"
                    break
            ctx.meta["attachment_notes_applied"] = True
        # ctx.meta["mode"] 仅在无 manager 时回退 result.mode
        mode_manager = ctx.meta.get("mode_manager")
        if mode_manager is not None:
            ctx.meta["mode"] = mode_manager.mode
        elif "mode" not in ctx.meta:
            ctx.meta["mode"] = getattr(result, "mode", "execute")
        await self._inject_frame_state(ctx)
        await self._emit_perception_analysis(ctx, result)

    async def _inject_mode_directive(self, ctx: ExecutionContext) -> None:
        # 功能：B5a 1.7 mode 切换增量注入（组装 system 之后、skill directive 同区）
        from bandi.core.message import Message, MessageRole

        mode_manager = ctx.meta.get("mode_manager")
        mode = getattr(mode_manager, "mode", "") if mode_manager is not None else ""
        if not mode or mode == ctx.meta.get("_injected_mode"):
            return
        ctx.meta["_injected_mode"] = mode
        label = self._MODE_LABELS.get(mode, mode)
        directive = Message(
            role=MessageRole.SYSTEM,
            content=(
                f"{self._MODE_SWITCH_PREFIX} 当前模式：{label}。"
                "若与历史指令（含首条 system 中的旧模式说明）冲突，以本条为准。"
            ),
        )
        insert_at = len(ctx.messages)
        for i, m in enumerate(ctx.messages):
            if m.role != MessageRole.SYSTEM or (m.content or "").startswith(
                self._MODE_SWITCH_PREFIX
            ):
                insert_at = i
                break
        ctx.messages.insert(insert_at, directive)

    _MODE_SWITCH_PREFIX = "[模式切换]"
    _MODE_LABELS: dict[str, str] = {
        "plan": "plan（只读规划，禁止写操作与执行变更）",
        "execute": "execute（全功能执行）",
    }

    _FRAME_STATE_PREFIX = "[SOP 帧状态]"

    async def _inject_frame_state(self, ctx: ExecutionContext) -> None:
        # 功能：——SOP 帧状态注入（agent 主循环消费帧引擎状态：当前节点/
        # 待收集字段/最近提问），纯读不推进；帧文件缺失/损坏不阻断主链。
        # 去重键为渲染内容 + 消息存在性检查（消息被压缩裁掉后下轮自然重注入）。
        # 会话 id 含 '::' 不合法，派生与组织层投影（seat_executor）同源同 scope。
        scope = str(ctx.meta.get("frame_scope") or "")
        if not scope:
            session_id = str(ctx.meta.get("session_id") or "")
            if not session_id:
                return
            from bandi.skills.progress import frame_scope_for_session

            scope = frame_scope_for_session(session_id)
        from bandi.core.message import Message, MessageRole
        from bandi.skills.frame_store import (
            DEFAULT_FRAMES_DIR,
            SkillFrameLoadError,
            load_frames,
        )
        from bandi.skills.runtime import render_active_frame

        frames_dir = str(ctx.meta.get("frames_dir") or DEFAULT_FRAMES_DIR)
        try:
            stack = load_frames(scope, frames_dir)
        except SkillFrameLoadError:
            logger.warning("frame state load failed (scope=%s); skipped", scope)
            return
        content = render_active_frame(stack) if stack is not None else None
        if not content:
            return
        existing = [
            m
            for m in ctx.messages
            if m.role == MessageRole.SYSTEM
            and (m.content or "").startswith(self._FRAME_STATE_PREFIX)
        ]
        if ctx.meta.get("_injected_frame_state") == content and existing:
            return
        ctx.meta["_injected_frame_state"] = content
        # 状态变化时替换旧帧状态消息（恒单条不膨胀）；id 身份比较防同内容误删
        drop_ids = {id(m) for m in existing}
        ctx.messages[:] = [m for m in ctx.messages if id(m) not in drop_ids]
        directive = Message(role=MessageRole.SYSTEM, content=content)
        insert_at = len(ctx.messages)
        for i, m in enumerate(ctx.messages):
            if m.role != MessageRole.SYSTEM or (m.content or "").startswith(
                self._MODE_SWITCH_PREFIX
            ):
                insert_at = i
                break
        ctx.messages.insert(insert_at, directive)

    async def _emit_perception_analysis(self, ctx: ExecutionContext, result: object) -> None:
        from bandi.core.events import Event, EventType

        if ctx.bus is None:
            return
        fairness = getattr(result, "fairness_report", None)
        # intent_hint / domain_route 只作观测、不驱动路由，但必须**落盘**——
        # 评测的「意图命中率 / domain 精度」靠 events 对照用例标注集算，
        # 不落盘这两项就没有基准可比。
        domain_route = getattr(result, "domain_route", None)
        await ctx.bus.emit(
            Event(
                type=EventType.PERCEPTION_ANALYZED,
                data={
                    "session_id": ctx.meta.get("session_id"),
                    "fairness_score": fairness.score if fairness is not None else None,
                    "fairness_issue_count": len(fairness.issues) if fairness is not None else 0,
                    "entities_referenced": getattr(result, "entities_referenced", []),
                    "estimated_input_tokens": getattr(result, "estimated_input_tokens", 0),
                    "mode": getattr(result, "mode", ""),
                    "intent_hint": getattr(result, "intent_hint", ""),
                    "domain_key": getattr(domain_route, "domain_key", "") if domain_route else "",
                    "domain_confidence": (
                        getattr(domain_route, "confidence", 0.0) if domain_route else 0.0
                    ),
                    "domain_action": (getattr(domain_route, "action", "") if domain_route else ""),
                },
            )
        )

    @staticmethod
    def _inject_unknown_tool_outcomes(ctx: ExecutionContext, calls: list[Any]) -> None:
        """功能：为待定副作用调用合成「结果未知」TOOL 消息（同 id 幂等，不重复注入）。

        插位对齐 tool_message_guard 的 stub 语义——紧跟前置 assistant 的 tool
        段末尾；USER 插队后 append 到列表尾会被请求面 guard 判孤儿丢弃（文案消失）。
        """
        from bandi.core.message import Message

        existing_ids = {m.tool_call_id for m in ctx.messages if m.role == MessageRole.TOOL}
        for call in calls:
            call_id = str(getattr(call, "id", "") or "")
            if not call_id:
                logger.warning(
                    "unknown-outcome injection skipped: empty tool_call_id (tool=%s)",
                    getattr(call, "name", "?"),
                )
                continue
            if call_id in existing_ids:
                continue
            msg = Message(
                role=MessageRole.TOOL,
                content=TOOL_OUTCOME_UNKNOWN_TEXT,
                tool_call_id=call_id,
                name=str(getattr(call, "name", "") or ""),
                turn_index=ctx.turn_index,
                meta={"tool_error": True, "outcome_unknown": True},
            )
            insert_at = _assistant_tool_segment_end(ctx.messages, call_id)
            if insert_at is None:
                ctx.messages.append(msg)
            else:
                ctx.messages.insert(insert_at, msg)
            existing_ids.add(call_id)

    async def run(
        self, ctx: ExecutionContext, session_timeout_sec: int = 0
    ) -> TerminalReason:  # 功能：启动 Agent 主循环，按状态机流转直到 TERMINATED
        # 一级：self 无效（None/0/负/非 int）→ 回落 ctx.config（实例配置是
        # 用户显式意图，0/None 经 `or` 天然落此，负数须显式兜）
        # 二级：ctx.config 也无效 → 兜底类默认（恒正，杜绝 0 立断/负数穿透）
        # 钳制收敛在消费点：构造与装配点一律直传，全路径行为一致。
        max_turns = self._max_iterations
        if not isinstance(max_turns, int) or max_turns <= 0:
            max_turns = ctx.config.max_iterations
        if not isinstance(max_turns, int) or max_turns <= 0:
            max_turns = AgentConfig().max_iterations
        turns_taken = 0

        self._wire_safety(ctx)
        if self._abort_controller is not None:
            ctx.meta["abort_controller"] = self._abort_controller
        try:
            # 不重跑；有挂起工具调用则从 续跑（跳过感知层）；带新输入
            # 的恢复清除上一轮终止标记后从 INIT 正常启动（快照中的 COMPLETED 属于上一轮）
            if ctx.terminal_reason is not None and not ctx.raw_input and not ctx.current_tool_calls:
                return ctx.terminal_reason
            # 2.8：轮计自增收敛到主循环入口——直接调 AgentLoop.run 的路径（subagent/
            ctx.compress.turns_since_maintenance += 1
            # 对话时，恢复的旧 current_tool_calls 会无视新输入直接 executing 重放
            if ctx.current_tool_calls and not ctx.raw_input:
                # 未记录派发（未启动）的调用可安全重放；只读工具仍走安全重试；全部落入
                # unknown 时回 INIT 交模型重新决策。
                replayable, unknown = partition_recovery_tool_calls(
                    ctx.current_tool_calls,
                    dispatched_ids=ctx.meta.get("dispatched_tool_call_ids"),
                    # 🟡-9：已有结果的调用不参与重放/未知判定——按批次粗判会把
                    executed_ids=executed_tool_call_ids(ctx),
                    registry=getattr(getattr(ctx, "tool_executor", None), "registry", None),
                )
                if unknown:
                    self._inject_unknown_tool_outcomes(ctx, unknown)
                ctx.current_tool_calls = replayable or None
                if ctx.current_tool_calls:
                    ctx.current_state = "executing"
                else:
                    if ctx.terminal_reason is not None:
                        ctx.terminal_reason = None
                    ctx.current_state = "init"
                    await self._run_perception(ctx)
            else:
                if ctx.terminal_reason is not None:
                    ctx.terminal_reason = None
                ctx.current_tool_calls = None
                ctx.current_state = "init"
                await self._run_perception(ctx)

            loop_coro = self._run_loop(ctx, max_turns, turns_taken)
            if session_timeout_sec > 0:
                self._session_timeout_sec = session_timeout_sec
                try:
                    return await asyncio.wait_for(loop_coro, timeout=session_timeout_sec)
                except TimeoutError:
                    self._on_timeout(ctx, turns_taken)
                    await ctx.bus.emit(
                        Event(
                            type=EventType.TERMINATED,
                            data={
                                "terminal_reason": "TIMEOUT",
                                "turn_index": ctx.turn_index,
                            },
                        )
                    )
                    return TerminalReason.TIMEOUT
            return await loop_coro
        finally:
            # 不得随 loop 退出静默丢失（abort 检查先于消费点的竞态窗口）
            self._requeue_pending_injection(ctx)

    def _requeue_pending_injection(self, ctx: ExecutionContext) -> None:
        """功能：终态兜底——退出前把 ctx 中未消费的注入文本回退 steer 队列。

        文本已被 push 成功写入但 loop 未及消费（abort/超时/异常先到）时，若不回退
        将随 ctx 生命周期结束静默丢失；回退后由下次会话批边界 drain 重新投递。
        回退走 `queue_only` 直入队——ctx 正在退出，实时注入通道只会写进
        死 ctx；也不再为规避该通道把终态改成 MODEL_ERROR（终态语义保持原值）。
        """
        text = _take_pending_injection(ctx)
        if not text:
            return
        # 🟢-C：回队即脱离本 ctx——清注入痕迹，防未消费轮 episodic importance +0.1
        ctx.injected_text = None
        from bandi.agent.steer import get_default_registry

        session_id = str(ctx.session_id or ctx.meta.get("session_id") or "")
        registry = ctx.meta.get("steer_registry") or get_default_registry()
        if not session_id or registry is None:
            logger.warning(
                "pending injected input dropped at loop exit (no session/registry): %.80s", text
            )
            return
        try:
            registry.push(session_id, text, queue_only=True)
        except Exception as e:  # noqa: BLE001 — 回退失败不改变已确定的退出结果
            # warning，运维无法区分「已回队」与「静默丢失」）
            ctx.meta["injection_requeue_failures"] = (
                int(ctx.meta.get("injection_requeue_failures", 0) or 0) + 1
            )
            logger.error(
                "pending injected input requeue failed (session=%s len=%d): %.80s | %s",
                session_id,
                len(text),
                text,
                e,
            )
        else:
            logger.warning("injected input requeued at loop exit (unconsumed): %.80s", text)

    def _persist_checkpoint(self, ctx: ExecutionContext, turns_taken: int) -> bool:
        """功能：L-2a 常规保存点——按间隔条件写 checkpoint（含压缩上下文）
        + session_entries 标记；RETRY 分支与正常路径共用。"""
        if not (self._checkpoint_store and self._checkpoint_interval > 0 and turns_taken > 0):
            return False
        if not (turns_taken == 1 or turns_taken % self._checkpoint_interval == 0):
            return False
        return self._write_checkpoint(ctx)

    def _persist_checkpoint_now(self, ctx: ExecutionContext, turns_taken: int) -> bool:
        """
        无条件落盘，不受 interval 门控（副作用安全不受采样率支配）。
        turns_taken 可为 0（注入可发生于首迭代前），只要求 store 就绪。"""
        if not self._checkpoint_store:
            return False
        return self._write_checkpoint(ctx)

    def _write_checkpoint(self, ctx: ExecutionContext) -> bool:
        """功能：checkpoint 写入体（常规/强制两点共用）——快照序列化 + 落库
        + session_entries 标记 + C2b command_id（事件溯源对齐）。
        屏障：先 flush 未落库消息（flush 屏障语义），再写 checkpoint。"""
        from bandi.agent.checkpoint import Checkpoint

        self._flush_messages_before_checkpoint(ctx)
        command_id = get_query_deps(ctx).new_id()
        # 保护机制不得反向成为单点故障：序列化/落库失败 best-effort（告警 +
        # 返回 False），与相邻的 flush 屏障同为容错口径，不裸抛毁掉整轮
        try:
            qs = ctx.to_query_state()
            state_str = json.dumps(dataclasses.asdict(qs), ensure_ascii=False)
            self._checkpoint_store.save(
                Checkpoint(
                    id=get_query_deps(ctx).new_id(),
                    session_id=ctx.meta.get("session_id", ""),
                    # 排序键用会话累计 turn_index（与 state_json 一致）——保证
                    # 恢复点总是会话内最新状态
                    turn_index=ctx.turn_index,
                    state_json=state_str,
                    # session store，恢复链只读 state_json；列保留兼容旧库 schema
                    messages_json="",
                    command_id=command_id,
                )
            )
        except Exception as e:  # noqa: BLE001 — checkpoint 写失败不阻断主链
            logger.warning("checkpoint save failed (best-effort): %s", e)
            return False
        # C2b：command_id 进事件上下文——后续事件（STATE_CHANGED/TERMINATED
        # 等）自动携带，checkpoint 与事件链共享同一幂等键
        from bandi.core.event_bus import set_event_context

        set_event_context(command_id=command_id)
        if self._session_store is not None:
            try:
                self._session_store.save_checkpoint_entry(
                    ctx.meta.get("session_id", ""),
                    turn_index=ctx.turn_index,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("checkpoint entry mark failed (best-effort): %s", e)
        return True

    def _flush_messages_before_checkpoint(self, ctx: ExecutionContext) -> None:
        """功能（屏障语义 flush 屏障）：checkpoint 的
                恢复点语义 =「此刻全部消息已持久化」——写 checkpoint 前把 ctx.messages 中
                尚未落库的工具/助手消息补写 session store（tool_call_id / 轮次+内容幂等）
                否则恢复时 messages 缺尾而 state 标量超前（两库分叉窗口）。
        best-effort：失败仅告警不阻断 checkpoint。"""
        store = self._session_store
        session_id = ctx.meta.get("session_id", "")
        if store is None or not session_id:
            return
        try:
            # 去重需覆盖 boundary 之前的原始历史（防 preserved segment 异常时重复落库）
            entries = store.get_branch(session_id, full=True)
            seen_tools = {
                e.get("tool_call_id")
                for e in entries
                if e.get("entry_type") == "message" and e.get("role") == "tool"
            }
            seen_assistant = {
                (e.get("turn_index"), e.get("content") or "")
                for e in entries
                if e.get("entry_type") == "message" and e.get("role") == "assistant"
            }
            for msg in ctx.messages:
                if msg.role == MessageRole.TOOL and msg.tool_call_id:
                    if msg.tool_call_id not in seen_tools:
                        store.append_message(session_id, msg)
                        seen_tools.add(msg.tool_call_id)
                elif msg.role == MessageRole.ASSISTANT:
                    key = (msg.turn_index, msg.content or "")
                    if key not in seen_assistant:
                        store.append_message(session_id, msg)
                        seen_assistant.add(key)
        except Exception as e:  # noqa: BLE001 — 屏障 best-effort 不阻断主链
            logger.warning("checkpoint message flush failed (best-effort): %s", e)

    def _on_timeout(self, ctx: ExecutionContext, turns_taken: int) -> None:
        """功能：超时终止分支——终态落盘（timeout 不落盘曾丢失进度）+ 发事件。"""
        ctx.terminal_reason = TerminalReason.TIMEOUT
        ctx.final_message = f"会话超时（{self._session_timeout_sec} 秒），已停止本轮。"
        self._persist_checkpoint_now(ctx, turns_taken)

    def _persist_injected_message(self, ctx: ExecutionContext, msg: Any) -> None:
        """功能：注入 USER 消息同步落 session store（恢复链唯一消息来源）。

        幂等：同 session+turn+content 已存在即跳过（崩溃重放/重复消费不双写）；
        store 缺失（未接持久化的库用法）/ 写失败一律静默跳过，不阻断转向消费。
        """
        store = ctx.meta.get("session_store") or self._session_store
        session_id = str(ctx.meta.get("session_id") or ctx.session_id or "")
        if store is None or not session_id:
            return
        try:
            entries = store.get_branch(session_id, full=True)
            turn = int(getattr(msg, "turn_index", 0) or 0)
            content = msg.content or ""
            for entry in entries:
                if (
                    entry.get("entry_type") == "message"
                    and entry.get("role") == "user"
                    and int(entry.get("turn_index") or 0) == turn
                    and (entry.get("content") or "") == content
                ):
                    return
            store.append_message(session_id, msg)
        except Exception as e:  # noqa: BLE001 — 落库 best-effort 不阻断转向消费
            logger.warning("injected message persist skipped (best-effort): %s", e)

    def _inject_retrieval_stall_notice(self, ctx: ExecutionContext) -> None:
        """功能：检索空转时插一句「停止检索、直接回答」的指令。

        插在每次 thinking 之前：模型连换关键词重查时结果不会变，耗尽
        max_iterations 就该轮没有最终回复。这里不终止，只提示——检索本来就
        合法，何时该收手由模型看完已有信息判断。
        """
        rounds = int(ctx.meta.get("_retrieval_stall_rounds", 0) or 0)
        if rounds < 1:
            self._inject_repeated_failure_notice(ctx)
            return
        from bandi.agent.states import RETRIEVAL_STALL_TEXT

        if self._push_notice(ctx, RETRIEVAL_STALL_TEXT.format(rounds=rounds)):
            return
        self._inject_repeated_failure_notice(ctx)

    def _inject_turn_budget_notice(
        self, ctx: ExecutionContext, max_turns: int, turns_taken: int
    ) -> None:
        """功能：轮次将尽时提示收尾——剩余 ≤2 轮必须直接输出最终正文。

        员工长任务（几十轮工具往返）在耗尽轮次时本轮没有最终回复，卡被判定
        空输出失败；收尾提示让模型把已有证据收敛成交付正文。
        """
        remaining = max_turns - turns_taken
        if remaining < 1 or remaining > 2:
            return
        from bandi.agent.states import TURN_BUDGET_TEXT

        self._push_notice(ctx, TURN_BUDGET_TEXT.format(remaining=remaining))

    def _inject_repeated_failure_notice(self, ctx: ExecutionContext) -> None:
        """功能：同一工具反复失败时提示改路——别把轮次耗在无效重试上。

        典型现场：参数校验类错误下模型会不断换参数形状重调同一个工具，直到
        耗尽 max_iterations，该轮就永远没有最终回复。这里按「本轮同一工具的
        连续失败次数」提示改用其他工具或直接作答。
        """
        results = list(getattr(ctx, "tool_results", None) or [])
        if len(results) < _REPEATED_FAILURE_THRESHOLD:
            return
        tail = results[-_REPEATED_FAILURE_THRESHOLD:]
        if any(getattr(r, "success", False) for r in tail):
            return
        names = {str(getattr(r, "tool_name", "") or "") for r in tail}
        if len(names) != 1:
            return
        name = names.pop()
        if not name:
            return
        self._push_notice(
            ctx,
            REPEATED_FAILURE_TEXT.format(tool=name, times=_REPEATED_FAILURE_THRESHOLD),
        )

    def _push_notice(self, ctx: ExecutionContext, notice: str) -> bool:
        """功能：把提示插到最近一条 system 之后；同一条已插过则不再重复插。"""
        insert_at = len(ctx.messages)
        for idx in range(len(ctx.messages) - 1, -1, -1):
            if ctx.messages[idx].role == MessageRole.SYSTEM:
                if ctx.messages[idx].content == notice:
                    return False
                insert_at = idx + 1
                break
        ctx.messages.insert(
            insert_at,
            Message(role=MessageRole.SYSTEM, content=notice, turn_index=ctx.turn_index + 1),
        )
        return True

    async def _consume_injected_input(self, ctx: ExecutionContext, turns_taken: int) -> bool:
        """功能：实时转向消费——原子取走注入文本并作为 USER 消息追加当前轮。

        取走/清空在 ctx 注入锁内完成（并发 push 不丢、不双读）；E2b：新输入
        带来新方向，只读循环计数清零（防更快误杀）。
        """
        injected = _take_pending_injection(ctx)
        if injected is None:
            return False
        from bandi.core.message import Message, MessageRole

        # 首轮 init + 注入：消费点直接置 thinking，on_init 的 hooks（SessionStart/
        # 不在消费点补跑 init 侧钩子（补跑会与 on_init 的终止/安全前置语义打架）。
        user_msg = Message(role=MessageRole.USER, content=injected, turn_index=ctx.turn_index + 1)
        ctx.messages.append(user_msg)
        ctx.injected_text = injected
        ctx.current_state = "thinking"
        ctx.meta.pop("_readonly_loop_sig", None)
        ctx.meta.pop("_readonly_loop_streak", None)
        # 转向失效标记：投影「最新指令」节据此声明旧约束作废（以本轮注入为准）
        ctx.meta["_steer_note"] = _steer_invalidation_note()
        # 🟡 放大：注入 USER 消息必须落库——恢复链从 session store 重建
        # messages，只进内存列表时崩溃恢复丢转向（checkpoint 屏障覆盖不到它）。
        self._persist_injected_message(ctx, user_msg)
        # 注入上下文突变前必须先有恢复点：快照含注入消息（append 先于落盘）
        # 恢复重放不会二次注入；文本已被原子取走，此处不再 clear——否则 push
        # 落在「已读未清」窗口会被 clear 吞掉。
        self._persist_checkpoint_now(ctx, turns_taken)
        await ctx.bus.emit(
            Event(
                type=EventType.USER_INPUT_INJECTED,
                data={
                    "session_id": ctx.meta.get("session_id", ""),
                    # turn_index 由 on_thinking +1，此处报 +1 的预盼值
                    # 不在此提前改动 ctx.turn_index（避免双增）
                    "turn_index": ctx.turn_index + 1,
                },
            )
        )
        return True

    async def _run_loop(
        self, ctx: ExecutionContext, max_turns: int, turns_taken: int
    ) -> TerminalReason:
        """.1：绑定 run 级日志上下文后进入主循环。

        用薄壳包一层、而不是在循环体内缩进——主循环有 130 行且含多处 continue/break
        逐轮 `with`/`finally` 复位既啰嗦又容易被后续改动破坏；这里一次性绑定，由
        `with` 在循环整体退出时精确复原（含异常路径）。
        """
        with bind_log_context(
            session_id=str(ctx.meta.get("session_id") or getattr(ctx, "session_id", "") or ""),
            agent_id=str(ctx.meta.get("agent_id") or ""),
            parent_agent_id=str(ctx.meta.get("parent_agent_id") or ""),
            # 工厂写入的键是 agent_role（meta["role"] 无生产者）——两键兼容读取
            role=str(ctx.meta.get("role") or ctx.meta.get("agent_role") or ""),
            run_id=str(ctx.meta.get("run_id") or ""),
            user_id=str(ctx.meta.get("user_id") or ""),
        ):
            return await self._run_loop_body(ctx, max_turns, turns_taken)

    async def _run_loop_body(
        self, ctx: ExecutionContext, max_turns: int, turns_taken: int
    ) -> TerminalReason:
        while ctx.current_state != "terminated":
            # 每轮刷新 turn 维度（复原因外层 bind_log_context 的 finally 统一兜住；
            # 此处刻意不复位，避免被下方 continue/break 绕过而漏复原）。
            set_log_context(turn_index=ctx.turn_index)
            if self._abort_controller and self._abort_controller.should_abort:
                ctx.terminal_reason = TerminalReason.USER_INTERRUPT
                ctx.final_message = "已被用户中断。"
                break

            if await self._consume_injected_input(ctx, turns_taken):
                continue

            if ctx.current_state == "thinking":
                # 最后一轮禁用工具：模型只能输出正文（防止长任务耗尽轮次后空输出）
                if max_turns - turns_taken <= 1:
                    if getattr(ctx, "active_tools", None):
                        logger.info("final turn: tools disabled to force final answer")
                    ctx.active_tools = []
                self._inject_retrieval_stall_notice(ctx)
                self._inject_turn_budget_notice(ctx, max_turns, turns_taken)
                turns_taken += 1
                if turns_taken > max_turns:
                    ctx.terminal_reason = TerminalReason.MAX_TURNS
                    ctx.current_state = "terminated"
                    break

            handler = self._handlers[ctx.current_state]
            # 必须已持久化本轮快照，崩溃重放才不重复副作用（dsh 事务协议）
            if ctx.current_state == "executing":
                # 快照缺 id，恢复按「未启动」重放副作用
                _mark_dispatched_tool_calls(ctx)
                self._persist_checkpoint_now(ctx, turns_taken)
            handler_succeeded = False
            try:
                await handler(ctx)
                if ctx.recovery_manager is not None:
                    ctx.recovery_manager.on_turn_completed()
                handler_succeeded = True
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("AgentLoop turn error: %s: %s", type(e).__name__, e)
                if ctx.recovery_manager is not None:
                    action = await ctx.recovery_manager.handle(e, ctx.current_state)
                    if action == RecoveryAction.ESCALATE:
                        ctx.terminal_reason = TerminalReason.ESCALATED
                    elif action == RecoveryAction.TERMINATE:
                        ctx.terminal_reason = TerminalReason.MODEL_ERROR
                        ctx.final_message = error_text(e)
                    elif action == RecoveryAction.RETRY:
                        # L-2a：重试前先落盘当前状态 + 发 STATE_CHANGED
                        # （dsh 事务协议——副作用/跳转前确保持久化完成）
                        # 裸 continue 会跳过保存点导致重试轮次不可恢复。
                        self._persist_checkpoint_now(ctx, turns_taken)
                        await ctx.bus.emit(
                            Event(
                                type=EventType.STATE_CHANGED,
                                data={
                                    "session_id": ctx.meta.get("session_id", ""),
                                    "turn": ctx.turn_index,
                                    "reason": "retry",
                                },
                            )
                        )
                        continue
                    elif action == RecoveryAction.CONTINUE_DEGRADED:
                        logger.warning(
                            "turn error tolerated (CONTINUE_DEGRADED): %s: %s",
                            type(e).__name__,
                            e,
                        )
                        # DegradationState 无执行层消费；DEGRADATION 事件已在
                        # recovery_manager 内发射）
                        degraded = set(ctx.meta.get("degraded_layers") or [])
                        degraded.add(ctx.current_state)
                        snapshot = ctx.recovery_manager.degradation.snapshot()
                        degraded.update(snapshot.keys())
                        ctx.meta["degraded_layers"] = sorted(degraded)
                else:
                    ctx.terminal_reason = TerminalReason.MODEL_ERROR
                    ctx.final_message = error_text(e)

            next_state = self.state_machine.next(ctx)
            await ctx.bus.emit(
                Event(
                    type=EventType.STATE_CHANGED,
                    data={
                        "from": ctx.current_state,
                        "to": next_state,
                        "session_id": ctx.meta.get("session_id", ""),
                        "turn": ctx.turn_index,
                        "reason": self.state_machine.last_reason,
                    },
                )
            )
            ctx.current_state = next_state
            if handler_succeeded:
                self._persist_checkpoint(ctx, turns_taken)

        if ctx.terminal_reason is None:
            ctx.terminal_reason = TerminalReason.MAX_TURNS

        # 语义边界统一在此闭合（不依赖下一轮 interval 兜底）
        self._persist_checkpoint_now(ctx, turns_taken)

        # ——落盘在终态边界，失败不阻断（helper 内部静默）
        await asyncio.to_thread(_persist_plan_file, ctx)

        await ctx.bus.emit(
            Event(
                type=EventType.TERMINATED,
                data={
                    "terminal_reason": ctx.terminal_reason.name,
                    "turn_index": ctx.turn_index,
                    "session_id": ctx.meta.get("session_id", ""),
                },
            )
        )
        return ctx.terminal_reason
