# 功能：知识库可见性契约——角色∩绑定交集、员工归属过滤（catalog/search/read）、节点级联。

from __future__ import annotations

import pytest

from bandi.knowledge.nodes import KnowledgeNodeStore
from bandi.knowledge.scopes import (
    intersect_knowledge_bases,
    reset_runtime_knowledge_bases,
    reset_runtime_knowledge_employee,
    set_runtime_knowledge_bases,
    set_runtime_knowledge_employee,
)
from bandi.knowledge.store import KnowledgeStore
from bandi.tools.doc.knowledge_search import KnowledgeSearchTool


class _EmptyMemory:
    def retrieve(self, *_args, **_kwargs):
        return []

    def search_fts(self, *_args, **_kwargs):
        return []

    def get(self, *_args, **_kwargs):
        return None


@pytest.fixture()
def store(tmp_path) -> KnowledgeStore:
    return KnowledgeStore(tmp_path / "k.db")


def test_intersection_rules():
    assert intersect_knowledge_bases(None, None) is None
    assert intersect_knowledge_bases([], ["a"]) == []
    assert intersect_knowledge_bases(None, ["a", "b"]) == ["a", "b"]
    assert intersect_knowledge_bases(["a"], None) == ["a"]
    assert intersect_knowledge_bases(["a", "b"], ["b", "c"]) == ["b"]
    assert intersect_knowledge_bases(["a"], ["b"]) == []


def _tool(store: KnowledgeStore) -> KnowledgeSearchTool:
    return KnowledgeSearchTool(memory=_EmptyMemory(), knowledge_store=store)


def _run(tool: KnowledgeSearchTool, payload: dict):
    import asyncio

    return asyncio.run(tool.execute(payload))


def test_catalog_hides_other_employees_documents(store):
    shared = store.ingest_document("b1", "共享.md", "共同规则")
    private = store.ingest_document("b1", "私有.md", "私有规则")
    store.set_document_employee(private.document_id, "emp-1")
    tool = _tool(store)

    base_token = set_runtime_knowledge_bases(["b1"])
    own_token = set_runtime_knowledge_employee("emp-1")
    try:
        own = _run(tool, {"action": "catalog"})
        assert {item["filename"] for item in own["documents"]} == {"共享.md", "私有.md"}
    finally:
        reset_runtime_knowledge_employee(own_token)
        reset_runtime_knowledge_bases(base_token)

    base_token = set_runtime_knowledge_bases(["b1"])
    other_token = set_runtime_knowledge_employee("emp-2")
    try:
        other = _run(tool, {"action": "catalog"})
        assert [item["filename"] for item in other["documents"]] == ["共享.md"]
    finally:
        reset_runtime_knowledge_employee(other_token)
        reset_runtime_knowledge_bases(base_token)
    assert shared.document_id != private.document_id


def test_search_and_read_respect_employee_scope(store):
    store.ingest_document("b1", "共享.md", "共同规则")
    private = store.ingest_document("b1", "私有.md", "私有规则")
    store.set_document_employee(private.document_id, "emp-1")
    tool = _tool(store)
    private_chunk = store.list_chunks(private.document_id)[0]

    base_token = set_runtime_knowledge_bases(["b1"])
    other_token = set_runtime_knowledge_employee("emp-2")
    try:
        found = _run(tool, {"action": "search", "query": "规则"})
        document_ids = {item.get("document_id") for item in found["results"]}
        assert private.document_id not in document_ids
        denied = _run(tool, {"action": "read", "item_id": private_chunk.id})
        assert denied["success"] is False
    finally:
        reset_runtime_knowledge_employee(other_token)
        reset_runtime_knowledge_bases(base_token)


def test_node_employee_cascades_to_documents(tmp_path):
    nodes = KnowledgeNodeStore(tmp_path / "shared.db")
    documents = KnowledgeStore(tmp_path / "shared.db")
    try:
        folder = nodes.create_node("b1", "制度", "folder")
        node = nodes.create_node("b1", "考勤.md", "md", folder.id)
        ingested = documents.ingest_document("b1", "考勤.md", "考勤规则")
        documents.set_document_node(ingested.document_id, node.id)

        nodes.set_employee(folder.id, "emp-9")
        document = documents.get_document(ingested.document_id)
        assert document is not None
        assert document.node_employee_id == "emp-9"

        nodes.set_employee(folder.id, None)
        cleared = documents.get_document(ingested.document_id)
        assert cleared is not None
        assert cleared.node_employee_id is None
    finally:
        nodes.close()
        documents.close()
