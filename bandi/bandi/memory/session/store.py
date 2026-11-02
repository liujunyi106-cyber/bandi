# 功能：SessionStore — SQLite WAL 会话持久化（主存储 + JSONL 导出）

from __future__ import annotations

import gzip
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bandi.core.message import Message, MessageRole
from bandi.utils.db import connect_app_db
from bandi.utils.timefmt import migrate_naive_timestamps

# `too many SQL variables`；该异常在上层被吞 → TTL 清理/归档**永久静默失效**。
_SQL_VAR_CHUNK = 500

# 员工产出回写会话历史的幂等键前缀（`tool_call_id` 列承载，与线程回写 `message_id=item.id` 同源）
_EMPLOYEE_REPLY_MARKER = "employee-reply:"

# 需要 UTC 归一的时间列（历史库为本地 naive，构造时一次性迁移）
_TIME_COLUMNS: dict[str, tuple[str, ...]] = {
    "sessions": ("created_at", "updated_at"),
    "session_entries": ("created_at",),
    "session_artifacts": ("created_at",),
    "session_context_usage": ("updated_at",),
    "session_entries_archive": ("created_at",),
}


def _chunks(items: list[str], size: int = _SQL_VAR_CHUNK) -> Iterator[list[str]]:
    """功能：把 id 列表切成不超过 SQL 参数上限的批。"""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _safe_json_load(value: str | None, default: object | None = None) -> object:
    if default is None:
        default = []
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class SessionStore:
    def __init__(self, db_path: str | Path) -> None:
        self._sqlite = connect_app_db(
            db_path, isolation_level=None, row_factory=True, foreign_keys=True
        )
        # 可重入锁：append_employee_reply 需要在持锁段内复用 get_session/append_message
        # （幂等判定与插入必须同一临界区，防并发双写）；普通 Lock 会在自调用处死锁。
        self._lock = threading.RLock()
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        self._sqlite.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                mode TEXT DEFAULT 'execute',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                status TEXT DEFAULT 'active'
            )
        """)
        self._sqlite.execute("""
            CREATE TABLE IF NOT EXISTS session_entries (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                parent_id TEXT,
                entry_type TEXT NOT NULL,
                turn_index INTEGER,
                role TEXT,
                content TEXT,
                tool_call_id TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                feedback TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
        """)
        cols = {r[1] for r in self._sqlite.execute("PRAGMA table_info(session_entries)")}
        if "feedback" not in cols:
            try:
                self._sqlite.execute("ALTER TABLE session_entries ADD COLUMN feedback TEXT")
            except sqlite3.OperationalError:
                # 并发进程已补齐该列（duplicate column）——迁移幂等
                pass
        self._sqlite.execute("""
            CREATE INDEX IF NOT EXISTS idx_entries_session
            ON session_entries(session_id, created_at)
        """)
        self._sqlite.execute("""
            CREATE INDEX IF NOT EXISTS idx_entries_parent
            ON session_entries(parent_id)
        """)
        self._sqlite.execute("""
            CREATE TABLE IF NOT EXISTS session_artifacts (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                turn_index INTEGER,
                tool_name TEXT NOT NULL,
                artifact_type TEXT DEFAULT 'file',
                path TEXT NOT NULL,
                hash TEXT DEFAULT '',
                work_item_id TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
        """)
        # 迁移：旧库补列（幂等——列已存在时 ALTER 报错即忽略）
        try:
            self._sqlite.execute(
                "ALTER TABLE session_artifacts ADD COLUMN work_item_id TEXT DEFAULT ''"
            )
        except Exception:  # noqa: BLE001 — 列已存在/迁移不適用皆跳过
            pass
        self._sqlite.execute("""
            CREATE INDEX IF NOT EXISTS idx_artifacts_session
            ON session_artifacts(session_id, created_at)
        """)
        # 功能：上下文用量快照（每会话一行，覆盖式写入——只服务「当前这一轮」展示）
        # 不设外键：员工执行会话/子代理会话 id 不保证在 sessions 表内，外键失败会让
        # 展示面静默归零（该表是派生缓存，无级联语义需求）。
        self._sqlite.execute("""
            CREATE TABLE IF NOT EXISTS session_context_usage (
                session_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # 功能：截断归档——编辑重开前的后续条目落档，支持一次撤销
        self._sqlite.execute("""
            CREATE TABLE IF NOT EXISTS session_entries_archive (
                archive_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self._sqlite.execute("""
            CREATE INDEX IF NOT EXISTS idx_entries_archive_op
            ON session_entries_archive(session_id, operation_id)
        """)
        # 功能：历史本地 naive 时间串归一为 UTC 带偏移（幂等；无 naive 值时零写入）
        migrate_naive_timestamps(self._sqlite, _TIME_COLUMNS)
        self._sqlite.commit()

    def count_sessions(self, status: str = "active") -> int:
        with self._lock:
            row = self._sqlite.execute(
                "SELECT COUNT(*) FROM sessions WHERE status = ?", (status,)
            ).fetchone()
        return row[0] if row else 0

    def rotate(
        self,
        archive_dir: str | Path,
        max_sessions: int = 1000,
        archive_after_days: int = 30,
    ) -> dict[str, Any]:
        archive_path = Path(archive_dir)
        archive_path.mkdir(parents=True, exist_ok=True)
        with self._lock:
            over_limit = self._sqlite.execute(
                """SELECT id, title, mode, created_at, updated_at, metadata, status
                   FROM sessions WHERE status = 'active'
                   ORDER BY updated_at DESC
                   LIMIT -1 OFFSET ?""",
                (max_sessions,),
            ).fetchall()
            to_archive: dict[str, sqlite3.Row] = {r["id"]: r for r in over_limit}
            if archive_after_days > 0:
                age_cutoff = (datetime.now(UTC) - timedelta(days=archive_after_days)).isoformat()
                old_sessions = self._sqlite.execute(
                    """SELECT id, title, mode, created_at, updated_at, metadata, status
                       FROM sessions WHERE status = 'active'
                       AND updated_at < ?
                       ORDER BY updated_at ASC""",
                    (age_cutoff,),
                ).fetchall()
                for r in old_sessions:
                    to_archive[r["id"]] = r
            if not to_archive:
                return {"archived_count": 0, "archived_ids": [], "archive_dir": str(archive_path)}
            ids = list(to_archive.keys())
            # → 归档/TTL **永久静默失效**（长期累积不减）。每会话的行都落在含该 id
            # 的那一批内，批内 ORDER BY session_id, created_at 保序语义不变。
            entries_by_session: dict[str, list[dict]] = {}
            for chunk in _chunks(ids):
                placeholders = ",".join("?" for _ in chunk)
                for e in self._sqlite.execute(
                    f"""SELECT * FROM session_entries
                        WHERE session_id IN ({placeholders})
                        ORDER BY session_id, created_at""",
                    chunk,
                ).fetchall():
                    d = _row_to_dict(e)
                    entries_by_session.setdefault(d["session_id"], []).append(d)
            archived_ids: list[str] = []
            for sid, srow in to_archive.items():
                # 归档文件名用完整 session id，避免同日创建且 id 前缀相同的 session 归档互相覆盖（内容丢失）
                safe_sid = re.sub(r"[^A-Za-z0-9_-]", "_", sid)
                archive_file = (
                    archive_path / f"session_{safe_sid}_{srow['created_at'][:10]}.jsonl.gz"
                )
                s_entries = entries_by_session.get(sid, [])
                with gzip.open(str(archive_file), "wt", encoding="utf-8") as f:
                    f.write(json.dumps(dict(srow), ensure_ascii=False) + "\n")
                    for entry in s_entries:
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._delete_session_children(sid)
                self._sqlite.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                archived_ids.append(sid)
            self._sqlite.commit()
        return {
            "archived_count": len(archived_ids),
            "archived_ids": archived_ids,
            "archive_dir": str(archive_path),
        }

    async def on_state_change(self, event: object) -> None:
        data = event.data if hasattr(event, "data") else {}
        session_id = data.get("session_id")
        if not session_id:
            return
        if data.get("to") != "terminated":
            return
        # terminated 不插 checkpoint——终止元数据已由 on_terminated 写入
        # session metadata，历史恢复走 message 条目

    async def on_terminated(self, event: object) -> None:
        data = event.data if hasattr(event, "data") else {}
        session_id = data.get("session_id")
        if not session_id:
            return
        session = self.get_session(session_id)
        if session is None:
            return
        metadata = _safe_json_load(session.get("metadata"), default={})
        metadata["last_terminal_reason"] = data.get("terminal_reason", "unknown")
        self.update_session(
            session_id,
            metadata=json.dumps(metadata, ensure_ascii=False),
            updated_at=datetime.now(UTC).isoformat(),
        )

    def count_expired(self, ttl_days: int = 90) -> int:
        """功能：统计将被 `delete_expired` 命中的会话数（与删除同口径，供清理前预览）。"""
        cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).isoformat()
        with self._lock:
            row = self._sqlite.execute(
                "SELECT COUNT(*) FROM sessions WHERE updated_at < ?", (cutoff,)
            ).fetchone()
        return int(row[0] if row else 0)

    def delete_expired(self, ttl_days: int = 90) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).isoformat()
        with self._lock:
            if self._sqlite.in_transaction:
                # （外部写入凭空消失）；此处不触碰外部事务，由调用方决定去留。
                raise RuntimeError("delete_expired must not run inside caller transaction")
            try:
                # 🟡-D：本连接为 autocommit（isolation_level=None），分批 DELETE
                # 若中途失败，先前批次已永久提交、异常又被维护上层吞掉后还报
                # deleted=0——数据已丢却报告没删。显式事务包住全部分批：任一
                self._sqlite.execute("BEGIN IMMEDIATE")
                expired = self._sqlite.execute(
                    "SELECT id FROM sessions WHERE updated_at < ?", (cutoff,)
                ).fetchall()
                ids = [r["id"] for r in expired]
                if not ids:
                    self._sqlite.rollback()
                    return 0
                # → 过期会话永远删不掉（静默失效）
                for chunk in _chunks(ids):
                    placeholders = ",".join("?" for _ in chunk)
                    for table in (
                        "session_artifacts",
                        "session_context_usage",
                        "session_entries_archive",
                        "session_entries",
                    ):
                        self._sqlite.execute(
                            f"DELETE FROM {table} WHERE session_id IN ({placeholders})", chunk
                        )
                    self._sqlite.execute(
                        f"DELETE FROM sessions WHERE id IN ({placeholders})", chunk
                    )
                self._sqlite.commit()
            except BaseException:
                self._sqlite.rollback()
                raise
            return len(ids)

    def close(self) -> None:
        with self._lock:
            self._sqlite.close()

    def create_session(
        self,
        session_id: str | None = None,
        title: str = "",
        mode: str = "execute",
        project_root: str | None = None,
        project_id: str | None = None,
        origin: str | None = None,
        target_role: str | None = None,
        employee_id: str | None = None,
        employee_name: str | None = None,
    ) -> str:
        sid = session_id or str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        meta: dict[str, Any] = {}
        if project_root:
            meta["project_root"] = project_root
        if project_id:
            meta["project_id"] = project_id
        if origin:
            meta["origin"] = origin
        if target_role:
            meta["target_role"] = str(target_role).strip()
        if employee_id:
            meta["employee_id"] = str(employee_id).strip()
        if employee_name:
            meta["employee_name"] = str(employee_name).strip()
        metadata = json.dumps(meta, ensure_ascii=False) if meta else "{}"
        with self._lock:
            self._sqlite.execute(
                "INSERT INTO sessions (id, title, mode, created_at, updated_at, metadata, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, title, mode, now, now, metadata, "active"),
            )
            self._sqlite.commit()
        return sid

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._sqlite.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        return _row_to_dict(row)

    def session_project_root(self, session_id: str) -> str | None:
        """功能：从会话 metadata 解析 project_root（后续轮未显式传
        project_root 时工具层基准回填的单一来源）。无会话/无字段返回 None。"""
        session = self.get_session(session_id)
        if not session:
            return None
        raw = session.get("metadata") or "{}"
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                return None
        root = (raw or {}).get("project_root") if isinstance(raw, dict) else None
        return str(root) if root else None

    def update_session(self, session_id: str, **kwargs: Any) -> bool:
        allowed = {"title", "mode", "metadata", "status", "updated_at"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        if "updated_at" not in kwargs:
            updates["updated_at"] = datetime.now(UTC).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [session_id]
        with self._lock:
            cur = self._sqlite.execute(f"UPDATE sessions SET {set_clause} WHERE id = ?", values)
            self._sqlite.commit()
        return cur.rowcount > 0

    def update_session_if_metadata(
        self,
        session_id: str,
        new_metadata: dict[str, Any],
        expected_metadata: dict[str, Any],
    ) -> bool:
        """CAS 原子更新——仅当当前 metadata 与 expected 相等时写入
        （跨进程原子认领：两进程同时认领时仅一个成功）。键序与原写入格式一致
        （json.dumps 无 sort_keys，Python dict 保序读回后键序不变）。"""
        new_json = json.dumps(new_metadata, ensure_ascii=False)
        expected_json = json.dumps(expected_metadata, ensure_ascii=False)
        with self._lock:
            cur = self._sqlite.execute(
                "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ? AND metadata = ?",
                (new_json, datetime.now(UTC).isoformat(), session_id, expected_json),
            )
            self._sqlite.commit()
        return cur.rowcount > 0

    def list_sessions(self, limit: int = 50, status: str = "active") -> list[dict[str, Any]]:
        with self._lock:
            rows = self._sqlite.execute(
                "SELECT * FROM sessions WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_entry(self, entry_id: str, session_id: str | None = None) -> dict[str, Any] | None:
        """功能：按 id 取单条 entry（带 session_id 约束防跨会话读取）。"""
        with self._lock:
            if session_id:
                row = self._sqlite.execute(
                    "SELECT * FROM session_entries WHERE id = ? AND session_id = ?",
                    (entry_id, session_id),
                ).fetchone()
            else:
                row = self._sqlite.execute(
                    "SELECT * FROM session_entries WHERE id = ?", (entry_id,)
                ).fetchone()
        return dict(row) if row is not None else None

    def set_entry_feedback(
        self, entry_id: str, feedback: str | None, session_id: str | None = None
    ) -> bool:
        """功能：写入/清除消息点赞标记（空值落 NULL；跨会话写入被 session_id 约束拒绝）。"""
        value = str(feedback).strip() if feedback else None
        with self._lock:
            if session_id:
                cur = self._sqlite.execute(
                    "UPDATE session_entries SET feedback = ? WHERE id = ? AND session_id = ?",
                    (value, entry_id, session_id),
                )
            else:
                cur = self._sqlite.execute(
                    "UPDATE session_entries SET feedback = ? WHERE id = ?", (value, entry_id)
                )
            self._sqlite.commit()
        return cur.rowcount > 0

    def _delete_session_children(self, session_id: str) -> None:
        # 功能：按会话清理子表（产物/用量/归档/消息），避免外键残留或删除失败。
        for table in (
            "session_artifacts",
            "session_context_usage",
            "session_entries_archive",
            "session_entries",
        ):
            self._sqlite.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            self._delete_session_children(session_id)
            cur = self._sqlite.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._sqlite.commit()
        return cur.rowcount > 0

    def append_message(self, session_id: str, msg: Message) -> str:
        entry_id = str(uuid.uuid4())
        role = msg.role.value if isinstance(msg.role, MessageRole) else msg.role
        now = datetime.now(UTC).isoformat()
        meta_extra = {
            k: msg.meta[k]
            for k in (
                "tool_error",
                "safety_blocked",
                "attachments",
                "employee_reply",
                "sender_role",
                "sender_name",
                "process_events",
                "note_kind",
                "dedupe_key",
                # 功能：thinking 模式续轮需要（会话恢复后带工具调用消息仍可回传）
                "reasoning_content",
            )
            if isinstance(msg.meta, dict) and msg.meta.get(k)
        }
        if msg.name:
            # message_from_entry 读回据此恢复——工具归因/折叠落盘命名依赖）
            meta_extra["name"] = msg.name
        metadata_json = json.dumps(
            {
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in msg.tool_calls
                ]
                if msg.tool_calls
                else [],
                **meta_extra,
            },
            ensure_ascii=False,
        )
        with self._lock:
            parent_id = self._get_latest_leaf(session_id)
            try:
                self._sqlite.execute("BEGIN")
                self._sqlite.execute(
                    """INSERT INTO session_entries
                       (id, session_id, parent_id, entry_type, turn_index,
                        role, content, tool_call_id, metadata, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry_id,
                        session_id,
                        parent_id,
                        "message",
                        msg.turn_index,
                        role,
                        msg.content,
                        msg.tool_call_id,
                        metadata_json,
                        now,
                    ),
                )
                self._sqlite.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                self._sqlite.commit()
            except Exception:
                self._sqlite.rollback()
                raise
        return entry_id

    def append_employee_reply(
        self,
        session_id: str,
        *,
        content: str,
        sender_role: str,
        work_item_id: str,
        sender_name: str = "",
        process_events: list[dict[str, Any]] | None = None,
        next_assignee: dict[str, str] | None = None,
    ) -> str | None:
        """功能：员工产出回写**会话历史**（同卡恰一条：后写覆盖前写；会话不存在/内容空/work_item 空返回 None）。

        为什么需要：员工回复原本只写线程（threads/*.md），会话历史里没有 ⇒ 刷新/切走/
        新开会话后员工回复消失（承接场景断档）。本方法把同一条产出落进会话条目。

        幂等键 = `tool_call_id` 列上的 `employee-reply:<work item id>`（与线程回写
        `message_id=item.id` 同源）：同卡重放不新增；**最新产出覆盖旧产出**（失败轮
        终止文案先占键后，成功重试的写回必须能替换它）。判定与写入同一临界区（RLock）。
        `turn_index` 取该会话现有最大值（本回复属"会话当前轮之后的补充"）。
        """
        text = str(content or "").strip()
        marker_id = str(work_item_id or "").strip()
        if not session_id or not text or not marker_id:
            return None
        marker = f"{_EMPLOYEE_REPLY_MARKER}{marker_id}"
        entry_meta: dict[str, Any] = {
            "employee_reply": True,
            "sender_role": str(sender_role or ""),
            # 写入时快照显示名：侧栏预览/分享页等无法解析员工的界面也能正确显示
            "sender_name": str(sender_name or ""),
            # 工作项关联（成员任务卡/交付物卡片按此匹配；刷新后不丢）
            "work_item_id": marker_id,
            # 过程快照随回复落条目——刷新后过程块可回看
            **({"process_events": list(process_events)} if process_events else {}),
            # 交接：本轮之后下一个待执行卡的执行者（回复底部「下一步」行）
            **({"next_assignee": dict(next_assignee)} if next_assignee else {}),
        }
        with self._lock:
            if self.get_session(session_id) is None:
                return None
            existing = self._sqlite.execute(
                "SELECT id FROM session_entries WHERE session_id = ? AND tool_call_id = ?"
                " ORDER BY rowid DESC LIMIT 1",
                (session_id, marker),
            ).fetchone()
            if existing is not None:
                self._sqlite.execute(
                    "UPDATE session_entries SET content = ?, metadata = ?, created_at = ?"
                    " WHERE id = ?",
                    (
                        text,
                        json.dumps(entry_meta, ensure_ascii=False),
                        datetime.now(UTC).isoformat(),
                        existing["id"],
                    ),
                )
                self._sqlite.commit()
                return str(existing["id"])
            row = self._sqlite.execute(
                "SELECT COALESCE(MAX(turn_index), 0) AS turn FROM session_entries"
                " WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            turn_index = int(row["turn"] or 0) if row is not None else 0
            return self.append_message(
                session_id,
                Message(
                    role=MessageRole.ASSISTANT,
                    content=text,
                    tool_call_id=marker,
                    turn_index=turn_index,
                    meta=entry_meta,
                ),
            )

    def append_process_entry(
        self, session_id: str, turn_index: int, nodes: list[dict[str, Any]]
    ) -> str:
        """-3c：轮末过程节点落一条（entry_type="process"；content 空，节点在
        metadata.process——与前端 ProcessNode 同构）。非 message 条目不进对话链
        （_get_latest_leaf 只取 message），fork/恢复语义不受影响。"""
        entry_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        metadata_json = json.dumps({"process": nodes, "turn_index": turn_index}, ensure_ascii=False)
        with self._lock:
            parent_id = self._get_latest_leaf(session_id)
            try:
                self._sqlite.execute("BEGIN")
                self._sqlite.execute(
                    """INSERT INTO session_entries
                       (id, session_id, parent_id, entry_type, turn_index,
                        role, content, tool_call_id, metadata, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry_id,
                        session_id,
                        parent_id,
                        "process",
                        turn_index,
                        "assistant",
                        "",
                        None,
                        metadata_json,
                        now,
                    ),
                )
                self._sqlite.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                self._sqlite.commit()
            except Exception:
                self._sqlite.rollback()
                raise
        return entry_id

    def register_artifact(
        self,
        session_id: str,
        *,
        tool_name: str,
        path: str,
        artifact_type: str = "file",
        hash_value: str = "",
        turn_index: int = 0,
        work_item_id: str = "",
    ) -> str:
        """-3d：产物登记（工具写文件成功后调用——谁产出/类型/路径/hash）。

        work_item_id 非空时把产物关联到工作项（员工回复下方的交付物卡片据此精确匹配）。
        """
        artifact_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._sqlite.execute(
                """INSERT INTO session_artifacts
                   (id, session_id, turn_index, tool_name, artifact_type, path, hash,
                    work_item_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    artifact_id,
                    session_id,
                    turn_index,
                    tool_name,
                    artifact_type,
                    path,
                    hash_value,
                    work_item_id,
                    now,
                ),
            )
            self._sqlite.commit()
        return artifact_id

    def list_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        """-3d：会话产物清单（右栏制品预览 Tab；created_at 升序）。"""
        with self._lock:
            cursor = self._sqlite.execute(
                "SELECT * FROM session_artifacts WHERE session_id = ? ORDER BY created_at, rowid",
                (session_id,),
            )
            return [_row_to_dict(row) for row in cursor.fetchall()]

    def list_artifacts_by_prefix(self, session_prefix: str) -> list[dict[str, Any]]:
        """功能：按会话 id 前缀查产物（员工执行会话 `role-session::<项目>::` 聚合用）。

        前缀按 LIKE 转义（`\\`/`%`/`_`），避免项目名含通配符时误匹配。
        """
        escaped = str(session_prefix).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._sqlite.execute(
                "SELECT * FROM session_artifacts WHERE session_id LIKE ? ESCAPE '\\'"
                " ORDER BY created_at, rowid",
                (f"{escaped}%",),
            )
            return [_row_to_dict(row) for row in cursor.fetchall()]

    def save_context_usage(self, session_id: str, payload: dict[str, Any]) -> None:
        """功能：写入当前轮的上下文用量快照（每会话一行，覆盖式——只保留最新一轮）。"""
        body = json.dumps(payload, ensure_ascii=False)
        updated_at = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            self._sqlite.execute(
                "INSERT INTO session_context_usage(session_id, payload, updated_at)"
                " VALUES(?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET payload = excluded.payload,"
                " updated_at = excluded.updated_at",
                (session_id, body, updated_at),
            )
            self._sqlite.commit()

    def get_context_usage(self, session_id: str) -> dict[str, Any] | None:
        """功能：读取上下文用量快照（无记录/坏 JSON 返回 None，由调用方给「暂无」态）。"""
        with self._lock:
            row = self._sqlite.execute(
                "SELECT payload FROM session_context_usage WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def skip_process_tool_node(self, session_id: str, tool_call_id: str) -> bool:
        """-3c：跳过态随存——按 tool_call_id 定位本会话 process 条目内的工具节点
        翻转 status error→skipped。定位失败返回 False（不静默成功）。"""
        with self._lock:
            rows = self._sqlite.execute(
                "SELECT id, metadata FROM session_entries "
                "WHERE session_id = ? AND entry_type = 'process' ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        for row_id, raw in rows:
            try:
                meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (TypeError, ValueError):
                continue
            nodes = meta.get("process")
            if not isinstance(nodes, list):
                continue
            changed = False
            for node in nodes:
                if (
                    isinstance(node, dict)
                    and node.get("kind") == "tool"
                    and node.get("toolCallId") == tool_call_id
                    and node.get("status") == "error"
                ):
                    node["status"] = "skipped"
                    changed = True
            if changed:
                with self._lock:
                    cur = self._sqlite.execute(
                        "UPDATE session_entries SET metadata = ? "
                        "WHERE id = ? AND session_id = ? AND entry_type = 'process'",
                        (
                            json.dumps({**meta, "process": nodes}, ensure_ascii=False),
                            row_id,
                            session_id,
                        ),
                    )
                    self._sqlite.commit()
                    return cur.rowcount > 0
        return False

    def _get_latest_leaf(self, session_id: str) -> str | None:
        # checkpoint 等导航条目不是对话链节点——消息 parent 必须取最近 message 条目
        # （Windows 时钟粒度下同 created_at 常态，fork/分支链因此错乱）
        row = self._sqlite.execute(
            "SELECT id FROM session_entries WHERE session_id = ? AND entry_type = 'message' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return row[0] if row else None

    def get_branch(self, session_id: str, *, full: bool = False) -> list[dict]:
        """默认返回压缩边界之后的活动切片（活动视图/恢复用）。

        full=True 返回全部条目（审计/fork/去重需要原始历史）。无 boundary 时
        两者等价。边界之后包含：summary 条目 + 压缩时重挂的 preserved segment
        + 之后新增的消息。
        """
        with self._lock:
            cursor = self._sqlite.execute(
                "SELECT * FROM session_entries WHERE session_id = ? ORDER BY created_at, rowid",
                (session_id,),
            )
            entries = [_row_to_dict(row) for row in cursor.fetchall()]
        if full:
            return entries
        boundary_idx = -1
        for i, entry in enumerate(entries):
            if entry.get("entry_type") in ("summary", "compact_boundary"):
                boundary_idx = i
        return entries[boundary_idx:] if boundary_idx >= 0 else entries

    def get_full_branch(self, session_id: str) -> list[dict]:
        """显式取全量历史（边界之前 + 之后）——审计/归档/分支操作专用。"""
        return self.get_branch(session_id, full=True)

    def resolved_branch(self, session_id: str, at: str | None = None) -> list[dict]:
        """Day127 无状态预览：返回会话分支按恢复点 at 截断（含自身）的视图。

        起 at=None 返回**全量历史**（审计/时间旅行视图），活动切片走
        get_branch()。at 指向不存在的 entry 显式报错，不静默回退。
        边界 index 语义与 fork/truncate_branch 共用（DRY）。不写状态。"""
        entries = self.get_full_branch(session_id)
        if not at:
            return entries
        split_idx = next((i for i, e in enumerate(entries) if e["id"] == at), -1)
        if split_idx == -1:
            raise ValueError(f"Entry {at} not found in session {session_id}")
        return entries[: split_idx + 1]

    def copy_entries(self, source_entries: list[dict], new_session_id: str) -> dict[str, str]:
        import uuid

        id_map: dict[str, str] = {}
        with self._lock:
            try:
                self._sqlite.execute("BEGIN")
                for entry in source_entries:
                    new_id = str(uuid.uuid4())
                    id_map[entry["id"]] = new_id
                    old_parent = entry.get("parent_id")
                    parent = id_map.get(old_parent) if old_parent else None
                    self._sqlite.execute(
                        """INSERT INTO session_entries
                           (id, session_id, parent_id, entry_type, turn_index,
                            role, content, tool_call_id, metadata, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            new_id,
                            new_session_id,
                            parent,
                            entry.get("entry_type"),
                            entry.get("turn_index"),
                            entry.get("role"),
                            entry.get("content"),
                            entry.get("tool_call_id"),
                            entry.get("metadata", "{}"),
                            entry["created_at"],
                        ),
                    )
                self._sqlite.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (datetime.now(UTC).isoformat(), new_session_id),
                )
                self._sqlite.commit()
            except Exception:
                self._sqlite.rollback()
                raise
        return id_map

    def truncate_branch(self, session_id: str, entry_id: str) -> int:
        """Day127：截断——删除恢复点 entry 之后的所有条目并归档，保留恢复点自身。"""
        removed, _operation_id = self.truncate_branch_archived(session_id, entry_id)
        return removed

    def truncate_branch_archived(self, session_id: str, entry_id: str) -> tuple[int, str]:
        """功能：截断分支并把被删条目写入归档，返回（删除数, 操作 id）"""
        entries = self.get_full_branch(session_id)
        if entry_id == "":
            remove_ids = [e["id"] for e in entries]
        else:
            split_idx = next((i for i, e in enumerate(entries) if e["id"] == entry_id), -1)
            if split_idx == -1:
                raise ValueError(f"Entry {entry_id} not found in session {session_id}")
            remove_ids = [e["id"] for e in entries[split_idx + 1 :]]
        if not remove_ids:
            return 0, ""
        removed_set = set(remove_ids)
        removed_rows = [e for e in entries if e["id"] in removed_set]
        operation_id = uuid.uuid4().hex
        now = datetime.now(UTC).isoformat()
        with self._lock:
            try:
                self._sqlite.execute("BEGIN")
                for entry in removed_rows:
                    self._sqlite.execute(
                        """INSERT INTO session_entries_archive
                           (archive_id, operation_id, session_id, payload, created_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (
                            uuid.uuid4().hex,
                            operation_id,
                            session_id,
                            json.dumps(entry, ensure_ascii=False, default=str),
                            now,
                        ),
                    )
                    self._sqlite.execute(
                        "DELETE FROM session_entries WHERE id = ? AND session_id = ?",
                        (entry["id"], session_id),
                    )
                self._prune_archive_locked(session_id)
                self._sqlite.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                self._sqlite.commit()
            except Exception:
                self._sqlite.rollback()
                raise
        return len(remove_ids), operation_id

    def restore_archived(self, session_id: str, operation_id: str) -> int:
        """功能：撤销一次截断——把归档条目按原 id 恢复（幂等，已存在的 id 跳过）"""
        with self._lock:
            rows = self._sqlite.execute(
                "SELECT payload FROM session_entries_archive "
                "WHERE session_id = ? AND operation_id = ? ORDER BY rowid",
                (session_id, operation_id),
            ).fetchall()
            if not rows:
                return 0
            restored = 0
            try:
                self._sqlite.execute("BEGIN")
                for (payload,) in rows:
                    entry = json.loads(payload)
                    cursor = self._sqlite.execute(
                        """INSERT OR IGNORE INTO session_entries
                           (id, session_id, parent_id, entry_type, turn_index,
                            role, content, tool_call_id, metadata, created_at, feedback)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            entry.get("id"),
                            session_id,
                            entry.get("parent_id"),
                            entry.get("entry_type"),
                            entry.get("turn_index"),
                            entry.get("role"),
                            entry.get("content"),
                            entry.get("tool_call_id"),
                            entry.get("metadata", "{}"),
                            entry.get("created_at"),
                            entry.get("feedback"),
                        ),
                    )
                    restored += max(cursor.rowcount, 0)
                self._sqlite.execute(
                    "DELETE FROM session_entries_archive WHERE session_id = ? AND operation_id = ?",
                    (session_id, operation_id),
                )
                self._sqlite.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (datetime.now(UTC).isoformat(), session_id),
                )
                self._sqlite.commit()
            except Exception:
                self._sqlite.rollback()
                raise
        return restored

    def _prune_archive_locked(self, session_id: str, keep_operations: int = 20) -> None:
        """功能：每会话只保留最近 N 次截断归档（防撤销表无限增长）"""
        stale = self._sqlite.execute(
            """SELECT operation_id FROM session_entries_archive
               WHERE session_id = ?
               GROUP BY operation_id
               ORDER BY MAX(rowid) DESC
               LIMIT -1 OFFSET ?""",
            (session_id, keep_operations),
        ).fetchall()
        for (operation_id,) in stale:
            self._sqlite.execute(
                "DELETE FROM session_entries_archive WHERE session_id = ? AND operation_id = ?",
                (session_id, operation_id),
            )

    def count_branch(self, session_id: str) -> int:
        # 只计活动切片（最新 boundary 之后）——避免 preserved segment 把
        # 重挂消息与原条目一起计数导致会话列表 message_count 虚高。
        with self._lock:
            row = self._sqlite.execute(
                "SELECT COUNT(*) FROM session_entries WHERE session_id = ? AND rowid > "
                "COALESCE(("
                "  SELECT MAX(rowid) FROM session_entries WHERE session_id = ? "
                "  AND entry_type IN ('summary','compact_boundary')), -1)",
                (session_id, session_id),
            ).fetchone()
        return int(row[0]) if row else 0

    def export_jsonl(self, session_id: str, output_path: str | Path) -> int:
        # 导出=审计语义，必须含边界之前原始历史
        entries = self.get_full_branch(session_id)
        with open(output_path, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)
        return len(entries)

    def save_checkpoint_entry(self, session_id: str, turn_index: int) -> None:
        """写入 checkpoint 类型 entry——支持 TraceExplainer 时间旅行导航。"""
        try:
            with self._lock:
                entry_id = str(uuid.uuid4())
                now = datetime.now(UTC).isoformat()
                self._sqlite.execute(
                    """INSERT INTO session_entries
                       (id, session_id, parent_id, entry_type, turn_index,
                        role, content, tool_call_id, metadata, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry_id,
                        session_id,
                        None,
                        "checkpoint",
                        turn_index,
                        None,
                        None,
                        None,
                        json.dumps({"turn_index": turn_index}, ensure_ascii=False),
                        now,
                    ),
                )
                self._sqlite.commit()
        except sqlite3.IntegrityError:
            # session 不存在时外键约束拒绝写入——静默忽略，不影响主流程
            pass

    def append_summary_entry(
        self,
        session_id: str,
        turn_index: int,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """写入压缩边界/摘要 entry（entry_type="summary"）。

        与 message 条目并存——原始历史仍可审计；恢复侧按最新 summary 的
        active slice 投影，避免重启后 get_branch 读回全量旧消息反复重压。
        `_get_latest_leaf` 只取 message，故本类型自动不进 parent 链。
        """
        try:
            with self._lock:
                entry_id = str(uuid.uuid4())
                now = datetime.now(UTC).isoformat()
                meta = {"kind": "compact_boundary", "turn_index": turn_index}
                if metadata:
                    meta.update(metadata)
                self._sqlite.execute(
                    """INSERT INTO session_entries
                       (id, session_id, parent_id, entry_type, turn_index,
                        role, content, tool_call_id, metadata, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry_id,
                        session_id,
                        None,
                        "summary",
                        turn_index,
                        "system",
                        content,
                        None,
                        json.dumps(meta, ensure_ascii=False, default=str),
                        now,
                    ),
                )
                self._sqlite.commit()
        except sqlite3.IntegrityError:
            # session 不存在时外键约束拒绝写入——静默忽略，不影响主流程
            pass

    def append_preserved_segment(self, session_id: str, messages: list[Message]) -> int:
        """把压缩后的 active slice 重新挂到 boundary 之后。

        原始消息条目仍在 boundary 之前，`get_branch()` 默认只返回
        boundary 之后（含重挂的真实 entry id/metadata），恢复/UI 直接用
        活动切片。本方法不重建 boundary 回放的摘要信息。
        """
        written = 0
        for msg in messages:
            self.append_message(session_id, msg)
            written += 1
        return written

    def list_checkpoint_entries(self, session_id: str) -> list[dict]:
        """列出某会话所有 checkpoint entry，按 turn_index 排序。"""
        with self._lock:
            rows = self._sqlite.execute(
                "SELECT turn_index, created_at FROM session_entries "
                "WHERE session_id=? AND entry_type='checkpoint' "
                "ORDER BY turn_index, created_at",
                (session_id,),
            ).fetchall()
            return [{"turn_index": r["turn_index"], "created_at": r["created_at"]} for r in rows]
