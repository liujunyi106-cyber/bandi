"""功能：压缩区间选择——保留最近原文尾部，只压头部旧历史。

参考实现（deepseek-harness `compaction-basic/region.ts` 的 `selectCompactableRange`）：
从尾部向前累加 token 直到达到 retain 预算，把这段**原样保留**，压缩它之前的头部。
这样模型始终能看到最近几轮的真实原文，而不是只有一份摘要。

与「整段分块摘要」的差别：后者会把最近的对话也一起改写成摘要，导致
① 最近上下文失真；② 每轮都要重压大段历史，单次压缩 30-40 秒；③ 压完仍可能超阈值
（因为尾部新增长的内容又被算进下一轮），于是反复压缩直到迭代预算耗尽。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bandi.core.message import Message, MessageRole
from bandi.utils.token_counter import TokenCounter

logger = logging.getLogger(__name__)

# 功能：尾部保留比例——按有效窗口算，参考实现取 0.16。
DEFAULT_RETAIN_RATIO = 0.16
# 功能：尾部保留的绝对值下限——窗口很小时也要留住最近几轮真实对话。
MIN_RETAIN_TOKENS = 2_000


@dataclass(frozen=True)
class CompactRange:
    """功能：一次压缩的区间——head 进摘要，tail 原样保留。"""

    head: list[Message]
    tail: list[Message]
    head_tokens: int
    tail_tokens: int


def _tool_call_spans(message: Message) -> int:
    """功能：该消息携带的工具调用数（用于配对判定）。"""
    calls = getattr(message, "tool_calls", None) or []
    return len(calls) if isinstance(calls, list) else 0


def _is_tool_result(message: Message) -> bool:
    """功能：是否为工具结果消息。"""
    return message.role == MessageRole.TOOL


def _balanced_cut(messages: list[Message], cut: int) -> int:
    """功能：把切点向前调整到「工具调用/结果配对完整」的位置。

    切点之后若出现工具结果、而其调用落在切点之前，模型会看到无主的工具输出。
    因此切点必须往前退到该结果所属调用之前。
    """
    if cut <= 0 or cut >= len(messages):
        return cut
    # 尾部以工具结果开头 → 一路向前退到它所属的调用之前
    while cut > 0 and _is_tool_result(messages[cut]):
        cut -= 1
    # 退完之后，若切点前一条是带工具调用的助手消息，说明它的结果被留在了尾部，
    # 这是允许的（调用与结果分处两侧才是非法），所以再退一步把调用也留在头部外
    while cut > 0 and _tool_call_spans(messages[cut - 1]) > 0:
        cut -= 1
    return cut


def select_compact_range(
    messages: list[Message],
    *,
    retain_ratio: float = DEFAULT_RETAIN_RATIO,
    effective_window: int = 0,
    retain_tokens: int | None = None,
) -> CompactRange | None:
    """功能：选出「头部待压 + 尾部保留」的切分；无意义时不返回区间。

    Args:
        messages: 完整消息序列（含 SYSTEM）。
        retain_ratio: 尾部保留占有效窗口的比例。
        effective_window: 有效上下文窗口；<=0 时用绝对值下限。
        retain_tokens: 显式指定尾部预算（优先于比例）。

    Returns:
        可压缩区间；消息太少或无可压头部时返回 None。
    """
    if len(messages) <= 1:
        return None

    budget = retain_tokens
    if budget is None:
        budget = int(effective_window * retain_ratio) if effective_window > 0 else MIN_RETAIN_TOKENS
    budget = max(MIN_RETAIN_TOKENS, budget)

    # 从尾部向前累计，找到保留段的起点
    accumulated = 0
    cut = len(messages)
    for index in range(len(messages) - 1, -1, -1):
        accumulated += TokenCounter.count_message(messages[index])
        cut = index
        if accumulated >= budget:
            break
    cut = _balanced_cut(messages, cut)

    if cut <= 0:
        # 整段都在保留预算内——没有可压的头部
        return None

    head = messages[:cut]
    tail = messages[cut:]
    # SYSTEM 不可压缩（身份/规则/工具面），留在头部之外单独处理
    head_compactable = [m for m in head if m.role != MessageRole.SYSTEM]
    if not head_compactable:
        return None

    return CompactRange(
        head=head,
        tail=tail,
        head_tokens=TokenCounter.count_messages(head_compactable),
        tail_tokens=TokenCounter.count_messages(tail),
    )
