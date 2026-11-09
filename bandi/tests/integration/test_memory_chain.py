"""
1. run_agent_turn 生产主链路一轮 → episodic episode 落库（k 日层）
2. working 会话草稿快照/工具历史落库（m 日层）
3. search_all 三分类聚合召回（long_term + episodic + working 同查）
4. 压缩纪要 long_term 稳定键落库 → search_all 高优召回闭环（n/o 日层）
"""

from typing import Any

import pytest

from bandi.core.config import BandiConfig
from bandi.core.message import LLMResponse, TokenUsage


@pytest.mark.asyncio
async def test_full_memory_chain_roundtrip(tmp_path: Any, monkeypatch: Any) -> None:
    from pathlib import Path
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.episodic import EpisodicStore
    from bandi.memory.long_term import LongTermMemory
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore
    from bandi.memory.working import ScratchpadStore

    ltm = LongTermMemory(str(tmp_path / "lt.db"))
    episodic = EpisodicStore(str(tmp_path / "ep.db"))
    working = ScratchpadStore(str(tmp_path / "wk.db"))
    memory = MemoryManager(long_term=ltm, episodic=episodic, working=working)

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="结论：蓝鲸是地球上现存最大的动物，栖息于全球海洋。",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=30, completion_tokens=15),
            finish_reason="stop",
        )
    )

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            await run_agent_turn(
                user_input="整理蓝鲸栖息地资料",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
                memory_manager=memory,
            )

    # 1. episodic 层：当轮经历已落库且含任务关键词
    recent = episodic.list_recent(limit=5)
    assert any("蓝鲸" in (e.summary or "") for e in recent), (
        f"episodic 必须有当轮 episode（实际 {[e.summary for e in recent]}）"
    )

    # 2. working 层：会话草稿快照已落库（turn KV）
    snapshot = working.get("turn:1", session_id=sid)
    assert snapshot is not None and "蓝鲸" in str(snapshot), (
        f"working 必须有当轮任务快照（实际 {snapshot}）"
    )

    # 3. search_all 三分类聚合召回
    hits = memory.search_all("蓝鲸", session_id=sid)
    assert hits.get("episodic"), "episodic 分类必须命中"
    assert hits.get("working"), "working 分类必须命中"
    assert all("蓝鲸" in h.value for cat in ("episodic", "working") for h in hits[cat])

    # 4. 压缩纪要链：long_term 稳定键落库 → search_all 高优召回（o 日语义串联）
    from bandi.memory.long_term import MemoryItem

    ltm.store(
        MemoryItem(
            item_id=f"compaction:{sid[:8]}:1",
            content="[上下文摘要] 用户正在研究蓝鲸栖息地",
            importance=0.7,
        )
    )
    refetched = memory.search_all("蓝鲸", session_id=sid)
    lt_values = [h.value for h in refetched.get("long_term", [])]
    assert any("上下文摘要" in v for v in lt_values), (
        f"压缩纪要必须经 search_all 召回（实际 {lt_values}）"
    )
