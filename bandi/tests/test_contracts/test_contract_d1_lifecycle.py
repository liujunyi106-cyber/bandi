# 功能：契约——记忆/会话生命周期治理（并发工程 批次4 / ）
#
# `delete_expired` 零生产调用；`consolidate_default` 仅手工端点。本契约钉：
# episodic 单会话容量有界（500）/ 低价值旧情节被遗忘 / 高价值情节晋升长期层 /
# 会话 TTL 清理与归档可被调度调用 / scheduler 维护钩子按间隔执行且失败不炸 tick。
from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture()
def pending_store(tmp_path: Any):
    """/维护晋升走暂存区——隔离注入，防写穿真实 ~/.bandi/memory。"""
    from bandi.memory.staging import MemoryPendingStore, reset_pending_store

    store = MemoryPendingStore(str(tmp_path / "memory"))
    reset_pending_store(store)
    yield store
    reset_pending_store()


def _episode(
    episode_id: str,
    *,
    session_id: str = "s1",
    summary: str = "内容",
    importance: float = 0.5,
    recall_count: int = 0,
    created_at: datetime | None = None,
    project_id: str | None = None,
    employee_id: str | None = None,
) -> Any:
    from bandi.memory.episodic import Episode

    moment = created_at or datetime.now(UTC)
    return Episode(
        summary=summary,
        episode_id=episode_id,
        session_id=session_id,
        project_id=project_id,
        employee_id=employee_id,
        importance=importance,
        recall_count=recall_count,
        created_at=moment.isoformat(),
    )


class TestEpisodicCapacity:
    def test_single_session_capacity_is_bounded(self, tmp_path: Any) -> None:
        from bandi.memory.episodic import EpisodicStore

        store = EpisodicStore(str(tmp_path / "ep.db"))
        for i in range(510):
            store.record(_episode(f"e{i}", summary=f"情节 {i}", importance=0.1 + (i % 5) * 0.1))

        assert store.count() <= 500, f"单会话容量未受限：{store.count()}"

    def test_capacity_prunes_lowest_importance_first(self, tmp_path: Any) -> None:
        from bandi.memory.episodic import EpisodicStore

        store = EpisodicStore(str(tmp_path / "ep.db"))
        for i in range(499):
            store.record(_episode(f"low{i}", summary=f"低 {i}", importance=0.1))
        store.record(_episode("keep", summary="高价值", importance=0.9))
        store.record(_episode("new", summary="新写入", importance=0.8))

        assert store.get("keep") is not None, "高价值情节不得被裁剪"
        assert store.get("new") is not None, "新写入不得被裁剪"
        assert store.count() <= 500

    def test_fresh_write_not_deleted_by_time_boundary(self, tmp_path: Any) -> None:
        from bandi.memory.episodic import EpisodicStore

        store = EpisodicStore(str(tmp_path / "ep.db"))
        store.record(_episode("fresh", summary="刚写入", importance=0.05))
        assert store.get("fresh") is not None, "新写入不得被生命周期清理误删"

    def test_global_bucket_without_session_is_bounded(self, tmp_path: Any) -> None:
        """残余 空 session_id 的「无会话全局桶」也必须有容量上限。

        原实现对空 id 直接跳过裁剪——无会话情节（跨会话全局经历）可无限增长
        绕过单会话 500 上限。全局桶按同一上限独立裁剪，不影响带 session 分区。
        """
        from bandi.memory.episodic import EpisodicStore

        store = EpisodicStore(str(tmp_path / "ep.db"))
        for i in range(510):
            store.record(_episode(f"g{i}", session_id="", summary=f"无会话 {i}", importance=0.1))

        assert store.count() <= 500, f"空会话全局桶未受限：{store.count()}"

    def test_global_bucket_prune_does_not_touch_sessioned_episodes(self, tmp_path: Any) -> None:
        """全局桶裁剪不得误伤带 session 分区（两类分区计数独立）。"""
        from bandi.memory.episodic import EpisodicStore

        store = EpisodicStore(str(tmp_path / "ep.db"))
        for i in range(505):
            store.record(_episode(f"g{i}", session_id="", summary=f"无会话 {i}", importance=0.1))
        for i in range(10):
            store.record(_episode(f"s{i}", session_id="s1", summary=f"会话 {i}", importance=0.9))

        assert all(store.get(f"s{i}") is not None for i in range(10)), (
            "带 session 分区不得被全局桶裁剪波及"
        )
        assert store.count() <= 510


class TestLowValueForgetAndPromotion:
    def test_old_low_value_episodes_are_forgotten(self, tmp_path: Any) -> None:
        from bandi.memory.episodic import EpisodicStore
        from bandi.memory.maintenance import forget_low_value_episodes

        store = EpisodicStore(str(tmp_path / "ep.db"))
        old = datetime.now(UTC) - timedelta(days=130)
        store.record(_episode("old-low", summary="陈旧低价值", importance=0.05, created_at=old))
        store.record(_episode("old-high", summary="陈旧高价值", importance=0.5, created_at=old))
        store.record(_episode("fresh-low", summary="新低价值", importance=0.05))

        forgotten = forget_low_value_episodes(store)

        assert "old-low" in forgotten, "陈旧低价值情节必须被遗忘"
        assert store.get("old-low") is None
        assert store.get("old-high") is not None, "高价值不得误删"
        assert store.get("fresh-low") is not None, "未过期低价值不得误删"

    def test_notable_episodes_promoted_to_long_term(
        self, tmp_path: Any, pending_store: Any
    ) -> None:
        from bandi.memory.episodic import EpisodicStore
        from bandi.memory.long_term import LongTermMemory
        from bandi.memory.maintenance import promote_notable_episodes

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        episodic.record(
            _episode("notable", summary="被反复召回的经验", importance=0.3, recall_count=2)
        )
        episodic.record(_episode("plain", summary="普通情节", importance=0.1, recall_count=0))

        promoted = promote_notable_episodes(episodic, long_term)

        assert "notable" in promoted, "达阈值情节必须晋升"
        assert "plain" not in promoted
        records = [r for r in pending_store.list_pending() if r.action == "promote_episode"]
        assert [r.payload["item"]["id"] for r in records] == ["consolidated:notable"], (
            "晋升须进入待批准队列（审批后落层）"
        )


class TestSessionLifecycle:
    def test_delete_expired_sessions_via_maintenance(self, tmp_path: Any) -> None:
        from bandi.memory.session.store import SessionStore

        store = SessionStore(str(tmp_path / "sess.db"))
        fresh = store.create_session(mode="execute")
        stale = store.create_session(mode="execute")
        # 直接回拨 updated_at 模拟长期未活跃（无公开时间戳写入口）
        old_ts = (datetime.now() - timedelta(days=100)).isoformat()
        store._sqlite.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (old_ts, stale))
        store._sqlite.commit()

        deleted = store.delete_expired(ttl_days=90)

        assert deleted >= 1
        ids = {row["id"] for row in store.list_sessions(limit=50, status="active")}
        assert fresh in ids and stale not in ids

    def test_run_memory_maintenance_passes_ttl_through(self, tmp_path: Any) -> None:
        """ttl_days 必须真正透传到 SessionStore.delete_expired（不得传了不用）。

        后维护链为「先归档后删除」：关闭按龄归档（archive_after_days=0）以
        隔离 TTL 清理语义——不归档时 >TTL 会话恰由 delete_expired 删除。
        """
        from bandi.memory.maintenance import run_session_maintenance
        from bandi.memory.session.store import SessionStore

        def _stale_session(db_path: Any) -> Any:
            sessions = SessionStore(str(db_path))
            session_id = sessions.create_session(mode="execute")
            old_ts = (datetime.now() - timedelta(days=100)).isoformat()
            sessions._sqlite.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (old_ts, session_id)
            )
            sessions._sqlite.commit()
            return sessions

        short_ttl = run_session_maintenance(
            _stale_session(tmp_path / "sess-a.db"),
            tmp_path / "archive-a",
            ttl_days=90,
            archive_after_days=0,
        )
        long_ttl = run_session_maintenance(
            _stale_session(tmp_path / "sess-b.db"),
            tmp_path / "archive-b",
            ttl_days=200,
            archive_after_days=0,
        )

        assert short_ttl["deleted"] == 1, "90 天 TTL 必须清掉 100 天未活跃会话"
        assert long_ttl["deleted"] == 0, "ttl_days 未透传时此处会误判为已过期"


class TestSchedulerMaintenanceHook:
    @pytest.mark.asyncio
    async def test_due_maintenance_runs_and_is_rate_limited(self, tmp_path: Any) -> None:
        from bandi.scheduler.scheduler import SchedulerConfig, TaskScheduler
        from bandi.scheduler.store import TaskStore

        calls = {"n": 0}

        def _maintenance() -> None:
            calls["n"] += 1

        scheduler = TaskScheduler(
            store=TaskStore(str(tmp_path / "tasks.db")),
            runner=lambda task: None,
            config=SchedulerConfig(
                maintenance_callable=_maintenance,
                maintenance_interval_sec=3600.0,
            ),
        )
        await scheduler._tick()
        assert calls["n"] == 1, "首次 tick 必须执行维护"

        await scheduler._tick()
        assert calls["n"] == 1, "未到间隔不得重复执行（每日一次语义）"

    @pytest.mark.asyncio
    async def test_maintenance_failure_does_not_break_tick(self, tmp_path: Any) -> None:
        from bandi.scheduler.scheduler import SchedulerConfig, TaskScheduler
        from bandi.scheduler.store import TaskStore

        def _boom() -> None:
            raise RuntimeError("maintenance down")

        scheduler = TaskScheduler(
            store=TaskStore(str(tmp_path / "tasks.db")),
            runner=lambda task: None,
            config=SchedulerConfig(maintenance_callable=_boom, maintenance_interval_sec=0.0),
        )
        await scheduler._tick()  # 不得抛出

    @pytest.mark.asyncio
    async def test_disabled_maintenance_not_called(self, tmp_path: Any) -> None:
        from bandi.scheduler.scheduler import SchedulerConfig, TaskScheduler
        from bandi.scheduler.store import TaskStore

        calls = {"n": 0}
        scheduler = TaskScheduler(
            store=TaskStore(str(tmp_path / "tasks.db")),
            runner=lambda task: None,
            config=SchedulerConfig(
                maintenance_enabled=False,
                maintenance_callable=lambda: calls.__setitem__("n", calls["n"] + 1),
                maintenance_interval_sec=0.0,
            ),
        )
        await scheduler._tick()
        assert calls["n"] == 0, "关闭后不得执行维护"

    @pytest.mark.asyncio
    async def test_maintenance_failure_schedules_short_retry(self, tmp_path: Any) -> None:
        """缺陷 4.1：维护失败不得等满一个正常间隔才重试——短周期重试且不风暴。"""
        from bandi.scheduler.scheduler import SchedulerConfig, TaskScheduler
        from bandi.scheduler.store import TaskStore

        clock = {"now": datetime(2026, 1, 1, 0, 0, 0)}
        calls = {"n": 0, "fail": True}

        def _maintenance() -> None:
            calls["n"] += 1
            if calls["fail"]:
                raise RuntimeError("maintenance down")

        scheduler = TaskScheduler(
            store=TaskStore(str(tmp_path / "tasks.db")),
            runner=lambda task: None,
            config=SchedulerConfig(
                maintenance_callable=_maintenance,
                maintenance_interval_sec=3600.0,
                now_fn=lambda: clock["now"],
            ),
        )
        await scheduler._tick()
        assert calls["n"] == 1

        clock["now"] += timedelta(seconds=360)  # 短重试窗口 max(60, 3600/10)
        calls["fail"] = False
        await scheduler._tick()
        assert calls["n"] == 2, "失败后必须短周期重试，不得等满正常间隔"

        clock["now"] += timedelta(seconds=100)
        await scheduler._tick()
        assert calls["n"] == 2, "成功后恢复正常间隔节流"


class TestScopedMaintenancePartitions:
    """缺陷 2：维护扫描必须覆盖带维度的生产情节，且遗忘从最旧端扫描。"""

    def test_scoped_episodes_are_forgotten_and_promoted(
        self, tmp_path: Any, pending_store: Any
    ) -> None:
        from bandi.memory.episodic import EpisodicStore
        from bandi.memory.long_term import LongTermMemory
        from bandi.memory.maintenance import (
            forget_low_value_episodes,
            promote_notable_episodes,
        )

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        old = datetime.now(UTC) - timedelta(days=130)
        episodic.record(
            _episode(
                "scoped-old-low",
                summary="项目陈旧低价值",
                importance=0.05,
                created_at=old,
                project_id="p1",
                employee_id="e1",
            )
        )
        episodic.record(
            _episode(
                "emp-old-low",
                summary="员工陈旧低价值",
                importance=0.05,
                created_at=old,
                employee_id="e2",
            )
        )
        episodic.record(
            _episode(
                "scoped-notable",
                summary="项目被召回经验",
                importance=0.3,
                recall_count=2,
                project_id="p1",
                employee_id="e1",
            )
        )

        forgotten = forget_low_value_episodes(episodic)

        assert "scoped-old-low" in forgotten, "带 project/employee 维度的陈旧低价值必须被遗忘"
        assert "emp-old-low" in forgotten, "带 employee 维度的陈旧低价值必须被遗忘"
        assert episodic.get("scoped-notable") is not None

        promoted = promote_notable_episodes(episodic, long_term)

        assert "scoped-notable" in promoted, "带维度的可晋升情节必须被晋升"
        records = [r for r in pending_store.list_pending() if r.action == "promote_episode"]
        assert len(records) == 1
        assert records[0].payload["item"]["id"] == "consolidated:scoped-notable"
        assert records[0].payload["project_id"] == "p1", "晋升暂存须保留原维度（不得串写）"
        assert records[0].payload["employee_id"] == "e1"

    def test_forget_scans_from_oldest_end(self, tmp_path: Any, monkeypatch: Any) -> None:
        """只看最新窗口会让最该忘的最旧条目永远进不了扫描窗口。"""
        from bandi.memory import maintenance
        from bandi.memory.episodic import EpisodicStore

        monkeypatch.setattr(maintenance, "_SCAN_LIMIT", 2)
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        old = datetime.now(UTC) - timedelta(days=130)
        episodic.record(
            _episode(
                "oldest-low",
                summary="最旧低价值",
                importance=0.05,
                created_at=old,
                project_id="p1",
                employee_id="e1",
            )
        )
        episodic.record(
            _episode("mid", summary="中间", importance=0.9, project_id="p1", employee_id="e1")
        )
        episodic.record(
            _episode("newest", summary="最新", importance=0.9, project_id="p1", employee_id="e1")
        )

        forgotten = maintenance.forget_low_value_episodes(episodic)

        assert "oldest-low" in forgotten, "遗忘必须从最旧端扫描（窗口不得只覆盖最新 N 条）"

    def test_list_for_maintenance_scope_and_order(self, tmp_path: Any) -> None:
        from bandi.memory.episodic import EpisodicStore

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        old = datetime.now(UTC) - timedelta(days=10)
        episodic.record(_episode("old", created_at=old, project_id="p", employee_id="e"))
        episodic.record(_episode("new", project_id="p", employee_id="e"))
        episodic.record(_episode("global", created_at=old))

        assert ("p", "e") in episodic.list_scopes()
        assert (None, None) in episodic.list_scopes()

        oldest = episodic.list_for_maintenance(
            project_id="p", employee_id="e", oldest_first=True, limit=10
        )
        assert [e.episode_id for e in oldest] == ["old", "new"]
        newest = episodic.list_for_maintenance(
            project_id="p", employee_id="e", oldest_first=False, limit=1
        )
        assert [e.episode_id for e in newest] == ["new"]


class TestDefaultMaintenanceEntrypoint:
    """缺陷 1：默认数据目录入口必须真实跑通（不得运行即 TypeError）。"""

    def test_runs_end_to_end_and_forgets_stale_episode(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from bandi.memory.episodic import EpisodicStore
        from bandi.memory.maintenance import default_maintenance_callable
        from bandi.memory.session.store import SessionStore

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        data_dir = tmp_path / ".bandi" / "data"
        data_dir.mkdir(parents=True)
        old = datetime.now(UTC) - timedelta(days=130)
        seed = EpisodicStore(str(data_dir / "episodic.db"))
        seed.record(
            _episode(
                "stale-low",
                summary="陈旧低价值",
                importance=0.05,
                created_at=old,
                project_id="p1",
                employee_id="e1",
            )
        )
        seed.close()
        SessionStore(str(data_dir / "bandi.db")).close()

        report = default_maintenance_callable()()

        assert isinstance(report, dict)
        assert {"forgotten", "promoted", "session"} <= set(report), f"report 键缺失：{report}"
        assert "stale-low" in report["forgotten"]
        verified = EpisodicStore(str(data_dir / "episodic.db"))
        assert verified.get("stale-low") is None, "遗忘须真实落库"
        verified.close()

    def test_long_term_construction_failure_closes_episodic(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """破坏者：LongTermMemory 构造失败时不得泄漏已开的 episodic 连接。"""
        from bandi.memory import maintenance
        from bandi.memory.episodic import EpisodicStore

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        created: list[EpisodicStore] = []
        real_episodic = EpisodicStore

        class _SpyEpisodic(real_episodic):
            def __init__(self, db_path: str) -> None:
                super().__init__(db_path)
                created.append(self)

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("long_term init failed")

        monkeypatch.setattr(maintenance, "EpisodicStore", _SpyEpisodic)
        monkeypatch.setattr(maintenance, "LongTermMemory", _boom)

        with pytest.raises(RuntimeError, match="long_term init failed"):
            maintenance.default_maintenance_callable()()

        assert created, "episodic 应已尝试构造"
        with pytest.raises(sqlite3.ProgrammingError):
            created[0].count()  # 连接已关闭才可用（关闭后操作抛 ProgrammingError）


class TestMaintenancePromotionObservability:
    """缺陷 4.2：维护晋升的合法作用域写入不得计入 long_term 漏传维度观测。"""

    def test_promotion_does_not_count_scope_miss(self, tmp_path: Any) -> None:
        from bandi.memory.episodic import EpisodicStore
        from bandi.memory.long_term import (
            LongTermMemory,
            reset_scope_strict_misses,
            scope_strict_misses,
        )
        from bandi.memory.maintenance import promote_notable_episodes

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        episodic.record(
            _episode(
                "scoped",
                summary="项目经验",
                importance=0.3,
                recall_count=2,
                project_id="p1",
                employee_id="e1",
            )
        )
        episodic.record(_episode("global", summary="全局经验", importance=0.3, recall_count=2))

        reset_scope_strict_misses()
        promoted = promote_notable_episodes(episodic, long_term)

        assert set(promoted) == {"scoped", "global"}
        assert all(v == 0 for v in scope_strict_misses().values()), (
            f"维护晋升是镜像情节作用域的合法写，不得计为漏传：{scope_strict_misses()}"
        )


class TestEpisodesSessionIndex:
    """缺陷 4.3：session 维度复合索引必须随开库生效（旧库亦然）。"""

    def test_session_composite_index_created_on_open(self, tmp_path: Any) -> None:
        from bandi.memory.episodic import EpisodicStore

        db = tmp_path / "ep.db"
        raw = sqlite3.connect(str(db))
        raw.execute(
            """CREATE TABLE episodes (
                episode_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                project_id TEXT,
                employee_id TEXT,
                summary TEXT NOT NULL,
                user_input TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                importance REAL NOT NULL DEFAULT 0.4,
                recall_count INTEGER NOT NULL DEFAULT 0,
                last_recalled_at TEXT,
                created_at TEXT NOT NULL,
                recall_queries TEXT NOT NULL DEFAULT '[]'
            )"""
        )
        raw.execute(
            "INSERT INTO episodes (episode_id, summary, created_at) VALUES ('legacy', '旧库行', '2020-01-01T00:00:00+00:00')"
        )
        raw.commit()
        raw.close()

        store = EpisodicStore(str(db))
        names = {row[1] for row in store._conn.execute("PRAGMA index_list(episodes)")}
        store.close()

        assert "idx_episodes_session" in names, f"复合索引缺失：{names}"


class TestPromotionStarvation:
    """晋升每轮取前 N，已晋升/已暂存的候选不得继续占位——否则第 N+1 名饿死。

    生产维护晋升走暂存区（无人值守强制暂存），故「已晋升」有两种形态：
    已批准落长期层（long_term 存在稳定键 `consolidated:<episode_id>`）与
    仍在暂存区等待批准（pending 记录同键）。两者都必须跳过。
    """

    @staticmethod
    def _seed(tmp_path: Any, count: int = 60) -> Any:
        from bandi.memory.episodic import EpisodicStore

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        for i in range(count):
            episodic.record(
                _episode(
                    f"ep{i:02d}",
                    # 单会话容量上限 500——跨会话分摊避免种子被容量裁剪
                    session_id=f"s{i % 4}",
                    summary=f"可晋升经验 {i}",
                    importance=0.25 + i * 0.01,
                    recall_count=2,
                )
            )
        return episodic

    def test_second_round_promotes_remaining_after_approval(
        self, tmp_path: Any, pending_store: Any
    ) -> None:
        from bandi.memory.long_term import LongTermMemory
        from bandi.memory.maintenance import promote_notable_episodes
        from bandi.memory.staging import replay_record

        episodic = self._seed(tmp_path)
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        try:
            first = promote_notable_episodes(episodic, long_term, limit=48)
            assert len(first) == 48, f"首轮应晋升 48 条（实得 {len(first)}）"
            for record in list(pending_store.list_pending()):
                assert replay_record(record, long_term=long_term), "审批重放必须落长期层"
                pending_store.discard_pending(record.id8)

            second = promote_notable_episodes(episodic, long_term, limit=48)

            assert len(second) == 12, f"第二轮必须晋升剩余 12 条（实得 {len(second)}）"
            assert not set(first) & set(second), "已晋升候选不得重复占位"
            assert set(first) | set(second) == {f"ep{i:02d}" for i in range(60)}

            for record in list(pending_store.list_pending()):
                assert replay_record(record, long_term=long_term)
                pending_store.discard_pending(record.id8)
            assert long_term.count() == 60, "稳定 id REPLACE 不得产生重复行"

            third = promote_notable_episodes(episodic, long_term, limit=48)
            assert third == [], "全部晋升后不得重复晋升（幂等）"
            assert long_term.count() == 60, "重复晋升不得产生重复行"
        finally:
            episodic.close()
            long_term.close()

    def test_second_round_progresses_while_pending(self, tmp_path: Any, pending_store: Any) -> None:
        """未批准也必须前进：首轮 48 条在暂存区时，第二轮应晋升其余 12 条。"""
        from bandi.memory.long_term import LongTermMemory
        from bandi.memory.maintenance import promote_notable_episodes

        episodic = self._seed(tmp_path)
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        try:
            first = promote_notable_episodes(episodic, long_term, limit=48)
            second = promote_notable_episodes(episodic, long_term, limit=48)

            assert len(second) == 12, f"待批准候选不得继续占位（实得 {len(second)}）"
            assert not set(first) & set(second)
            pending_ids = {r.payload["item"]["id"] for r in pending_store.list_pending()}
            assert pending_ids == {f"consolidated:ep{i:02d}" for i in range(60)}
            assert long_term.count() == 0, "无人值守路径仍不得直写长期层"
        finally:
            episodic.close()
            long_term.close()

    def test_scan_beyond_window_when_newest_promoted(
        self, tmp_path: Any, pending_store: Any
    ) -> None:
        """>1000 边界（破坏者）：扫描窗口被已晋升候选占满时，窗口外候选仍必须可达。"""
        from bandi.memory.long_term import LongTermMemory
        from bandi.memory.maintenance import promote_notable_episodes

        episodic = self._seed(tmp_path, count=1100)
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        try:
            # 最新 1000 条（ep0100..ep1099）已晋升——恰好占满单页扫描窗口
            long_term._conn.executemany(
                "INSERT INTO long_term_memory "
                "(id, content, metadata, timestamp, importance, project_id, employee_id) "
                "VALUES (?, ?, '{}', 1.0, 0.5, NULL, NULL)",
                [(f"consolidated:ep{i:02d}", "已晋升经验") for i in range(100, 1100)],
            )
            long_term._conn.commit()

            promoted = promote_notable_episodes(episodic, long_term, limit=48)

            assert len(promoted) == 48, f"窗口占满后仍须晋升窗口外候选（实得 {len(promoted)}）"
            assert all(int(e[2:]) < 100 for e in promoted), (
                f"只能晋升窗口外（最旧 100 条）未晋升候选：{sorted(promoted)[:5]}"
            )
        finally:
            episodic.close()
            long_term.close()


class TestSessionMaintenanceArchivesBeforeDelete:
    """>TTL 会话必须先归档后删除（原实现先删后转，未归档即永久丢失）。"""

    @staticmethod
    def _stale_session(tmp_path: Any, days: int = 100) -> tuple[Any, str]:
        from bandi.memory.session.store import SessionStore

        store = SessionStore(str(tmp_path / "sess.db"))
        session_id = store.create_session(mode="execute")
        old_ts = (datetime.now() - timedelta(days=days)).isoformat()
        store._sqlite.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (old_ts, session_id)
        )
        store._sqlite.commit()
        return store, session_id

    def test_rotate_called_before_delete_and_archive_written(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from bandi.memory import maintenance

        store, stale = self._stale_session(tmp_path)
        order: list[str] = []
        real_rotate = store.rotate
        real_delete = store.delete_expired

        def _rotate(*args: Any, **kwargs: Any) -> Any:
            order.append("rotate")
            return real_rotate(*args, **kwargs)

        def _delete(*args: Any, **kwargs: Any) -> Any:
            order.append("delete")
            return real_delete(*args, **kwargs)

        monkeypatch.setattr(store, "rotate", _rotate)
        monkeypatch.setattr(store, "delete_expired", _delete)

        report = maintenance.run_session_maintenance(store, tmp_path / "archive", ttl_days=90)

        assert order == ["rotate", "delete"], f"必须先归档后删除（实得 {order}）"
        assert report["rotated"]["archived_count"] == 1, "100 天会话必须先归档"
        archive_files = list((tmp_path / "archive").glob("*.gz"))
        assert archive_files, "归档文件必须存在（不得未归档即删除）"
        with gzip.open(archive_files[0], "rt", encoding="utf-8") as handle:
            archived = [json.loads(line) for line in handle]
        assert any(row.get("id") == stale for row in archived), "归档文件必须包含原会话行"
        assert store.get_session(stale) is None, "已归档会话移出活跃库"
        store.close()

    def test_archive_window_clamped_to_ttl(self, tmp_path: Any) -> None:
        """archive_after_days > ttl 时不得「未归档即删除」——归档窗口收紧到 TTL。"""
        from bandi.memory.maintenance import run_session_maintenance

        store, stale = self._stale_session(tmp_path)

        report = run_session_maintenance(
            store, tmp_path / "archive", ttl_days=90, archive_after_days=365
        )

        assert report["rotated"]["archived_count"] == 1, "删除前必须有成功归档"
        assert list((tmp_path / "archive").glob("*.gz")), "归档文件必须存在"
        assert store.get_session(stale) is None
        store.close()

    def test_rotate_failure_skips_delete(self, tmp_path: Any, monkeypatch: Any) -> None:
        """归档失败必须跳过删除——未经归档的硬删不可恢复（安全失败）。"""
        from bandi.memory import maintenance

        store, stale = self._stale_session(tmp_path)

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("archive unavailable")

        monkeypatch.setattr(store, "rotate", _boom)

        report = maintenance.run_session_maintenance(store, tmp_path / "archive", ttl_days=90)

        assert report["deleted"] == 0, "归档失败不得继续硬删"
        assert store.get_session(stale) is not None, "会话必须保留待下轮重试"
        store.close()

    def test_without_archive_dir_session_maintenance_skipped(self, tmp_path: Any) -> None:
        """archive_dir 缺省时行为与现状一致：不触碰会话（无归档目标即不维护）。"""
        from bandi.memory.episodic import EpisodicStore
        from bandi.memory.long_term import LongTermMemory
        from bandi.memory.maintenance import run_memory_maintenance

        store, stale = self._stale_session(tmp_path)
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        try:
            report = run_memory_maintenance(episodic, long_term, store, None)

            assert "session" not in report
            assert store.get_session(stale) is not None, "无归档目录不得删会话"
        finally:
            store.close()
            episodic.close()
            long_term.close()


class TestPromotionReportDistinguishesStaging:
    """🟢-E：无人值守暂存路径不得把「待批准」混同为真正落库的晋升。

    既有 d1/cj_b3 契约钉住返回列表包含被选中的 episode id；修复在保持该
    语义的前提下让结果对象额外暴露 `staged` / `persisted`，并在
    `run_memory_maintenance` 报告中单列 `staged`。
    """

    def test_promoted_result_marks_staged_and_not_persisted(
        self, tmp_path: Any, pending_store: Any
    ) -> None:
        from bandi.memory.episodic import EpisodicStore
        from bandi.memory.long_term import LongTermMemory
        from bandi.memory.maintenance import promote_notable_episodes

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        try:
            episodic.record(_episode("notable", summary="经验", importance=0.3, recall_count=2))

            promoted = promote_notable_episodes(episodic, long_term)

            assert "notable" in promoted, "既有契约：被选中候选仍在返回列表"
            assert list(getattr(promoted, "staged", [])) == ["notable"], "暂存候选必须单独标注"
            assert list(getattr(promoted, "persisted", [])) == [], "暂存候选不得计为已落库"
            assert long_term.count() == 0, "无人值守路径不得直写长期层"
        finally:
            episodic.close()
            long_term.close()

    def test_run_memory_maintenance_reports_staged_separately(
        self, tmp_path: Any, pending_store: Any
    ) -> None:
        from bandi.memory.episodic import EpisodicStore
        from bandi.memory.long_term import LongTermMemory
        from bandi.memory.maintenance import run_memory_maintenance

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        try:
            episodic.record(_episode("notable", summary="经验", importance=0.3, recall_count=2))

            report = run_memory_maintenance(episodic, long_term)

            assert report["staged"] == ["notable"], "报告必须区分暂存与真正落库"
            assert report["promoted"] == ["notable"], "既有契约：promoted 仍为被选中 id 列表"
            assert long_term.count() == 0
        finally:
            episodic.close()
            long_term.close()
