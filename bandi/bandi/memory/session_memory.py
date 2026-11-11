"""SM-compact = 传统 LLM 摘要压缩的前置替代：轮末自动沉淀的会话记忆
保留窗口算法完成压缩——全程零 LLM 调用。

与 cc-haha sessionMemoryCompact.ts 的映射：
* 数据源 getSessionMemoryContent → ``build_session_memory_summary``（scratchpad 快照）
* calculateMessagesToKeepIndex → ``calculate_keep_index``（min 双下限/max 上限/
  boundary floor/tool 对不拆散一一对应）
* trySessionMemoryCompaction 返回 null 回退 legacy compact → ``try_session_compact``
  返回 None 回退 CompressionPipeline
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bandi.core.message import Message, MessageRole
from bandi.memory.compaction import SUMMARY_MESSAGE_PREFIX, repair_tool_pairs
from bandi.utils.token_counter import TokenCounter

if TYPE_CHECKING:
    from bandi.agent.deps import CompactOutcome
    from bandi.memory.working import ScratchpadStore

logger = logging.getLogger(__name__)

DEFAULT_MIN_TOKENS = 10_000
DEFAULT_MIN_TEXT_MESSAGES = 5
DEFAULT_MAX_TOKENS = 40_000
DEFAULT_SUMMARY_MAX_TOKENS = 4000

_TURN_KEY_PREFIX = "turn:"
_SESSION_MEMORY_STAGE = "session_memory"


@dataclass(frozen=True)
class SessionMemoryCompactConfig:
    min_tokens: int = DEFAULT_MIN_TOKENS
    min_text_messages: int = DEFAULT_MIN_TEXT_MESSAGES
    max_tokens: int = DEFAULT_MAX_TOKENS


def build_session_memory_summary(
    store: ScratchpadStore, session_id: str, max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS
) -> str | None:
    """功能：从 working scratchpad turn 快照拼装结构化摘要（带摘要前缀协议
    审查项3：摘要总 token 受 max_tokens 上限约束——超预算时尾部（最新）快照优先
    从最新向旧贪心纳入至预算耗尽（会话记忆新近性优先，防摘要失控挤爆上下文）。"""
    snapshots = {
        key: value
        for key, value in store.all(session_id).items()
        if key.startswith(_TURN_KEY_PREFIX)
    }
    if not snapshots:
        return None

    def _turn_no(key: str) -> int:
        try:
            return int(key[len(_TURN_KEY_PREFIX) :])
        except ValueError:
            return 0

    lines = [f"- turn {_turn_no(key)}: {snapshots[key]}" for key in sorted(snapshots, key=_turn_no)]
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        tok = TokenCounter.estimate(line)
        if kept and total + tok > max_tokens:
            break
        kept.append(line)
        total += tok
    kept.reverse()

    # 校验含包装（前缀+标题）的总 token 不超预算——超则从最旧丢弃直至收敛
    while kept:
        header = f"## 会话记忆（自动提取，{len(kept)} 轮任务快照）"
        current = TokenCounter.estimate(f"{SUMMARY_MESSAGE_PREFIX}{header}\n" + "\n".join(kept))
        if current <= max_tokens:
            break
        kept.pop(0)  # 丢最旧（最早）快照

    body = "\n".join(kept)
    return f"{SUMMARY_MESSAGE_PREFIX}## 会话记忆（自动提取，{len(kept)} 轮任务快照）\n{body}"


_DONE_TERMINALS = frozenset({"COMPLETED", "VERIFIED"})
_BLOCKED_TERMINALS = frozenset(
    {
        "MODEL_ERROR",
        "TOOL_ERROR",
        "SAFETY_BLOCKED",
        "LOOP_DETECTED",
        "PROMPT_TOO_LONG",
        "ESCALATED",
        "REWORK",
    }
)
_WRITE_TOOL_NAMES = frozenset({"write", "edit", "apply_patch"})
_SNAPSHOT_MAX_CHARS = 700
_SNAPSHOT_GOAL_CHARS = 120
_SNAPSHOT_RESULT_CHARS = 160
_SNAPSHOT_ITEM_CHARS = 60
_SNAPSHOT_PATH_CHARS = 80
_SNAPSHOT_MAX_ITEMS = 2
_SNAPSHOT_OMIT_MARK = "…"
_REPLY_PATH_RE = re.compile(
    r"[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\-./\\\u4e00-\u9fff]*"
    r"\.(?:html|htm|py|tsx|ts|jsx|js|md|json|ya?ml|css|sql|sh|ps1|txt|csv)\b"
)


def classify_turn_outcome(terminal: str) -> str:
    """功能：终态归并为 done / blocked / partial——快照与状态块共用的唯一口径。"""
    name = str(terminal or "").upper()
    if name in _DONE_TERMINALS:
        return "done"
    if name in _BLOCKED_TERMINALS:
        return "blocked"
    return "partial"


def _artifacts_from_reply(text: str) -> list[str]:
    """功能：从回复正文补出产物路径（预览路径/生成文件常见形态），排除 URL。"""
    found: list[str] = []
    for match in _REPLY_PATH_RE.finditer(str(text or "")):
        path = match.group(0).strip("`'\"").rstrip(".,;:）)")
        if not path or path.startswith(("http://", "https://", "www.")):
            continue
        if "//" in str(text or "")[max(0, match.start() - 3) : match.start()]:
            continue
        if len(path) > _SNAPSHOT_PATH_CHARS * 2:
            continue
        path = path[:_SNAPSHOT_PATH_CHARS]
        if path not in found:
            found.append(path)
        if len(found) >= _SNAPSHOT_MAX_ITEMS:
            break
    return found


def _turn_artifacts(messages: list[Any], turn_index: int) -> list[str]:
    """功能：本轮写类工具涉及的产物路径（去重、按出现顺序，最多 N 条）。"""
    paths: list[str] = []
    for message in messages:
        if getattr(message, "role", None) != MessageRole.ASSISTANT:
            continue
        owner = getattr(message, "turn_index", None)
        if owner is not None and int(owner) != int(turn_index):
            continue
        for call in getattr(message, "tool_calls", None) or []:
            if getattr(call, "name", "") not in _WRITE_TOOL_NAMES:
                continue
            args = getattr(call, "arguments", None) or {}
            if not isinstance(args, dict):
                continue
            path = args.get("file_path") or args.get("path") or args.get("filename")
            if isinstance(path, str) and path.strip():
                path = path.strip()[:_SNAPSHOT_PATH_CHARS]
                if path not in paths:
                    paths.append(path)
            if len(paths) >= _SNAPSHOT_MAX_ITEMS:
                return paths
    return paths


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit] + _SNAPSHOT_OMIT_MARK if len(text) > limit else text


def build_turn_snapshot(
    raw_input: str,
    final_message: str,
    terminal: str,
    messages: list[Any] | tuple[Any, ...] = (),
    turn_index: int = 0,
    ledger_items: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> str:
    """功能：单轮结构化快照——目标 / 终态 / 结果 / 待办 / 产物，单行有界文本，
    作为会话记忆压缩的数据源（代码拼装，零 LLM 调用）。"""
    outcome = classify_turn_outcome(terminal)
    parts = [f"目标: {_clip(raw_input, _SNAPSHOT_GOAL_CHARS) or '(空)'}"]
    parts.append(f"终态: {outcome}")
    result = _clip(final_message, _SNAPSHOT_RESULT_CHARS)
    parts.append(f"结果: {result}" if result else "结果: 未完成（无产出文本）")

    pending = [
        _clip(str(item.get("content") or item.get("title") or ""), _SNAPSHOT_ITEM_CHARS)
        for item in ledger_items
        if isinstance(item, dict) and str(item.get("status") or "") != "completed"
    ]
    pending = [item for item in pending if item][:_SNAPSHOT_MAX_ITEMS]
    if pending:
        parts.append(f"待办: {len(pending)} 项（{'；'.join(pending)}）")
    else:
        parts.append("待办: 0 项")

    artifacts = _turn_artifacts(list(messages), turn_index)
    for path in _artifacts_from_reply(final_message):
        if path not in artifacts:
            artifacts.append(path)
    artifacts = artifacts[:_SNAPSHOT_MAX_ITEMS]
    if artifacts:
        parts.append(f"产物: {'；'.join(artifacts)}")

    snapshot = " | ".join(parts)
    if len(snapshot) > _SNAPSHOT_MAX_CHARS:
        snapshot = snapshot[:_SNAPSHOT_MAX_CHARS] + _SNAPSHOT_OMIT_MARK
    return snapshot


def _is_summary(m: Message) -> bool:
    return bool((m.content or "").startswith(SUMMARY_MESSAGE_PREFIX))


def _has_text(m: Message) -> bool:
    return m.role in (MessageRole.USER, MessageRole.ASSISTANT) and bool((m.content or "").strip())


def _boundary_floor(messages: list[Message]) -> int:
    """最后一个 [压缩边界] 标记的下一条位置——向前扩展不得越过（cc-haha floor 不变量）。"""
    from bandi.memory.compression_pipeline import COMPACT_BOUNDARY_PREFIX

    floor = 0
    for i, m in enumerate(messages):
        if m.role == MessageRole.SYSTEM and (m.content or "").startswith(COMPACT_BOUNDARY_PREFIX):
            floor = i + 1
    return floor


def session_memory_boundary(
    store: ScratchpadStore, session_id: str, messages: list[Message]
) -> int | None:
    """功能：计算「已被会话记忆覆盖」的消息边界——最大已快照 turn 之后的第一条
    消息位置（cc-haha lastSummarizedMessageId 对应物）；归属不明（turn_index 缺失）
    保守视为未覆盖。无任何快照返回 None。"""
    turns: set[int] = set()
    for key in store.all(session_id):
        if key.startswith(_TURN_KEY_PREFIX):
            try:
                turns.add(int(key[len(_TURN_KEY_PREFIX) :]))
            except ValueError:
                continue
    if not turns:
        return None
    covered_max = max(turns)
    # calculate_keep_index 以 non_system 列表取 floor 会错位误裁未覆盖消息。
    non_system_idx = 0
    for m in messages:
        if m.role == MessageRole.SYSTEM:
            continue
        ti = getattr(m, "turn_index", None)
        if ti is None or ti > covered_max:
            return non_system_idx
        non_system_idx += 1
    return non_system_idx


def calculate_keep_index(
    messages: list[Message],
    *,
    summarized_boundary: int | None = None,
    config: SessionMemoryCompactConfig | None = None,
    token_fn: Callable[[Message], int] | None = None,
) -> int:
    """功能：计算 SM 压缩后的保留区起点——从尾部向前扩展至双下限满足或撞上限
    同名算法 + lastSummarized 起点语义）。"""
    cfg = config or SessionMemoryCompactConfig()
    estimate = token_fn or (lambda m: TokenCounter.estimate(m.content or ""))
    n = len(messages)
    if n == 0:
        return 0

    floor = _boundary_floor(messages)
    if summarized_boundary is not None:
        floor = max(floor, summarized_boundary)
    idx = n
    total = 0
    text_count = 0
    for i in range(n - 1, floor - 1, -1):
        m = messages[i]
        total += estimate(m)
        if _has_text(m):
            text_count += 1
        idx = i
        if total >= cfg.max_tokens:
            break
        if total >= cfg.min_tokens and text_count >= cfg.min_text_messages:
            break
    return repair_tool_pairs(messages, idx)


def _chars(messages: list[Any]) -> int:
    return sum(len(m.content or "") for m in messages)


async def try_session_compact(ctx: Any) -> CompactOutcome | None:
    """功能：SM 压缩主入口——数据/配置任一缺失返回 None 由调用方回退传统
    pipeline；成功产出 CompactOutcome(stage="session_memory")。"""
    from bandi.memory.compression_pipeline import (
        CompressionStats,
        build_boundary_message,
        strip_compact_boundaries,
    )

    manager = ctx.meta.get("memory_manager")
    working = getattr(manager, "working", None) if manager is not None else None
    session_id = ctx.meta.get("session_id")
    if working is None or not session_id:
        return None
    summary = build_session_memory_summary(working, session_id)
    if summary is None:
        return None

    msgs = list(ctx.messages)
    core = [m for m in strip_compact_boundaries(msgs) if not _is_summary(m)]
    system_head = [m for m in core if m.role == MessageRole.SYSTEM]
    non_system = [m for m in core if m.role != MessageRole.SYSTEM]

    summarized_boundary = session_memory_boundary(working, session_id, msgs)
    keep_idx = calculate_keep_index(non_system, summarized_boundary=summarized_boundary)
    kept = non_system[keep_idx:]

    boundary = build_boundary_message(
        _SESSION_MEMORY_STAGE, len(msgs), len(system_head) + len(kept) + 1, _chars(msgs), 0
    )
    summary_msg = Message(role=MessageRole.SYSTEM, content=summary)
    result = [*system_head, boundary, summary_msg, *kept]

    # 超水位守卫：压缩后仍超水位 → 放弃 SM 回退
    # 传统 pipeline（其 progressive 分层裁剪可真正收敛到目标水位）。
    # 守卫必须计入 on_compressing 随后的 continuity anchor 增量——否则
    # compressing→thinking 出口逃逸失败，假 MAX_TURNS 空回复（anchor 判定
    # 与 states._inject_continuity_anchor 对齐）。
    # 注：此处刻意仍用 raw config.context_window（不改成 effective 口径）——
    # 本守卫决定「SM 压缩 vs 传统 progressive」的路径选择，改用 effective（更小的
    # 窗口）会使其更易 abort、把链路推给 progressive，属行为变更；且哨兵 0 下
    # `window > 0` 为假只是跳过本守卫（放行 SM），无安全风险（生产链窗口恒由
    # factory 解析为正数）。保持既有路径选择语义不动。
    window = getattr(ctx.config, "context_window", 0)
    threshold = getattr(ctx.config, "compression_threshold", 0.85)
    post_tokens = sum(TokenCounter.estimate(m.content or "") for m in result)
    raw = getattr(ctx, "raw_input", None)
    if raw:
        anchored = any(m.role == MessageRole.USER and m.content == raw for m in result)
        if not anchored:
            post_tokens += TokenCounter.estimate(raw)
    elif not any(m.role == MessageRole.USER and bool(m.meta.get("synthetic")) for m in result):
        from bandi.memory.compaction import CONTINUE_PROMPT

        post_tokens += TokenCounter.estimate(CONTINUE_PROMPT)
    if window > 0 and post_tokens >= window * threshold:
        logger.info(
            "session_memory compaction aborted: post %d tokens >= %.0f (window %d)",
            post_tokens,
            window * threshold,
            window,
        )
        return None

    stats = CompressionStats(
        stage=_SESSION_MEMORY_STAGE,
        input_messages=len(msgs),
        output_messages=len(result),
        input_chars=_chars(msgs),
        output_chars=_chars(result),
    )
    logger.info(
        "session_memory compaction: msgs %d->%d chars %d->%d",
        stats.input_messages,
        stats.output_messages,
        stats.input_chars,
        stats.output_chars,
    )

    from bandi.agent.deps import CompactOutcome

    return CompactOutcome(messages=result, success=True, stats=stats)


__all__ = [
    "SessionMemoryCompactConfig",
    "build_session_memory_summary",
    "calculate_keep_index",
    "session_memory_boundary",
    "try_session_compact",
]
