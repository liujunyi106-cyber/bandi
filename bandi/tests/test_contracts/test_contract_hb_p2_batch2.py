# 功能：契约——批 2（8 项：记忆 5 + 调度 3）
# 钉行为：
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

# ── 快照异常不吞后续 ────────────────────────────────────────────────


class TestSnapshotFailureRecorded:
    @pytest.mark.asyncio
    async def test_snapshot_failure_does_not_raise_and_is_recorded(self) -> None:
        from bandi.agent.base import ExecutionContext
        from bandi.core import agent_runner as ar
        from bandi.core.config import AgentConfig
        from bandi.core.event_bus import EventBus
        from bandi.observability.write_failures import get_registry

        class _BoomHierarchy:
            def save_memory(self, *a: object, **k: object) -> None:
                raise RuntimeError("snapshot boom")

        ctx = ExecutionContext(messages=[], provider=None, config=AgentConfig(), bus=EventBus())
        ctx.current_content = "本轮最终产出"  # 快照仅在有产出时触发
        before = get_registry().total
        await ar._persist_memory(
            ctx,
            None,
            "sess-p2",
            hierarchy=_BoomHierarchy(),
            project_id="p1",
            employee_id="e1",
        )
        assert get_registry().total > before, (
            "快照失败必须登记（此前裸调用直接冒泡，吞掉后续 compaction 落库与事件）"
        )


# ── close 传播 markdown ─────────────────────────────────────────────


class TestCloseIncludesMarkdown:
    def test_memory_manager_close_closes_markdown(self) -> None:
        from bandi.memory.memory_provider import MemoryManager

        closed: list[str] = []

        class _F:
            def __init__(self, name: str) -> None:
                self._name = name

            def close(self) -> None:
                closed.append(self._name)

        mgr = MemoryManager(
            long_term=_F("long_term"),
            episodic=_F("episodic"),
            working=_F("working"),
            markdown_memory=_F("markdown"),
        )
        mgr.close()
        assert "markdown" in closed, "close 必须传播到 markdown（FTS 连接泄漏）"

    def test_hierarchy_manager_close_closes_markdown(self, tmp_path: Path) -> None:
        from bandi.memory.memory_manager import MemoryHierarchyManager

        closed: list[str] = []

        class _F:
            def __init__(self, name: str) -> None:
                self._name = name

            def close(self) -> None:
                closed.append(self._name)

        hm = MemoryHierarchyManager(
            global_store=_F("global"),
            markdown_memory=_F("markdown"),
            work_item_store=_F("work_items"),
            store_dir=str(tmp_path / "hier"),
        )
        hm.close()
        assert "markdown" in closed, "层级 close 必须传播到 markdown"


# ── pending 超龄清理 ────────────────────────────────────────────────


class TestPendingPurgeExpired:
    def test_purge_expired_removes_only_old(self, tmp_path: Path) -> None:
        import time as _time

        from bandi.memory.staging import MemoryPendingStore

        store = MemoryPendingStore(str(tmp_path / "mem"))
        old = store.stage(
            {
                "action": "promote",
                "summary": "旧记录",
                "created_at": _time.time() - 40 * 86400,
                "payload": {"target": "long_term", "item": {"k": "old"}},
            }
        )
        fresh = store.stage(
            {
                "action": "promote",
                "summary": "新记录",
                "created_at": _time.time(),
                "payload": {"target": "long_term", "item": {"k": "new"}},
            }
        )
        assert store.pending_count() == 2
        removed = store.purge_expired(max_age_days=30.0)
        assert removed == 1
        remaining = [r.id8 for r in store.list_pending()]
        assert fresh.id8 in remaining and old.id8 not in remaining


# ── concept 全文扫描 ────────────────────────────────────────────────


class TestConceptSearchFullBody:
    def test_hit_after_2400_chars_not_dropped(self) -> None:
        from bandi.knowledge.okf import search_concepts

        filler = "无关文本。" * 700  # 3500 字符，关键 token 落在 2400 之后
        row = SimpleNamespace(
            concept_id="c1",
            concept_type="Concept",
            title="测试概念",
            description="",
            content_md=f"---\ntitle: x\n---\n\n{filler}量子纠缠引擎的说明",
            updated_at="2026-01-01",
        )
        hits = search_concepts("量子纠缠引擎", [row])
        assert hits, "第 2400 字符之后的正文命中不得被静默丢弃"


# ── episodic 空白维度归一 ───────────────────────────────────────────


class TestEpisodicBlankDimensions:
    def test_blank_dimensions_normalized_to_none(self) -> None:
        from bandi.memory.episodic import Episode, EpisodicStore

        store = EpisodicStore(":memory:")
        ep = store.record(Episode(summary="空白维度用例", project_id="", employee_id="  "))
        assert ep.project_id is None and ep.employee_id is None, "空白维度必须归一为 None"
        rows = store.list_for_maintenance(project_id=None, employee_id=None, limit=10)
        assert any(r.episode_id == ep.episode_id for r in rows), "归一后 IS NULL 查询必须取到该条"


# ── 调度取消回落 ────────────────────────────────────────────────────


class TestSchedulerFireCancelled:
    def test_fire_cancelled_resets_status_to_idle(self) -> None:
        from bandi.scheduler.scheduler import TaskScheduler
        from bandi.scheduler.store import ScheduledTask, TaskStatus, TaskStore

        store = TaskStore(":memory:")
        store.save(ScheduledTask(task_id="t1", name="n", cron="0 9 * * *", prompt="p"))
        task = store.get("t1")
        assert task is not None

        async def cancelled_runner(t: ScheduledTask) -> None:
            raise asyncio.CancelledError()

        sched = TaskScheduler(store=store, runner=cancelled_runner)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(sched._fire(task, datetime.now(UTC)))
        assert store.get("t1").status == TaskStatus.IDLE, (
            "取消后状态必须回落（否则 DB 永久 running，重启后任务卡死）"
        )


# ── org 草案原子写 ─────────────────────────────────────────────────


class TestOrgDraftAtomic:
    def test_failed_replace_leaves_no_partial_draft(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bandi.company.secretary import SecretaryService

        svc = object.__new__(SecretaryService)
        svc._org_engine = object()
        svc._org_drafts_dir = tmp_path / "drafts"
        result = svc._save_org_draft([{"kind": "add_role", "role_id": "r1"}])
        assert "draft" in result
        assert len(list(svc._org_drafts_dir.glob("*.json"))) == 1

        def boom(self: Path, target: object) -> None:
            raise OSError("simulated crash")

        monkeypatch.setattr(Path, "replace", boom)
        result2 = svc._save_org_draft([{"kind": "add_role", "role_id": "r2"}])
        monkeypatch.undo()
        assert "failed" in str(result2).lower(), "写入失败必须可观测（返回失败消息）"
        assert len(list(svc._org_drafts_dir.glob("*.json"))) == 1, (
            "replace 失败不得产生半写草案（原子性）"
        )


# ── 异常释放座位 ───────────────────────────────────────────────────


class TestReleasePickedSeats:
    def test_release_only_claimed_roles(self) -> None:
        from bandi.company.company_mode import CompanyDispatcher

        disp = object.__new__(CompanyDispatcher)
        released: list[str] = []
        disp.release = lambda role, _item="": released.append(role)  # type: ignore[method-assign]
        report = SimpleNamespace(released=[])
        picked = [
            ("w1", "r1", "worker", "READY"),
            ("w2", "r2", "", "READY"),
            ("w3", "r3", "reviewer", "READY"),
        ]
        disp._release_picked_seats(report, picked)
        assert released == ["worker", "reviewer"], "只释放已认领座位"
        assert report.released == ["worker", "reviewer"]
