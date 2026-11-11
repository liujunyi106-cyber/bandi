# 功能：会话压缩服务（CLI /compact 与 API 端点共用——当前会话内替换）
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SessionCompactResult:
    """功能：压缩结果（成功 = 当前会话内替换；失败含原因码）。"""

    success: bool
    new_session_id: str = ""
    stage: str = ""
    before_messages: int = 0
    after_messages: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
    has_summary: bool = False
    reason: str = ""


async def compact_session(store: Any, session_id: str, provider: Any) -> SessionCompactResult:
    """功能：压缩指定会话（单块全量摘要 → 当前会话内替换）。

    reason 语义：session not found / too few messages / no reduction。
    """
    from bandi.core.message import Message, MessageRole, message_from_entry, message_to_snapshot
    from bandi.memory.compaction import SUMMARY_MESSAGE_PREFIX, MemoryCompactor
    from bandi.memory.compression_pipeline import CompressionPipeline, ProgressiveCompressor

    if store.get_session(session_id) is None:
        return SessionCompactResult(success=False, reason="session not found")
    entries = store.get_branch(session_id)
    messages = [message_from_entry(e) for e in entries if e.get("entry_type") == "message"]
    if len(messages) < 3:
        return SessionCompactResult(success=False, reason="too few messages")

    total_tokens = sum(provider.count_tokens(m.content or "") for m in messages)
    # total==window + threshold=0.5 使 usage_ratio=1.0 恒落 progressive 区且压缩后预算判定宽松
    pipeline = CompressionPipeline(
        progressive=ProgressiveCompressor(
            target_token_budget=max(64, total_tokens // 2),
            max_chunks=1,
        )
    )
    from bandi.llm.accounted import accounted_complete

    async def _accounted_complete(prompt_messages: list[dict]) -> Any:
        return await accounted_complete(
            provider,
            prompt_messages,
            label="session_compact",
            session_id=session_id,
            agent_id="session_compact",
        )

    compressed, success, _, stats = await pipeline.compress(
        messages, total_tokens, total_tokens or 1, 0.5, _accounted_complete
    )
    forced = False
    if not success or stats.stage == "none" or len(compressed) >= len(messages):
        # 手动 /compact 必须产出检查点：短会话低于渐进压缩的“值得压缩”门槛时，
        # 强制走一次全量结构化摘要，而不是返回 no reduction 让用户无感。
        compactor = MemoryCompactor(target_token_budget=max(64, total_tokens // 2))
        compressed, success, _ = await compactor.compact(
            messages,
            _accounted_complete,
            target_token_budget=max(64, total_tokens // 2),
            keep_tail=False,
            force=True,
        )
        forced = True
        if success:
            stats.stage = "manual"
    if not success or stats.stage == "none":
        return SessionCompactResult(success=False, reason="no reduction")
    if not forced and len(compressed) >= len(messages):
        return SessionCompactResult(success=False, reason="no reduction")

    summary_text = next(
        (
            str(message.content or "")
            for message in compressed
            if str(message.content or "").startswith(SUMMARY_MESSAGE_PREFIX)
        ),
        "",
    )
    preserved = [message for message in compressed if message.role != MessageRole.SYSTEM]
    after_tokens = sum(provider.count_tokens(message.content or "") for message in compressed)
    turn_index = max(
        (int(getattr(message, "turn_index", 0) or 0) for message in messages), default=0
    )
    card = {
        "before_messages": len(messages),
        "after_messages": len(compressed),
        "before_tokens": total_tokens,
        "after_tokens": after_tokens,
        "stage": stats.stage,
        "summary": summary_text[len(SUMMARY_MESSAGE_PREFIX) :].strip() if summary_text else "",
    }
    store.append_summary_entry(
        session_id,
        turn_index,
        summary_text,
        metadata={
            "success": True,
            "stage": stats.stage,
            "message_count": len(preserved),
            "before_messages": len(messages),
            "after_messages": len(compressed),
            "before_tokens": total_tokens,
            "after_tokens": after_tokens,
            "manual_compact": True,
            "active_messages": [message_to_snapshot(message) for message in preserved],
        },
    )
    store.append_preserved_segment(session_id, preserved)
    store.append_message(
        session_id,
        Message(
            role=MessageRole.SYSTEM,
            content="",
            turn_index=turn_index,
            meta={"compaction_card": card},
        ),
    )

    return SessionCompactResult(
        success=True,
        new_session_id=session_id,
        stage=stats.stage,
        before_messages=len(messages),
        after_messages=len(compressed),
        before_tokens=total_tokens,
        after_tokens=after_tokens,
        has_summary=bool(summary_text),
    )
