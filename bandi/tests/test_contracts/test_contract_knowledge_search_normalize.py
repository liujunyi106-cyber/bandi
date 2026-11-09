# 功能：契约 — 知识检索入口查询规范化 + 空结果无检索依据声明（外在幻觉源头治理）
from __future__ import annotations

from typing import Any

import pytest

from bandi.knowledge.store import KnowledgeStore


def _store() -> KnowledgeStore:
    store = KnowledgeStore(":memory:")
    store.ingest_document(
        "kb-main", "release.md", "# 发布口径\n产品发布时间是下周一", title="发布口径"
    )
    return store


def _tool() -> Any:
    from bandi.tools.doc.knowledge_search import KnowledgeSearchTool

    return KnowledgeSearchTool()


def _ctx() -> Any:
    from bandi.tools.base import ToolPermissionContext

    return ToolPermissionContext(role="analyst2")


class TestQueryNormalize:
    """查询规范化纯函数：全半角归一 / 包裹引号与边缘标点剥离 / 空白折叠。"""

    def test_fullwidth_to_halfwidth_and_trailing_question(self) -> None:
        from bandi.tools.doc.knowledge_search import normalize_query

        assert normalize_query("ＡＩ  模型？") == "AI 模型"

    def test_wrapping_quotes_and_edge_punct_stripped(self) -> None:
        from bandi.tools.doc.knowledge_search import normalize_query

        assert normalize_query("“产品发布时间？”") == "产品发布时间"
        assert normalize_query("产品发布时间！！") == "产品发布时间"

    def test_internal_whitespace_collapsed(self) -> None:
        from bandi.tools.doc.knowledge_search import normalize_query

        assert normalize_query("产品   发布　　时间") == "产品 发布 时间"

    def test_plain_query_unchanged(self) -> None:
        from bandi.tools.doc.knowledge_search import normalize_query

        assert normalize_query("薪酬口径") == "薪酬口径"


class TestGarbageQueryFiltered:
    """垃圾查询在入口被过滤：规范化后为空 → 参数错误，不进检索链。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["？？", "。。。", "   ", "“”", "？！。"])
    async def test_garbage_query_rejected(self, raw: str) -> None:
        tool = _tool()
        tool._knowledge_store = _store()
        out = await tool.execute({"action": "search", "query": raw}, context=_ctx())
        assert out["success"] is False
        assert "query is required" in out["error"]

    @pytest.mark.asyncio
    async def test_normalized_query_still_hits_store(self) -> None:
        """规范化不得误伤正常语义：全角问号结尾的 query 仍命中知识库。"""
        tool = _tool()
        tool._knowledge_store = _store()
        out = await tool.execute({"action": "search", "query": "产品发布时间？"}, context=_ctx())
        assert out["success"] is True
        assert out["total"] >= 1
        assert "下周一" in str(out["results"])


class TestNoEvidenceDeclaration:
    """空结果元认知声明：检索零命中 → evidence_status=none + 无检索依据提示进模型上下文。"""

    @pytest.mark.asyncio
    async def test_empty_search_declares_no_evidence(self) -> None:
        tool = _tool()
        tool._knowledge_store = _store()
        out = await tool.execute(
            {"action": "search", "query": "zzqq无依据探针xyzww"}, context=_ctx()
        )
        assert out["success"] is True
        assert out["total"] == 0
        assert out["evidence_status"] == "none"
        assert "无检索依据" in out["hint"]

    @pytest.mark.asyncio
    async def test_hit_search_marks_hit_without_notice(self) -> None:
        tool = _tool()
        tool._knowledge_store = _store()
        out = await tool.execute({"action": "search", "query": "产品发布时间"}, context=_ctx())
        assert out["evidence_status"] == "hit"
        assert "无检索依据" not in out["hint"]
