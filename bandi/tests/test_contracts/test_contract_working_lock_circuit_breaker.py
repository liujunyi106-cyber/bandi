# 功能：契约工作层锁冲突须快速熔断，不得拖死整轮检索
#
# 行为不变式（钉行为，不钉实现）：
#   1. SQLite 锁冲突（非数据损坏）触发工作层短路，其余分类照常召回
#   2. 熔断窗口内不再重复撞锁——search_unified 的多候选词循环必须提前收敛
#   3. 非锁错误（如文件损坏）不触发熔断，仍按「降级空 list」处理
#   4. 破坏者视角：单次锁冲突的耗时上界不超过一次 busy_timeout，不得随
#      候选词数量线性放大

from __future__ import annotations

import time
from typing import Any

import pytest

from bandi.memory.memory_provider import MemoryManager


class _FakeWorking:
    """功能：可编程的工作层替身——记录调用次数，按脚本抛异常。"""

    def __init__(self, errors: list[BaseException] | None = None) -> None:
        self.errors = list(errors or [])
        self.calls = 0

    def search(self, query: str, limit: int, session_id: str = "") -> list[Any]:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return []


class _FakeLongTerm:
    def search(self, query: str, limit: int = 5, **kwargs: Any) -> list[Any]:
        return []


class _FakeEpisodic:
    def search(self, query: str, limit: int = 5, **kwargs: Any) -> list[Any]:
        return []


@pytest.fixture
def manager_with_locked_working() -> MemoryManager:
    locked = __import__("sqlite3").OperationalError("database is locked")
    return MemoryManager(
        long_term=_FakeLongTerm(),  # type: ignore[arg-type]
        episodic=_FakeEpisodic(),  # type: ignore[arg-type]
        working=_FakeWorking([locked, locked, locked, locked, locked, locked]),  # type: ignore[arg-type]
    )


class TestLockErrorDetection:
    def test_lock_error_detected(self) -> None:
        assert MemoryManager._is_lock_error(RuntimeError("database is locked")) is True
        assert MemoryManager._is_lock_error(RuntimeError("database table is locked")) is True

    def test_non_lock_error_not_treated_as_lock(self) -> None:
        assert MemoryManager._is_lock_error(RuntimeError("no such table: scratch_kv")) is False
        assert MemoryManager._is_lock_error(ValueError("bad query")) is False


class TestWorkingCircuitBreaker:
    def test_lock_error_marks_blocked(self) -> None:
        """功能：锁冲突须置熔断标记。"""
        mgr = MemoryManager()

        assert mgr._mark_working_blocked(RuntimeError("database is locked")) is True
        assert mgr._working_blocked() is True

    def test_non_lock_error_does_not_mark_blocked(self) -> None:
        """功能：数据损坏类错误不熔断（否则一次偶发错误会长时间丢弃工作层）。"""
        mgr = MemoryManager()

        assert mgr._mark_working_blocked(RuntimeError("no such table")) is False
        assert mgr._working_blocked() is False

    def test_blocked_window_expires(self) -> None:
        """功能：熔断是窗口而非永久——窗口过后工作层恢复可用。"""
        mgr = MemoryManager()
        mgr._working_blocked_until = time.monotonic() - 0.01

        assert mgr._working_blocked() is False


class TestSearchUnifiedDoesNotAmplifyLock:
    def test_locked_working_searched_at_most_once(
        self, manager_with_locked_working: MemoryManager
    ) -> None:
        """功能：核心不变式——一次 search_unified 里工作层最多被撞一次。

        修复前：`for candidate in candidates[:limit*3]` 逐词重查，每次撞满
        busy_timeout（5s），15 个候选词 = 75s，预取线程拖死主链。
        """
        working = manager_with_locked_working.working
        manager_with_locked_working.search_unified("你好呀你能帮我做什么", limit=5)

        assert working.calls == 1  # type: ignore[union-attr]

    def test_healthy_working_still_searched(self) -> None:
        """功能：无回归——工作层可用时照常参与多候选词检索。"""
        working = _FakeWorking()
        mgr = MemoryManager(
            long_term=_FakeLongTerm(),  # type: ignore[arg-type]
            episodic=_FakeEpisodic(),  # type: ignore[arg-type]
            working=working,  # type: ignore[arg-type]
        )

        mgr.search_unified("部署端口是多少", limit=5)

        assert working.calls >= 1

    def test_lock_breakers_expired_allows_retry(self) -> None:
        """功能：熔断窗口过后重新尝试工作层（不是永久放弃）。"""
        working = _FakeWorking()
        mgr = MemoryManager(
            long_term=_FakeLongTerm(),  # type: ignore[arg-type]
            episodic=_FakeEpisodic(),  # type: ignore[arg-type]
            working=working,  # type: ignore[arg-type]
        )
        mgr._working_blocked_until = time.monotonic() - 0.01

        mgr.search_unified("你好呀", limit=5)

        assert working.calls >= 1


class TestOtherLayersSurviveWorkingLock:
    def test_long_term_still_returned_when_working_locked(self) -> None:
        """功能：破坏者视角——工作层不可用不得连累长期记忆召回。

        替身须提供 search_all 遍历到的真实接口（.id/.content/metadata）。
        """

        class _Row:
            def __init__(self, rid: str, content: str) -> None:
                self.id = rid
                self.content = content
                self.metadata: dict[str, Any] = {}
                self.importance = 0.9
                self.timestamp = None

        class _LongTermHit(_FakeLongTerm):
            def search_fts_ranked(self, query: str, limit: int = 5, **kwargs: Any) -> list[Any]:
                return [_Row("k1", "我最喜欢数字 88")]

        locked = __import__("sqlite3").OperationalError("database is locked")
        mgr = MemoryManager(
            long_term=_LongTermHit(),  # type: ignore[arg-type]
            episodic=_FakeEpisodic(),  # type: ignore[arg-type]
            working=_FakeWorking([locked]),  # type: ignore[arg-type]
        )

        hits = mgr.search_unified("我最喜欢什么数字", limit=5)

        assert any("88" in (h.value or "") for h in hits)
