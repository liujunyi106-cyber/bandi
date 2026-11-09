# 功能：存量知识文档迁移到资料库节点树——可预演、幂等、校验与回滚。

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from bandi.knowledge.nodes import KnowledgeNodeStore
from bandi.knowledge.storage import NodeFileStore
from bandi.resources.knowledge_sources import _safe_base_id, docs_root
from bandi.utils.db import connect_app_db

_EXT_KIND = {
    "md": "md",
    "markdown": "md",
    "txt": "md",
    "csv": "csv",
    "docx": "docx",
    "doc": "docx",
    "xlsx": "xlsx",
    "xls": "xlsx",
    "pptx": "pptx",
    "ppt": "pptx",
    "pdf": "pdf",
    "html": "html",
    "htm": "html",
}


def _kind_of(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXT_KIND.get(ext, "md")


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    return any(str(row[1]) == column for row in rows)


def _load_rows(db_path: Path) -> list[dict[str, Any]]:
    # 功能：只读读取库与文档；缺少 node_id 列时按 NULL 处理。
    conn = connect_app_db(db_path, timeout=5, row_factory=True)
    try:
        with_node = _has_column(conn, "knowledge_documents", "node_id")
        node_expr = "d.node_id" if with_node else "NULL"
        employee_expr = (
            "d.node_employee_id"
            if _has_column(conn, "knowledge_documents", "node_employee_id")
            else "NULL"
        )
        query = (
            "SELECT d.id, d.base_id, d.filename, d.title, "
            f"{node_expr} AS node_id, {employee_expr} AS node_employee_id, "
            "b.name AS base_name, b.employee_id AS base_employee_id "
            "FROM knowledge_documents d LEFT JOIN knowledge_bases b ON b.id = d.base_id "
            "ORDER BY d.created_at ASC, d.id ASC"
        )
        return [dict(row) for row in conn.execute(query).fetchall()]
    finally:
        conn.close()


def _source_path(source_root: Path, base_id: str, filename: str) -> Path:
    # 功能：旧布局源文件路径（<docs-root>/<base slug>/<文件名>）。
    return source_root / _safe_base_id(base_id) / Path(str(filename or "")).name


def _root_folder(nodes: KnowledgeNodeStore, base_id: str, base_name: str) -> str:
    # 功能：每个库一个根文件夹；已存在（metadata 标记）则复用，保证幂等。
    for node in nodes.list_children(base_id, ""):
        if node.kind == "folder" and node.metadata.get("migrated_base_id") == base_id:
            return node.id
    folder = nodes.create_node(
        base_id,
        base_name or base_id,
        "folder",
        "",
        metadata={"migrated_base_id": base_id},
    )
    return folder.id


def _ensure_document_columns(conn: sqlite3.Connection) -> None:
    # 功能：旧库补 node_id / node_employee_id 列，保证迁移可写回。
    if not _has_column(conn, "knowledge_documents", "node_id"):
        conn.execute("ALTER TABLE knowledge_documents ADD COLUMN node_id TEXT DEFAULT NULL")
    if not _has_column(conn, "knowledge_documents", "node_employee_id"):
        conn.execute(
            "ALTER TABLE knowledge_documents ADD COLUMN node_employee_id TEXT DEFAULT NULL"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_node ON knowledge_documents(node_id)")
    conn.commit()


def migrate(
    db_path: Path,
    files_root: Path,
    source_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    # 功能：执行迁移并返回报告；dry_run 只统计不写库。
    documents = _load_rows(db_path)
    pending = [row for row in documents if not row.get("node_id")]
    report: dict[str, Any] = {
        "documents": len(documents),
        "pending": len(pending),
        "bases": sorted({str(row["base_id"]) for row in pending}),
        "folders": 0,
        "nodes": 0,
        "copied": 0,
        "missing_files": 0,
        "dry_run": dry_run,
    }
    if dry_run:
        return report

    backup = db_path.with_name(f"{db_path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(db_path, backup)
    report["backup"] = str(backup)

    nodes = KnowledgeNodeStore(db_path)
    files = NodeFileStore(files_root)
    conn = connect_app_db(db_path)
    _ensure_document_columns(conn)
    folders: set[str] = set()
    try:
        for row in pending:
            base_id = str(row["base_id"])
            base_name = str(row.get("base_name") or base_id)
            parent_id = _root_folder(nodes, base_id, base_name)
            folders.add(base_id)
            filename = str(row["filename"] or "未命名.md")
            node = nodes.create_node(
                base_id,
                filename,
                _kind_of(filename),
                parent_id,
                employee_id=row.get("base_employee_id"),
                metadata={"migrated_document_id": str(row["id"])},
            )
            source = _source_path(source_root, base_id, filename)
            if source.is_file():
                data = source.read_bytes()
                report["copied"] += 1
            else:
                data = b""
                report["missing_files"] += 1
            files.write(node.id, node.name, data)
            nodes.set_storage(
                node.id,
                f"{node.id}/{node.name}",
                len(data),
            )
            conn.execute(
                "UPDATE knowledge_documents SET node_id = ?, node_employee_id = ?, "
                "updated_at = ? WHERE id = ?",
                (node.id, row.get("base_employee_id"), time.time(), str(row["id"])),
            )
            conn.commit()
            report["nodes"] += 1
    finally:
        conn.close()
        nodes.close()

    report["folders"] = len(folders)
    remaining = _load_rows(db_path)
    report["unmigrated"] = sum(1 for row in remaining if not row.get("node_id"))
    report["consistent"] = report["unmigrated"] == 0
    return report


def rollback(db_path: Path, files_root: Path) -> dict[str, Any]:
    # 功能：回滚迁移——删除带迁移标记的节点/视图/目录，并清空文档节点列。
    nodes = KnowledgeNodeStore(db_path)
    conn = connect_app_db(db_path)
    removed: list[str] = []
    try:
        rows = conn.execute("SELECT id, metadata FROM knowledge_nodes").fetchall()
        migrated = [
            str(row[0])
            for row in rows
            if "migrated_document_id" in str(row[1] or "")
            or "migrated_base_id" in str(row[1] or "")
        ]
        for node_id in migrated:
            conn.execute("DELETE FROM knowledge_node_views WHERE node_id = ?", (node_id,))
            conn.execute("DELETE FROM knowledge_nodes WHERE id = ?", (node_id,))
            removed.append(node_id)
        placeholders = ",".join("?" for _ in removed) or "''"
        conn.execute(
            f"UPDATE knowledge_documents SET node_id = NULL, node_employee_id = NULL "
            f"WHERE node_id IN ({placeholders})",
            removed,
        )
        conn.commit()
    finally:
        conn.close()
        nodes.close()
    files = NodeFileStore(files_root)
    for node_id in removed:
        files.delete(node_id)
    return {"removed_nodes": len(removed)}


def _default_paths() -> tuple[Path, Path, Path]:
    home = Path.home() / ".bandi"
    return (
        home / "data" / "knowledge.db",
        home / "knowledge" / "files",
        docs_root(),
    )


def main(argv: list[str] | None = None) -> int:
    default_db, default_files, default_docs = _default_paths()
    parser = argparse.ArgumentParser(description="迁移知识文档到资料库节点树")
    parser.add_argument("--db", type=Path, default=default_db)
    parser.add_argument("--files-root", type=Path, default=default_files)
    parser.add_argument("--docs-root", type=Path, default=default_docs)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args(argv)
    if not args.db.exists():
        print(json.dumps({"error": f"database not found: {args.db}"}, ensure_ascii=False))
        return 2
    if args.rollback:
        result = rollback(args.db, args.files_root)
    else:
        result = migrate(
            args.db,
            args.files_root,
            args.docs_root,
            dry_run=args.dry_run,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())
