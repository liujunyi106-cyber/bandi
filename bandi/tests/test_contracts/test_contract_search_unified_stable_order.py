"""契约：search_unified 同分排序确定性（不随候选收集序漂移）。

背景：门面层多候选词召回时，完全同分条目的
顺序依赖「收集先后」（稳定排序保留收集序）——同分 episodic 兄弟互相挤占
最终配额（单条目标入选不稳定）。修复 = 排序 key 末端加稳定 tie-breaker
（条目 key 字符串序），与收集序解耦。

行为不变式：
  1. 同一组同分条目，无论收集序如何，最终顺序一致（由 key 决定）。
  2. 顺序可重复（同输入两次结果相同）。
"""

from __future__ import annotations

from typing import Any

from bandi.memory.memory_provider import MemoryHit, MemoryManager

_META: dict[str, Any] = {
    "category": "long_term",
    "score": 0.5,
    "importance": 0.5,
    "created_at": "T00:00:00",
}


def _hit(key: str) -> MemoryHit:
    return MemoryHit(key=key, value=f"同分内容-{key}", metadata=dict(_META))


class _OrderShiftingManager(MemoryManager):
    """功能：受控 search_all——递增调用反转收集序，模拟多候选词先后命中差异。"""

    def __init__(self, *, start: int = 0) -> None:
        self.calls = start
        self._working_blocked_until = 0.0

    def search_all(self, query: Any = None, **kwargs: Any) -> dict[str, list[MemoryHit]]:  # type: ignore[override]
        self.calls += 1
        a, b = _hit("eval-order-a"), _hit("eval-order-b")
        return {"long_term": [a, b] if self.calls % 2 == 1 else [b, a]}

    def _note_long_term_recalls(self, hits: Any) -> None:
        return None

    def _note_episodic_recalls(self, hits: Any, *, query: str = "") -> None:
        return None

    def _apply_enrichment(self, hits: Any) -> None:
        return None


def test_same_score_order_is_collection_independent() -> None:
    """契约 1：同分条目顺序由 key 决定，与收集序无关（破坏者：注入相反收集序）。"""
    first = [h.key for h in _OrderShiftingManager(start=0).search_unified("q-order", limit=5)]
    second = [h.key for h in _OrderShiftingManager(start=1).search_unified("q-order", limit=5)]
    assert first == second, f"同分排序不得随收集序漂移：{first} vs {second}"
    assert set(first) == {"eval-order-a", "eval-order-b"}, f"两条同分条目都应保留：{first}"


def test_same_score_order_is_repeatable() -> None:
    """契约 2：同输入两次结果相同（可重复）。"""
    one = [h.key for h in _OrderShiftingManager(start=0).search_unified("q-order", limit=5)]
    two = [h.key for h in _OrderShiftingManager(start=0).search_unified("q-order", limit=5)]
    assert one == two
