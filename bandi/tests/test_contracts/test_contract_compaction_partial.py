"""契约：部分压缩（片段）必须告知模型「你只看到一段」。

对齐 Claude Code `PARTIAL_COMPACT_PROMPT` 的语义。渐过分块压缩每次只喂一段，
若提示词按「整段会话」写，模型会编造看不到的内容（或把片段当全局结论）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bandi.core.message import Message, MessageRole
from bandi.memory.compaction import PARTIAL_SUMMARY_NOTE, MemoryCompactor


def _msgs() -> list[Message]:
    msgs = [Message(role=MessageRole.SYSTEM, content="sys")]
    for i in range(6):
        msgs.append(Message(role=MessageRole.USER, content=f"第{i}轮：核对报表口径与字段"))
        msgs.append(Message(role=MessageRole.ASSISTANT, content=f"第{i}轮结论：口径已确认"))
    return msgs


def _capture():
    seen: list[list[dict]] = []

    async def llm(messages: list[dict], **kwargs):
        seen.append(messages)
        return SimpleNamespace(content="## 目标\n- 核对报表口径")

    return seen, llm


def test_partial_compaction_includes_fragment_notice() -> None:
    compactor = MemoryCompactor(target_token_budget=1)
    seen, llm = _capture()

    asyncio.run(compactor.compact(_msgs(), llm, target_token_budget=1, partial=True))

    assert seen, "压缩必须真的调用模型（预算 1 强制压缩）"
    prompt = "\n".join(str(m.get("content") or "") for m in seen[0])
    assert PARTIAL_SUMMARY_NOTE.strip() in prompt, "部分压缩必须带「只总结本片段」提示"


def test_full_compaction_has_no_fragment_notice() -> None:
    """破坏者视角：全量压缩不得带片段提示（否则模型会漏掉早期内容）。"""
    compactor = MemoryCompactor(target_token_budget=1)
    seen, llm = _capture()

    asyncio.run(compactor.compact(_msgs(), llm, target_token_budget=1))

    prompt = "\n".join(str(m.get("content") or "") for m in seen[0])
    assert PARTIAL_SUMMARY_NOTE.strip() not in prompt
