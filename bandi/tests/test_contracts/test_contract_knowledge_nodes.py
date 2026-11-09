# 功能：资料库节点树契约——层级/重名/防环/级联删除/员工归属/命名清洗。

from __future__ import annotations

import sqlite3

import pytest

from bandi.knowledge.nodes import (
    ROOT_PARENT,
    KnowledgeNodeStore,
    sanitize_name,
    with_suffix_index,
)
from bandi.knowledge.store import KnowledgeStore


@pytest.fixture()
def nodes(tmp_path) -> KnowledgeNodeStore:
    store = KnowledgeNodeStore(tmp_path / "knowledge.db")
    yield store
    store.close()


def test_create_and_list_children_puts_folders_first(nodes):
    folder = nodes.create_node("b1", "制度", "folder")
    nodes.create_node("b1", "a.md", "md")
    nodes.create_node("b1", "b.md", "md")
    children = nodes.list_children("b1")
    assert [c.kind for c in children][0] == "folder"
    assert children[0].id == folder.id
    assert {c.name for c in children} == {"制度", "a.md", "b.md"}


def test_duplicate_name_gets_suffix_index(nodes):
    first = nodes.create_node("b1", "报告.md", "md")
    second = nodes.create_node("b1", "报告.md", "md")
    third = nodes.create_node("b1", "报告.md", "md")
    assert first.name == "报告.md"
    assert second.name == "报告 (1).md"
    assert third.name == "报告 (2).md"


def test_rename_collision_gets_suffix_and_empty_falls_back(nodes):
    a = nodes.create_node("b1", "a.md", "md")
    nodes.create_node("b1", "b.md", "md")
    renamed = nodes.rename_node(a.id, "b.md")
    assert renamed.name == "b (1).md"
    blank = nodes.rename_node(a.id, "   ")
    assert blank.name == "未命名"


def test_move_rejects_self_and_descendant(nodes):
    root = nodes.create_node("b1", "root", "folder")
    child = nodes.create_node("b1", "child", "folder", parent_id=root.id)
    grand = nodes.create_node("b1", "grand", "folder", parent_id=child.id)
    with pytest.raises(ValueError):
        nodes.move_node(root.id, root.id)
    with pytest.raises(ValueError):
        nodes.move_node(root.id, grand.id)
    moved = nodes.move_node(grand.id, ROOT_PARENT)
    assert moved.parent_id == ROOT_PARENT


def test_move_name_collision_gets_suffix(nodes):
    left = nodes.create_node("b1", "left", "folder")
    right = nodes.create_node("b1", "right", "folder")
    nodes.create_node("b1", "same", "folder", parent_id=left.id)
    doc = nodes.create_node("b1", "same", "folder", parent_id=right.id)
    moved = nodes.move_node(doc.id, left.id)
    assert moved.name == "same (1)"


def test_delete_cascades_whole_subtree(nodes):
    root = nodes.create_node("b1", "root", "folder")
    child = nodes.create_node("b1", "child", "folder", parent_id=root.id)
    leaf = nodes.create_node("b1", "leaf.md", "md", parent_id=child.id)
    removed = nodes.delete_node(root.id)
    assert set(removed) == {root.id, child.id, leaf.id}
    assert nodes.get_node(leaf.id) is None
    assert nodes.list_children("b1") == []


def test_set_employee_cascades_subtree(nodes):
    root = nodes.create_node("b1", "root", "folder")
    child = nodes.create_node("b1", "child", "folder", parent_id=root.id)
    leaf = nodes.create_node("b1", "leaf.md", "md", parent_id=child.id)
    affected = nodes.set_employee(root.id, "emp-1")
    assert affected == 3
    assert nodes.get_node(root.id).employee_id == "emp-1"
    assert nodes.get_node(leaf.id).employee_id == "emp-1"
    nodes.set_employee(child.id, None)
    assert nodes.get_node(child.id).employee_id is None
    assert nodes.get_node(root.id).employee_id == "emp-1"


def test_create_rejects_unknown_kind_and_foreign_parent(nodes):
    with pytest.raises(ValueError):
        nodes.create_node("b1", "x.exe", "exe")
    doc = nodes.create_node("b1", "a.md", "md")
    with pytest.raises(ValueError):
        nodes.create_node("b1", "b.md", "md", parent_id=doc.id)
    other = nodes.create_node("b2", "other", "folder")
    with pytest.raises(ValueError):
        nodes.create_node("b1", "c.md", "md", parent_id=other.id)


def test_sanitize_name_handles_reserved_and_invalid_chars():
    assert sanitize_name("CON") == "_CON"
    assert sanitize_name('a/b:c*?"<>|.md') == "a_b_c______.md"
    assert sanitize_name("   ") == "未命名"
    assert sanitize_name("名字" + "x" * 200 + ".md").endswith(".md")
    assert with_suffix_index("报告.md", 2) == "报告 (2).md"
    assert with_suffix_index("目录", 1) == "目录 (1)"


def test_document_employee_column_is_added_once(tmp_path):
    db = tmp_path / "shared.db"
    legacy = KnowledgeStore(db)
    legacy.ensure_base("b1", "库")
    legacy.close()
    first = KnowledgeNodeStore(db)
    first.close()
    second = KnowledgeNodeStore(db)
    second.close()
    conn = sqlite3.connect(db)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(knowledge_documents)")]
        assert columns.count("node_employee_id") == 1
    finally:
        conn.close()
