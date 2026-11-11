"""契约族：store 时间戳口径一致性。

生产口径（2026-10-31 拍板，替代原「业务时间戳 = naive 本地」）：
存储层统一 UTC 带偏移串；历史本地 naive 值由
`utils/timefmt.migrate_naive_timestamps` 在 store 初始化时一次性归一（幂等）；
接口层 `utils/timefmt.iso_local` 统一输出本地时区偏移；租约时效同为 UTC。
生产调用路径：
1. WorkItemStore.save_org_roles / save_org_employees——组织定义时间戳列
2. WorkItemStore.claim_for_execution——work item 租约时间戳（UTC 口径闭环）
3. WorkItemStore.save_memory_snapshot——层级记忆快照时间戳列
4. SessionStore.create_session——会话时间戳列
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.utils.timefmt import iso_local


def _mk_store(tmp_path: Path) -> WorkItemStore:
    return WorkItemStore(str(tmp_path / "wi.db"))


def _read_created_updated(db_path: str, table: str, id_col: str) -> tuple[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            f"SELECT created_at, updated_at FROM {table} ORDER BY {id_col} LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"{table} 应有一行"
    return str(row[0]), str(row[1])


def _assert_utc(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() == timedelta(0), f"必须 UTC 带偏移：{value!r}"


def test_org_role_timestamps_are_utc_aware(tmp_path: Path) -> None:
    """组织角色时间戳为 UTC 带偏移——跨时区/跨进程比较不漂移。"""
    store = _mk_store(tmp_path)
    store.save_org_roles([{"role_id": "ceo", "name": "CEO"}])
    db_path = store.db_path
    store.close()
    created, updated = _read_created_updated(db_path, "org_roles", "role_id")
    _assert_utc(created)
    _assert_utc(updated)


def test_org_employee_timestamps_are_utc_aware(tmp_path: Path) -> None:
    """员工定义时间戳口径同组织角色（UTC 带偏移）。"""
    store = _mk_store(tmp_path)
    store.save_org_employees([{"employee_id": "emp1", "name": "张三", "role_id": "ceo"}])
    db_path = store.db_path
    store.close()
    created, updated = _read_created_updated(db_path, "org_employees", "employee_id")
    _assert_utc(created)
    _assert_utc(updated)


def test_work_item_lease_timestamp_is_utc_aware(tmp_path: Path) -> None:
    """work item 租约时间戳为 UTC——纯时效比较，不随本地时区漂移。"""
    store = _mk_store(tmp_path)
    item = create_work_item(title="t1", turn_type=WorkItemTurnType.EXECUTE)
    item.phase = Phase.READY
    store.save(item)
    assert store.claim_for_execution(item.id, "inst-a", 120)
    claimed = store.get(item.id)
    assert claimed is not None
    expires = str(claimed.metadata["execution_claim"]["lease_expires_at"])
    _assert_utc(expires)


def test_memory_snapshot_timestamp_is_utc_aware(tmp_path: Path) -> None:
    """层级记忆快照时间戳为 UTC 带偏移。"""
    store = _mk_store(tmp_path)
    store.save_memory_snapshot(
        project_id="p1",
        session_id="s1",
        employee_id="emp1",
        memory_scope="session",
        memory_kind="process",
        memory_text="内容",
    )
    snap = store.get_memory_snapshot(
        project_id="p1", session_id="s1", employee_id="emp1", memory_scope="session"
    )
    assert snap is not None
    _assert_utc(str(snap["created_at"]))
    _assert_utc(str(snap["updated_at"]))


# ==================== timestamp_exit ====================
# 功能：契约——存储 UTC、出口本地偏移（职责分离）


def test_global_store_now_is_naive_local() -> None:
    """契约（时间口径）：global_store 写入口径为 naive 本地时间——不得带时区偏移
    （存量 UTC 值由 _parse_ts 归一化排序兼容；该库不在会话/工作项两链迁移范围）。"""
    from bandi.memory.global_store import _now

    value = _now()
    assert "+" not in value, f"naive 本地时间不得带时区偏移：{value}"


def test_checkpoint_created_at_is_naive() -> None:
    """契约（时间口径）：checkpoint created_at 默认值为 naive 本地（同 global_store 口径）。"""
    from bandi.agent.checkpoint import Checkpoint

    cp = Checkpoint(session_id="s", turn_index=1, state_json="{}")
    assert "+" not in cp.created_at, f"created_at 必须 naive：{cp.created_at}"


def test_session_store_timestamps_are_utc_aware(tmp_path: Path) -> None:
    """契约（时间口径）：session store 的 created_at/updated_at 为 UTC 带偏移。"""
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(tmp_path / "s.db"))
    try:
        sid = store.create_session(mode="execute")
        row = next(s for s in store.list_sessions() if s["id"] == sid)
        _assert_utc(str(row["created_at"]))
        _assert_utc(str(row["updated_at"]))
    finally:
        store.close()


def test_session_store_exit_converts_to_local_offset(tmp_path: Path) -> None:
    """契约（出口口径）：存储 UTC 值经 iso_local 后为本地时区偏移，且时刻不变。"""
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(tmp_path / "s2.db"))
    try:
        sid = store.create_session(mode="execute")
        stored = next(s for s in store.list_sessions() if s["id"] == sid)["created_at"]
        exported = iso_local(stored)
        assert (
            datetime.fromisoformat(exported).utcoffset()
            == datetime.now(UTC).astimezone().utcoffset()
        )
        assert datetime.fromisoformat(exported) == datetime.fromisoformat(str(stored))
    finally:
        store.close()
