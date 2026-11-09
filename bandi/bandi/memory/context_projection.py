# 功能：上下文投影——压缩后结构化状态恢复。
# awaiting_input（等待输入）/ pending_tasks（任务台账）/ memory（会话记忆）/
# 投影段固定前缀 `[上下文投影]` 幂等可替换；总字符帽防膨胀。
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

PROJECTION_PREFIX = "[上下文投影]"
STATE_PREFIX = "[任务状态]"
_MAX_STATE_CHARS = 900
_STATE_GOAL_CHARS = 120
_STATE_ORIGINAL_GOAL_CHARS = 240
_STATE_RAW_CHARS = 200
_STATE_ITEM_CHARS = 60
_STATE_ARTIFACT_CHARS = 100
_STATE_MAX_PENDING = 3
_STATE_MAX_ARTIFACTS = 2
_SWITCH_STRONG_CUES = (
    "换个方向",
    "换一个项目",
    "新需求",
    "新的活",
    "重新开始",
    "先不弄",
    "先停一下",
    "先搁置",
    "先放着",
    "不相关",
    "改主意了",
)
SWITCH_HINT_TEXT = "本轮可能是新任务：若与上一轮目标不同，请从头开始，不要续做上一轮。"


def detect_task_switch_hint(text: Any) -> str:
    """功能：显式换任务提示——仅强信号（先不弄/换个方向/重新开始…）命中时返回
    提示语；强信号优先于指代否决，弱信号（另外/再做个）不参与判定。"""
    raw = str(text or "")
    if not raw:
        return ""
    return SWITCH_HINT_TEXT if any(cue in raw for cue in _SWITCH_STRONG_CUES) else ""


def _clip_slot(value: Any, limit: int) -> str:
    """功能：槽位值压成单行并截断（状态块每槽一行，超长补省略号）。"""
    text = " ".join(str(value or "").split())
    return text[:limit] + "…" if len(text) > limit else text


def build_state_block(
    *,
    goal: Any = "",
    original_goal: Any = "",
    outcome: Any = "",
    pending: Any = (),
    raw_input: Any = "",
    artifacts: Any = (),
    hint: Any = "",
) -> str:
    """功能：常驻任务状态块——六槽 + 可选提示渲染（原始目标 / 当前目标 / 上轮结论 /
    未完成项 / 本轮原话 / 活跃产物 / 提示），空槽省略，总长受 _MAX_STATE_CHARS 约束。"""
    lines = [STATE_PREFIX]
    if str(original_goal or "").strip():
        lines.append(f"原始目标: {_clip_slot(original_goal, _STATE_ORIGINAL_GOAL_CHARS)}")
    if str(goal or "").strip():
        lines.append(f"当前目标: {_clip_slot(goal, _STATE_GOAL_CHARS)}")
    if str(outcome or "").strip():
        lines.append(f"上轮结论: {_clip_slot(outcome, _STATE_ITEM_CHARS)}")
    items = [_clip_slot(item, _STATE_ITEM_CHARS) for item in (pending or ())]
    items = [item for item in items if item][:_STATE_MAX_PENDING]
    if items:
        lines.append(f"未完成: {len(items)} 项（{'；'.join(items)}）")
    if str(raw_input or "").strip():
        lines.append(f"本轮原话: {_clip_slot(raw_input, _STATE_RAW_CHARS)}")
    paths = [_clip_slot(path, _STATE_ARTIFACT_CHARS) for path in (artifacts or ())]
    paths = [path for path in paths if path][:_STATE_MAX_ARTIFACTS]
    if paths:
        lines.append(f"活跃产物: {'；'.join(paths)}")
    if str(hint or "").strip():
        lines.append(f"提示: {_clip_slot(hint, _STATE_ITEM_CHARS * 2)}")
    text = "\n".join(lines)
    if len(text) > _MAX_STATE_CHARS:
        text = text[:_MAX_STATE_CHARS].rstrip() + "…"
    return text


def _slot_of(snapshot: str, key: str) -> str:
    """功能：从单轮快照文本取某槽值（快照格式 `槽: 值 | 槽: 值`）。"""
    for part in str(snapshot or "").split(" | "):
        prefix = f"{key}: "
        if part.startswith(prefix):
            return part[len(prefix) :].strip()
    return ""


def _prev_snapshot(working: Any, session_id: str, turn_index: int) -> str:
    """功能：取上一轮快照文本——优先 turn:(N-1)，断档时退到最近一条更早快照；
    读取失败/无 store 视为空，不阻断请求。"""
    if working is None or turn_index <= 1:
        return ""
    try:
        exact = working.get(f"turn:{turn_index - 1}", session_id=session_id)
        if exact:
            return str(exact)
        rows = working.all(session_id) or {}
        best_no = -1
        best_key = ""
        for key in rows:
            if not key.startswith("turn:"):
                continue
            try:
                number = int(key[len("turn:") :])
            except ValueError:
                continue
            if 0 < number < turn_index and number > best_no:
                best_no, best_key = number, key
        return str(rows.get(best_key) or "")
    except Exception:  # noqa: BLE001 — 状态块属增强信息，读取失败降级为空
        return ""


def collect_state_block(
    *,
    ledger: Any = None,
    working: Any = None,
    session_id: str = "",
    turn_index: int = 0,
    raw_input: str = "",
    hint_source: str | None = None,
    original_goal: str = "",
) -> str:
    """功能：六槽取值唯一入口——台账（目标/未完成）+ 上一轮快照（上轮结论/活跃
    产物）+ 本轮原话 + 原始目标，渲染为常驻状态块。

    hint_source：换任务判定专用原文——「本轮原话」槽可能因去重置空，判定不得跟着丢。"""
    items = [
        item
        for item in (getattr(ledger, "items", None) or [])
        if isinstance(item, dict) and str(item.get("status") or "") != "completed"
    ]
    pending = [str(item.get("content") or item.get("title") or "") for item in items]
    goal = pending[0] if pending else ""
    snapshot = _prev_snapshot(working, session_id, turn_index)
    artifacts = [part.strip() for part in _slot_of(snapshot, "产物").split("；") if part.strip()]
    outcome = _slot_of(snapshot, "终态")
    hint_text = raw_input if hint_source is None else hint_source
    hint = detect_task_switch_hint(hint_text) if turn_index > 1 else ""
    if not (goal or outcome or pending or artifacts or hint or original_goal):
        return ""
    return build_state_block(
        goal=goal,
        original_goal=original_goal,
        outcome=outcome,
        pending=pending,
        raw_input=raw_input,
        artifacts=artifacts,
        hint=hint,
    )


MAX_PROJECTION_CHARS = 2000
_MAX_TOOL_LINES = 4
_MAX_FRAME_LINES = 3
_MAX_TASK_LINES = 5
_MAX_MEMORY_LINES = 3
_MAX_KNOWLEDGE_LINES = 3
_MAX_LINE_CHARS = 200


@dataclass
class ProjectionSources:
    # 功能：六投影数据源（callable 惰性求值；None=跳过该节；异常按空处理）
    tool_results: Callable[[], list[str]] | None = None
    frames: Callable[[], Any] | None = None  # SkillFrameStack | None
    tasks: Callable[[], list[dict[str, Any]]] | None = None
    memories: Callable[[], list[str]] | None = None
    knowledge: Callable[[], list[str]] | None = None
    steer_note: Callable[[], str] | None = None


def _safe_call(loader: Callable[[], Any] | None, default: Any) -> Any:
    if loader is None:
        return default
    try:
        value = loader()
    except Exception:  # noqa: BLE001 — 投影源失败按空处理（不阻断主链）
        return default
    return default if value is None else value


def _clean_lines(values: Any, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        out.append(text[:_MAX_LINE_CHARS])
        if len(out) >= limit:
            break
    return out


def _frames_projection(stack: Any) -> tuple[list[str], list[str]]:
    # 功能：step_skill（流程节点）+ awaiting_input（等待输入）——从帧栈快照提取
    if stack is None:
        return [], []
    skill_lines: list[str] = []
    awaiting_lines: list[str] = []
    active = getattr(stack, "active", None)
    if active is not None:
        node = getattr(active, "node_id", None) or "-"
        skill_lines.append(f"{getattr(active, 'skill_id', '?')} @ {node}（active）")
        fields = getattr(active, "awaiting_fields", None) or []
        if fields:
            awaiting_lines.append("、".join(str(f) for f in fields))
    for frame in list(getattr(stack, "pending", None) or [])[-_MAX_FRAME_LINES:]:
        skill_lines.append(
            f"{getattr(frame, 'skill_id', '?')} @ {getattr(frame, 'node_id', None) or '-'}（suspended）"
        )
    return skill_lines, awaiting_lines


def _task_projection(tasks: Any) -> list[str]:
    # 功能：pending_tasks 渲染——[status] content（宽松字段提取；非列表降级为空）
    if not isinstance(tasks, list):
        return []
    out: list[str] = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or item.get("text") or "").strip()
        if not content:
            continue
        status = str(item.get("status") or "pending").strip()
        out.append(f"[{status}] {content[:_MAX_LINE_CHARS]}")
        if len(out) >= _MAX_TASK_LINES:
            break
    return out


def _render_section(title: str, lines: list[str]) -> str:
    return "## " + title + "\n" + "\n".join(f"- {line}" for line in lines)


def build_context_projection(sources: ProjectionSources) -> str:
    # 功能：组装投影段（各节空则省略；总帽 MAX_PROJECTION_CHARS；全空/异常返回空串）
    try:
        sections = _collect_sections(sources)
    except Exception:  # noqa: BLE001 — 投影失败绝不阻断主链（D1）
        return ""
    if not sections:
        return ""
    text = PROJECTION_PREFIX + "\n" + "\n".join(sections)
    if len(text) > MAX_PROJECTION_CHARS:
        text = text[:MAX_PROJECTION_CHARS].rstrip() + "…"
    return text


def _collect_sections(sources: ProjectionSources) -> list[str]:
    sections: list[str] = []

    steer_note = str(_safe_call(sources.steer_note, "") or "").strip()
    if steer_note:
        sections.append(_render_section("最新指令", [steer_note[:_MAX_LINE_CHARS]]))

    tool_lines = _clean_lines(_safe_call(sources.tool_results, []), _MAX_TOOL_LINES)
    if tool_lines:
        sections.append(_render_section("工具结果", tool_lines))

    stack = _safe_call(sources.frames, None)
    skill_lines, awaiting_lines = _frames_projection(stack)
    if skill_lines:
        sections.append(_render_section("流程节点", skill_lines))
    if awaiting_lines:
        sections.append(_render_section("等待输入", awaiting_lines))

    task_lines = _task_projection(_safe_call(sources.tasks, []))
    if task_lines:
        sections.append(_render_section("任务台账", task_lines))

    memory_lines = _clean_lines(_safe_call(sources.memories, []), _MAX_MEMORY_LINES)
    if memory_lines:
        sections.append(_render_section("相关记忆", memory_lines))

    knowledge_lines = _clean_lines(_safe_call(sources.knowledge, []), _MAX_KNOWLEDGE_LINES)
    if knowledge_lines:
        sections.append(_render_section("知识命中", knowledge_lines))

    return sections


def projection_fingerprint(sources: ProjectionSources) -> str:
    # 功能：变更指纹（帧 revision/节点状态 + 任务条数）——轻量（不做记忆/知识读取）
    parts: list[str] = []
    stack = _safe_call(sources.frames, None)
    if stack is not None:
        parts.append(f"rev:{getattr(stack, 'revision', '')}")
        active = getattr(stack, "active", None)
        if active is not None:
            parts.append(
                f"{getattr(active, 'skill_id', '')}:{getattr(active, 'node_id', '')}"
                f":{getattr(active, 'status', '')}"
            )
        parts.append(f"pending:{len(getattr(stack, 'pending', None) or [])}")
    tasks = _safe_call(sources.tasks, [])
    parts.append(f"tasks:{len(tasks) if isinstance(tasks, list) else 0}")
    steer_note = str(_safe_call(sources.steer_note, "") or "").strip()
    if steer_note:
        parts.append(f"steer:{steer_note[:80]}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _content_of(message: Any) -> str:
    return str(getattr(message, "content", "") or "")


def _role_of(message: Any) -> str:
    role = getattr(message, "role", "")
    return str(getattr(role, "value", role))


def projection_message_index(messages: list[Any]) -> int:
    # 功能：定位已有投影消息（SYSTEM + 固定前缀双条件——防 USER 内容前缀碰撞误替换）
    for index, message in enumerate(messages):
        if _role_of(message) != "system":
            continue
        if _content_of(message).startswith(PROJECTION_PREFIX):
            return index
    return -1


def upsert_projection_message(messages: list[Any], text: str) -> bool:
    # 功能：插入/替换投影消息（幂等）——插入位置=最后一条 SYSTEM 之后（摘要在前
    # 保留对话在后）；已有投影消息原地替换（不新增条数）
    if not text:
        return False
    from bandi.core.message import Message, MessageRole

    index = projection_message_index(messages)
    if index >= 0:
        messages[index] = Message(role=MessageRole.SYSTEM, content=text)
        return True
    insert_at = 0
    for i, message in enumerate(messages):
        if _role_of(message) == "system":
            insert_at = i + 1
    messages.insert(insert_at, Message(role=MessageRole.SYSTEM, content=text))
    return True
