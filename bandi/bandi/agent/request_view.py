# 功能：请求前折叠视图（fold-view）——只影响发往 LLM 的请求视图，不修改
# 折叠视图：仅折叠「已完成轮次」的中间工具细节，正在进行的轮次不干预。

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from bandi.core.message import Message, MessageRole, ToolCall

logger = logging.getLogger(__name__)

FOLD_PLACEHOLDER = "[旧工具输出已折叠，完整内容保留在会话历史中]"
# 回传长度（~1.3K）⇒ 已摘要消息必被折叠、落盘路径随折叠丢失。抬到 4000
# 并跳过含摘要/落盘标记的消息（下）——它们是压缩产物，体积已小且携带取回出路。
DEFAULT_FOLD_MAX_TOOL_CHARS = 4000
# 旧轮 assistant tool_calls 参数折叠阈值：参数 JSON 超总阈值时，将超长字符串值
# 截断（write/edit/apply_patch 的整文件内容不再永久留史；短值 path/command 保留）
_OLD_TOOL_ARGS_JSON_MAX = 1200
_OLD_TOOL_ARG_STRING_MAX = 240
_OLD_TOOL_ARG_FOLD_HINT = "...[参数已折叠，完整内容保留在会话历史中]"
# 保留最近 N 个 LLM 轮的工具原文（当前轮刚回填结果必在 max 轮）。keep=2 与
# compression_pipeline._PRUNE_PROTECT_RECENT_TURNS=2 同族对齐——长工具链中
# 「turn t 的结果在 turn t+1/t+2 再被引用」（对比两次 read/bash 输出）时
_KEEP_RECENT_TURNS = 2


def _fold_tool_call_arguments(tool_call: ToolCall) -> ToolCall:
    """功能：旧轮 assistant tool_calls 参数折叠——参数 JSON 超阈值时才处理
    超长字符串值截断并标注；未触发时返回原对象（identity 判定用）。"""
    try:
        raw = json.dumps(tool_call.arguments or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raw = ""
    if len(raw) <= _OLD_TOOL_ARGS_JSON_MAX:
        return tool_call
    folded: dict[str, Any] = {}
    for key, value in (tool_call.arguments or {}).items():
        if isinstance(value, str) and len(value) > _OLD_TOOL_ARG_STRING_MAX:
            folded[key] = value[:_OLD_TOOL_ARG_STRING_MAX] + _OLD_TOOL_ARG_FOLD_HINT
        else:
            folded[key] = value
    return ToolCall(id=tool_call.id, name=tool_call.name, arguments=folded)


def build_request_view(
    messages: list[Message],
    *,
    fold_max_tool_chars: int = DEFAULT_FOLD_MAX_TOOL_CHARS,
    keep_recent_turns: int = _KEEP_RECENT_TURNS,
    fold_persist: Callable[[str, str], str | None] | None = None,
) -> list[Message]:
    """功能：构建请求前视图——turn_index 早于「最近活跃轮」的超长 TOOL 输出替换为
    占位符（保留 role/tool_call_id/name 防 API 孤儿与配对破坏）；旧轮
    assistant(tool_calls) 的超长字符串参数同轮折叠（id/name/短值保留）。

    - fold_max_tool_chars <= 0 → 关闭折叠（视图与输入同内容）
    - 折叠只替换超阈值消息的 content/arguments，其余消息原对象直传（零拷贝语义）
    - 占位符短于阈值 → 对折叠视图重复调用天然幂等
    - keep_recent_turns：保留最近 N 轮工具原文（默认 2，与剪枝保护轮对齐）
    - fold_persist：折叠时把原文落盘的回调（工具名, 全文）→ 路径；提供时占位符
      携带自动取回路径（read 读回），失败/None 回退「重新调用」出路
    """
    if fold_max_tool_chars <= 0 or not messages:
        return list(messages)
    max_turn = max(
        (m.turn_index for m in messages if m.role != MessageRole.SYSTEM),
        default=0,
    )
    # 单轮会话（或全部轮次归属缺失）无折叠价值——保守跳过
    fold_before = max_turn - max(1, keep_recent_turns)
    if fold_before <= 0:
        return list(messages)
    result: list[Message] = list(messages)
    for i, m in enumerate(result):
        if m.role == MessageRole.ASSISTANT and m.tool_calls and m.turn_index < fold_before:
            folded_calls = [_fold_tool_call_arguments(tc) for tc in m.tool_calls]
            if any(new is not old for new, old in zip(folded_calls, m.tool_calls)):
                result[i] = Message(
                    role=MessageRole.ASSISTANT,
                    content=m.content,
                    tool_calls=folded_calls,
                    turn_index=m.turn_index,
                    meta=m.meta,
                )
            continue
        if m.role != MessageRole.TOOL:
            continue
        content = m.content or ""
        if len(content) <= fold_max_tool_chars:
            continue
        # 已摘要/已落盘的消息是压缩产物：体积已被压小且携带「取回出路」（落盘
        # 路径 / offset 指引）——折叠会把出路一起丢掉，得不偿失，保留原文
        if "[full output persisted:" in content or "[tool:summary]" in content:
            continue
        if m.turn_index < fold_before:
            outlet = (
                f"{FOLD_PLACEHOLDER}；如需其完整内容，请重新调用该工具"
                f"（{m.name or 'tool'}）并用更精确的参数分段获取。"
            )
            if fold_persist is not None:
                path: str | None = None
                try:
                    path = fold_persist(m.name or "tool", content)
                except Exception:
                    logger.warning("fold_persist failed for tool %s", m.name, exc_info=True)
                if path:
                    outlet = (
                        f"{FOLD_PLACEHOLDER}；完整内容已存至 {path}"
                        f"（use read with offset/limit 读回），或重新调用该工具"
                        f"（{m.name or 'tool'}）分段获取。"
                    )
            result[i] = Message(
                role=MessageRole.TOOL,
                # 模型 read 即可读回，不必重跑昂贵/有副作用的工具）。
                content=outlet,
                tool_call_id=m.tool_call_id,
                name=m.name,
                turn_index=m.turn_index,
                meta=m.meta,
            )
    return result


def inject_state_block(messages: list[Message], block: str) -> list[Message]:
    """功能：把常驻任务状态块挂到最后一轮 user 消息之前——只改请求视图、不写回
    历史（不累积），历史消息保持原对象；重复注入按前缀剥离后重建（幂等）。

    - block 为空 → 返回输入副本（视图与输入等价）
    - 无 user 消息 → 以 user 角色追加到末尾
    """
    if not (block or "").strip():
        return list(messages)
    from bandi.memory.context_projection import STATE_PREFIX

    result = list(messages)
    target = -1
    for index in range(len(result) - 1, -1, -1):
        if result[index].role == MessageRole.USER:
            target = index
            break

    def _strip_existing(text: str) -> str:
        body = text or ""
        if not body.startswith(STATE_PREFIX):
            return body
        rest = body[len(STATE_PREFIX) :]
        separator = rest.find("\n\n")
        return (rest[separator + 2 :] if separator >= 0 else "").strip()

    if target < 0:
        result.append(Message(role=MessageRole.USER, content=block))
        return result

    base = _strip_existing(result[target].content)
    content = f"{block}\n\n{base}" if base else block
    original = result[target]
    result[target] = Message(
        role=original.role,
        content=content,
        tool_calls=original.tool_calls,
        tool_call_id=original.tool_call_id,
        name=original.name,
        turn_index=original.turn_index,
        meta=original.meta,
    )
    return result


__all__ = ["build_request_view", "inject_state_block", "FOLD_PLACEHOLDER"]
