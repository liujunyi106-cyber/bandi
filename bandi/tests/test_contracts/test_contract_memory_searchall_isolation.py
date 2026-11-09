# 功能：契约——MemoryManager.search_all 各子库异常必须单点隔离：坏库以空 list 呈现
# 其余分类照常召回、检索链绝不抛异常（fail-closed 语义：记忆文件损坏不炸注入主链）。
# 生产路径族：search_unified（只包整体 try 的兜底不是隔离——单子库损坏会全灭所有分类）；
# Prefetch/MemoryContextSource 全部只经 search_all/search_unified 取数。

from __future__ import annotations

from typing import Any

from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.memory_provider import MemoryManager


class _BoomEpisodic:
    """功能：episodic 子库损坏替身（search 抛异常）。"""

    def search(self, *a: Any, **k: Any) -> list[Any]:
        raise RuntimeError("episodic store corrupt")

    def note_recall(self, *a: Any, **k: Any) -> None:
        pass


class _BoomWorking:
    """功能：working 子库损坏替身（search 抛异常）。"""

    def search(self, *a: Any, **k: Any) -> list[Any]:
        raise RuntimeError("scratchpad store corrupt")


class _BoomLongTerm:
    """功能：核心 long_term 子库损坏替身（主检索路径抛异常）。"""

    def search_fts_ranked(self, *a: Any, **k: Any) -> list[Any]:
        raise RuntimeError("long_term store corrupt")


def test_episodic_failure_isolated_others_recall() -> None:
    """契约：episodic 损坏 → search_all 不炸，long_term 结果照常召回
    坏子库分类以空 list 呈现（非缺失键、非抛异常）。"""
    lt = LongTermMemory()
    try:
        lt.store(MemoryItem(item_id="lt1", content="灰度发布回滚预案", importance=0.5))
        mm = MemoryManager(long_term=lt, episodic=_BoomEpisodic(), working=None)
        try:
            results = mm.search_all("回滚", limit=5)
        finally:
            mm.close()
    finally:
        lt.close()
    assert [h.key for h in results["long_term"]] == ["lt1"], (
        "episodic 损坏不得吞掉 long_term 召回（单点隔离而非全灭）"
    )
    assert results.get("episodic") == [], "坏子库必须呈现为空 list（隔离语义）"


def test_working_failure_isolated_others_recall() -> None:
    """契约：working 损坏同样隔离，long_term 不受影响，working 空 list。"""
    lt = LongTermMemory()
    try:
        lt.store(MemoryItem(item_id="lt2", content="Kubernetes 滚动更新记录", importance=0.5))
        mm = MemoryManager(long_term=lt, working=_BoomWorking())
        try:
            results = mm.search_all("Kubernetes", limit=5)
        finally:
            mm.close()
    finally:
        lt.close()
    assert "working" in results and results["working"] == [], "坏子库必须呈现为空 list"
    assert [h.key for h in results["long_term"]] == ["lt2"]


def test_core_long_term_failure_isolated() -> None:
    """契约（破坏者视角）：核心 long_term 损坏 → search_all 直接调用也不
    抛异常（而非只在 search_unified 的全局 try 内兜底）；long_term 以空 list 呈现。"""
    mm = MemoryManager(long_term=_BoomLongTerm(), working=None, episodic=None)
    try:
        results = mm.search_all("灰度", limit=5)
    finally:
        mm.close()
    assert results["long_term"] == [], "核心库损坏必须降级空 list（fail-closed 不炸链）"
    assert set(results.keys()) & {"long_term"}, "损坏分类仍须出现在结果键中"


def test_search_all_never_raises_when_mixed_stores() -> None:
    """契约：healthy long_term + 双坏子库混合 → search_all 不抛，healthy 分类照常。"""
    lt = LongTermMemory()
    try:
        lt.store(MemoryItem(item_id="lt3", content="灰度发布回滚预案", importance=0.5))
        mm = MemoryManager(long_term=lt, episodic=_BoomEpisodic(), working=_BoomWorking())
        try:
            results = mm.search_all("回滚", limit=5)
        finally:
            mm.close()
    finally:
        lt.close()
    assert [h.key for h in results["long_term"]] == ["lt3"]
    assert results["episodic"] == [] and results["working"] == []
