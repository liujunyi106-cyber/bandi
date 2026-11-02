# 功能：工作项存储——SQLite 持久化 + 查询

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from bandi.company.phase import InvalidPhaseTransitionError, Phase, coerce_phase, kanban_column
from bandi.company.work_item import WorkItem, WorkItemTurnType
from bandi.utils.db import connect_app_db
from bandi.utils.timefmt import migrate_naive_timestamps

if TYPE_CHECKING:
    from bandi.company.reorg_manager import OrgSnapshot, ReorgProposal

# 需要 UTC 归一的时间列（历史库为本地 naive，初始化时一次性迁移）
_TIME_COLUMNS: dict[str, tuple[str, ...]] = {
    "work_items": ("created_at", "updated_at"),
    "work_item_journal": ("created_at",),
    "org_snapshots": ("created_at",),
    "reorg_proposals": ("created_at", "updated_at"),
    "reorg_events": ("created_at",),
    "memory_snapshots": ("created_at", "updated_at"),
    "org_roles": ("created_at", "updated_at"),
    "org_employees": ("created_at", "updated_at"),
}


class WorkItemStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = connect_app_db(db_path, row_factory=True)
        self._init_db()

    @property
    def db_path(self) -> str:
        """SQLite 数据文件路径（dispatcher 据此推导演进状态 home 目录）。"""
        return self._db_path

    def _init_db(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS work_items (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                phase TEXT NOT NULL,
                turn_type TEXT NOT NULL,
                assignee_role TEXT,
                parent_id TEXT,
                children_ids TEXT DEFAULT '[]',
                links TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}',
                runtime_metadata TEXT DEFAULT '{}',
                comments TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0
            )
        """)
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(work_items)").fetchall()]
        if "revision" not in cols:
            self._conn.execute(
                "ALTER TABLE work_items ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
            )
            self._conn.commit()
        # 功能：工作项 journal——阶段迁移/认领/完成的追加审计流
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS work_item_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_item_id TEXT NOT NULL,
                session_id TEXT DEFAULT '',
                event TEXT NOT NULL,
                detail TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_work_item_journal
            ON work_item_journal(work_item_id, id)
        """)
        self._conn.commit()
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS org_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                org_version INTEGER NOT NULL,
                runtime_topology_version INTEGER NOT NULL,
                company_name TEXT DEFAULT '',
                topology TEXT DEFAULT '',
                roles TEXT DEFAULT '[]',
                company_profile TEXT DEFAULT 'corporate',
                active_tasks TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS reorg_proposals (
                proposal_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                session_id TEXT,
                task_id TEXT,
                initiated_by TEXT DEFAULT 'owner',
                source_role_id TEXT DEFAULT '',
                scope TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                rationale TEXT DEFAULT '',
                user_confirmation_required INTEGER DEFAULT 1,
                old_org_version INTEGER DEFAULT 1,
                new_org_version INTEGER DEFAULT 1,
                old_runtime_topology_version INTEGER DEFAULT 1,
                new_runtime_topology_version INTEGER DEFAULT 1,
                changeset TEXT DEFAULT '{}',
                migration_plan TEXT DEFAULT '{}',
                impact_summary TEXT DEFAULT '{}',
                approval_notes TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS reorg_events (
                event_id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                summary TEXT DEFAULT '',
                details TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_work_items_phase ON work_items(phase)")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_snapshots (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                employee_id TEXT NOT NULL,
                role_id TEXT NOT NULL DEFAULT '',
                memory_scope TEXT NOT NULL,
                memory_kind TEXT NOT NULL,
                summary_text TEXT DEFAULT '',
                memory_text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_snapshots_lookup "
            "ON memory_snapshots(project_id, session_id, employee_id)"
        )
        try:
            self._conn.execute("ALTER TABLE work_items ADD COLUMN comments TEXT DEFAULT '[]'")
            self._conn.commit()
        except sqlite3.OperationalError:
            self._conn.rollback()
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS org_roles (
                role_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                reports_to TEXT,
                can_spawn TEXT DEFAULT '[]',
                responsibility TEXT DEFAULT '',
                permissions TEXT DEFAULT '{}',
                tool_tags TEXT DEFAULT '[]',
                is_final_decider INTEGER NOT NULL DEFAULT 0,
                team_id TEXT DEFAULT '',
                fs_scopes TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        _org_role_cols = [
            c[1] for c in self._conn.execute("PRAGMA table_info(org_roles)").fetchall()
        ]
        if "tool_tags" not in _org_role_cols:
            self._conn.execute("ALTER TABLE org_roles ADD COLUMN tool_tags TEXT DEFAULT '[]'")
        if "is_final_decider" not in _org_role_cols:
            self._conn.execute(
                "ALTER TABLE org_roles ADD COLUMN is_final_decider INTEGER NOT NULL DEFAULT 0"
            )
        if "team_id" not in _org_role_cols:
            self._conn.execute("ALTER TABLE org_roles ADD COLUMN team_id TEXT DEFAULT ''")
        if "fs_scopes" not in _org_role_cols:
            self._conn.execute("ALTER TABLE org_roles ADD COLUMN fs_scopes TEXT DEFAULT '[]'")
        self._conn.commit()
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS org_employees (
                employee_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                role_id TEXT NOT NULL,
                template_id TEXT DEFAULT '',
                description TEXT DEFAULT '',
                category TEXT DEFAULT '',
                domains TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                status TEXT DEFAULT 'active',
                llm_config TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        _org_employee_cols = [
            c[1] for c in self._conn.execute("PRAGMA table_info(org_employees)").fetchall()
        ]
        if "llm_config" not in _org_employee_cols:
            self._conn.execute("ALTER TABLE org_employees ADD COLUMN llm_config TEXT")
        # 功能：历史本地 naive 时间串归一为 UTC 带偏移（幂等；无 naive 值时零写入）
        migrate_naive_timestamps(self._conn, _TIME_COLUMNS)
        self._conn.commit()

    def _row_to_work_item(self, row: sqlite3.Row) -> WorkItem:
        try:
            phase = Phase(row["phase"])
        except ValueError as e:
            raise ValueError(f"Invalid phase {row['phase']!r} for work item {row['id']!r}.") from e
        try:
            turn_type = WorkItemTurnType(row["turn_type"])
        except ValueError as e:
            raise ValueError(
                f"Invalid turn_type {row['turn_type']!r} for work item {row['id']!r}."
            ) from e
        return WorkItem(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            phase=phase,
            turn_type=turn_type,
            assignee_role=row["assignee_role"],
            parent_id=row["parent_id"],
            children_ids=json.loads(row["children_ids"]),
            links=json.loads(row["links"]),
            metadata=json.loads(row["metadata"]),
            runtime_metadata=json.loads(row["runtime_metadata"]),
            comments=json.loads(row["comments"]) if row["comments"] else [],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            revision=int(row["revision"] or 0),
        )

    def save(self, item: WorkItem) -> None:
        with self._lock:
            # 乐观锁版本号：基于库内当前 revision 递增（新插入从 0 起）
            row = self._conn.execute(
                "SELECT revision, metadata FROM work_items WHERE id=?", (item.id,)
            ).fetchone()
            metadata = dict(item.metadata or {})
            if row is not None:
                # execution_claim 时，保留库内 claim —— 否则旧快照写回会把 metadata
                # 整体覆盖、连带抹掉他方租约，同一 work item 会被第二个执行者领走
                # 调用方显式携带 claim 时以其为准（持有者可自行更新/释放）。
                stored_claim = (json.loads(row["metadata"] or "{}")).get("execution_claim") or {}
                if not metadata.get("execution_claim") and self._lease_valid(
                    stored_claim.get("lease_expires_at")
                ):
                    metadata["execution_claim"] = stored_claim
            item.revision = (int(row["revision"]) if row else 0) + 1
            self._conn.execute(
                """
            INSERT OR REPLACE INTO work_items
                (id, title, description, phase, turn_type, assignee_role,
                 parent_id, children_ids, links, metadata, runtime_metadata,
                 comments, created_at, updated_at, revision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    item.id,
                    item.title,
                    item.description,
                    item.phase.value,
                    item.turn_type.value,
                    item.assignee_role,
                    item.parent_id,
                    json.dumps(item.children_ids),
                    json.dumps(item.links),
                    json.dumps(metadata),
                    json.dumps(item.runtime_metadata),
                    json.dumps(item.comments),
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                    item.revision,
                ),
            )
            self._conn.commit()

    def save_checked(self, item: WorkItem, expected_revision: int) -> bool:
        """乐观锁保存：仅当库内 revision == expected 时写入并递增。
        返回 False 表示并发修改冲突——调用方（refresh_dependents）整轮放弃可重试。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT revision FROM work_items WHERE id=?", (item.id,)
            ).fetchone()
            if row is None or int(row["revision"]) != expected_revision:
                return False
            item.revision = expected_revision + 1
            self._conn.execute(
                """
            UPDATE work_items SET title=?, description=?, phase=?, turn_type=?,
                assignee_role=?, parent_id=?, children_ids=?, links=?,
                metadata=?, runtime_metadata=?, comments=?, updated_at=?, revision=?
                WHERE id=?
            """,
                (
                    item.title,
                    item.description,
                    item.phase.value,
                    item.turn_type.value,
                    item.assignee_role,
                    item.parent_id,
                    json.dumps(item.children_ids),
                    json.dumps(item.links),
                    json.dumps(item.metadata),
                    json.dumps(item.runtime_metadata),
                    json.dumps(item.comments),
                    item.updated_at.isoformat(),
                    item.revision,
                    item.id,
                ),
            )
            self._conn.commit()
            return True

    @staticmethod
    def _lease_valid(expires_at: Any) -> bool:
        """租约是否有效：仅以过期时刻判定（owner/持有者判定由调用方比较
        本函数单源负责"未过期"这一维）。租约时效统一 UTC 口径——写入/判定/
        消费点（company_mode、agent_runner）三处同源，并兼容历史 naive 值。"""
        if not expires_at:
            return False
        try:
            expires = datetime.fromisoformat(str(expires_at))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            return datetime.now(UTC) < expires
        except (TypeError, ValueError):
            return False

    def claim_for_execution(self, item_id: str, instance_id: str, lease_seconds: int) -> bool:
        """原子认领：仅当卡在 READY/READY_FOR_REWORK 且无他实例有效租约时，写入
        本实例租约并转 RUNNING。CAS 期望 = 读取时的 phase+metadata+updated_at
        原值，行数==1 才算成功（跨进程读-写竞态由 UPDATE...WHERE 原子性消除）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT phase, metadata, updated_at FROM work_items WHERE id=?",
                (item_id,),
            ).fetchone()
            if row is None:
                return False
            cur_phase, cur_meta_json, cur_updated = row
            if cur_phase not in (Phase.READY.value, Phase.READY_FOR_REWORK.value):
                return False
            metadata = json.loads(cur_meta_json or "{}")
            claim = metadata.get("execution_claim") or {}
            if self._lease_valid(claim.get("lease_expires_at")):
                return False
            now = datetime.now(UTC)
            metadata["execution_claim"] = {
                "instance_id": instance_id,
                "claimed_at": now.isoformat(),
                "lease_expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
            }
            cur = self._conn.execute(
                "UPDATE work_items SET phase=?, metadata=?, updated_at=?, revision=revision+1"
                " WHERE id=? AND phase=? AND metadata=? AND updated_at=?",
                (
                    Phase.RUNNING.value,
                    json.dumps(metadata, ensure_ascii=False),
                    now.isoformat(),
                    item_id,
                    cur_phase,
                    cur_meta_json,
                    cur_updated,
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def claim_from_queued(self, item_id: str, instance_id: str, lease_seconds: int) -> bool:
        """原子认领 QUEUED 卡：仅当卡在 QUEUED 且无他实例有效租约时，一步
        QUEUED→RUNNING + 写本实例租约（CAS 期望=读取快照）。消除「先无条件
        转 READY 再 CAS」的覆盖窗口——预转若用 store.save 覆盖写会抹掉他进程
        CAS 建立的租约导致双执行（回归路径）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT phase, metadata, updated_at FROM work_items WHERE id=?",
                (item_id,),
            ).fetchone()
            if row is None:
                return False
            cur_phase, cur_meta_json, cur_updated = row
            if cur_phase != Phase.QUEUED.value:
                return False
            metadata = json.loads(cur_meta_json or "{}")
            claim = metadata.get("execution_claim") or {}
            if self._lease_valid(claim.get("lease_expires_at")):
                return False
            now = datetime.now(UTC)
            metadata["execution_claim"] = {
                "instance_id": instance_id,
                "claimed_at": now.isoformat(),
                "lease_expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
            }
            cur = self._conn.execute(
                "UPDATE work_items SET phase=?, metadata=?, updated_at=?, revision=revision+1"
                " WHERE id=? AND phase=? AND metadata=? AND updated_at=?",
                (
                    Phase.RUNNING.value,
                    json.dumps(metadata, ensure_ascii=False),
                    now.isoformat(),
                    item_id,
                    cur_phase,
                    cur_meta_json,
                    cur_updated,
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def renew_execution_lease(self, item_id: str, instance_id: str, lease_seconds: int) -> bool:
        """续约：仅当租约属于本实例时刷新 expires_at（WHERE 以读取快照为期望
        他进程已接管则行数 0 失败）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT metadata, updated_at FROM work_items WHERE id=?",
                (item_id,),
            ).fetchone()
            if row is None:
                return False
            cur_meta_json, cur_updated = row
            metadata = json.loads(cur_meta_json or "{}")
            claim = metadata.get("execution_claim") or {}
            if claim.get("instance_id") != instance_id:
                return False
            now = datetime.now(UTC)
            metadata["execution_claim"] = {
                "instance_id": instance_id,
                "claimed_at": claim.get("claimed_at") or now.isoformat(),
                "lease_expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
            }
            cur = self._conn.execute(
                "UPDATE work_items SET metadata=?, updated_at=?, revision=revision+1"
                " WHERE id=? AND metadata=? AND updated_at=?",
                (
                    json.dumps(metadata, ensure_ascii=False),
                    now.isoformat(),
                    item_id,
                    cur_meta_json,
                    cur_updated,
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def release_execution_claim(self, item_id: str, *, instance_id: str | None = None) -> None:
        """清除卡上执行租约（prepare 失败等认领回滚场景）。CAS 式：基于读取快照
        条件更新，避免清除窗口期他进程新写入的租约。

        给定 `instance_id` 时做 owner 校验——仅清除自己持有的租约
        （旧执行者跑完 release 不得清掉新接管者的活租约→防双执行窗口）。
        `instance_id=None` 保留旧「无条件清除」语义，仅供存量调用/测试兼容。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT metadata, updated_at FROM work_items WHERE id=?",
                (item_id,),
            ).fetchone()
            if row is None:
                return
            cur_meta_json, cur_updated = row
            metadata = json.loads(cur_meta_json or "{}")
            claim = metadata.get("execution_claim")
            if "execution_claim" not in metadata:
                return
            if instance_id is not None:
                if not isinstance(claim, dict) or claim.get("instance_id") != instance_id:
                    return
            metadata.pop("execution_claim", None)
            now = datetime.now(UTC)
            self._conn.execute(
                "UPDATE work_items SET metadata=?, updated_at=?, revision=revision+1"
                " WHERE id=? AND metadata=? AND updated_at=?",
                (
                    json.dumps(metadata, ensure_ascii=False),
                    now.isoformat(),
                    item_id,
                    cur_meta_json,
                    cur_updated,
                ),
            )
            self._conn.commit()

    def reopen_approved_work_item_for_rework(
        self,
        item_id: str,
        *,
        target_phase: Phase | str = Phase.READY_FOR_REWORK,
        metadata_updates: dict[str, Any] | None = None,
    ) -> WorkItem | None:
        """功能：把 卡受控转回可重跑态（用户/评审要求返工的唯一入口）。

        命名 bypass
        不放开 `ALLOWED_TRANSITIONS` 全表——常规仍只允许回 READY，仅本入口
        额外放行 READY_FOR_REWORK。守卫 fail-closed：卡必须存在且处于 ；
        target 只允许 READY_FOR_REWORK / READY，其余一律拒绝。

        回转动作：改 phase + 合并 metadata + **清除执行租约**——残留租约会让 gate 与
        恢复守卫（有效租约跳过）永久跳过该卡，卡死无人推进。
        """
        target = coerce_phase(target_phase)
        if target not in (Phase.READY_FOR_REWORK, Phase.READY):
            raise InvalidPhaseTransitionError(Phase.APPROVED, target)
        with self._lock:
            row = self._conn.execute(
                "SELECT metadata, phase FROM work_items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                return None
            cur_meta_json, cur_phase = row
            if str(cur_phase) != Phase.APPROVED.value:
                raise InvalidPhaseTransitionError(coerce_phase(str(cur_phase)), target)
            metadata = json.loads(cur_meta_json or "{}")
            if metadata_updates:
                metadata.update(dict(metadata_updates))
            metadata.pop("execution_claim", None)
            now = datetime.now(UTC)
            cur = self._conn.execute(
                "UPDATE work_items SET phase=?, metadata=?, updated_at=?, revision=revision+1 WHERE id=? AND phase=?",
                (
                    target.value,
                    json.dumps(metadata, ensure_ascii=False),
                    now.isoformat(),
                    item_id,
                    Phase.APPROVED.value,
                ),
            )
            self._conn.commit()
            hit = cur.rowcount == 1
        if not hit:
            # 本次 metadata_updates 未落库，不得静默返回「成功」——回读实际相位显式失败
            current = self.get(item_id)
            actual = current.phase if current is not None else target
            raise InvalidPhaseTransitionError(actual, target)
        return self.get(item_id)

    def get(self, item_id: str) -> WorkItem | None:
        with self._lock:
            cursor = self._conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_work_item(row)

    def get_many(self, item_ids: list[str]) -> list[WorkItem]:
        if not item_ids:
            return []
        with self._lock:
            placeholders = ",".join("?" for _ in item_ids)
            cursor = self._conn.execute(
                f"SELECT * FROM work_items WHERE id IN ({placeholders})",
                tuple(item_ids),
            )
            rows = {r["id"]: r for r in cursor.fetchall()}
        return [self._row_to_work_item(rows[i]) for i in item_ids if i in rows]

    def list_by_phase(self, phase: Phase) -> list[WorkItem]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM work_items WHERE phase = ? ORDER BY created_at",
                (phase.value,),
            )
            return [self._row_to_work_item(row) for row in cursor.fetchall()]

    def list_by_column(self, column: str) -> list[WorkItem]:
        all_items = self.list_all()
        return [item for item in all_items if kanban_column(item.phase) == column]

    def list_all(self) -> list[WorkItem]:
        with self._lock:
            cursor = self._conn.execute("SELECT * FROM work_items ORDER BY created_at")
            return [self._row_to_work_item(row) for row in cursor.fetchall()]

    def count_by_column(self) -> dict[str, int]:
        with self._lock:
            cursor = self._conn.execute("SELECT phase, COUNT(*) FROM work_items GROUP BY phase")
            counts: dict[str, int] = {"todo": 0, "in_progress": 0, "in_review": 0, "done": 0}
            for phase_value, n in cursor.fetchall():
                try:
                    col = kanban_column(Phase(phase_value))
                except ValueError:
                    continue
                counts[col] = counts.get(col, 0) + n
        return counts

    def delete(self, item_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM work_items WHERE id = ?", (item_id,))
            self._conn.commit()

    def record_journal(
        self,
        work_item_id: str,
        event: str,
        *,
        detail: dict[str, Any] | None = None,
        session_id: str = "",
    ) -> None:
        """功能：追加一条工作项 journal（失败不抛，审计不阻断调度）"""
        wid = str(work_item_id or "").strip()
        if not wid or not str(event or "").strip():
            return
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO work_item_journal
                       (work_item_id, session_id, event, detail, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        wid,
                        str(session_id or ""),
                        str(event),
                        json.dumps(detail or {}, ensure_ascii=False, default=str),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                self._conn.commit()
            except Exception:  # noqa: BLE001 — journal 是审计旁路
                self._conn.rollback()

    def list_journal(self, work_item_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """功能：读取工作项 journal（按时间正序，最近 limit 条）"""
        wid = str(work_item_id or "").strip()
        if not wid:
            return []
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, task_id, event, detail, session_id, created_at
                   FROM (SELECT id, work_item_id AS task_id, event, detail, session_id, created_at
                         FROM work_item_journal WHERE work_item_id = ?
                         ORDER BY id DESC LIMIT ?)
                   ORDER BY id""",
                (wid, max(1, min(int(limit), 1000))),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                detail = json.loads(row[3] or "{}")
            except (TypeError, ValueError):
                detail = {}
            out.append(
                {
                    "id": int(row[0]),
                    "work_item_id": str(row[1]),
                    "event": str(row[2]),
                    "detail": detail,
                    "session_id": str(row[4] or ""),
                    "created_at": str(row[5] or ""),
                }
            )
        return out

    def save_org_snapshot(self, snapshot: Any) -> None:
        from bandi.company.reorg_manager import OrgSnapshot

        if not isinstance(snapshot, OrgSnapshot):
            raise TypeError(f"Expected OrgSnapshot, got {type(snapshot).__name__}")
        data = snapshot.to_dict()
        with self._lock:
            self._conn.execute(
                """
            INSERT OR REPLACE INTO org_snapshots
                (snapshot_id, project_id, org_version, runtime_topology_version,
                 company_name, topology, roles, company_profile, active_tasks,
                 metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    data["snapshot_id"],
                    data["project_id"],
                    data["org_version"],
                    data["runtime_topology_version"],
                    data["company_name"],
                    data["topology"],
                    json.dumps(data["roles"]),
                    data["company_profile"],
                    json.dumps(data["active_tasks"]),
                    json.dumps(data["metadata"]),
                    data["created_at"],
                ),
            )
            self._conn.commit()

    def get_org_snapshot(self, snapshot_id: str) -> OrgSnapshot | None:
        from bandi.company.reorg_manager import OrgSnapshot

        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM org_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return OrgSnapshot.from_dict(
            {
                **dict(row),
                "roles": json.loads(row["roles"]),
                "active_tasks": json.loads(row["active_tasks"]),
                "metadata": json.loads(row["metadata"]),
            }
        )

    def get_latest_org_snapshot(self, project_id: str) -> OrgSnapshot | None:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM org_snapshots WHERE project_id = ? "
                "ORDER BY org_version DESC, created_at DESC LIMIT 1",
                (project_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self.get_org_snapshot(row["snapshot_id"])

    def save_reorg_proposal(self, proposal: Any) -> None:
        from bandi.company.reorg_manager import ReorgProposal

        if not isinstance(proposal, ReorgProposal):
            raise TypeError(f"Expected ReorgProposal, got {type(proposal).__name__}")
        data = proposal.to_dict()
        with self._lock:
            self._conn.execute(
                """
            INSERT OR REPLACE INTO reorg_proposals
                (proposal_id, project_id, session_id, task_id, initiated_by,
                 source_role_id, scope, risk_level, status, title, summary,
                 rationale, user_confirmation_required, old_org_version,
                 new_org_version, old_runtime_topology_version,
                 new_runtime_topology_version, changeset, migration_plan,
                 impact_summary, approval_notes, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    data["proposal_id"],
                    data["project_id"],
                    data["session_id"],
                    data["task_id"],
                    data["initiated_by"],
                    data["source_role_id"],
                    data["scope"],
                    data["risk_level"],
                    data["status"],
                    data["title"],
                    data["summary"],
                    data["rationale"],
                    int(data["user_confirmation_required"]),
                    data["old_org_version"],
                    data["new_org_version"],
                    data["old_runtime_topology_version"],
                    data["new_runtime_topology_version"],
                    json.dumps(data["changeset"]),
                    json.dumps(data["migration_plan"]),
                    json.dumps(data["impact_summary"]),
                    data["approval_notes"],
                    json.dumps(data["metadata"]),
                    data["created_at"],
                    data["updated_at"],
                ),
            )
            self._conn.commit()

    def get_reorg_proposal(self, proposal_id: str) -> ReorgProposal | None:
        from bandi.company.reorg_manager import ReorgProposal

        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM reorg_proposals WHERE proposal_id = ?", (proposal_id,)
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return ReorgProposal.from_dict(
            {
                **dict(row),
                "changeset": json.loads(row["changeset"]),
                "migration_plan": json.loads(row["migration_plan"]),
                "impact_summary": json.loads(row["impact_summary"]),
                "metadata": json.loads(row["metadata"]),
                "user_confirmation_required": bool(row["user_confirmation_required"]),
            }
        )

    def list_reorg_proposals(
        self,
        project_id: str,
        status: str | None = None,
        limit: int = 20,
    ) -> list[Any]:
        with self._lock:
            query = "SELECT * FROM reorg_proposals WHERE project_id = ?"
            params: list[Any] = [project_id]
            if status:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        return [self.get_reorg_proposal(row["proposal_id"]) for row in rows]

    def record_reorg_event(self, event: Any) -> None:
        from bandi.company.reorg_manager import ReorgEventRecord

        if not isinstance(event, ReorgEventRecord):
            raise TypeError(f"Expected ReorgEventRecord, got {type(event).__name__}")
        data = event.to_dict()
        with self._lock:
            self._conn.execute(
                """
            INSERT OR REPLACE INTO reorg_events
                (event_id, proposal_id, project_id, event_kind, summary, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    data["event_id"],
                    data["proposal_id"],
                    data["project_id"],
                    data["event_kind"],
                    data["summary"],
                    json.dumps(data["details"]),
                    data["created_at"],
                ),
            )
            self._conn.commit()

    def list_reorg_events(
        self,
        project_id: str,
        proposal_id: str | None = None,
        limit: int = 50,
    ) -> list[Any]:
        from bandi.company.reorg_manager import ReorgEventRecord

        with self._lock:
            query = "SELECT * FROM reorg_events WHERE project_id = ?"
            params: list[Any] = [project_id]
            if proposal_id:
                query += " AND proposal_id = ?"
                params.append(proposal_id)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        return [
            ReorgEventRecord.from_dict(
                {
                    **dict(row),
                    "details": json.loads(row["details"]),
                }
            )
            for row in rows
        ]

    def save_memory_snapshot(
        self,
        project_id: str,
        session_id: str,
        employee_id: str,
        memory_scope: str,
        memory_kind: str,
        memory_text: str,
        *,
        role_id: str = "",
        summary_text: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        import uuid

        now = datetime.now(UTC).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id FROM memory_snapshots WHERE project_id = ? "
                "AND session_id = ? AND employee_id = ? "
                "AND memory_scope = ? AND memory_kind = ?",
                (project_id, session_id, employee_id, memory_scope, memory_kind),
            )
            row = cursor.fetchone()
            if row is not None:
                snapshot_id = row["id"]
                self._conn.execute(
                    """
                UPDATE memory_snapshots SET
                    summary_text = CASE WHEN ? <> '' THEN ? ELSE summary_text END,
                    memory_text = ?,
                    role_id = ?, metadata = ?, updated_at = ?
                WHERE id = ?
                """,
                    (
                        summary_text,
                        summary_text,
                        memory_text,
                        role_id,
                        json.dumps(metadata or {}),
                        now,
                        snapshot_id,
                    ),
                )
            else:
                snapshot_id = f"ms_{uuid.uuid4().hex[:12]}"
                self._conn.execute(
                    """
                INSERT INTO memory_snapshots
                    (id, project_id, session_id, employee_id, role_id, memory_scope,
                     memory_kind, summary_text, memory_text, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        snapshot_id,
                        project_id,
                        session_id,
                        employee_id,
                        role_id,
                        memory_scope,
                        memory_kind,
                        summary_text,
                        memory_text,
                        json.dumps(metadata or {}),
                        now,
                        now,
                    ),
                )
            self._conn.commit()
        return snapshot_id

    def append_snapshot_summary(
        self,
        project_id: str,
        session_id: str,
        employee_id: str,
        memory_scope: str,
        memory_kind: str,
        entry: str,
    ) -> None:
        """功能：追加一行到快照 summary_text（纪要通道）——与 memory_text（快照
        正文，覆盖式）字段分离，快照写入不冲纪要（纪要须跨轮存活）。
        幂等：同 entry 已存在则跳过；无快照行时建行（正文为空）。"""
        text = str(entry or "").strip()
        if not text:
            return
        import uuid

        now = datetime.now(UTC).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT id, summary_text FROM memory_snapshots WHERE project_id = ? "
                "AND session_id = ? AND employee_id = ? AND memory_scope = ? AND memory_kind = ?",
                (project_id, session_id, employee_id, memory_scope, memory_kind),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                INSERT INTO memory_snapshots
                    (id, project_id, session_id, employee_id, role_id, memory_scope,
                     memory_kind, summary_text, memory_text, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, '', ?, ?, ?, '', '{}', ?, ?)
                """,
                    (
                        f"ms_{uuid.uuid4().hex[:12]}",
                        project_id,
                        session_id,
                        employee_id,
                        memory_scope,
                        memory_kind,
                        text,
                        now,
                        now,
                    ),
                )
            else:
                current = str(row["summary_text"] or "")
                if text in current:
                    return
                merged = f"{current}\n{text}" if current else text
                self._conn.execute(
                    "UPDATE memory_snapshots SET summary_text = ?, updated_at = ? WHERE id = ?",
                    (merged, now, row["id"]),
                )
            self._conn.commit()

    def get_memory_snapshot(
        self,
        project_id: str,
        session_id: str,
        employee_id: str,
        *,
        memory_scope: str | None = None,
        memory_kind: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            query = (
                "SELECT * FROM memory_snapshots WHERE project_id = ? "
                "AND session_id = ? AND employee_id = ?"
            )
            params: list[Any] = [project_id, session_id, employee_id]
            if memory_scope:
                query += " AND memory_scope = ?"
                params.append(memory_scope)
            if memory_kind:
                query += " AND memory_kind = ?"
                params.append(memory_kind)
            query += " ORDER BY updated_at DESC LIMIT 1"
            cursor = self._conn.execute(query, params)
            row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_memory_snapshot(row)

    def list_memory_snapshots(
        self,
        project_id: str,
        employee_id: str | None = None,
        memory_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            query = "SELECT * FROM memory_snapshots WHERE project_id = ?"
            params: list[Any] = [project_id]
            if employee_id:
                query += " AND employee_id = ?"
                params.append(employee_id)
            if memory_scope:
                query += " AND memory_scope = ?"
                params.append(memory_scope)
            query += " ORDER BY updated_at DESC"
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        return [self._row_to_memory_snapshot(row) for row in rows]

    def delete_memory_snapshots(
        self,
        project_id: str,
        session_id: str,
        employee_id: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM memory_snapshots "
                "WHERE project_id = ? AND session_id = ? AND employee_id = ?",
                (project_id, session_id, employee_id),
            )
            self._conn.commit()

    # ── 组织本体持久化 ────────────────────────────────────────────────

    def save_org_roles(self, roles: list[dict[str, Any]]) -> None:
        """批量保存角色定义（INSERT OR REPLACE）。"""
        with self._lock:
            # 功能：时间戳按本地时间口径记录（与全仓拍板口径一致）
            now = datetime.now(UTC).isoformat()
            self._conn.execute("DELETE FROM org_roles")
            for r in roles:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO org_roles
                        (role_id, name, reports_to, can_spawn, responsibility,
                         permissions, tool_tags, is_final_decider, team_id, fs_scopes,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["role_id"],
                        r["name"],
                        r.get("reports_to"),
                        json.dumps(r.get("can_spawn", [])),
                        r.get("responsibility", ""),
                        json.dumps(r.get("permissions", {})),
                        json.dumps(r.get("tool_tags", [])),
                        1 if r.get("is_final_decider", False) else 0,
                        r.get("team_id", ""),
                        json.dumps(r.get("fs_scopes", [])),
                        now,
                        now,
                    ),
                )
            self._conn.commit()

    def load_org_roles(self) -> list[dict[str, Any]]:
        """加载所有角色定义。"""
        with self._lock:
            cursor = self._conn.execute("SELECT * FROM org_roles ORDER BY role_id")
            rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "role_id": row["role_id"],
                    "name": row["name"],
                    "reports_to": row["reports_to"],
                    "can_spawn": json.loads(row["can_spawn"] or "[]"),
                    "responsibility": row["responsibility"] or "",
                    "permissions": json.loads(row["permissions"] or "{}"),
                    "tool_tags": json.loads(row["tool_tags"] or "[]"),
                    "is_final_decider": bool(row["is_final_decider"]),
                    "team_id": row["team_id"] or "",
                    "fs_scopes": json.loads(row["fs_scopes"] or "[]"),
                }
            )
        return result

    def save_org_employees(self, employees: list[dict[str, Any]]) -> None:
        """批量保存员工定义（INSERT OR REPLACE）。

        非生产通道（=A：生产组织持久化 = org.json，见 org_persistence.py）；
        保留供测试与既有装配，llm_config 三态（None/{}/值）保真。
        """
        with self._lock:
            # 功能：时间戳按本地时间口径记录（与全仓拍板口径一致）
            now = datetime.now(UTC).isoformat()
            self._conn.execute("DELETE FROM org_employees")
            for e in employees:
                raw_llm = e.get("llm_config")
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO org_employees
                        (employee_id, name, role_id, template_id, description,
                         category, domains, metadata, status, llm_config, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        e["employee_id"],
                        e["name"],
                        e["role_id"],
                        e.get("template_id", ""),
                        e.get("description", ""),
                        e.get("category", ""),
                        json.dumps(e.get("domains", [])),
                        json.dumps(e.get("metadata", {})),
                        e.get("status", "active"),
                        json.dumps(raw_llm) if raw_llm is not None else None,
                        now,
                        now,
                    ),
                )
            self._conn.commit()

    def load_org_employees(self) -> list[dict[str, Any]]:
        """加载所有员工定义（非生产通道，见 save_org_employees）。"""
        with self._lock:
            cursor = self._conn.execute("SELECT * FROM org_employees ORDER BY employee_id")
            rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "employee_id": row["employee_id"],
                    "name": row["name"],
                    "role_id": row["role_id"],
                    "template_id": row["template_id"] or "",
                    "description": row["description"] or "",
                    "category": row["category"] or "",
                    "domains": json.loads(row["domains"] or "[]"),
                    "metadata": json.loads(row["metadata"] or "{}"),
                    "status": row["status"] or "active",
                    "llm_config": json.loads(row["llm_config"]) if row["llm_config"] else None,
                }
            )
        return result

    def _row_to_memory_snapshot(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "snapshot_id": row["id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "employee_id": row["employee_id"],
            "role_id": row["role_id"],
            "memory_scope": row["memory_scope"],
            "memory_kind": row["memory_kind"],
            "summary_text": row["summary_text"],
            "memory_text": row["memory_text"],
            "metadata": json.loads(row["metadata"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def close(self) -> None:
        # 每次自建实例泄漏连接。对齐 GlobalMemoryStore.close 的锁保护模式。
        with self._lock:
            self._conn.close()
