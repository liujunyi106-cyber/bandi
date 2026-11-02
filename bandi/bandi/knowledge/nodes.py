# 功能：资料库节点树（文件夹/文档/链接）存储——层级、同级重名、移动防环、员工归属级联。

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from bandi.utils.db import connect_app_db

NODE_KINDS: frozenset[str] = frozenset(
    {"folder", "md", "csv", "docx", "xlsx", "pptx", "pdf", "html", "link"}
)
FOLDER_KIND = "folder"
ROOT_PARENT = ""
MAX_NAME_LEN = 100
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
_INVALID_CHARS = str.maketrans({c: "_" for c in '\\/:*?"<>|'})


@dataclass
class KnowledgeNode:
    id: str
    base_id: str
    parent_id: str
    kind: str
    name: str
    sort_order: int = 0
    employee_id: str | None = None
    storage_path: str = ""
    size: int = 0
    url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return "kn_" + uuid4().hex[:12]


def sanitize_name(raw: str) -> str:
    # 功能：清洗节点名——去控制字符/非法字符、去尾部点与空格、规避 Windows 保留名、限长保留扩展名。
    name = "".join(ch for ch in (raw or "") if ch >= " " and ch != "\x7f")
    name = name.translate(_INVALID_CHARS).strip().strip(".")
    if not name:
        return "未命名"
    stem, dot, ext = name.rpartition(".")
    if dot and stem and len(ext) <= 10:
        head = stem[: max(1, MAX_NAME_LEN - len(ext) - 1)]
        name = f"{head}.{ext}"
    else:
        name = name[:MAX_NAME_LEN]
    if name.split(".")[0].strip().lower() in _RESERVED_NAMES:
        name = "_" + name
    return name


def with_suffix_index(name: str, index: int) -> str:
    # 功能：在同名冲突时插入 (n) 后缀——文件保留扩展名，文件夹直接追加。
    stem, dot, ext = name.rpartition(".")
    if dot and stem and len(ext) <= 10:
        return f"{stem} ({index}).{ext}"
    return f"{name} ({index})"


class KnowledgeNodeStore:
    # 功能：节点树读写入口——单连接 + 线程锁，所有变更在事务内提交。
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._lock = threading.Lock()
        self._conn = connect_app_db(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_nodes ("
                "  id TEXT PRIMARY KEY,"
                "  base_id TEXT NOT NULL,"
                "  parent_id TEXT NOT NULL DEFAULT '',"
                "  kind TEXT NOT NULL,"
                "  name TEXT NOT NULL,"
                "  sort_order INTEGER NOT NULL DEFAULT 0,"
                "  employee_id TEXT DEFAULT NULL,"
                "  storage_path TEXT NOT NULL DEFAULT '',"
                "  size INTEGER NOT NULL DEFAULT 0,"
                "  url TEXT NOT NULL DEFAULT '',"
                "  metadata TEXT DEFAULT '{}',"
                "  created_at REAL NOT NULL,"
                "  updated_at REAL NOT NULL,"
                "  UNIQUE(base_id, parent_id, name)"
                ")"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_nodes_parent ON knowledge_nodes(base_id, parent_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_nodes_employee ON knowledge_nodes(employee_id)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_node_views ("
                "  node_id TEXT PRIMARY KEY,"
                "  kind TEXT NOT NULL,"
                "  state_json TEXT NOT NULL,"
                "  updated_at REAL NOT NULL"
                ")"
            )
            self._ensure_document_employee_column()
            self._conn.commit()

    def get_view(self, node_id: str) -> dict[str, Any] | None:
        # 功能：读取节点视图态（CSV 筛选/分组/排序等），无记录返回 None。
        row = self._conn.execute(
            "SELECT state_json FROM knowledge_node_views WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None:
            return None
        parsed = _json_load(row[0])
        return parsed or {}

    def set_view(self, node_id: str, kind: str, state: dict[str, Any]) -> None:
        # 功能：写入/覆盖节点视图态（幂等 upsert）。
        with self._lock:
            self._conn.execute(
                "INSERT INTO knowledge_node_views(node_id, kind, state_json, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(node_id) DO UPDATE SET kind = excluded.kind,"
                " state_json = excluded.state_json, updated_at = excluded.updated_at",
                (node_id, kind, _json_dump(state), _now()),
            )
            self._conn.commit()

    def _ensure_document_employee_column(self) -> None:
        columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(knowledge_documents)").fetchall()
        }
        if columns and "node_employee_id" not in columns:
            self._conn.execute(
                "ALTER TABLE knowledge_documents ADD COLUMN node_employee_id TEXT DEFAULT NULL"
            )

    # -- 读 -----------------------------------------------------------------

    def get_node(self, node_id: str) -> KnowledgeNode | None:
        row = self._conn.execute(
            "SELECT * FROM knowledge_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return self._row_to_node(row) if row else None

    def list_children(self, base_id: str, parent_id: str = ROOT_PARENT) -> list[KnowledgeNode]:
        rows = self._conn.execute(
            "SELECT * FROM knowledge_nodes WHERE base_id = ? AND parent_id = ? "
            "ORDER BY kind = 'folder' DESC, sort_order ASC, name ASC",
            (base_id, parent_id),
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def list_tree(self, base_id: str) -> list[KnowledgeNode]:
        rows = self._conn.execute(
            "SELECT * FROM knowledge_nodes WHERE base_id = ? "
            "ORDER BY kind = 'folder' DESC, sort_order ASC, name ASC",
            (base_id,),
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def descendant_ids(self, node_id: str) -> list[str]:
        found: list[str] = []
        pending = [node_id]
        while pending:
            current = pending.pop()
            children = self._conn.execute(
                "SELECT id FROM knowledge_nodes WHERE parent_id = ?", (current,)
            ).fetchall()
            for (child_id,) in children:
                found.append(str(child_id))
                pending.append(str(child_id))
        return found

    def unique_name(self, base_id: str, parent_id: str, name: str, exclude_id: str = "") -> str:
        candidate = sanitize_name(name)
        index = 0
        while True:
            row = self._conn.execute(
                "SELECT id FROM knowledge_nodes WHERE base_id = ? AND parent_id = ? AND name = ?",
                (base_id, parent_id, candidate),
            ).fetchone()
            if row is None or str(row[0]) == exclude_id:
                return candidate
            index += 1
            candidate = with_suffix_index(sanitize_name(name), index)

    # -- 写 -----------------------------------------------------------------

    def create_node(
        self,
        base_id: str,
        name: str,
        kind: str,
        parent_id: str = ROOT_PARENT,
        employee_id: str | None = None,
        storage_path: str = "",
        size: int = 0,
        url: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> KnowledgeNode:
        # 功能：新建节点——校验类型/父级，同级重名自动加 (1)，排序取同级最大值 +1。
        if kind not in NODE_KINDS:
            raise ValueError(f"unknown node kind: {kind}")
        parent = ROOT_PARENT
        if parent_id:
            parent_node = self.get_node(parent_id)
            if parent_node is None or parent_node.base_id != base_id:
                raise ValueError(f"parent not found: {parent_id}")
            if parent_node.kind != FOLDER_KIND:
                raise ValueError("parent must be a folder")
            parent = parent_id
        with self._lock:
            final_name = self.unique_name(base_id, parent, name)
            order_row = self._conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM knowledge_nodes "
                "WHERE base_id = ? AND parent_id = ?",
                (base_id, parent),
            ).fetchone()
            now = _now()
            node = KnowledgeNode(
                id=_new_id(),
                base_id=base_id,
                parent_id=parent,
                kind=kind,
                name=final_name,
                sort_order=int(order_row[0]) if order_row else 1,
                employee_id=employee_id,
                storage_path=storage_path,
                size=size,
                url=url,
                metadata=dict(metadata or {}),
                created_at=now,
                updated_at=now,
            )
            self._conn.execute(
                "INSERT INTO knowledge_nodes (id, base_id, parent_id, kind, name, sort_order,"
                " employee_id, storage_path, size, url, metadata, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node.id,
                    node.base_id,
                    node.parent_id,
                    node.kind,
                    node.name,
                    node.sort_order,
                    node.employee_id,
                    node.storage_path,
                    node.size,
                    node.url,
                    _json_dump(node.metadata),
                    node.created_at,
                    node.updated_at,
                ),
            )
            self._conn.commit()
        return node

    def set_storage(self, node_id: str, storage_path: str, size: int) -> KnowledgeNode | None:
        # 功能：记录文件落盘位置与字节数——上传完成后回填。
        with self._lock:
            self._conn.execute(
                "UPDATE knowledge_nodes SET storage_path = ?, size = ?, updated_at = ? WHERE id = ?",
                (storage_path, size, _now(), node_id),
            )
            self._conn.commit()
        return self.get_node(node_id)

    def rename_node(self, node_id: str, name: str) -> KnowledgeNode:
        # 功能：重命名——同级重名自动加 (1)；空名回落为「未命名」。
        node = self.get_node(node_id)
        if node is None:
            raise ValueError(f"node not found: {node_id}")
        with self._lock:
            final_name = self.unique_name(node.base_id, node.parent_id, name, exclude_id=node_id)
            self._conn.execute(
                "UPDATE knowledge_nodes SET name = ?, updated_at = ? WHERE id = ?",
                (final_name, _now(), node_id),
            )
            self._conn.commit()
        updated = self.get_node(node_id)
        assert updated is not None
        return updated

    def move_node(self, node_id: str, new_parent_id: str) -> KnowledgeNode:
        # 功能：移动节点——目标必须为同库文件夹，禁止移入自身或后代。
        node = self.get_node(node_id)
        if node is None:
            raise ValueError(f"node not found: {node_id}")
        parent = ROOT_PARENT
        if new_parent_id:
            target = self.get_node(new_parent_id)
            if target is None or target.base_id != node.base_id:
                raise ValueError(f"parent not found: {new_parent_id}")
            if target.kind != FOLDER_KIND:
                raise ValueError("parent must be a folder")
            if new_parent_id == node_id or new_parent_id in self.descendant_ids(node_id):
                raise ValueError("cannot move node into itself or its descendants")
            parent = new_parent_id
        with self._lock:
            final_name = self.unique_name(node.base_id, parent, node.name, exclude_id=node_id)
            order_row = self._conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM knowledge_nodes "
                "WHERE base_id = ? AND parent_id = ?",
                (node.base_id, parent),
            ).fetchone()
            self._conn.execute(
                "UPDATE knowledge_nodes SET parent_id = ?, name = ?, sort_order = ?, updated_at = ?"
                " WHERE id = ?",
                (parent, final_name, int(order_row[0]) if order_row else 1, _now(), node_id),
            )
            self._conn.commit()
        updated = self.get_node(node_id)
        assert updated is not None
        return updated

    def delete_node(self, node_id: str) -> list[str]:
        # 功能：硬删节点及其整棵子树，返回被删除的 id（含自身，先子后父）。
        node = self.get_node(node_id)
        if node is None:
            return []
        removed = self.descendant_ids(node_id) + [node_id]
        with self._lock:
            placeholders = ",".join("?" for _ in removed)
            self._conn.execute(f"DELETE FROM knowledge_nodes WHERE id IN ({placeholders})", removed)
            self._conn.commit()
        return removed

    def delete_views(self, node_ids: list[str]) -> int:
        # 功能：删除节点视图态（CSV 筛选/分组等）——节点删除时一并清理，避免孤儿行。
        if not node_ids:
            return 0
        placeholders = ",".join("?" for _ in node_ids)
        with self._lock:
            cursor = self._conn.execute(
                f"DELETE FROM knowledge_node_views WHERE node_id IN ({placeholders})", node_ids
            )
            self._conn.commit()
        return int(cursor.rowcount or 0)

    def set_employee(self, node_id: str, employee_id: str | None) -> int:
        # 功能：设置节点员工归属并级联整棵子树，返回受影响行数。
        node = self.get_node(node_id)
        if node is None:
            return 0
        affected = self.descendant_ids(node_id) + [node_id]
        with self._lock:
            placeholders = ",".join("?" for _ in affected)
            self._conn.execute(
                f"UPDATE knowledge_nodes SET employee_id = ?, updated_at = ?"
                f" WHERE id IN ({placeholders})",
                [employee_id, _now(), *affected],
            )
            if self._documents_table_ready():
                self._conn.execute(
                    f"UPDATE knowledge_documents SET node_employee_id = ?"
                    f" WHERE node_id IN ({placeholders})",
                    [employee_id, *affected],
                )
            self._conn.commit()
        return len(affected)

    def _documents_table_ready(self) -> bool:
        # 功能：判断文档表是否带 node_id 列——纯节点库（无文档表）时跳过级联更新。
        columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(knowledge_documents)").fetchall()
        }
        return "node_id" in columns

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_node(row: tuple[Any, ...]) -> KnowledgeNode:
        return KnowledgeNode(
            id=str(row[0]),
            base_id=str(row[1]),
            parent_id=str(row[2] or ""),
            kind=str(row[3]),
            name=str(row[4]),
            sort_order=int(row[5] or 0),
            employee_id=(str(row[6]) if row[6] else None),
            storage_path=str(row[7] or ""),
            size=int(row[8] or 0),
            url=str(row[9] or ""),
            metadata=_json_load(row[10]),
            created_at=float(row[11] or 0.0),
            updated_at=float(row[12] or 0.0),
        )


def _json_dump(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _json_load(raw: Any) -> dict[str, Any]:
    import json

    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
