# 功能：会话压缩服务
# append-only 约束下原会话原样保留，新会话承载压缩结果）。
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from bandi.session.manager import INHERITED_METADATA_KEYS


@dataclass(frozen=True)
class SessionCompactResult:
    """功能：压缩结果（成功 = fork 新会话；失败含原因码）。"""

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
    """功能：压缩指定会话（单块全量摘要 → fork 新会话；原会话保留）。

    reason 语义：session not found / too few messages / no reduction。
    """
    from bandi.core.message import message_from_entry
    from bandi.memory.compaction import SUMMARY_MESSAGE_PREFIX
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
    compressed, success, _, stats = await pipeline.compress(
        messages, total_tokens, total_tokens or 1, 0.5, provider.complete
    )
    if not success or stats.stage == "none" or len(compressed) >= len(messages):
        return SessionCompactResult(success=False, reason="no reduction")

    original = store.get_session(session_id) or {}
    original_title = str(original.get("title") or "")
    source_meta = _source_metadata(original.get("metadata"))
    new_sid = store.create_session(title=f"{original_title} (compacted)".strip(), mode="execute")
    inherited = {key: source_meta[key] for key in INHERITED_METADATA_KEYS if source_meta.get(key)}
    inherited["compacted_from_session_id"] = session_id
    store.update_session(new_sid, metadata=json.dumps(inherited, ensure_ascii=False))
    for message in compressed:
        store.append_message(new_sid, message)

    return SessionCompactResult(
        success=True,
        new_session_id=new_sid,
        stage=stats.stage,
        before_messages=len(messages),
        after_messages=len(compressed),
        before_tokens=total_tokens,
        after_tokens=sum(provider.count_tokens(m.content or "") for m in compressed),
        has_summary=any(SUMMARY_MESSAGE_PREFIX in (m.content or "") for m in compressed),
    )


def _source_metadata(raw: Any) -> dict[str, Any]:
    # 功能：解析来源会话 metadata（JSON 串/字典；损坏或非字典返回空）
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
