# 功能：压缩管道（MicroCompressor/压缩统计/失败熔断/附件追踪）
from __future__ import annotations

import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bandi.core.message import Message, MessageRole
from bandi.memory.compaction import (
    DEFAULT_SUMMARY_MAX_TOKENS,
    SUMMARY_ANALYSIS_INSTRUCTION,
    SUMMARY_MESSAGE_PREFIX,
    SUMMARY_SECTIONS_GUIDE,
    SUMMARY_UPDATE_RULES,
    MemoryCompactor,
    append_previous_summary,
    build_summary_appendix,
    extract_previous_summary,
    strip_analysis_scratchpad,
)
from bandi.memory.compaction import MIN_MESSAGES_FOR_COMPRESSION as _MIN_MESSAGES_FOR_COMPRESSION
from bandi.memory.fidelity import summarize_with_fidelity
from bandi.utils.token_counter import TokenCounter, format_omission_marker

logger = logging.getLogger(__name__)
_SPLIT_RATIO = 0.5
# 小窗对话真实「过大」时不能被绝对预算掩盖而整体拦截。有效目标 =
# max(下限, min(绝对预算, context_window * 窗口系数))，大窗保持 16000 不变。
# 「未到预算」bail → 渐进全失败退化成 truncate 丢暗号；1.0 令 2500 窗目标=2500
# 4181>2500 真实触发收敛）。
_WINDOW_TARGET_ALPHA = 1.0
_WINDOW_TARGET_MIN = 800
# 小窗口阈值：4K~64K 时块会小于全局 16K 目标，必须强制摘要防退化 truncate
_SMALL_WINDOW_MIN_TOKENS = 4096
_SMALL_WINDOW_MAX_TOKENS = 64_000
# 渐进入口 bail 的「值不值得压」token 上限封顶——只跳过「小到没压头」的琐碎上下文。
# 保真 merge budget=1 时 threshold=10/1，让 ~176/~104 token 内容真实进入 progressive 由
# compact 逐块判罚），又封顶了 _effective_target 在极小窗下 `max(800,窗口)` 的上漂
# （默认预算+窗口 1000 → 原 threshold=1000，把 840>紧迫 500 应对齐的对话整体跳过 →
# progressive 从不尝试 → progressive_failures 恒 0，破坏「压缩失败须上报」契约：
# test_pipeline_reports_failure_when_progressive_unreduced）。封顶后默认预算一律按本值
_PROGRESSIVE_MIN_WORTH_TOKENS = 100
_MICRO_MAX_SHORT_MSG_LEN = 80
_MICRO_TOOL_TRIM_START = 500
_MICRO_TOOL_TRIM_END = 200

_PRUNE_PROTECT_TOKENS = 40_000
_PRUNE_MIN_SAVINGS_TOKENS = 20_000
_PRUNE_PROTECT_RECENT_TURNS = 2
_PRUNE_PROTECTED_TOOLS = frozenset({"skill_invoke", "skill_discover"})

_MAINTENANCE_TURN_INTERVAL = 50
_MAINTENANCE_IDLE_SECONDS = 1800.0
_LOW_RETURNS_LIMIT = 3
_LOW_RETURNS_MIN_SAVINGS_TOKENS = 500

_CONFIRMATION_USER_RE = re.compile(
    r"^(?:好的|继续|ok|okay|嗯+|是的|对|确认|收到|yes|go|行|好)[。，！!．.~～\s]*$",
    re.IGNORECASE,
)
_CONFIRMATION_USER_MAX_CHARS = 12


def is_maintenance_due(
    turns_since_maintenance: int, last_active_ts: float | None, now: float
) -> bool:
    if turns_since_maintenance >= _MAINTENANCE_TURN_INTERVAL:
        return True
    return last_active_ts is not None and (now - last_active_ts) >= _MAINTENANCE_IDLE_SECONDS


def prune_confirmation_pairs(msgs: list[Message], stats: CompressionStats) -> list[Message]:
    """
    无 tool_calls 短 assistant 一起删（防悬空）；最近 turn 保护；幂等。"""
    n = len(msgs)
    delete_flags = [False] * n
    user_turns_seen = 0
    for i in range(n - 1, -1, -1):
        m = msgs[i]
        if m.role != MessageRole.USER:
            continue
        user_turns_seen += 1
        if user_turns_seen <= _PRUNE_PROTECT_RECENT_TURNS:
            continue
        content = (m.content or "").strip()
        if not content or len(content) > _CONFIRMATION_USER_MAX_CHARS:
            continue
        if not _CONFIRMATION_USER_RE.match(content):
            continue
        j = i + 1
        if j >= n or msgs[j].role != MessageRole.ASSISTANT:
            continue
        assistant_msg = msgs[j]
        if assistant_msg.tool_calls:
            continue
        assistant_content = (assistant_msg.content or "").strip()
        if not assistant_content or len(assistant_content) > _MICRO_MAX_SHORT_MSG_LEN:
            continue
        delete_flags[i] = True
        delete_flags[j] = True
        stats.micro_trimmed += 2
    if not any(delete_flags):
        return msgs
    return [m for k, m in enumerate(msgs) if not delete_flags[k]]


COMPACT_BOUNDARY_PREFIX = "[压缩边界]"


def strip_compact_boundaries(messages: list[Message]) -> list[Message]:
    return [
        m
        for m in messages
        if not (
            m.role == MessageRole.SYSTEM and (m.content or "").startswith(COMPACT_BOUNDARY_PREFIX)
        )
    ]


POST_COMPACT_MAX_FILES = 5
POST_COMPACT_FILE_PREFIX = "[后压缩上下文]"
# 每文件/总内容预算——回注真实正文而非仅路径（CCB 5 文件/50K 的保守版）
POST_COMPACT_MAX_FILE_CHARS = 4000
POST_COMPACT_MAX_TOTAL_CHARS = 20000
_FILE_TOUCH_TOOL_NAMES = frozenset({"read", "write", "edit", "apply_patch", "git_commit"})


def _prune_old_post_compact_files(msgs: list[Message], stats: CompressionStats) -> list[Message]:
    idxs = [
        i
        for i, m in enumerate(msgs)
        if m.role == MessageRole.SYSTEM and (m.content or "").startswith(POST_COMPACT_FILE_PREFIX)
    ]
    if len(idxs) <= 1:
        return msgs
    stale = set(idxs[:-1])
    stats.micro_trimmed += len(stale)
    return [m for i, m in enumerate(msgs) if i not in stale]


def _removed_read_contents(removed: list[Message]) -> dict[str, str]:
    """从被移除的 read 工具结果还原真实文件内容（path → content）。

    零额外 I/O：read 的 TOOL 消息正文就是模型此前看到的真实文件内容，
    直接按 tool_call_id 反查 path 复用，避免只回路径导致「压缩后失忆」。
    """
    call_paths: dict[str, str] = {}
    for m in removed:
        for tc in m.tool_calls or []:
            if tc.name == "read":
                path = str((tc.arguments or {}).get("path", "")).strip()
                if path:
                    call_paths[tc.id] = path
    contents: dict[str, str] = {}
    if not call_paths:
        return contents
    for m in removed:
        if m.role != MessageRole.TOOL or not m.tool_call_id:
            continue
        path = call_paths.get(m.tool_call_id)
        if path and (m.content or ""):
            contents[path] = m.content or ""
    return contents


def _read_disk_snippet(resolved_path: str, limit: int) -> str:
    """功能：read_state.resolved_path 重读片段；仅限工作区内，越界/失败静默跳过。"""
    if not resolved_path:
        return ""
    try:
        from bandi.tools.fs_tool import workspace_root

        path = Path(resolved_path).resolve()
        if not path.is_relative_to(workspace_root().resolve()):
            return ""
        raw = path.read_bytes()[: limit * 4]
    except (OSError, ValueError):
        return ""
    if b"\x00" in raw:
        return ""
    return raw.decode("utf-8", "replace")[:limit]


def build_post_compact_file_attachment(
    removed: list[Message],
    max_files: int = POST_COMPACT_MAX_FILES,
    read_state: dict[str, Any] | None = None,
) -> Message | None:
    """
    （结构化 tool_calls，非文本猜测），生成确定性参考清单；无触碰返回 None。
    CE-23：read_state（工具层真实读取记录）非空时——真读过的按读取时间降序排前
    仅提及未读取的候选垫后；无记录时保持既有「最近触碰优先」顺序（兼容）。
    回注真实内容片段（优先复用被移除的 read TOOL 正文，兜底磁盘重读），
    而非只给路径。"""
    files: list[str] = []
    seen: set[str] = set()
    for m in reversed(removed):  # 最近触碰优先
        for tc in m.tool_calls or []:
            if tc.name not in _FILE_TOUCH_TOOL_NAMES:
                continue
            path = str((tc.arguments or {}).get("path", "")).strip()
            if path and path not in seen:
                seen.add(path)
                files.append(path)
    if read_state:
        read_paths = [p for p in files if p in read_state]
        read_paths.sort(
            key=lambda p: str((read_state.get(p) or {}).get("read_at") or ""), reverse=True
        )
        files = read_paths + [p for p in files if p not in read_state]
    files = files[:max_files]
    if not files:
        return None
    contents = _removed_read_contents(removed)
    blocks: list[str] = []
    used = 0
    for path in files:
        source = contents.get(path, "")
        if not source and read_state:
            resolved = str((read_state.get(path) or {}).get("resolved_path") or "")
            source = _read_disk_snippet(resolved, POST_COMPACT_MAX_FILE_CHARS + 1)
        if not source:
            blocks.append(f"### {path}\n（内容不可回注，请重新 read）")
            continue
        cap = min(POST_COMPACT_MAX_FILE_CHARS, POST_COMPACT_MAX_TOTAL_CHARS - used)
        if cap <= 0:
            break
        snippet = source[:cap]
        used += len(snippet)
        tail = "\n…（片段已截断，完整内容请重新 read）" if len(source) > len(snippet) else ""
        blocks.append(f"### {path}\n{snippet}{tail}")
    header = (
        f"{POST_COMPACT_FILE_PREFIX} 本次压缩已移除下列文件的读取记录，"
        f"以下为最近涉及文件的真实内容片段（供继续工作；完整内容请重新读取）："
    )
    return Message(role=MessageRole.SYSTEM, content=header + "\n" + "\n".join(blocks))


def _insert_post_compact_files(result: list[Message], attachment: Message) -> list[Message]:
    """功能：附件插入 system 区末尾、保留对话之前（boundary → summary → files → kept）。"""
    idx = next((i for i, m in enumerate(result) if m.role != MessageRole.SYSTEM), len(result))
    return result[:idx] + [attachment] + result[idx:]


def build_boundary_message(
    stage: str, msgs_in: int, msgs_out: int, chars_in: int, chars_out: int
) -> Message:
    """功能：构造 CompactBoundary 标记消息（压缩事件可观测：stage + 前后消息/字符数）。"""
    content = (
        f"{COMPACT_BOUNDARY_PREFIX} stage={stage} "
        f"msgs {msgs_in}→{msgs_out} chars {chars_in}→{chars_out}"
    )
    return Message(role=MessageRole.SYSTEM, content=content)


def _insert_boundary_marker(result: list[Message], marker: Message) -> list[Message]:
    """功能：boundary 插入原 system 区末尾、摘要/保留对话之前，"""
    idx = next(
        (
            i
            for i, m in enumerate(result)
            if m.role != MessageRole.SYSTEM or (m.content or "").startswith(SUMMARY_MESSAGE_PREFIX)
        ),
        len(result),
    )
    return result[:idx] + [marker] + result[idx:]


@dataclass
class CompressionStats:
    stage: str = ""
    input_messages: int = 0
    output_messages: int = 0
    input_chars: int = 0
    output_chars: int = 0
    micro_deduplicated: int = 0
    micro_trimmed: int = 0
    micro_merged: int = 0
    progressive_chunks: int = 0
    progressive_failures: int = 0
    forced_small_chunks: int = 0
    fidelity_retries: int = 0
    fidelity_failures: int = 0
    # 4.9：末次摘要保真缺失率（None=无报告/未摘要）——轮末摘要 importance 动态输入
    fidelity_missing_ratio: float | None = None
    duration_ms: float = 0.0


_BASE64_IMAGE_RE = re.compile(
    r"!\[.*?\]\(data:image/[^;]+;base64,[^)]+\)|data:image/[^;]+;base64,[A-Za-z0-9+/=]{100,}"
)
_IMAGE_PLACEHOLDER = "[图片已压缩，如需查看请重新运行工具]"


class FailureCircuitBreaker:
    def __init__(self, max_failures: int = 3, reset_after_success: bool = True):
        self._max_failures = max_failures
        self._reset_after_success = reset_after_success
        self._consecutive_failures = 0
        self._tripped = False

    def record_success(self) -> None:
        if self._reset_after_success:
            self._consecutive_failures = 0
            self._tripped = False

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_failures:
            self._tripped = True

    @property
    def is_tripped(self) -> bool:
        return self._tripped

    def reset(self) -> None:
        self._consecutive_failures = 0
        self._tripped = False

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures


class AttachmentTracker:
    def __init__(self) -> None:
        self._stripped: dict[str, str] = {}

    def record_stripped(self, msg_index: int, content_snippet: str) -> None:
        self._stripped[str(msg_index)] = content_snippet[:200]

    def get_attachment(self, msg_index: int) -> str | None:
        return self._stripped.get(str(msg_index))

    @property
    def attachment_count(self) -> int:
        return len(self._stripped)

    def summary(self) -> str:
        if not self._stripped:
            return ""
        lines = [f"- 消息 #{idx}: 包含图片/附件" for idx in self._stripped]
        return "已压缩附件:\n" + "\n".join(lines)


def strip_images_from_text(text: str) -> tuple[str, int]:
    """功能：单段文本的 base64 媒体块剥离（审计 2.12）——工具结果入上下文入口即调用
    返回（剥离后文本, 剥离计数）。占位符保留位置语义，计数供可观测性。"""
    if not text:
        return text, 0
    stripped, n = _BASE64_IMAGE_RE.subn(_IMAGE_PLACEHOLDER, text)
    return stripped, n


def strip_images_from_messages(messages: list[Message]) -> tuple[list[Message], AttachmentTracker]:
    tracker = AttachmentTracker()
    result: list[Message] = []
    for i, m in enumerate(messages):
        if m.content and _BASE64_IMAGE_RE.search(m.content):
            stripped = _BASE64_IMAGE_RE.sub(_IMAGE_PLACEHOLDER, m.content)
            if stripped != m.content:
                tracker.record_stripped(i, m.content[:200])
                result.append(
                    Message(
                        role=m.role,
                        content=stripped,
                        tool_call_id=m.tool_call_id,
                        name=m.name,
                        tool_calls=m.tool_calls,
                        turn_index=m.turn_index,
                        meta=m.meta,
                    )
                )
                continue
        result.append(m)
    return result, tracker


def _prune_tool_outputs_by_budget(msgs: list[Message], stats: CompressionStats) -> list[Message]:
    """
    _PRUNE_PROTECT_TOKENS 的更早输出替换规则摘要；最近 _PRUNE_PROTECT_RECENT_TURNS
    个 user-turn 保护；保护名单豁免；可剪量低于门槛不动作。"""
    from bandi.tools.result_summary import is_already_shaped, rule_summary

    result = list(msgs)
    n = len(result)
    turn_count = 0
    accumulated = 0
    savings = 0
    prune_targets: list[int] = []
    for i in range(n - 1, -1, -1):
        m = result[i]
        if m.role == MessageRole.USER:
            turn_count += 1
            continue
        if m.role != MessageRole.TOOL or not m.content:
            continue
        if turn_count < _PRUNE_PROTECT_RECENT_TURNS:
            continue
        if m.name in _PRUNE_PROTECTED_TOOLS:
            continue
        if is_already_shaped(m.content):
            continue  # 已摘要/已占位（幂等）
        estimate = TokenCounter.count_message(m)
        accumulated += estimate
        if accumulated <= _PRUNE_PROTECT_TOKENS:
            continue
        prune_targets.append(i)
        savings += estimate
    if not prune_targets or savings < _PRUNE_MIN_SAVINGS_TOKENS:
        return msgs  # 收益低于门槛不动作
    from bandi.tools.output_store import persist_tool_content
    from bandi.tools.result_summary import persisted_excerpt

    for i in prune_targets:
        old = result[i]
        persisted = persist_tool_content(old.name or "tool", old.content)
        # 落盘成功时保留真实头/尾（信息不丢，中段按路径回读）；落盘失败才退规则摘要
        summary = (
            persisted_excerpt(old.content, old.name or "", persisted)
            if persisted
            else rule_summary(old.content, old.name or "")
        )
        result[i] = Message(
            role=old.role,
            content=summary,
            tool_call_id=old.tool_call_id,
            name=old.name,
            turn_index=old.turn_index,
            meta=old.meta,
        )
        stats.micro_trimmed += 1
    return result


def prune_before_request(messages: list[Message]) -> tuple[list[Message], int]:
    """功能：每次 LLM 请求前的零 LLM 免费 prune——旧工具输出超保护预算替换占位符 + 旧附件/确认对清理。
    低于收益门槛/幂等场景不动消息（既有 prune 语义），返回（消息, 清理计数）。"""
    stats = CompressionStats()
    msgs = _prune_tool_outputs_by_budget(messages, stats)
    msgs = _prune_old_post_compact_files(msgs, stats)
    msgs = prune_confirmation_pairs(msgs, stats)
    return msgs, stats.micro_trimmed


class MicroCompressor:
    def compress(self, messages: list[Message]) -> tuple[list[Message], CompressionStats]:
        stats = CompressionStats(
            stage="micro",
            input_messages=len(messages),
            input_chars=sum(len(m.content or "") for m in messages),
        )
        msgs = list(messages)

        msgs = self._deduplicate_tool_outputs(msgs, stats)
        msgs = self._trim_verbose_tool_outputs(msgs, stats)
        msgs = self._merge_short_messages(msgs, stats)
        msgs = self._remove_empty_messages(msgs)

        stats.output_messages = len(msgs)
        stats.output_chars = sum(len(m.content or "") for m in msgs)
        return msgs, stats

    @staticmethod
    def _deduplicate_tool_outputs(msgs: list[Message], stats: CompressionStats) -> list[Message]:
        if len(msgs) < 2:
            return msgs
        result: list[Message] = [msgs[0]]
        for i in range(1, len(msgs)):
            prev = result[-1]
            curr = msgs[i]
            if (
                curr.role == MessageRole.TOOL
                and prev.role == MessageRole.TOOL
                and curr.tool_call_id == prev.tool_call_id
                and curr.content == prev.content
            ):
                stats.micro_deduplicated += 1
                continue
            result.append(curr)
        return result

    @staticmethod
    def _trim_verbose_tool_outputs(msgs: list[Message], stats: CompressionStats) -> list[Message]:
        from bandi.tools.result_summary import is_already_shaped

        result: list[Message] = []
        for m in msgs:
            if (
                m.role == MessageRole.TOOL
                and m.content
                and not is_already_shaped(m.content)
                and len(m.content) > _MICRO_TOOL_TRIM_START + _MICRO_TOOL_TRIM_END
            ):
                from bandi.tools.output_store import persist_tool_content

                persisted = persist_tool_content(m.name or "tool", m.content)
                hint = f"\n[full output persisted: {persisted}]" if persisted else ""
                trimmed = (
                    m.content[:_MICRO_TOOL_TRIM_START]
                    + f"\n...[已截断，省略 {len(m.content) - _MICRO_TOOL_TRIM_START - _MICRO_TOOL_TRIM_END} 字符]...\n"
                    + m.content[-_MICRO_TOOL_TRIM_END:]
                    + hint
                )
                result.append(
                    Message(
                        role=m.role,
                        content=trimmed,
                        tool_call_id=m.tool_call_id,
                        name=m.name,
                        turn_index=m.turn_index,
                        meta=m.meta,
                    )
                )
                stats.micro_trimmed += 1
            else:
                result.append(m)
        return result

    @staticmethod
    def _merge_short_messages(msgs: list[Message], stats: CompressionStats) -> list[Message]:
        if len(msgs) < 2:
            return msgs
        result: list[Message] = []
        buffer: list[Message] = []
        for m in msgs:
            content_len = len(m.content or "")
            if (
                m.role in (MessageRole.USER, MessageRole.ASSISTANT)
                and content_len < _MICRO_MAX_SHORT_MSG_LEN
                and not m.tool_calls
            ):
                # 仅同角色连续短消息合并——跨角色合并会破坏对话结构
                # （指令/回答归属错乱）
                if buffer and buffer[-1].role != m.role:
                    merged = _merge_message_buffer(buffer)
                    if merged:
                        result.append(merged)
                        stats.micro_merged += len(buffer)
                    buffer = []
                buffer.append(m)
            else:
                if buffer:
                    merged = _merge_message_buffer(buffer)
                    if merged:
                        result.append(merged)
                        stats.micro_merged += len(buffer)
                    buffer = []
                result.append(m)
        if buffer:
            merged = _merge_message_buffer(buffer)
            if merged:
                result.append(merged)
                stats.micro_merged += len(buffer)
        return result

    @staticmethod
    def _remove_empty_messages(msgs: list[Message]) -> list[Message]:
        return [m for m in msgs if m.content or m.tool_calls]


_MAX_MERGED_CONTENT_CHARS = 500


def _merge_message_buffer(buffer: list[Message]) -> Message | None:
    if not buffer:
        return None
    texts: list[str] = []
    for m in buffer:
        if m.content:
            texts.append(m.content)
    combined = " | ".join(texts)
    if len(combined) > _MAX_MERGED_CONTENT_CHARS:
        omitted = len(combined)
        for _ in range(3):
            marker = format_omission_marker(omitted)
            keep = max(_MAX_MERGED_CONTENT_CHARS - len(marker), 0)
            omitted = len(combined) - keep
        marker = format_omission_marker(omitted)
        keep = max(_MAX_MERGED_CONTENT_CHARS - len(marker), 0)
        combined = combined[:keep] + marker
    merged_meta: dict[str, Any] = {}
    for m in buffer:
        if m.meta:
            merged_meta.update(m.meta)
    return Message(
        role=buffer[0].role,
        content=combined,
        tool_calls=buffer[0].tool_calls,
        tool_call_id=buffer[0].tool_call_id,
        name=buffer[0].name,
        turn_index=buffer[-1].turn_index,
        meta=merged_meta,
    )


def _merge_fallback_summary(previous_summary: str, first_summary: str) -> str:
    # 功能：合并失败时保留旧摘要 + 首块新摘要，避免旧上下文永久丢失。
    old = (previous_summary or "").strip()
    new = (first_summary or "").strip()
    if old and new and old not in new:
        return f"{old}\n\n{new}"
    return new or old


class ProgressiveCompressor:
    def __init__(
        self,
        max_summary_chars: int = 2000,
        target_token_budget: int = 16000,
        max_chunks: int = 4,
        keep_ratio: float = 0.25,
        circuit_breaker: FailureCircuitBreaker | None = None,
        *,
        max_summary_tokens: int | None = DEFAULT_SUMMARY_MAX_TOKENS,
    ):
        self._max_summary_chars = max_summary_chars
        self._target_token_budget = target_token_budget
        self._max_chunks = max_chunks
        self._keep_ratio = keep_ratio
        self._max_summary_tokens = max_summary_tokens
        self._inner = MemoryCompactor(
            max_summary_chars, target_token_budget, max_summary_tokens=max_summary_tokens
        )
        self._circuit_breaker = circuit_breaker or FailureCircuitBreaker()

    def _build_chunks(self, non_system: list[Message], *, coalesce: bool) -> list[list[Message]]:
        # 功能：按消息数分块。coalesce=True（整段已超窗口）时保证每块 ≥
        # _MIN_MESSAGES_FOR_COMPRESSION 条，防止 singleton 子块在 compact 命中最小
        if not coalesce:
            n = len(non_system)
            chunk_size = max(1, n // self._max_chunks)
            chunks = [non_system[i : i + chunk_size] for i in range(0, n, chunk_size)]
            while len(chunks) >= 2 and len(chunks[-1]) <= _MIN_MESSAGES_FOR_COMPRESSION:
                tail = chunks.pop()
                chunks[-1].extend(tail)
            return chunks
        n = len(non_system)
        min_per = _MIN_MESSAGES_FOR_COMPRESSION + 1
        desired = min(self._max_chunks, max(1, n // min_per))
        chunk_size = max(min_per, math.ceil(n / desired))
        chunks = [non_system[i : i + chunk_size] for i in range(0, n, chunk_size)]
        while len(chunks) >= 2 and len(chunks[-1]) <= _MIN_MESSAGES_FOR_COMPRESSION:
            tail = chunks.pop()
            chunks[-1].extend(tail)
        return chunks

    def _effective_target(self, context_window: int | None) -> int:
        # 功能：渐进压缩的块级 token 目标预算——随 context_window 缩放，防绝对
        # 语义：窗口缩放只「下调」，永不高于调用方显式配置的绝对预算
        # target_token_budget（否则显式小预算的契约测试/小窗配置会被抬到 800 破坏）。
        if context_window is None or context_window <= 0:
            return self._target_token_budget
        window_target = max(_WINDOW_TARGET_MIN, int(context_window * _WINDOW_TARGET_ALPHA))
        return min(self._target_token_budget, window_target)

    async def compress(
        self,
        messages: list[Message],
        llm_complete: Callable[[list[dict]], Awaitable[Any]],
        *,
        context_window: int | None = None,
    ) -> tuple[list[Message], CompressionStats]:
        stats = CompressionStats(
            stage="progressive",
            input_messages=len(messages),
            input_chars=sum(len(m.content or "") for m in messages),
        )

        if self._circuit_breaker.is_tripped:
            stats.stage = "progressive_skipped_circuit_breaker"
            return messages, stats

        non_system = [m for m in messages if m.role != MessageRole.SYSTEM]
        # 渐进入口 bail 只按 token：可压缩对话总量 ≤ 琐碎下限即跳过（没什么值得压缩）。
        # 阈值 = min(_effective_target(窗口), _PROGRESSIVE_MIN_WORTH_TOKENS)：尊重显式小预算
        # 进入 progressive 由 compact 逐块判罚）；同时封顶 _effective_target 在极小窗下
        # `max(800,窗口)` 的上漂——默认预算+窗口 1000 会被抬到 1000，把「略超紧迫预算
        # （=窗口*threshold）但低于窗口」的对话整体跳过，progressive 从不尝试致失败计数
        # → 0 块 → 退化 truncate 丢内容。_MIN_MESSAGES_FOR_COMPRESSION 仅用于分块门槛。
        if TokenCounter.count_messages(non_system) <= min(
            self._effective_target(context_window), _PROGRESSIVE_MIN_WORTH_TOKENS
        ):
            self._circuit_breaker.record_success()
            return messages, stats

        # 失败回退分支均返回原始 messages，旧摘要不丢失
        previous_summary, stripped = extract_previous_summary(messages)
        system_msgs = [m for m in stripped if m.role == MessageRole.SYSTEM]
        non_system = [m for m in stripped if m.role != MessageRole.SYSTEM]
        # MemoryCompactor.compact 命中最小条数 bail 而「每块必失败」，渐进压缩整体
        # 退化成 truncate。仅在「整段对话已超模型窗口」时激进合并（避免小窗近临界的
        all_tokens = TokenCounter.count_messages(non_system)
        coalesce = context_window is not None and all_tokens > context_window
        chunks = self._build_chunks(non_system, coalesce=coalesce)

        summaries: list[str] = []
        effective_target = self._effective_target(context_window)
        force_small = (
            context_window is not None
            and _SMALL_WINDOW_MIN_TOKENS <= context_window <= _SMALL_WINDOW_MAX_TOKENS
        )
        logger.debug(
            "PROG-TRACE: n_chunks=%d all_tokens=%d coalesce=%s non_system_roles=%s",
            len(chunks),
            all_tokens,
            coalesce,
            [(m.role.value, len(m.content or "")) for m in non_system],
        )
        fallback_messages: list[Message] = []
        chunk_reports: list[Any] = []
        for chunk in chunks:
            chunk_msgs = system_msgs + chunk
            # keep_tail=False：块内整体进摘要目标。渐进分块场景下块内未摘要的后半段
            # 既不在摘要也不在保留区，会静默丢失；尾部原文由下方全局 keep_ratio 统一保留。
            compressed, success, _ = await self._inner.compact(
                chunk_msgs,
                llm_complete,
                target_token_budget=effective_target,
                keep_tail=False,
                backfill_missing=False,
                report_sink=chunk_reports,
                force_progressive=force_small,
                # 每个块只是长会话的一段：按「部分压缩」提示，防模型编造看不到的内容
                partial=True,
            )
            logger.debug(
                "PROG-TRACE: chunk msgs=%d chars=%d compact_success=%s",
                len(chunk),
                sum(len(m.content or "") for m in chunk),
                success,
            )
            if success:
                summary_text = ""
                for m in compressed:
                    if (
                        m.role == MessageRole.SYSTEM
                        and m.content
                        and SUMMARY_MESSAGE_PREFIX in m.content
                    ):
                        summary_text = m.content.replace(SUMMARY_MESSAGE_PREFIX, "")
                        break
                if summary_text:
                    summaries.append(summary_text)
                    stats.progressive_chunks += 1
                    if force_small:
                        stats.forced_small_chunks += 1
                    self._circuit_breaker.record_success()
                else:
                    # 摘要为空即该块失败——原文必须回填，否则整块内容无痕丢失
                    stats.progressive_failures += 1
                    fallback_messages.extend(chunk)
            else:
                stats.progressive_failures += 1
                fallback_messages.extend(chunk)
                self._circuit_breaker.record_failure()

        if not summaries:
            return messages, stats

        if len(summaries) > 1 or previous_summary:
            merge_user_content = "请合并以下摘要：\n\n" + "\n---\n".join(summaries)
            merge_rules = ""
            if previous_summary:
                # 增量场景：旧摘要以标签角色参与合并（区别于新块摘要）
                merge_rules = f"\n{SUMMARY_UPDATE_RULES}\n"
                merge_user_content = append_previous_summary(merge_user_content, previous_summary)
            merge_instruction = (
                f"你是一个对话摘要合并工具。请将以下 {len(summaries)} 个摘要合并成一个连贯的"
                f"结构化检查点摘要，保留原有已完成项并合并新进展。{merge_rules}"
                f"\n{SUMMARY_ANALYSIS_INSTRUCTION}\n{SUMMARY_SECTIONS_GUIDE}"
            )
            # 1.5：replay 真实 system 前缀（缓存可命中）+ 合并指令后置尾部 user
            final_prompt = [
                *[{"role": "system", "content": m.content or ""} for m in system_msgs],
                {
                    "role": "user",
                    "content": f"{merge_user_content}\n\n{merge_instruction}",
                },
            ]
            try:
                merge_source_parts = list(summaries)
                if previous_summary:
                    merge_source_parts.insert(0, previous_summary)
                merge_source = "\n".join(merge_source_parts)
                final_summary, report, retries, failures = await summarize_with_fidelity(
                    llm_complete,
                    final_prompt,
                    merge_source,
                    postprocess=strip_analysis_scratchpad,
                )
                stats.fidelity_retries += retries
                stats.fidelity_failures += failures
            except Exception as e:  # noqa: BLE001
                logger.warning("ProgressiveCompressor final merge failed: %s", e)
                final_summary = _merge_fallback_summary(previous_summary, summaries[0])
                report = None
            # 波动）——空摘要不能当作成功产物，回退首个块摘要
            if not final_summary.strip():
                final_summary = _merge_fallback_summary(previous_summary, summaries[0])
        # 合并失败/空结果时旧摘要不得丢失：旧摘要 + 首块摘要兜底。
        else:
            final_summary = summaries[0]
            # 在这种终产物路径不失效
            report = chunk_reports[-1] if chunk_reports else None

        # 附录（文件清单+保真兜底）也要占摘要预算——先构造取长度再切摘要
        # 与 MemoryCompactor 的 reserve_chars 口径一致（防止摘要+附录超预算）
        appendix = build_summary_appendix(non_system, report)
        summary_msg = Message(
            role=MessageRole.SYSTEM,
            content=(
                f"{SUMMARY_MESSAGE_PREFIX}"
                f"{self._inner._cut_summary(final_summary, reserve_chars=len(appendix))}"
                f"{appendix}"
            ),
        )
        # truncate 同用 repair_tool_pairs——否则保留区 head 落在 assistant+tool_result
        # 中间产生 API 孤儿
        from bandi.memory.compaction import repair_tool_pairs

        keep_count = int(len(non_system) * self._keep_ratio)
        # 尾部连续性仅保留「未压缩的最近消息」。移除对 keep_count 的 max(1,...) 下限：
        # 单条/极小上下文（n<=3，keep_ratio 0.25 取整为 0）时全部消息都进了压缩块并被摘要
        # 覆盖，强制保留 ≥1 条会把整条已压缩的巨大消息原样塞回 → 产物未缩小 → 误报
        # 行为不变；连续性由 states 的 _inject_continuity_anchor 兜底。
        keep_start = repair_tool_pairs(non_system, max(0, len(non_system) - keep_count))
        # 失败块原文按时间序回填在摘要之后、全局尾区之前；与尾区重叠者去重
        # （失败块可能恰好落在尾部保留区内）。
        fallback_ids = {id(m) for m in fallback_messages}
        tail = [m for m in non_system[keep_start:] if id(m) not in fallback_ids]
        result = system_msgs + [summary_msg] + fallback_messages + tail
        logger.debug(
            "PROG-TRACE: final_summary_chars=%d keep_start=%d result_msgs=%d result_chars=%d",
            len(final_summary),
            keep_start,
            len(result),
            sum(len(m.content or "") for m in result),
        )

        stats.output_messages = len(result)
        stats.output_chars = sum(len(m.content or "") for m in result)
        stats.fidelity_missing_ratio = float(report.missing_ratio) if report is not None else None
        return result, stats


class AutoCompactScheduler:
    def __init__(
        self,
        growth_rate_window: int = 3,
    ):
        self._growth_rate_window = growth_rate_window
        self._token_history: list[int] = []

    def record_tokens(self, total_tokens: int) -> None:
        self._token_history.append(total_tokens)
        if len(self._token_history) > self._growth_rate_window:
            self._token_history.pop(0)

    def select_strategy(
        self,
        total_tokens: int,
        context_window: int,
        compression_threshold: float,
        llm_degraded: bool = False,
    ) -> str:
        usage_ratio = total_tokens / context_window if context_window > 0 else 1.0

        if usage_ratio < compression_threshold:
            return "none"

        if len(self._token_history) >= 2:
            growth_rate = (self._token_history[-1] - self._token_history[0]) / max(
                1, len(self._token_history) - 1
            )
            fast_growth = growth_rate > context_window * 0.1
        else:
            fast_growth = False

        # 进入压缩区后按相对区间分级（span = 触发点→满窗的余量）：
        # 轻度过载且增速平缓先试免费 micro；中度叠加 progressive 摘要；
        # 重度或增速过快直接 LLM 摘要。
        # 宁可再试 LLM 也别 PROMPT_TOO_LONG）
        span = max(0.05, 1.0 - compression_threshold)
        severe = usage_ratio >= compression_threshold + span * 0.75
        if not fast_growth and usage_ratio < compression_threshold + span * 0.25:
            return "micro"
        if usage_ratio < compression_threshold + span * 0.5:
            return "micro" if (llm_degraded and not severe) else "micro_progressive"
        return "progressive" if (not llm_degraded or severe) else "micro"


class CompressionPipeline:
    def __init__(
        self,
        micro: MicroCompressor | None = None,
        progressive: ProgressiveCompressor | None = None,
        scheduler: AutoCompactScheduler | None = None,
        strip_images: bool = True,
    ):
        self._micro = micro or MicroCompressor()
        self._progressive = progressive or ProgressiveCompressor()
        self._scheduler = scheduler or AutoCompactScheduler()
        self._strip_images = strip_images
        self._attachment_tracker = AttachmentTracker()
        self._low_returns = 0

    @property
    def attachment_tracker(self) -> AttachmentTracker:
        return self._attachment_tracker

    def record_progressive_savings(self, before_tokens: int, after_tokens: int) -> None:
        if before_tokens - after_tokens < _LOW_RETURNS_MIN_SAVINGS_TOKENS:
            self._low_returns += 1
        else:
            self._low_returns = 0

    def llm_degraded(self) -> bool:
        return self._low_returns >= _LOW_RETURNS_LIMIT

    def token_history(self) -> tuple[int, ...]:
        return tuple(self._scheduler._token_history)

    @property
    def low_return_count(self) -> int:
        return self._low_returns

    def restore_state(self, token_history: tuple[int, ...], low_returns: int) -> None:
        self._scheduler._token_history = list(token_history)
        self._low_returns = low_returns

    async def compress(
        self,
        messages: list[Message],
        total_tokens: int,
        context_window: int,
        compression_threshold: float,
        llm_complete: Callable[[list[dict]], Awaitable[Any]],
        maintenance: bool = False,
        read_state: dict[str, Any] | None = None,
    ) -> tuple[list[Message], bool, int, CompressionStats]:
        msgs = strip_compact_boundaries(list(messages))
        chars_in = sum(len(m.content or "") for m in msgs)
        n_in = len(msgs)
        if self._strip_images:
            msgs, self._attachment_tracker = strip_images_from_messages(msgs)

        # 释放空间后可能直接改变所需策略；三条压缩策略路径均受益
        prune_stats = CompressionStats()
        msgs = _prune_tool_outputs_by_budget(msgs, prune_stats)

        b4_stats = CompressionStats()
        msgs = _prune_old_post_compact_files(msgs, b4_stats)
        msgs = prune_confirmation_pairs(msgs, b4_stats)

        # 先记录当前 token 再选择策略——select_strategy 用历史判断 fast_growth
        self._scheduler.record_tokens(total_tokens)
        strategy = self._scheduler.select_strategy(
            total_tokens, context_window, compression_threshold, llm_degraded=self.llm_degraded()
        )
        # 只做 prune/micro 不碰 LLM；否则 select_strategy 返回 none 会误走截断兜底
        if maintenance and strategy == "none":
            strategy = "micro"

        result, success, stats = await self._run_strategy(
            strategy, msgs, context_window, compression_threshold, llm_complete
        )
        stats.micro_trimmed += prune_stats.micro_trimmed + b4_stats.micro_trimmed
        if success and strategy != "none":
            marker = build_boundary_message(
                stats.stage or strategy,
                n_in,
                len(result),
                chars_in,
                sum(len(m.content or "") for m in result),
            )
            result = _insert_boundary_marker(result, marker)
            kept_ids = {id(m) for m in result}
            removed = [m for m in msgs if id(m) not in kept_ids]
            attachment = build_post_compact_file_attachment(removed, read_state=read_state)
            if attachment is not None:
                result = _insert_post_compact_files(result, attachment)
        return result, success, 0, stats

    async def _run_strategy(
        self,
        strategy: str,
        msgs: list[Message],
        context_window: int,
        compression_threshold: float,
        llm_complete: Callable[[list[dict]], Awaitable[Any]],
    ) -> tuple[list[Message], bool, CompressionStats]:
        """功能：按选定策略执行压缩，返回 (产物, 是否成功, 统计)。"""
        if strategy == "none":
            return msgs, False, CompressionStats(stage="none")

        if strategy == "micro":
            msgs, stats = self._micro.compress(msgs)
            if self._is_under_budget(msgs, context_window, compression_threshold):
                return msgs, True, stats
            before = TokenCounter.count_messages(msgs)
            msgs, pstats = await self._progressive.compress(
                msgs,
                llm_complete,
                context_window=context_window,
            )
            self._record_progressive_result(before, msgs, pstats, stats)
            stats.progressive_chunks = pstats.progressive_chunks
            stats.progressive_failures = pstats.progressive_failures
            stats.fidelity_missing_ratio = pstats.fidelity_missing_ratio
            # progressive 未收敛时成功必须在「真的缩小了上下文」，否则（关卡跳过
            # 摘要、消息数不足、熔断）误报 success 会让状态机把未收敛当完成 →
            # 假 MAX_TURNS 静默结束；报失败让调用方走截断兜底并触发显式失败语义
            if not self._is_under_budget(msgs, context_window, compression_threshold) and (
                pstats.progressive_failures > 0 or not self._shrunk(before, msgs)
            ):
                return msgs, False, stats
            return msgs, True, stats

        if strategy == "micro_progressive":
            msgs, stats = self._micro.compress(msgs)
            before = TokenCounter.count_messages(msgs)
            msgs, pstats = await self._progressive.compress(
                msgs,
                llm_complete,
                context_window=context_window,
            )
            self._record_progressive_result(before, msgs, pstats, stats)
            stats.progressive_chunks = pstats.progressive_chunks
            stats.progressive_failures = pstats.progressive_failures
            stats.fidelity_missing_ratio = pstats.fidelity_missing_ratio
            if not self._is_under_budget(msgs, context_window, compression_threshold) and (
                pstats.progressive_failures > 0 or not self._shrunk(before, msgs)
            ):
                return msgs, False, stats
            return msgs, True, stats

        before = TokenCounter.count_messages(msgs)
        msgs, stats = await self._progressive.compress(
            msgs,
            llm_complete,
            context_window=context_window,
        )
        self._record_progressive_result(before, msgs, stats, stats)
        if not self._is_under_budget(msgs, context_window, compression_threshold) and (
            stats.progressive_failures > 0 or not self._shrunk(before, msgs)
        ):
            return msgs, False, stats
        return msgs, True, stats

    @staticmethod
    def _shrunk(
        before_tokens: int, messages: list[Message]
    ) -> bool:  # 功能：判定压缩产物是否真实缩减
        after_tokens = TokenCounter.count_messages(messages)
        return after_tokens < before_tokens

    def _record_progressive_result(
        self,
        before_tokens: int,
        after_msgs: list[Message],
        pstats: CompressionStats,
        stats: CompressionStats,
    ) -> None:
        if pstats.progressive_chunks > 0:
            after = TokenCounter.count_messages(after_msgs)
            self.record_progressive_savings(before_tokens, after)

    @staticmethod
    def _is_under_budget(
        messages: list[Message], context_window: int, compression_threshold: float
    ) -> bool:
        total_tokens = TokenCounter.count_messages(messages)
        return total_tokens <= context_window * compression_threshold

    @staticmethod
    def truncate(
        messages: list[Message],
        min_keep: int = 2,
        keep_ratio: float = 0.33,
    ) -> list[Message]:
        return MemoryCompactor.truncate(messages, min_keep, keep_ratio)
