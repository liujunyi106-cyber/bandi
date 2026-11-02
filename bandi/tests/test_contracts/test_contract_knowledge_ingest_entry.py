# 功能：契约——知识库 ingest 生产入口 + OKF 概念消费接线。
# 三条生产路径逐条有契约（DoD-10）：
# ① KnowledgeIngestTool（Agent 工具面：LLM/组织员工投放文档）
# ② company API（前端 / 外部投放：POST ingest + GET documents + GET search）
# ③ KnowledgeSearchTool 概念合并（search 结果含 okf_concept 卡）
# 钉最终产物：落库可读回（三层 + 概念）、检索可命中、API 恒 200 信封语义按既有公司端点。

from __future__ import annotations

from typing import Any

import pytest

from bandi.knowledge.store import KnowledgeStore
from bandi.tools.doc.knowledge_ingest import KnowledgeIngestTool
from bandi.tools.doc.knowledge_search import KnowledgeSearchTool

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestIngestTool:
    @pytest.mark.asyncio
    async def test_ingest_tool_end_to_end(self, tmp_path: Any) -> None:
        """契约：knowledge_ingest 工具一次调用 → document/bucket/
        chunk 三层落库 + OKF 概念自动构建落库（读回断言最终产物，非 grep）。"""
        store = KnowledgeStore(db_path=tmp_path / "k.db")
        tool = KnowledgeIngestTool(knowledge_store=store)
        r = await tool.execute(
            {
                "base_id": "kb1",
                "filename": "sop.md",
                "text": "# 部署流程\n部署前必须运行 verify 脚本。\n",
                "title": "部署 SOP",
            }
        )
        assert r["success"] is True
        assert r["bucket_count"] >= 1 and r["chunk_count"] >= 1
        assert r["concept_count"] >= 2  # 入口概念 + 每桶概念
        doc = store.get_document(r["document_id"])
        assert doc is not None and doc.title == "部署 SOP"
        assert len(store.list_concepts("kb1")) == r["concept_count"]

    @pytest.mark.asyncio
    async def test_ingest_tool_rejects_blank_and_missing(self, tmp_path: Any) -> None:
        """契约（破坏者）：空白 text / 缺 filename / 缺 base_id →
        success=False 显式 error（不崩不落半截数据）。"""
        store = KnowledgeStore(db_path=tmp_path / "k.db")
        tool = KnowledgeIngestTool(knowledge_store=store)
        blank = await tool.execute({"base_id": "kb1", "filename": "a.md", "text": "  \n "})
        assert blank["success"] is False and "error" in blank
        no_name = await tool.execute({"base_id": "kb1", "text": "内容"})
        assert no_name["success"] is False
        no_base = await tool.execute({"filename": "a.md", "text": "内容"})
        assert no_base["success"] is False
        assert store.list_documents("kb1") == []


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestConceptSearchWiring:
    @pytest.mark.asyncio
    async def test_search_returns_okf_concept_cards(self, tmp_path: Any) -> None:
        """契约：装配同一 store 后，knowledge_search 的 search
        结果必须含 okf_concept 卡（title/content 剥 frontmatter）——概念层
        零消费即未接线。"""
        store = KnowledgeStore(db_path=tmp_path / "k.db")
        ingest = KnowledgeIngestTool(knowledge_store=store)
        await ingest.execute(
            {
                "base_id": "kb1",
                "filename": "sop.md",
                "text": "# 部署流程\n部署前必须运行 verify 脚本。\n",
            }
        )
        search = KnowledgeSearchTool(knowledge_store=store)
        r = await search.execute({"action": "search", "query": "部署流程"})
        assert r["success"] is True
        concept_hits = [x for x in r["results"] if x.get("source") == "okf_concept"]
        assert concept_hits, "概念卡必须出现在检索结果"
        assert any("部署" in c["title"] for c in concept_hits)
        assert all("---" not in c["content"][:4] for c in concept_hits)

    @pytest.mark.asyncio
    async def test_search_total_capped_by_limit(self, tmp_path: Any) -> None:
        """契约：search 的 total 恒 ≤ limit——概念卡
        (固定 3) + chunk + long_term 三源叠加不得突破 limit（破坏者：三源
        query 全命中时各源内部 limit 不足，total 溢出即破防）。"""
        store = KnowledgeStore(db_path=tmp_path / "k.db")
        ingest = KnowledgeIngestTool(knowledge_store=store)
        await ingest.execute(
            {
                "base_id": "kb1",
                "filename": "sop.md",
                "title": "部署手册",
                "text": "# 部署流程\n部署前必须运行 verify 脚本。\n\n"
                "# 部署回滚\n回滚前先停写入再切流量。\n\n"
                "# 部署验证\n部署后运行六项检查确认全过。\n",
            }
        )
        search = KnowledgeSearchTool(knowledge_store=store)
        r = await search.execute({"action": "search", "query": "部署", "limit": 2})
        assert r["success"] is True
        assert r["total"] <= 2
        assert len(r["results"]) <= 2


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_client(tmp_path: Any, monkeypatch: Any) -> Any:
    pytest.importorskip("fastapi")
    import sys

    from fastapi.testclient import TestClient

    import bandi.api.app  # noqa: F401 —— 确保模块已加载进 sys.modules

    app_module = sys.modules["bandi.api.app"]  # 真模块（包属性 app 被 FastAPI 实例遮蔽）

    test_store = KnowledgeStore(db_path=tmp_path / "api_knowledge.db")
    monkeypatch.setattr(app_module, "_get_knowledge_store", lambda: test_store)
    monkeypatch.setattr(app_module, "_API_KEY", "test")
    client = TestClient(app_module.app)
    client.headers["Authorization"] = "Bearer test"
    return client


# ---------------------------------------------------------------------------
# P7e catalog 上限：知识库文档清单必须有界（不随库大小膨胀）
# ---------------------------------------------------------------------------


class _EmptyMemory:
    def retrieve(self, query: str, limit: int = 10) -> list[Any]:
        return []


class TestCatalogCap:
    @pytest.mark.asyncio
    async def test_catalog_caps_knowledge_documents(self, tmp_path: Any, monkeypatch: Any) -> None:
        """契约：catalog 的知识库文档列表受 _MAX_CATALOG 约束
        （无界膨胀会使工具输出随库规模线性变大）。"""
        from bandi.tools.doc import knowledge_search as knowledge_search_module

        monkeypatch.setattr(knowledge_search_module, "_MAX_CATALOG", 2)
        store = KnowledgeStore(db_path=tmp_path / "k.db")
        for index in range(3):
            store.ingest_document(
                base_id="kb1", filename=f"d{index}.md", text=f"# 节\n内容 {index}。"
            )
        tool = KnowledgeSearchTool(memory=_EmptyMemory(), knowledge_store=store)
        r = await tool.execute({"action": "catalog"})
        assert r["success"] is True
        assert len(r["documents"]) <= 2
        assert r["total"] <= 2


class TestKnowledgeApi:
    def test_ingest_list_search_api_chain(self, api_client: Any) -> None:
        """契约：API 三步链——POST /api/company/knowledge/ingest
        落库 → GET documents 列出（含三层计数）→ GET search 命中 chunk 与概念。"""
        resp = api_client.post(
            "/api/company/knowledge/ingest",
            json={
                "base_id": "kb1",
                "filename": "sop.md",
                "text": "# 部署流程\n部署前必须运行 verify 脚本。\n",
                "title": "部署 SOP",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True and data["bucket_count"] >= 1 and data["concept_count"] >= 2

        resp = api_client.get("/api/company/knowledge/documents", params={"base_id": "kb1"})
        assert resp.status_code == 200
        docs = resp.json()["documents"]
        assert docs and docs[0]["filename"] == "sop.md"
        assert docs[0]["chunk_count"] >= 1

        resp = api_client.get(
            "/api/company/knowledge/search", params={"query": "verify", "base_id": "kb1"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["chunks"], "chunk 命中必须返回"
        assert any("verify" in c["content"] for c in body["chunks"])

    def test_ingest_api_rejects_blank_text(self, api_client: Any) -> None:
        """契约（破坏者）：API 空白 text → 422/400 显式拒绝（不落库）。"""
        resp = api_client.post(
            "/api/company/knowledge/ingest",
            json={"base_id": "kb1", "filename": "a.md", "text": " \n "},
        )
        assert resp.status_code in (400, 422)
        assert (
            api_client.get("/api/company/knowledge/documents", params={"base_id": "kb1"}).json()[
                "documents"
            ]
            == []
        )

    def test_ingest_api_rejects_blank_filename_and_base_id(self, api_client: Any) -> None:
        """契约（修复，破坏者）：空白 filename / 空白 base_id →
        API 显式 400 拒绝（工具侧已拒绝，API 面不得漏放行——跨入口语义一致）。
        空 base 名探测：不落任何 base 容器。"""
        blank_filename = api_client.post(
            "/api/company/knowledge/ingest",
            json={"base_id": "kb1", "filename": "   ", "text": "有效内容段落。"},
        )
        assert blank_filename.status_code in (400, 422)
        blank_base = api_client.post(
            "/api/company/knowledge/ingest",
            json={"base_id": "  ", "filename": "a.md", "text": "有效内容段落。"},
        )
        assert blank_base.status_code in (400, 422)
        assert (
            api_client.get("/api/company/knowledge/documents", params={"base_id": "kb1"}).json()[
                "documents"
            ]
            == []
        )
