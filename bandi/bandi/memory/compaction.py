# 功能：记忆压缩（MemoryCompactor 上下文压缩/截断）
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from bandi.core.message import Message, MessageRole
from bandi.memory.fidelity import FidelityReport, summarize_with_fidelity
from bandi.utils.token_counter import TokenCounter

logger = logging.getLogger(__name__)
# 4.12：压缩最小消息数常量单源（公开）——compression_pipeline 直接引用本值；
# 语义：条数 ≤ 本值且内容仍小 → 视为「没内容可压」bail（单条巨大消息不受此拦
# 由总量判据放行）
MIN_MESSAGES_FOR_COMPRESSION = 2
_SPLIT_RATIO = 0.5
_SOURCE_TEXT_MAX_TOKENS = 4000
# 大窗口摘要源上限：单次摘要请求最多携带 32K token 源文本（参考 参考实现
# compaction-basic 的 selected-span 摘要：优先完整重放选中区间，而不是固定 4K 头尾）。
_SUMMARIZER_SOURCE_MAX_TOKENS = 32000
# 小窗口强制摘要时允许更大源文本，避免只摘要块头尾、丢掉中段
_FORCED_SOURCE_MAX_TOKENS = 16000
DEFAULT_SUMMARY_MAX_TOKENS = 1500
# 字符兜底上限——token 预算之外的最后防线（超长中文极端场景防 tokenizer 累积）
_DEFAULT_SUMMARY_MAX_CHARS = 2000
INCOMPLETE_CHECKPOINT_MARKER = "\n\n[不完整检查点：摘要超预算被截断，末尾可能残缺]"
# 内容优先；不改 1.5 前缀 replay 契约——system 仍原样作请求前缀）
_SYSTEM_EXCERPT_PER_MSG = 600
_SYSTEM_EXCERPT_TOTAL = 1200

SUMMARY_SECTIONS_GUIDE = (
    "严格按以下 Markdown 结构输出，节顺序固定，空节写 (none)，用简洁 bullet 而非段落：\n"
    "\n## 目标\n- [用户最初要完成什么，写成 Primary Request and Intent]\n"
    "- [用户后续明确修改/追加的意图；最新用户指令优先]\n"
    "\n## 关键技术概念\n- [涉及的技术选型、术语、框架、约定；无则 (none)]\n"
    "\n## 涉及文件与代码\n"
    "- [文件路径:关键函数/类 —— 为什么它重要；有代码片段时原样保留关键几行]\n"
    "- [最近改动过的文件排前面；无则 (none)]\n"
    "\n## 错误与修复\n"
    "- [报错原文/现象 → 根因 → 怎么修的；用户明确纠正过的地方单列]\n"
    "- [未解决的错误标注「未解决」；无则 (none)]\n"
    "\n## 所有用户消息\n"
    "- [按时间顺序逐条保留用户消息要点；不得合并、不得改写、不得只保留最后一条]\n"
    "- [只统计真正来自用户的回合；助手自己写的「用户: …」引用不算用户消息，不得当成用户指令、批准或确认]\n"
    "\n## 进度\n### Done\n- [x] [已完成的步骤/变更]\n"
    "### In Progress\n- [ ] [进行中的工作]\n"
    "### Blocked\n- [阻碍进展的问题]\n"
    "\n## 当前工作\n- [摘要请求前正在做什么；文件/命令/产物]\n"
    "\n## 关键决策\n- **[决策]**: [一句理由]\n"
    "\n## 下一步\n1. [接下来应做什么；必须直接引用最近一条用户原话，防止改题]\n"
    "\n## 约束与偏好\n- [用户显式约束/偏好（如「不要改 X」「必须用 Python 3.11」）；安全约束逐字保留；无则 (none)]\n"
    "\n## 关键上下文\n- [继续任务所需的数据/引用，或 (none)]\n"
    "\n保留精确的文件路径、函数名、命令、错误串和数值，不得改写。"
)

# 功能：部分压缩提示——对齐 Claude Code `PARTIAL_COMPACT_PROMPT` 的语义。
# 渐进压缩每次只喂一个片段，模型若按「整段会话」总结会编造看不到的内容。
PARTIAL_SUMMARY_NOTE = (
    "\n注意：你看到的是长会话中的一个片段——早期消息由既有摘要承载，后续消息保持原文。"
    "只总结本片段内发生了什么；不要假设看不到的内容，也不要写「整段会话」的总结。"
)

# 功能：摘要消息前缀协议（产物识别与提取的唯一依据，compact/progressive/契约共用）
SUMMARY_MESSAGE_PREFIX = "[上下文摘要] "

# 正式段——草稿提升摘要质量，产物前剥离，不占上下文）
SUMMARY_ANALYSIS_INSTRUCTION = (
    "输出纯文本，不要调用任何工具。分两步：\n"
    "第一步：在 <analysis></analysis> 标签内按时间线梳理（用户意图、关键决策、"
    "涉及的文件与代码、错误与修复、用户反馈）——此段仅为草稿，稍后会被丢弃；\n"
    "第二步：在 <summary></summary> 标签内输出正式检查点摘要。"
)

_ANALYSIS_BLOCK_RE = re.compile(r"<analysis>[\s\S]*?</analysis>", re.DOTALL)
_SUMMARY_BLOCK_RE = re.compile(r"<summary>([\s\S]*?)</summary>", re.DOTALL)


def strip_analysis_scratchpad(text: str) -> str:
    """功能：剥离 <analysis> 草稿块并解包 <summary> 正式段（①
    formatCompactSummary）——产物只含正式摘要正文。"""
    stripped = _ANALYSIS_BLOCK_RE.sub("", text or "")
    match = _SUMMARY_BLOCK_RE.search(stripped)
    if match:
        # 函数替换：摘要正文含 C:\Users、\1、\t 时不得被 re.sub 当模板解析。
        stripped = _SUMMARY_BLOCK_RE.sub(lambda _m: match.group(1).strip(), stripped)
    return re.sub(r"\n{3,}", "\n\n", stripped).strip()


# 功能：/compact 报告中的边界含义说明（用户可读文案）
COMPACT_BOUNDARY_HINT = "上下文已在 [压缩边界] 标记处折叠，更早的内容见结构化摘要"

SUMMARY_UPDATE_RULES = (
    "这是对既有检查点摘要的增量更新：保留仍真实的信息；合并新增对话中的进展；"
    "已完成事项从 In Progress 移入 Done；根据最新状态刷新下一步；已失效内容可移除。"
)

CONTINUE_PROMPT = (
    "以上上下文经过压缩。请继续推进当前任务：若已有明确的下一步，直接继续执行；"
    "若不确定如何进行，先向用户澄清。"
)

_PREVIOUS_TAG_OPEN = "\n\n<previous-summary>\n"
_PREVIOUS_TAG_CLOSE = "\n</previous-summary>"


def append_previous_summary(user_content: str, previous_summary: str) -> str:
    """功能：以 <previous-summary> 标签把旧摘要追加到摘要请求 user 内容。"""
    return f"{user_content}{_PREVIOUS_TAG_OPEN}{previous_summary}{_PREVIOUS_TAG_CLOSE}"


def extract_previous_summary(messages: list[Message]) -> tuple[str | None, list[Message]]:
    """功能：识别并剥离历史压缩摘要消息，返回（摘要文本, 剩余消息）。多条摘要
    （fork/多路径压缩/历史恢复）全部按出现顺序合并保留——只留最后一条会令较早
    摘要承载的目标与决策消失。"""
    previous_parts: list[str] = []
    remaining: list[Message] = []
    for m in messages:
        if (
            m.role == MessageRole.SYSTEM
            and m.content
            and m.content.startswith(SUMMARY_MESSAGE_PREFIX)
        ):
            previous_parts.append(m.content[len(SUMMARY_MESSAGE_PREFIX) :])
            continue
        remaining.append(m)
    if not previous_parts:
        return None, remaining
    return "\n\n---\n\n".join(previous_parts), remaining


_FILE_OPS_TOOL_NAMES = frozenset({"read", "write", "edit", "apply_patch", "git_commit"})
FILE_OPS_APPENDIX_PREFIX = "[涉及文件]"
FIDELITY_BACKFILL_PREFIX = "[保真兜底]"
_FILE_OPS_MAX = 5
# 无上限时超长 path 可重新顶穿小窗预算
_FILE_OPS_VALUE_MAX = 160
_FILE_OPS_TOTAL_MAX = 800
_FIDELITY_BACKFILL_MAX = 3
_FIDELITY_BACKFILL_VALUE_MAX = 120
_FIDELITY_BACKFILL_TOTAL_MAX = 150


def extract_file_operations(messages: list[Message], max_files: int = _FILE_OPS_MAX) -> list[str]:
    """功能：从消息的 tool_calls 确定性提取被触碰文件路径（最近优先、去重保序）——
    摘要尾部文件清单来源。"""
    files: list[str] = []
    seen: set[str] = set()
    for m in reversed(messages):
        for tc in m.tool_calls or []:
            if tc.name not in _FILE_OPS_TOOL_NAMES:
                continue
            path = str((tc.arguments or {}).get("path", "")).strip()
            if path and path not in seen:
                seen.add(path)
                files.append(path)
                if len(files) >= max_files:
                    return files
    return files


def build_summary_appendix(messages: list[Message], report: FidelityReport | None = None) -> str:
    """功能：摘要消息尾部确定性附录——文件操作清单（审计 2.16）+ 保真硬失败时
    回填缺失的 filepath/error 原文附件（审计 2.17，限量防小窗压缩被附录压垮）。
    无内容返回空串。"""
    parts: list[str] = []
    files = extract_file_operations(messages)
    if files:
        lines: list[str] = []
        used = 0
        for path in files:
            value = path if len(path) <= _FILE_OPS_VALUE_MAX else path[:_FILE_OPS_VALUE_MAX] + "…"
            if used + len(value) > _FILE_OPS_TOTAL_MAX:
                break
            lines.append(f"- {value}")
            used += len(value)
        if lines:
            parts.append(f"\n\n{FILE_OPS_APPENDIX_PREFIX}\n" + "\n".join(lines))
    if report is not None and report.hard_failed:
        values: list[str] = []
        seen: set[str] = set()
        used_chars = 0
        for e in report.missing:
            if e.kind not in ("filepath", "error"):
                continue
            raw = e.value.strip()
            value = (
                raw
                if len(raw) <= _FIDELITY_BACKFILL_VALUE_MAX
                else raw[:_FIDELITY_BACKFILL_VALUE_MAX] + "…"
            )
            if not value or value in seen:
                continue
            if used_chars + len(value) > _FIDELITY_BACKFILL_TOTAL_MAX:
                break
            seen.add(value)
            values.append(value)
            used_chars += len(value)
            if len(values) >= _FIDELITY_BACKFILL_MAX:
                break
        if values:
            parts.append(f"\n\n{FIDELITY_BACKFILL_PREFIX} {'；'.join(values)}")
    return "".join(parts)


def repair_tool_pairs(messages: list[Message], idx: int) -> int:
    """功能：双向修复切割点附近的 tool pair——①保留区外 TOOL 缺配对 assistant
    向前扩窗；②保留区尾 assistant(tool_calls) 缺配对结果向后扩窗（反向
    不再只靠 guard 补 stub 丢失真实结果）。"""
    # ① 正向（既有）：保留区外 TOOL 结果缺配对 assistant → 向前扩窗
    kept_call_ids = {tc.id for m in messages[idx:] for tc in (m.tool_calls or [])}
    orphans = {
        m.tool_call_id
        for m in messages[idx:]
        if m.role == MessageRole.TOOL and m.tool_call_id and m.tool_call_id not in kept_call_ids
    }
    while orphans:
        for i in range(idx - 1, -1, -1):
            hits = {tc.id for tc in (messages[i].tool_calls or [])} & orphans
            if hits:
                idx = i
                orphans -= hits
                break
        else:
            logger.warning("repair_tool_pairs: %d 个孤儿 tool_result 无配对", len(orphans))
            break
    missing: set[str] = set()
    for m in messages[:idx]:
        for tc in m.tool_calls or []:
            if not any(
                t.role == MessageRole.TOOL and t.tool_call_id == tc.id for t in messages[idx:]
            ):
                missing.add(tc.id)
    if missing:
        new_idx = idx
        for i in range(idx, len(messages)):
            if messages[i].role == MessageRole.TOOL and messages[i].tool_call_id in missing:
                new_idx = i + 1
        if new_idx > idx:
            idx = new_idx
    return idx


class MemoryCompactor:
    def __init__(
        self,
        max_summary_chars: int = _DEFAULT_SUMMARY_MAX_CHARS,
        target_token_budget: int = 16000,
        *,
        max_summary_tokens: int | None = DEFAULT_SUMMARY_MAX_TOKENS,
    ):
        self._max_summary_chars = max_summary_chars
        self._target_token_budget = target_token_budget
        self._max_summary_tokens = max_summary_tokens

    def _cut_summary(self, text: str, reserve_chars: int = 0) -> str:
        """功能：摘要输出截断——token 预算优先，显式字符兜底其次（两者取更严格）；
        发生截断时预留预算追加「不完整检查点」标记（审计 2.3：截断不得无感）。
        RF-17：reserve_chars 为尾部附录预留（原预算不含附录，极端输入总产物超限）。"""
        marker = INCOMPLETE_CHECKPOINT_MARKER
        marker_cost = TokenCounter.count(marker)
        reserve = max(0, reserve_chars)
        truncated = False
        cut = text
        if self._max_summary_tokens is not None:
            # 字符数近似 token 数属保守预留（英文实际占用更少）
            budget = max(1, self._max_summary_tokens - marker_cost - reserve)
            new = TokenCounter.truncate_to_token_budget(cut, budget)
            if new != cut:
                truncated = True
            cut = new
        if self._max_summary_chars > 0:
            budget = max(1, self._max_summary_chars - len(marker) - reserve)
            new = cut[:budget]
            if new != cut:
                truncated = True
            cut = new
        return f"{cut}{marker}" if truncated else cut

    async def compact(
        self,
        messages: list[Message],
        llm_complete: Callable[[list[dict]], Awaitable[Any]],
        *,
        target_token_budget: int | None = None,
        keep_tail: bool = True,
        backfill_missing: bool = True,
        report_sink: list[FidelityReport] | None = None,
        force_progressive: bool = False,
        partial: bool = False,
    ) -> tuple[list[Message], bool, int]:
        from bandi.utils.token_counter import TokenCounter

        effective_budget = target_token_budget or self._target_token_budget
        non_system = [m for m in messages if m.role != MessageRole.SYSTEM]

        total_tokens = TokenCounter.count_messages(non_system)
        # 以 token 判据：可压缩总量 ≤ 预算 → 无从压缩/不值得，跳过（不浪费 LLM）；
        # 小窗口渐进压缩的块天然小于全局块目标，必须强制摘要而不是 bail，
        # 否则每块失败会退化为 truncate。极小块（≤800）仍跳过。
        if total_tokens <= effective_budget:
            if not force_progressive or total_tokens <= 800:
                return messages, False, 1
            effective_budget = max(800, total_tokens // 2)
        if (
            len(non_system) <= MIN_MESSAGES_FOR_COMPRESSION
            and total_tokens <= _SOURCE_TEXT_MAX_TOKENS
            and not force_progressive
        ):
            return messages, False, 1

        original_messages = messages
        previous_summary, messages = extract_previous_summary(messages)
        system_msgs = [m for m in messages if m.role == MessageRole.SYSTEM]
        non_system = [m for m in messages if m.role != MessageRole.SYSTEM]
        cumulative = 0
        split_idx = 0
        if keep_tail:
            split_tokens = effective_budget * _SPLIT_RATIO
            for i, m in enumerate(non_system):
                cumulative += TokenCounter.count_message(m)
                if cumulative > split_tokens and i > 0:
                    split_idx = i
                    break
            else:
                split_idx = max(1, len(non_system) // 2)
        else:
            # keep_tail=False（渐进压缩用）：整块进摘要目标、不留原文尾区。分块场景下
            # 块内后半段若既不被摘要又不被保留，会静默消失——这是渐进压缩丢信息的根因。
            split_idx = len(non_system)

        # 两路径 repair，compact 成功路径缺失）
        split_idx = repair_tool_pairs(non_system, split_idx)
        compress_targets = non_system[:split_idx]
        keep_messages = non_system[split_idx:]

        source_parts = [m.content for m in compress_targets if m.content]
        # system（身份/项目惯例/记忆/实体）约束摘录附 source 头部
        # 只保前缀会丢「最近对话」（续作关键上下文），与 executor._clip_head_tail 同口径
        system_excerpt = "\n".join(
            (m.content or "")[:_SYSTEM_EXCERPT_PER_MSG]
            for m in system_msgs
            if (m.content or "").strip()
        )[:_SYSTEM_EXCERPT_TOTAL]
        if system_excerpt:
            source_parts.insert(0, f"[系统约束摘录]\n{system_excerpt}")
        source_text = "\n".join(source_parts)
        source_max_tokens = max(
            _SOURCE_TEXT_MAX_TOKENS, min(total_tokens, _SUMMARIZER_SOURCE_MAX_TOKENS)
        )
        if force_progressive:
            source_max_tokens = max(
                _SOURCE_TEXT_MAX_TOKENS, min(total_tokens, _FORCED_SOURCE_MAX_TOKENS)
            )
        source_cut = TokenCounter.truncate_head_tail_to_token_budget(source_text, source_max_tokens)
        try:
            instruction = (
                "你是一个对话摘要工具。请将对话压缩为供另一个 LLM 续作任务的"
                f"结构化检查点摘要。\n{SUMMARY_ANALYSIS_INSTRUCTION}\n{SUMMARY_SECTIONS_GUIDE}"
            )
            user_content = f"请压缩以下对话内容为结构化检查点摘要：\n\n{source_cut}"
            if previous_summary:
                instruction = (
                    "你是一个对话摘要工具。对话包含一份既有检查点摘要与新增对话内容，"
                    f"请合并为一份更新后的结构化检查点摘要。\n{SUMMARY_UPDATE_RULES}\n"
                    f"{SUMMARY_SECTIONS_GUIDE}"
                )
                user_content = append_previous_summary(user_content, previous_summary)
            if partial:
                instruction = f"{instruction}{PARTIAL_SUMMARY_NOTE}"
            # 1.5：replay 真实 system 消息作前缀（与正常请求前缀一致，provider 前缀
            # 缓存可命中——摘要请求首条自带工具 system 会致缓存整段失效）；摘要指令
            summary_prompt = [
                *[{"role": "system", "content": m.content or ""} for m in system_msgs],
                {"role": "user", "content": f"{user_content}\n\n{instruction}"},
            ]
            summary_text, report, _retries, _failures = await summarize_with_fidelity(
                llm_complete,
                summary_prompt,
                source_cut,
                postprocess=strip_analysis_scratchpad,
            )
            if report_sink is not None:
                report_sink.append(report)
            appendix = build_summary_appendix(
                compress_targets or non_system, report if backfill_missing else None
            )
            summary_msg = Message(
                role=MessageRole.SYSTEM,
                content=(
                    f"{SUMMARY_MESSAGE_PREFIX}"
                    f"{self._cut_summary(summary_text, reserve_chars=len(appendix))}{appendix}"
                ),
            )
            result = system_msgs + [summary_msg] + keep_messages
            return result, True, 0
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "MemoryCompactor LLM compression failed: %s, returning original messages", e
            )
            return original_messages, False, 1

    @staticmethod
    def truncate(
        messages: list[Message],
        min_keep: int = 2,
        keep_ratio: float = 0.33,
    ) -> list[Message]:
        # 保留区含 TOOL 结果而其 assistant 被裁 = API 孤儿。
        system_msgs = [m for m in messages if m.role == MessageRole.SYSTEM]
        non_system = [m for m in messages if m.role != MessageRole.SYSTEM]
        keep_count = max(min_keep, int(len(messages) * keep_ratio))
        start = max(0, len(non_system) - keep_count)
        start = repair_tool_pairs(non_system, start)
        return system_msgs + non_system[start:]
