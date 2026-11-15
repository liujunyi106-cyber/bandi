# 功能：契约 — 删除群聊会话只清理本会话群记忆，不动其他群/团队/全局记忆
from __future__ import annotations

from pathlib import Path

from bandi.company.store import WorkItemStore
from bandi.memory.episodic import Episode, EpisodicStore
from bandi.memory.global_store import GlobalMemoryStore
from bandi.memory.group_scope import GROUP_SNAPSHOT_EMPLOYEE_ID, group_scope_id
from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.markdown_memory import MarkdownMemoryStore
from bandi.memory.memory_manager import MemoryHierarchyManager
from bandi.memory.staging import MemoryPendingStore


def _hierarchy(data_dir: Path) -> MemoryHierarchyManager:
    return MemoryHierarchyManager(
        global_store=GlobalMemoryStore(data_dir / "hierarchy_global.db"),
        markdown_memory=MarkdownMemoryStore(data_dir),
        work_item_store=WorkItemStore(data_dir / "hierarchy_work_items.db"),
        store_dir=data_dir,
    )


def test_purge_group_memory_clears_only_target_scope(tmp_path: Path) -> None:
    from bandi.memory.group_maintenance import purge_group_memory

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    episodic = EpisodicStore(data_dir / "episodic.db")
    long_term = LongTermMemory(data_dir / "long_term.db")
    work_items = WorkItemStore(data_dir / "hierarchy_work_items.db")
    hierarchy = _hierarchy(data_dir)
    pending = MemoryPendingStore(tmp_path / "pending")

    scope_a = group_scope_id("conv-a")
    scope_b = group_scope_id("conv-b")
    episodic.record(Episode(episode_id="a1", summary="群 A 的记忆", project_id=scope_a))
    episodic.record(Episode(episode_id="b1", summary="群 B 的记忆", project_id=scope_b))
    long_term.store(MemoryItem("a1", "群 A 的长期记忆"), project_id=scope_a)
    long_term.store(MemoryItem("b1", "群 B 的长期记忆"), project_id=scope_b)
    hierarchy.append_memory("project", "群 A 的 MEMORY.md", project_id=scope_a)
    hierarchy.append_memory("project", "群 B 的 MEMORY.md", project_id=scope_b)
    work_items.save_memory_snapshot(
        project_id=scope_a,
        session_id="conv-a",
        employee_id=GROUP_SNAPSHOT_EMPLOYEE_ID,
        memory_scope="group",
        memory_kind="final",
        memory_text="群 A 快照",
    )
    work_items.save_memory_snapshot(
        project_id=scope_b,
        session_id="conv-b",
        employee_id=GROUP_SNAPSHOT_EMPLOYEE_ID,
        memory_scope="group",
        memory_kind="final",
        memory_text="群 B 快照",
    )
    pending.stage({"action": "consolidate_episode", "payload": {"project_id": scope_a}})
    pending.stage({"action": "consolidate_episode", "payload": {"project_id": scope_b}})

    result = purge_group_memory("conv-a", data_dir=data_dir, pending_store=pending)

    assert result["episodes"] == 1
    assert result["long_term"] == 1
    assert result["project_md"] is True
    assert result["snapshots"] == 1
    assert result["pending"] == 1
    assert episodic.search("记忆", project_id=scope_a) == []
    assert long_term.search_fts_ranked("记忆", project_id=scope_a) == []
    assert hierarchy.load_memory("project", project_id=scope_a) == ""
    assert (
        work_items.get_memory_snapshot(
            project_id=scope_a,
            session_id="conv-a",
            employee_id=GROUP_SNAPSHOT_EMPLOYEE_ID,
            memory_scope="group",
        )
        is None
    )
    assert [record.payload["project_id"] for record in pending.list_pending()] == [scope_b]

    assert len(episodic.search("记忆", project_id=scope_b)) == 1
    assert len(long_term.search_fts_ranked("记忆", project_id=scope_b)) == 1
    assert "群 B" in hierarchy.load_memory("project", project_id=scope_b)
    assert (
        work_items.get_memory_snapshot(
            project_id=scope_b,
            session_id="conv-b",
            employee_id=GROUP_SNAPSHOT_EMPLOYEE_ID,
            memory_scope="group",
        )
        is not None
    )


def test_delete_team_memory_does_not_touch_group_or_global(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    episodic = EpisodicStore(data_dir / "episodic.db")
    group_scope = group_scope_id("conv-team-clean")
    episodic.record(Episode(episode_id="team-1", summary="team", project_id="p1", team_id="t1"))
    episodic.record(Episode(episode_id="group-1", summary="group", project_id=group_scope))
    episodic.record(Episode(episode_id="global-1", summary="global"))

    deleted = episodic.delete_team_memory()

    assert deleted == 1
    assert episodic.count_team_memory() == 0
    assert episodic.count_group_memory() == 1
    assert episodic.count() == 2
