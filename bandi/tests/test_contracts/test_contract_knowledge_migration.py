# 功能：存量知识文档迁移契约——预演/幂等/回滚/缺失源文件。

from __future__ import annotations

import sqlite3
from pathlib import Path

from bandi.knowledge.migrate_nodes import migrate, rollback
from bandi.knowledge.nodes import KnowledgeNodeStore
from bandi.knowledge.storage import NodeFileStore
from bandi.knowledge.store import KnowledgeStore
from bandi.resources.knowledge_sources import _safe_base_id


def _seed(tmp_path: Path) -> tuple[Path, Path, Path, list[str]]:
    db = tmp_path / "knowledge.db"
    files = tmp_path / "files"
    docs = tmp_path / "docs"
    store = KnowledgeStore(db)
    store.ensure_base("shared", name="共享库")
    store.ensure_base("kb:emp", name="员工库", employee_id="emp-1")
    first = store.ingest_document("shared", "甲.md", "# 甲\n\n内容一")
    second = store.ingest_document("kb:emp", "乙.md", "# 乙\n\n内容二")
    store.close()
    for base_id, filename, body in (
        ("shared", "甲.md", "# 甲\n\n内容一"),
        ("kb:emp", "乙.md", "# 乙\n\n内容二"),
    ):
        target = docs / _safe_base_id(base_id) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="")
    return db, files, docs, [first.document_id, second.document_id]


def test_dry_run_reports_without_writing(tmp_path):
    db, files, docs, _ = _seed(tmp_path)
    report = migrate(db, files, docs, dry_run=True)
    assert report["pending"] == 2
    assert report["nodes"] == 0
    assert report["dry_run"] is True
    conn = sqlite3.connect(db)
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_nodes'"
        ).fetchone()
    finally:
        conn.close()
    assert exists is None


def test_migrate_creates_folders_nodes_and_copies_files(tmp_path):
    db, files, docs, document_ids = _seed(tmp_path)
    report = migrate(db, files, docs)
    assert report["nodes"] == 2
    assert report["folders"] == 2
    assert report["copied"] == 2
    assert report["consistent"] is True

    nodes = KnowledgeNodeStore(db)
    try:
        roots = nodes.list_children("shared", "")
        assert [node.kind for node in roots] == ["folder"]
        children = nodes.list_children("shared", roots[0].id)
        assert [node.name for node in children] == ["甲.md"]
        assert children[0].employee_id is None
        emp_roots = nodes.list_children("kb:emp", "")
        emp_children = nodes.list_children("kb:emp", emp_roots[0].id)
        assert emp_children[0].employee_id == "emp-1"
        stored = NodeFileStore(files)
        assert stored.read(children[0].id, "甲.md") == "# 甲\n\n内容一".encode()
    finally:
        nodes.close()

    conn = sqlite3.connect(db)
    try:
        for document_id in document_ids:
            row = conn.execute(
                "SELECT node_id FROM knowledge_documents WHERE id = ?", (document_id,)
            ).fetchone()
            assert row is not None and row[0]
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path):
    db, files, docs, _ = _seed(tmp_path)
    first = migrate(db, files, docs)
    nodes = KnowledgeNodeStore(db)
    try:
        before = len(nodes.list_tree("shared")) + len(nodes.list_tree("kb:emp"))
    finally:
        nodes.close()
    second = migrate(db, files, docs)
    assert second["pending"] == 0
    assert second["nodes"] == 0
    nodes = KnowledgeNodeStore(db)
    try:
        after = len(nodes.list_tree("shared")) + len(nodes.list_tree("kb:emp"))
    finally:
        nodes.close()
    assert before == after == first["nodes"] + first["folders"]


def test_migrate_missing_source_creates_empty_node(tmp_path):
    db, files, docs, _ = _seed(tmp_path)
    (docs / "shared" / "甲.md").unlink()
    report = migrate(db, files, docs)
    assert report["missing_files"] == 1
    nodes = KnowledgeNodeStore(db)
    try:
        root = nodes.list_children("shared", "")[0]
        node = nodes.list_children("shared", root.id)[0]
    finally:
        nodes.close()
    assert NodeFileStore(files).read(node.id, node.name) == b""


def test_rollback_clears_nodes_and_document_links(tmp_path):
    db, files, docs, document_ids = _seed(tmp_path)
    migrate(db, files, docs)
    result = rollback(db, files)
    assert result["removed_nodes"] == 4
    nodes = KnowledgeNodeStore(db)
    try:
        assert nodes.list_tree("shared") == []
        assert nodes.list_tree("kb:emp") == []
    finally:
        nodes.close()
    conn = sqlite3.connect(db)
    try:
        for document_id in document_ids:
            row = conn.execute(
                "SELECT node_id FROM knowledge_documents WHERE id = ?", (document_id,)
            ).fetchone()
            assert row is not None and row[0] is None
    finally:
        conn.close()
    assert not any(files.rglob("*"))
