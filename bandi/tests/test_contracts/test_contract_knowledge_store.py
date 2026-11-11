# 功能：契约——KnowledgeStore 员工知识库三层实体（base/document/bucket/chunk）
# 钉行为不钉实现：摄取三层投影可读回 / FTS5 中文检索 + scope 隔离 / 级联删除 /
# 重摄取幂等无孤儿 / citations 重编号去重 / 破坏者形态（特殊字符/空文本/超大段落）。
# 生产路径族：KnowledgeStore.ingest_document / search / delete_document /
# citations.knowledge_citations_from_results / compact_citation_labels。
# 后端消费者（全部装配点）：tools/doc/knowledge_search.py（后续日接线）、company seat 上下文注入。

from __future__ import annotations

from typing import Any

import pytest

from bandi.knowledge.citations import (
    compact_citation_labels,
    knowledge_citations_from_results,
)
from bandi.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path: Any) -> KnowledgeStore:
    return KnowledgeStore(db_path=tmp_path / "knowledge.db")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestBaseLookup:
    def test_has_base_reflects_ensure_and_missing(self, store: KnowledgeStore) -> None:
        """契约：has_base 反映知识库容器存在性（唯一真源 = ensure_base 落库）。"""
        assert store.has_base("kb-x") is False
        store.ensure_base("kb-x", name="产品手册")
        assert store.has_base("kb-x") is True
        assert store.has_base("kb-other") is False


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestIngestProjection:
    def test_three_layer_counts_and_readback(self, store: KnowledgeStore) -> None:
        """契约：ingest_document 后 document/bucket/chunk 三层齐全
        文档计数（bucket_count/chunk_count）与实际行数一致；chunk 按
        document_id+bucket_id 可读回（最终产物断言，非实现存在断言）。"""
        text = (
            "# 安装指南\n"
            "运行 pip install bandi 完成安装。安装前需要 Python 3.11 以上版本。\n"
            "\n"
            "# 使用指南\n"
            "通过命令行入口 bandi chat 启动对话。对话中可以使用斜杠命令。\n"
        )
        result = store.ingest_document(base_id="kb1", filename="manual.md", text=text)
        doc = store.get_document(result.document_id)
        assert doc is not None
        assert doc.filename == "manual.md"
        assert doc.bucket_count == len(store.list_buckets(doc.id))
        assert doc.chunk_count == len(store.list_chunks(document_id=doc.id))
        assert doc.bucket_count >= 2  # 两个标题节 → 至少两个桶
        assert doc.chunk_count >= 2
        # 每个 chunk 必须挂在一个真实 bucket 上（引用完整性）
        bucket_ids = {b.id for b in store.list_buckets(doc.id)}
        for chunk in store.list_chunks(document_id=doc.id):
            assert chunk.bucket_id in bucket_ids
            assert chunk.content.strip()

    def test_empty_text_ingest_rejected(self, store: KnowledgeStore) -> None:
        """契约：空白文本摄取显式拒绝（ValueError），不留半截行。"""
        with pytest.raises(ValueError):
            store.ingest_document(base_id="kb1", filename="empty.md", text="   \n  ")
        assert store.list_documents("kb1") == []


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestSearchScope:
    def test_cjk_search_hits_chunk(self, store: KnowledgeStore) -> None:
        """契约：中文子串查询命中对应 chunk（2-gram FTS）
        命中结果带 document_id/bucket_id 定位链（引用链前置条件）。"""
        store.ingest_document(
            base_id="kb1",
            filename="policy.md",
            text="# 报销政策\n差旅报销需要在出差结束后七个工作日内提交发票。\n",
        )
        hits = store.search_chunks("报销 发票", base_id="kb1")
        assert hits, "中文查询必须命中（2-gram 索引）"
        top = hits[0]
        assert top.document_id and top.bucket_id
        assert "发票" in top.content

    def test_base_isolation(self, store: KnowledgeStore) -> None:
        """契约：base 之间检索互不串——kb_A 的内容不进 kb_B 的
        命中集（破坏者视角：跨库泄漏即隔离破防）。"""
        store.ingest_document(
            base_id="kb_a", filename="a.md", text="# A 机密\nAlpha 项目的私有参数是 42。\n"
        )
        store.ingest_document(
            base_id="kb_b", filename="b.md", text="# B 手册\nBeta 团队的公开手册。\n"
        )
        assert all("Alpha" not in c.content for c in store.search_chunks("Alpha", base_id="kb_b"))
        assert store.search_chunks("Alpha", base_id="kb_a")
        assert store.search_chunks("Beta", base_id="kb_b")

    def test_special_chars_query_no_crash(self, store: KnowledgeStore) -> None:
        """契约（破坏者）：FTS 特殊字符查询（引号/运算符/纯标点）
        不得抛异常——空命中或 LIKE 兜底，绝不炸检索链。"""
        store.ingest_document(base_id="kb1", filename="x.md", text="# 内容\n普通段落文字。\n")
        for q in ('" OR ', "NEAR(", "***", "％％％", "a" * 500):
            hits = store.search_chunks(q, base_id="kb1")  # 不抛即过
            assert isinstance(hits, list)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestCascadeAndIdempotency:
    def test_delete_document_cascades(self, store: KnowledgeStore) -> None:
        """契约：删文档 → 其 bucket/chunk（含 FTS 行）全清；
        检索不再命中已删内容（防幽灵索引——episodic FTS 幽灵教训 FM 同族）。"""
        result = store.ingest_document(
            base_id="kb1", filename="gone.md", text="# 独有词\n量子纠缠态的独有描述段落。\n"
        )
        assert store.search_chunks("量子纠缠", base_id="kb1")
        store.delete_document(result.document_id)
        assert store.get_document(result.document_id) is None
        assert store.list_buckets(result.document_id) == []
        assert store.list_chunks(document_id=result.document_id) == []
        assert store.search_chunks("量子纠缠", base_id="kb1") == []

    def test_reingest_replaces_not_duplicates(self, store: KnowledgeStore) -> None:
        """契约：同 base 同 filename 重摄取 → 旧三层整组替换
        不产生孤儿 bucket/chunk，计数与新版一致。"""
        store.ingest_document(base_id="kb1", filename="doc.md", text="# v1\n第一版内容段落。\n")
        result2 = store.ingest_document(
            base_id="kb1", filename="doc.md", text="# v2\n第二版完全不同的内容。\n"
        )
        docs = store.list_documents("kb1")
        assert len(docs) == 1 and docs[0].id == result2.document_id
        assert all(
            "第一版" not in c.content for c in store.list_chunks(document_id=result2.document_id)
        )
        assert store.search_chunks("第一版", base_id="kb1") == []


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# P1e 重摄取事务性：中途异常必须整体回滚（旧文档不得被删一半）
# ---------------------------------------------------------------------------


class TestIngestRollback:
    def test_ingest_failure_rolls_back_without_losing_old_document(
        self, store: KnowledgeStore
    ) -> None:
        """契约（破坏者）：同 filename 重摄取中途异常（metadata 不可
        序列化）→ 旧文档与旧 chunk 完好保留（DELETE 必须随事务回滚）。"""
        store.ingest_document(base_id="kb1", filename="m.md", text="# 旧节\n旧内容在此。")
        assert len(store.list_documents("kb1")) == 1

        with pytest.raises(TypeError):
            store.ingest_document(
                base_id="kb1",
                filename="m.md",
                text="# 新节\n新内容。",
                metadata={"bad": object()},
            )

        after = store.list_documents("kb1")
        assert len(after) == 1, "异常后旧文档必须保留（不得删一半）"
        assert store.search_chunks("旧内容", base_id="kb1"), "旧 chunk 必须仍可检索"


class TestCitations:
    def test_citations_from_results_dedup_and_limit(self) -> None:
        """契约：knowledge_citations_from_results 按 identity 去重
        limit 截断、label 从 [1] 连续编号（最终产物结构断言）。"""
        results: list[dict[str, Any]] = [
            {
                "evidence_pack": [
                    {"content": "内容A", "summary": "摘A", "source_path": "a.md", "chunk_id": "c1"},
                    {
                        "content": "内容A",
                        "summary": "摘A",
                        "source_path": "a.md",
                        "chunk_id": "c1",
                    },  # 重复
                    {"content": "内容B", "summary": "摘B", "source_path": "b.md", "chunk_id": "c2"},
                    {"content": "内容C", "summary": "摘C", "source_path": "c.md", "chunk_id": "c3"},
                ]
            }
        ]
        citations = knowledge_citations_from_results(results, limit=2)
        assert len(citations) == 2
        assert [c["label"] for c in citations] == ["[1]", "[2]"]
        assert citations[0]["kind"] == "evidence"
        assert citations[0]["excerpt"].startswith("内容A")

    def test_evidence_with_only_chunk_id_still_cited(self) -> None:
        """契约（破坏者）：evidence_pack 仅带 chunk_id（无 summary/source/
        excerpt）时仍生成引用——chunk_id 兜底身份必须可达，不得被空内容判死。"""
        citations = knowledge_citations_from_results(
            [{"evidence_pack": [{"chunk_id": "kchunk_1"}]}]
        )
        assert len(citations) == 1
        assert citations[0]["chunk_id"] == "kchunk_1"

    def test_compact_labels_renumber_by_first_appearance(self) -> None:
        """契约：compact_citation_labels 把正文引用标按首次出现
        顺序重编号，未在正文出现的引用被丢弃（StaffDeck citations 同构）。"""
        content = "结论见 [3]，再引用 [1]。"
        citations = [
            {"label": "[1]", "title": "甲"},
            {"label": "[2]", "title": "未引用"},
            {"label": "[3]", "title": "乙"},
        ]
        new_content, new_citations = compact_citation_labels(content, citations)
        assert "[1]" in new_content and "[2]" in new_content
        assert new_citations[0]["label"] == "[1]" and new_citations[0]["title"] == "乙"
        assert new_citations[1]["label"] == "[2]" and new_citations[1]["title"] == "甲"
        assert all(c["title"] != "未引用" for c in new_citations)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_ingest_search_cite_full_chain(self, store: KnowledgeStore) -> None:
        """契约：ingest → search → chunk 读回 → 构建引用 的
        生产主链路一次打通；引用可回指 chunk_id 并读到原文内容。"""
        store.ingest_document(
            base_id="kb1",
            filename="sop.md",
            text="# 部署流程\n部署前必须运行 verify 脚本确认六项全过。\n",
        )
        hits = store.search_chunks("verify 部署", base_id="kb1")
        assert hits
        chunk = store.get_chunk(hits[0].id)
        assert chunk is not None and "verify" in chunk.content
        citations = knowledge_citations_from_results(
            [
                {
                    "evidence_pack": [
                        {
                            "content": chunk.content,
                            "summary": chunk.summary or "",
                            "source_path": "sop.md",
                            "chunk_id": chunk.id,
                        }
                    ]
                }
            ]
        )
        assert citations and citations[0]["chunk_id"] == chunk.id


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestOversize:
    def test_oversize_paragraph_hard_split_no_loss(self, store: KnowledgeStore) -> None:
        """契约：无标题无空行的超大连续段落被硬切成多个 chunk
        全文拼接（去空白后）与原文一致——不静默丢内容。"""
        body = "这是一段没有结构的长文。" * 300
        result = store.ingest_document(base_id="kb1", filename="big.md", text=body)
        chunks = store.list_chunks(document_id=result.document_id)
        assert len(chunks) >= 2
        joined = "".join(c.content for c in chunks)
        assert joined.replace("\n", "") == body


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestSharedStoreInit:
    def test_shared_store_creates_data_dir_and_works(self, tmp_path: Any, monkeypatch: Any) -> None:
        """契约：当 ~/.bandi/data 不存在时
        get_shared_store 自动创建父目录并给出可落库检索的存储
        （破坏者：缺目录即 sqlite 连库失败——无部署前置即可用）。"""
        import pathlib

        import bandi.knowledge.store as store_mod

        fake_home = tmp_path / "no-data-dir-home"
        monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls, path=fake_home: path))
        monkeypatch.setattr(store_mod, "_SHARED_STORE", None)
        store = store_mod.get_shared_store()
        assert (fake_home / ".bandi" / "data").is_dir()
        assert (fake_home / ".bandi" / "data" / "knowledge.db").exists()
        store.ingest_document(
            base_id="kb1", filename="new.md", text="# 节\n共享库全新目录可落库。\n"
        )
        assert store.search_chunks("共享库", base_id="kb1")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestToolWiring:
    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_tool_search_merges_store_chunks(self, store: KnowledgeStore) -> None:
        """契约：装配 knowledge_store 后，工具 search 必须合并
        chunk 命中（source=knowledge_base 带 bucket_id/document_id 定位链）——
        零生产调用即未接线，禁止交付（DoD-11）。"""
        from bandi.tools.doc.knowledge_search import KnowledgeSearchTool

        store.ingest_document(
            base_id="kb1",
            filename="sop.md",
            text="# 部署流程\n部署前必须运行 verify 脚本确认六项全过。\n",
        )
        tool = KnowledgeSearchTool(knowledge_store=store)
        r = await tool.execute({"action": "search", "query": "verify 部署"})
        assert r["success"] is True
        kb_hits = [x for x in r["results"] if x.get("source") == "knowledge_base"]
        assert kb_hits and "verify" in kb_hits[0]["content"]
        assert kb_hits[0]["bucket_id"] and kb_hits[0]["document_id"]

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_tool_catalog_lists_documents(self, store: KnowledgeStore) -> None:
        """契约：catalog 列出知识库文档（含三层计数），空库提示保留。"""
        from bandi.tools.doc.knowledge_search import KnowledgeSearchTool

        store.ingest_document(base_id="kb1", filename="a.md", text="# 节\n内容段落文字。\n")
        tool = KnowledgeSearchTool(knowledge_store=store)
        r = await tool.execute({"action": "catalog"})
        assert r["success"] is True
        docs = [d for d in r["documents"] if d.get("source") == "knowledge_base"]
        assert docs and docs[0]["filename"] == "a.md"
        assert docs[0]["bucket_count"] >= 1 and docs[0]["chunk_count"] >= 1

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_tool_read_supports_chunk_id(self, store: KnowledgeStore) -> None:
        """契约：read 以 chunk id 读回 chunk 全文（跨 base 不泄漏——
        base_id 由命中结果携带，read 只认 chunk id 本体）。"""
        from bandi.tools.doc.knowledge_search import KnowledgeSearchTool

        store.ingest_document(base_id="kb1", filename="a.md", text="# 节\n独有内容词量子隧道。\n")
        chunk = store.list_chunks(document_id=store.list_documents("kb1")[0].id)[0]
        tool = KnowledgeSearchTool(knowledge_store=store)
        r = await tool.execute({"action": "read", "item_id": chunk.id})
        assert r["success"] is True
        assert "量子隧道" in r["text"]
