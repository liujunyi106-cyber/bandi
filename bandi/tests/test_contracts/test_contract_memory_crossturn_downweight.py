# 功能：MemoryContextSource 跨轮注入降权契约
"""
首轮无降权：fresh session 下所有未注入记忆保持原 score 顺序（降权 no-op）
跨轮降权：已注入过的记忆（同 session、同 entry.key）在后续轮排到未注入
   记忆之后（嵌入式降权）——高相关仍可召回，但减少重复占位
裁剪不计 seen：被预算裁掉的候选不递增 seen，不误降后续轮
仍可召回：已注入项降权排后，但预算充足时仍被注入（非完全排除）
session 隔离：不同 session 的 seen 互不影响（降权按 session_id 隔离）
跨轮把已注入记忆移到未注入之后（首轮 + 未注入语义不变）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from bandi.context.sources import memory_source as ms
from bandi.core.message import Message, MessageRole

_CHARS = ms._MEMORY_ITEM_CHARS


@pytest.fixture(autouse=True)
def _reset_seen() -> Any:
    """每测试隔离 SEEN_BY_SESSION（模块级进程态）。"""
    with ms._SEEN_LOCK:
        ms.SEEN_BY_SESSION.clear()
    yield
    with ms._SEEN_LOCK:
        ms.SEEN_BY_SESSION.clear()


def _hit(key: str, value: str, score: float = 0.5, category: str = "long_term") -> Any:
    from bandi.memory.memory_provider import MemoryHit

    return MemoryHit(
        key=key,
        value=value,
        metadata={"score": score, "category": category, "key": key},
    )


class _FixedManager:
    """返回固定有序 hit 列表——用于断言排序不变量。"""

    def __init__(self, hits: list[Any]) -> None:
        self.hits = hits

    def search_unified(self, query: str, **kw: Any) -> list[Any]:
        return list(self.hits)


class TestFirstTurnNoDownweight:
    @pytest.mark.asyncio
    async def test_untouched_order_on_fresh_session(self) -> None:
        """首轮：未注入记忆保持原 score 顺序（降权 no-op）。"""
        mgr = _FixedManager(
            [_hit("k1", "蓝鲸是海洋哺乳动物", 0.9), _hit("k2", "部署流程备份", 0.4)]
        )
        src = ms.MemoryContextSource(memory_manager=mgr)
        items = await src.collect(Message(role=MessageRole.USER, content="蓝鲸"), session_id="s1")
        keys = [(it.metadata or {}).get("key") for it in items]
        assert keys == ["k1", "k2"], "首轮必须保持原 score 顺序"

    @pytest.mark.asyncio
    async def test_empty_manager_no_items(self) -> None:
        src = ms.MemoryContextSource(memory_manager=None)
        assert await src.collect(Message(role=MessageRole.USER, content="x"), session_id="s1") == []


class TestCrossTurnDownweight:
    @pytest.mark.asyncio
    async def test_injected_moves_behind_untouched_next_turn(self) -> None:
        """跨轮：第一轮注入的 k1，第二轮排到未注入的 k2 之后（降权）。
        用首轮只返回 k1、次轮返回 [k1,k2] 控制注入视角——仅实际注入的才算 seen。"""
        src = ms.MemoryContextSource(
            memory_manager=_FixedManager([_hit("k1", "蓝鲸是海洋哺乳动物", 0.9)])
        )
        msg = Message(role=MessageRole.USER, content="蓝鲸")
        first = await src.collect(msg, session_id="s1")
        assert [(it.metadata or {}).get("key") for it in first] == ["k1"]

        src2 = ms.MemoryContextSource(
            memory_manager=_FixedManager(
                [_hit("k1", "蓝鲸是海洋哺乳动物", 0.9), _hit("k2", "部署流程备份", 0.4)]
            )
        )
        second = await src2.collect(msg, session_id="s1")
        keys2 = [(it.metadata or {}).get("key") for it in second]
        assert keys2 == ["k2", "k1"], f"已注入 k1 必须降权排到未注入 k2 之后（实际 {keys2}）"


class TestBudgetTrimNotCounted:
    @pytest.mark.asyncio
    async def test_trimmed_candidate_not_marked_seen(self) -> None:
        """裁剪不计 seen：预算装不下 k2 时，k2 虽在候选但不计 seen——
        后续轮 k2 仍视为未注入（不误降权）。"""
        from bandi.utils.token_counter import TokenCounter

        v1 = "短记忆A" * 60
        v2 = "超长记忆内容" * 60
        est1 = TokenCounter.estimate(v1[:_CHARS])
        est2 = TokenCounter.estimate(v2[:_CHARS])
        # budget 恰好装下 k1、装不下 k2（k2 snippet est 显著大于剩余）
        budget = est1 + 2
        assert budget < est1 + est2, "边界前提：k2 的 snippet 必须放不进预算"
        src = ms.MemoryContextSource(
            memory_manager=_FixedManager([_hit("k1", v1, 0.9), _hit("k2", v2, 0.4)])
        )
        msg = Message(role=MessageRole.USER, content="查")
        first = await src.collect(msg, session_id="s1", budget=budget)
        first_keys = [(it.metadata or {}).get("key") for it in first]
        assert "k2" not in first_keys, "k2 snippet 超预算应被裁掉"
        assert "k1" in first_keys, "k1 短应被注入"

        # 第二轮 k2 换回短 value——因首轮被裁未计入 seen，应排前
        src2 = ms.MemoryContextSource(
            memory_manager=_FixedManager([_hit("k1", v1, 0.9), _hit("k2", "短记忆B", 0.4)])
        )
        second = await src2.collect(msg, session_id="s1", budget=4096)
        keys2 = [(it.metadata or {}).get("key") for it in second]
        assert keys2 == ["k2", "k1"], f"k2 未注入应排前（实际 {keys2}）"


class TestStillRecalled:
    @pytest.mark.asyncio
    async def test_seen_item_still_injected_when_budget_roomy(self) -> None:
        """仍可召回：已注入项降权排后，但预算充足时仍被注入（非完全排除）。"""
        hits = [_hit("k1", "蓝鲸记忆", 0.9), _hit("k2", "部署备份", 0.4)]
        src = ms.MemoryContextSource(memory_manager=_FixedManager(hits))
        msg = Message(role=MessageRole.USER, content="查")
        await src.collect(msg, session_id="s1")
        second = await src.collect(msg, session_id="s1", budget=4096)
        keys2 = [(it.metadata or {}).get("key") for it in second]
        assert "k1" in keys2, "已注入项降权后仍应可召回"
        assert len(keys2) == 2, "预算充足时两条都应注入"


class TestSessionIsolation:
    @pytest.mark.asyncio
    async def test_seen_is_per_session(self) -> None:
        """session 隔离：s1 注入不影响 s2 的降权顺序。"""
        hits = [_hit("k1", "蓝鲸", 0.9), _hit("k2", "部署", 0.4)]
        src = ms.MemoryContextSource(memory_manager=_FixedManager(hits))
        msg = Message(role=MessageRole.USER, content="查")
        await src.collect(msg, session_id="s1")
        s2 = await src.collect(msg, session_id="s2")
        keys2 = [(it.metadata or {}).get("key") for it in s2]
        assert keys2 == ["k1", "k2"], "s2 fresh 不受 s1 降权影响"


class TestRealSeenStoreIsolation:
    @pytest.mark.asyncio
    async def test_collect_never_writes_real_user_seen_store(self) -> None:
        """/seen 持久化必须被隔离到 tmp——真实用户目录
        ~/.bandi/seen_memory.json 的存在性与 mtime 不得被测试改写
        （路径经 os.path.expanduser 解析，不受 Path.home patch 影响）。"""
        real_store = Path(os.path.expanduser("~")) / ".bandi" / "seen_memory.json"
        before = real_store.stat() if real_store.exists() else None

        src = ms.MemoryContextSource(
            memory_manager=_FixedManager([_hit("k1", "蓝鲸是海洋哺乳动物", 0.9)])
        )
        await src.collect(Message(role=MessageRole.USER, content="蓝鲸"), session_id="fm62-real")

        if before is None:
            assert not real_store.exists(), "测试不得在真实用户目录创建 seen_memory.json"
        else:
            after = real_store.stat()
            assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size), (
                "测试不得改写真实 seen_memory.json"
            )
