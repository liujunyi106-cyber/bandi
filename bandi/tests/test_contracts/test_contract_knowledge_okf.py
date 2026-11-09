# 功能：契约——OKF 概念层（KnowledgeConcept 持久化 + 概念构建 + 检索 + 概念卡/引用）。
# 钉行为不钉实现：文档→概念构建（入口概念+每桶概念）/ upsert 幂等 / 中文检索评分
# （标题命中 > 正文命中 / 阈值下不返回）/ 概念卡剥 frontmatter / markdown 往返等值 /
# 破坏者形态（concept_id 路径穿越归一化、空 query、空概念集）。
# 生产路径族：KnowledgeStore.ingest_document→build_okf_for_document→upsert_concepts；
# search_concepts / selected_concept_cards / okf_citations_for_concepts。

from __future__ import annotations

from typing import Any

import pytest

from bandi.knowledge.okf import (
    build_okf_for_document,
    normalize_concept_id,
    okf_citations_for_concepts,
    parse_okf_markdown,
    render_okf_markdown,
    safe_path_segment,
    search_concepts,
    selected_concept_cards,
)
from bandi.knowledge.schema import KnowledgeBucket, KnowledgeDocument
from bandi.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path: Any) -> KnowledgeStore:
    return KnowledgeStore(db_path=tmp_path / "knowledge.db")


def _doc_and_buckets() -> tuple[KnowledgeDocument, list[KnowledgeBucket]]:
    doc = KnowledgeDocument(id="kdoc:1", base_id="kb1", filename="sop.md", title="部署 SOP")
    buckets = [
        KnowledgeBucket(
            id="kbucket:1",
            base_id="kb1",
            document_id="kdoc:1",
            bucket_key="部署流程",
            title="部署流程",
            summary="部署前必须运行 verify 脚本确认六项全过。",
        ),
        KnowledgeBucket(
            id="kbucket:2",
            base_id="kb1",
            document_id="kdoc:1",
            bucket_key="回滚预案",
            title="回滚预案",
            summary="回滚时先停写入再切流量。",
        ),
    ]
    return doc, buckets


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestBuildConcepts:
    def test_build_yields_document_and_bucket_concepts(self) -> None:
        """契约：build_okf_for_document 产出 1 个 Source Document
        入口概念 + 每桶 1 个概念；concept_id 唯一且以文档 slug 为前缀。"""
        doc, buckets = _doc_and_buckets()
        concepts = build_okf_for_document(doc, buckets)
        ids = [c["concept_id"] for c in concepts]
        assert len(concepts) == 3
        assert len(set(ids)) == 3
        entry = concepts[0]
        assert entry["concept_type"] == "Source Document"
        assert all(cid.startswith(entry["concept_id"]) for cid in ids[1:])

    def test_markdown_roundtrip(self) -> None:
        """契约：render_okf_markdown → parse_okf_markdown 往返
        frontmatter 键值与正文逐字保留（无静默改写）。"""
        body = "# Summary\n\n部署前运行 verify。\n\n# Citations\n\n[1] [原文](ultrarag://kdoc:1)\n"
        md = render_okf_markdown({"type": "Topic", "title": "部署"}, body)
        parsed = parse_okf_markdown("sources/deploy", md)
        assert parsed.frontmatter["type"] == "Topic"
        assert parsed.frontmatter["title"] == "部署"
        assert "部署前运行 verify。" in parsed.body
        assert "[1] [原文](ultrarag://kdoc:1)" in parsed.body

    def test_same_heading_sections_concept_ids_unique(self, store: KnowledgeStore) -> None:
        """契约（同族，破坏者）：单文档内**同标题双节**
        （如两个 '# 注意事项'）→ 每节各得 1 个 Topic 概念，concept_id 互不
        撞车；upsert 落库后概念数 = 1 入口 + 2 桶（不丢概念、不互相覆盖）。
        生产路径族：ingest_document → build_okf_for_document → upsert_concepts
        → list_concepts（DoD-10 全链最终产物断言）。"""
        store.ingest_document(
            base_id="kb1",
            filename="sop.md",
            text="# 注意事项\n禁止裸奔。\n\n# 注意事项\n先备份。\n",
        )
        doc = store.list_documents("kb1")[0]
        buckets = store.list_buckets(doc.id)
        assert len(buckets) == 2  # 两个同标题节必须成两桶
        concepts = build_okf_for_document(doc, buckets)
        ids = [c["concept_id"] for c in concepts]
        assert len(ids) == len(set(ids)), f"同标题节 concept_id 撞车: {ids}"
        store.upsert_concepts("kb1", concepts)
        rows = store.list_concepts("kb1")
        assert len(rows) == 3, "概念数 = 入口 1 + 每桶 1×2，不得被覆盖吞并为 2"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestUpsertConcepts:
    def test_upsert_idempotent_update(self, store: KnowledgeStore) -> None:
        """契约：同 (base_id, concept_id) 重复 upsert → 单行且内容
        更新为最新（content_md 权威——frontmatter 随正文走；不重复不残留）。"""
        doc, buckets = _doc_and_buckets()
        concepts = build_okf_for_document(doc, buckets)
        store.upsert_concepts("kb1", concepts)
        concepts[1]["content_md"] = concepts[1]["content_md"].replace(
            "title: 部署流程", "title: 部署流程（修订）"
        )
        store.upsert_concepts("kb1", concepts)
        rows = store.list_concepts("kb1")
        assert len(rows) == 3
        titles = {r.title for r in rows}
        assert "部署流程（修订）" in titles

    def test_delete_document_cascades_concepts(self, store: KnowledgeStore) -> None:
        """契约：删文档 → 其概念整组清除（不留幽灵概念）。"""
        store.ingest_document(
            base_id="kb1", filename="sop.md", text="# 部署\n部署前运行 verify。\n"
        )
        doc = store.list_documents("kb1")[0]
        buckets = store.list_buckets(doc.id)
        store.upsert_concepts("kb1", build_okf_for_document(doc, buckets))
        assert store.list_concepts("kb1")
        store.delete_document(doc.id)
        assert store.list_concepts("kb1") == []

    def test_same_title_documents_concepts_isolated(self, store: KnowledgeStore) -> None:
        """契约：同 base 同 title 双文档 → concept_id
        互不撞车；各自归属性 document_id 正确；删一个文档不伤另一个的概念
        （破坏者：同 title 撞车即文档概念互相覆盖，删除后泄漏）。"""
        doc1 = KnowledgeDocument(id="kdoc:1", base_id="kb1", filename="a.md", title="部署手册")
        doc2 = KnowledgeDocument(id="kdoc:2", base_id="kb1", filename="b.md", title="部署手册")
        b1 = [
            KnowledgeBucket(
                id="kbucket:1",
                base_id="kb1",
                document_id="kdoc:1",
                bucket_key="安装",
                title="安装",
                summary="手动安装步骤。",
            )
        ]
        b2 = [
            KnowledgeBucket(
                id="kbucket:2",
                base_id="kb1",
                document_id="kdoc:2",
                bucket_key="升级",
                title="升级",
                summary="升级注意事项。",
            )
        ]
        concepts1 = build_okf_for_document(doc1, b1)
        concepts2 = build_okf_for_document(doc2, b2)
        ids1 = {c["concept_id"] for c in concepts1}
        ids2 = {c["concept_id"] for c in concepts2}
        assert ids1.isdisjoint(ids2), "同 title 双文档 concept_id 必须隔离"
        store.upsert_concepts("kb1", concepts1)
        store.upsert_concepts("kb1", concepts2)
        assert len(store.list_concepts("kb1")) == 4  # 每文档 1 入口 + 1 桶
        assert {r.document_id for r in store.list_concepts("kb1")} == {"kdoc:1", "kdoc:2"}
        store.delete_document("kdoc:1")
        remaining = store.list_concepts("kb1")
        assert remaining and {r.document_id for r in remaining} == {"kdoc:2"}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestSearchConcepts:
    def test_cjk_search_title_beats_body_and_threshold(self, store: KnowledgeStore) -> None:
        """契约：中文查询命中——标题命中得分高于正文命中；
        得分低于阈值的概念不返回；空 query 返回空。"""
        store.ingest_document(
            base_id="kb1",
            filename="sop.md",
            text="# 部署流程\n部署前必须运行 verify 脚本。\n\n# 其他\n无关内容段落。\n",
        )
        doc = store.list_documents("kb1")[0]
        store.upsert_concepts("kb1", build_okf_for_document(doc, store.list_buckets(doc.id)))
        rows = store.list_concepts("kb1")
        hits = search_concepts("部署流程", rows)
        assert hits, "标题精确命中必须返回"
        assert hits[0].title == "部署流程"
        assert search_concepts("", rows) == []
        assert search_concepts("完全不存在词", rows) == []


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestConceptCards:
    def test_cards_strip_frontmatter_and_cite(self, store: KnowledgeStore) -> None:
        """契约：selected_concept_cards 内容剥 frontmatter；
        okf_citations_for_concepts 从概念 markdown 引用节构建且 (concept,target) 去重。"""
        doc, buckets = _doc_and_buckets()
        store.upsert_concepts("kb1", build_okf_for_document(doc, buckets))
        rows = store.list_concepts("kb1")
        cards = selected_concept_cards(rows)
        assert len(cards) == 3
        assert all(
            "frontmatter" not in c["content"] and "---" not in c["content"][:4] for c in cards
        )
        entry = next(c for c in cards if c["type"] == "Source Document")
        assert entry["concept_id"].startswith("sources/")
        citations = okf_citations_for_concepts(rows)
        assert citations
        keys = {(c["concept_id"], c["target"]) for c in citations}
        assert len(keys) == len(citations)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestAdversarial:
    def test_concept_id_normalization_blocks_traversal(self) -> None:
        """契约：concept_id 路径穿越/反斜杠/.md 后缀/连续斜杠
        全部归一化为安全相对路径（无 ..、无绝对路径）。"""
        for raw in ("../../etc/passwd", "a\\..\\..\\b", "/abs/path.md", "a//b///c.md"):
            normalized = normalize_concept_id(raw)
            assert ".." not in normalized
            assert not normalized.startswith("/")
            assert not normalized.endswith(".md")

    def test_safe_segment_keeps_cjk(self) -> None:
        """契约：safe_path_segment 保留中文（知识库中文标题
        一等公民），剥离危险字符，空值回退 fallback。"""
        assert safe_path_segment("部署流程") == "部署流程"
        assert safe_path_segment("a/b\\c") == "a-b-c"
        assert safe_path_segment("") == "concept"
