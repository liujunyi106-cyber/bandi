from __future__ import annotations

import pytest

from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


class TestWorkItemStore:
    def test_save_and_get(self, store: WorkItemStore):
        item = create_work_item(title="store test", turn_type=WorkItemTurnType.EXECUTE)
        store.save(item)
        loaded = store.get(item.id)
        assert loaded is not None
        assert loaded.id == item.id
        assert loaded.title == item.title

    def test_get_nonexistent(self, store: WorkItemStore):
        assert store.get("nonexistent") is None

    def test_save_updates_existing(self, store: WorkItemStore):
        item = create_work_item(title="update test", turn_type=WorkItemTurnType.EXECUTE)
        store.save(item)
        item.title = "updated"
        store.save(item)
        loaded = store.get(item.id)
        assert loaded is not None
        assert loaded.title == "updated"

    def test_list_by_phase(self, store: WorkItemStore):
        items = [
            create_work_item(title=f"task{i}", turn_type=WorkItemTurnType.EXECUTE) for i in range(3)
        ]
        for item in items:
            store.save(item)
        items[0].phase = Phase.RUNNING
        store.save(items[0])
        running = store.list_by_phase(Phase.RUNNING)
        assert len(running) == 1
        assert running[0].id == items[0].id
        queued = store.list_by_phase(Phase.QUEUED)
        assert len(queued) == 2

    def test_list_by_column(self, store: WorkItemStore):
        items = [
            create_work_item(title="queued", turn_type=WorkItemTurnType.EXECUTE),
            create_work_item(
                title="running", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.RUNNING
            ),
            create_work_item(
                title="done", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.APPROVED
            ),
        ]
        for item in items:
            store.save(item)
        todo = store.list_by_column("todo")
        assert len(todo) == 1
        done = store.list_by_column("done")
        assert len(done) == 1

    def test_count_by_column(self, store: WorkItemStore):
        items = [
            create_work_item(title="queued", turn_type=WorkItemTurnType.EXECUTE),
            create_work_item(
                title="running", turn_type=WorkItemTurnType.EXECUTE, phase=Phase.RUNNING
            ),
        ]
        for item in items:
            store.save(item)
        counts = store.count_by_column()
        assert counts["todo"] == 1
        assert counts["in_progress"] == 1

    def test_delete(self, store: WorkItemStore):
        item = create_work_item(title="delete test", turn_type=WorkItemTurnType.EXECUTE)
        store.save(item)
        store.delete(item.id)
        assert store.get(item.id) is None

    def test_get_invalid_phase_raises_with_context(self, store: WorkItemStore):
        import datetime

        store._conn.execute(
            "INSERT OR REPLACE INTO work_items"
            " (id, title, description, phase, turn_type, assignee_role, parent_id,"
            "  children_ids, links, metadata, runtime_metadata, created_at, updated_at)"
            " VALUES (?, ?, '', 'bogus_phase', 'execute', NULL, NULL, '[]', '{}', '{}', '{}', ?, ?)",
            (
                "dirty-1",
                "dirty",
                datetime.datetime.now().isoformat(),
                datetime.datetime.now().isoformat(),
            ),
        )
        store._conn.commit()
        with pytest.raises(ValueError, match="dirty-1"):
            store.get("dirty-1")

    def test_get_invalid_turn_type_raises_with_context(self, store: WorkItemStore):
        import datetime

        store._conn.execute(
            "INSERT OR REPLACE INTO work_items"
            " (id, title, description, phase, turn_type, assignee_role, parent_id,"
            "  children_ids, links, metadata, runtime_metadata, created_at, updated_at)"
            " VALUES (?, ?, '', 'queued', 'bogus_turn', NULL, NULL, '[]', '{}', '{}', '{}', ?, ?)",
            (
                "dirty-2",
                "dirty",
                datetime.datetime.now().isoformat(),
                datetime.datetime.now().isoformat(),
            ),
        )
        store._conn.commit()
        with pytest.raises(ValueError, match="dirty-2"):
            store.get("dirty-2")


class TestPhaseIndexAndBatch:
    def test_phase_index_created(self, store: WorkItemStore):
        indexes = store._conn.execute("PRAGMA index_list('work_items')").fetchall()
        names = [row[1] for row in indexes]
        assert any("phase" in n for n in names), f"缺 phase 索引，实际 {names}"

    def test_get_many_batch(self, store: WorkItemStore):
        items = [
            create_work_item(title=f"batch{i}", turn_type=WorkItemTurnType.EXECUTE)
            for i in range(3)
        ]
        for item in items:
            store.save(item)
        got = store.get_many([items[0].id, items[1].id, "missing-id"])
        assert [g.id for g in got] == [items[0].id, items[1].id], "批量获取必须跳过缺失 id"
        assert store.get_many([]) == [], "空 id 列表必须返回空"

    def test_count_by_column_sql(self, store: WorkItemStore):
        items = [create_work_item(title="c1", turn_type=WorkItemTurnType.EXECUTE) for _ in range(2)]
        for item in items:
            store.save(item)
        counts = store.count_by_column()
        assert counts["todo"] == 2, f"GROUP BY 聚合必须正确，实际 {counts}"


class TestMemorySnapshots:
    def test_save_and_get_memory_snapshot(self, store: WorkItemStore):
        store.save_memory_snapshot(
            project_id="proj1",
            session_id="s1",
            employee_id="emp1",
            role_id="选品专员",
            memory_scope="session",
            memory_kind="process",
            memory_text="完成选品分析",
            metadata={"task_ids": ["t1"]},
        )
        snap = store.get_memory_snapshot(project_id="proj1", session_id="s1", employee_id="emp1")
        assert snap is not None
        assert snap["memory_text"] == "完成选品分析"
        assert snap["memory_scope"] == "session"
        assert snap["memory_kind"] == "process"
        assert snap["metadata"]["task_ids"] == ["t1"]

    def test_get_missing_snapshot_returns_none(self, store: WorkItemStore):
        assert store.get_memory_snapshot(project_id="p", session_id="s", employee_id="e") is None

    def test_get_snapshot_by_scope_and_kind(self, store: WorkItemStore):
        store.save_memory_snapshot(
            project_id="proj1",
            session_id="",
            employee_id="emp1",
            memory_scope="project",
            memory_kind="final",
            memory_text="最终记忆",
        )
        snap = store.get_memory_snapshot(
            project_id="proj1",
            session_id="",
            employee_id="emp1",
            memory_scope="project",
            memory_kind="final",
        )
        assert snap is not None
        assert snap["memory_text"] == "最终记忆"

    def test_delete_memory_snapshots(self, store: WorkItemStore):
        store.save_memory_snapshot(
            project_id="proj1",
            session_id="s1",
            employee_id="emp1",
            memory_scope="session",
            memory_kind="process",
            memory_text="快照",
        )
        store.delete_memory_snapshots(project_id="proj1", session_id="s1", employee_id="emp1")
        assert (
            store.get_memory_snapshot(project_id="proj1", session_id="s1", employee_id="emp1")
            is None
        )

    def test_update_snapshot_overwrites(self, store: WorkItemStore):
        store.save_memory_snapshot(
            project_id="proj1",
            session_id="s1",
            employee_id="emp1",
            memory_scope="session",
            memory_kind="process",
            memory_text="v1",
        )
        store.save_memory_snapshot(
            project_id="proj1",
            session_id="s1",
            employee_id="emp1",
            memory_scope="session",
            memory_kind="process",
            memory_text="v2",
        )
        snap = store.get_memory_snapshot(project_id="proj1", session_id="s1", employee_id="emp1")
        assert snap is not None
        assert snap["memory_text"] == "v2"

    def test_list_memory_snapshots_by_employee(self, store: WorkItemStore):
        store.save_memory_snapshot(
            project_id="proj1",
            session_id="s1",
            employee_id="emp1",
            memory_scope="session",
            memory_kind="process",
            memory_text="a",
        )
        store.save_memory_snapshot(
            project_id="proj1",
            session_id="s2",
            employee_id="emp1",
            memory_scope="session",
            memory_kind="process",
            memory_text="b",
        )
        snaps = store.list_memory_snapshots(project_id="proj1", employee_id="emp1")
        assert len(snaps) == 2


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_company.db")
    return WorkItemStore(db_path)
