"""OpenAI 兼容 API（DeepSeek/OpenAI/…）拒绝两种历史形态：

* ``role: tool`` 消息没有前置 assistant(tool_calls) 配对——渐进压缩、朴素裁剪
  损坏会话都可能在中途留下此类孤儿；
* assistant(tool_calls) 声明的 id 缺少后续 tool 结果——用户中途停止即产生。

两者都会让下一次请求 HTTP 400。本模块在每次 LLM 调用前对请求面做防御性清理
适配 Message 对象形态）。
"""

from __future__ import annotations

import logging

from bandi.core.message import Message, MessageRole, ToolCall

logger = logging.getLogger(__name__)

INTERRUPTED_TOOL_RESULT = '{"status":"interrupted","error":"工具调用被中断，未收到结果"}'


def drop_orphan_tool_messages(
    messages: list[Message], extra_pending_ids: set[str] | None = None
) -> list[Message]:
    """功能：剔除无配对前置的 tool 消息——assistant(tool_calls) 声明的 pending
    id 集合之外的一律视为孤儿；纯文本 assistant 与 user 等会话角色重置 pending
    SYSTEM 注入（压缩边界/摘要/提示）不重置——插在回合中间的真实 tool 结果
    不得被判孤儿整批删除。
    extra_pending_ids：当轮 current_tool_calls 的 id（结果回填先于清理执行
    其 TOOL 消息尚无历史 assistant 可配对，属合法保留）。"""
    kept: list[Message] = []
    pending_ids: set[str] | None = None
    for msg in messages:
        if msg.role == MessageRole.ASSISTANT:
            calls = msg.tool_calls or []
            pending_ids = {tc.id for tc in calls if tc.id} if calls else None
            kept.append(msg)
        elif msg.role == MessageRole.TOOL:
            tcid = (msg.tool_call_id or "").strip()
            if not tcid or pending_ids is None or tcid not in pending_ids:
                if extra_pending_ids and tcid in extra_pending_ids:
                    kept.append(msg)
                    continue
                logger.warning("orphan tool message dropped (tool_call_id=%r)", tcid)
                continue
            kept.append(msg)
        elif msg.role == MessageRole.SYSTEM:
            # 系统注入是同回合内插队（压缩边界/摘要/置信度/反思提示）——
            # 不得重置 pending，否则真实 tool 结果被判孤儿
            kept.append(msg)
        else:
            pending_ids = None
            kept.append(msg)
    return kept


def inject_missing_tool_result_stubs(messages: list[Message]) -> list[Message]:
    """功能：为 assistant(tool_calls) 中缺失结果的 id 补中断 stub——插入到紧随
    其后的连续 tool 段末尾；全部齐备时原样返回同一列表对象。"""
    result = list(messages)
    idx = 0
    while idx < len(result):
        msg = result[idx]
        if msg.role != MessageRole.ASSISTANT or not (msg.tool_calls or []):
            idx += 1
            continue

        id_by_call: dict[str, ToolCall] = {}
        for tc in msg.tool_calls or []:
            if tc.id and tc.id not in id_by_call:
                id_by_call[tc.id] = tc
        if not id_by_call:
            idx += 1
            continue

        end = idx + 1
        while end < len(result) and result[end].role == MessageRole.TOOL:
            end += 1
        answered = {(result[k].tool_call_id or "").strip() for k in range(idx + 1, end)}
        missing = [cid for cid in id_by_call if cid not in answered]
        if not missing:
            idx = end
            continue

        stubs = [
            Message(
                role=MessageRole.TOOL,
                content=INTERRUPTED_TOOL_RESULT,
                tool_call_id=cid,
            )
            for cid in missing
        ]
        result[end:end] = stubs
        idx = end + len(stubs)
    return result


def guard_messages(
    messages: list[Message], extra_pending_ids: set[str] | None = None
) -> list[Message]:
    """功能：LLM 请求面统一入口（Message 对象层，原地语义返回新列表）——
    先补 stub 再剔孤儿（stub 补齐后其结果行不再视作孤儿）。在 provider dict
    组装之前调用，避免跨协议 dict 往返不对称。"""
    return drop_orphan_tool_messages(
        inject_missing_tool_result_stubs(messages), extra_pending_ids=extra_pending_ids
    )


__all__ = [
    "drop_orphan_tool_messages",
    "guard_messages",
    "inject_missing_tool_result_stubs",
]
