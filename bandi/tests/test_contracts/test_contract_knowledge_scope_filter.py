# 功能：契约 — 知识检索角色级过滤（role.permissions["knowledge_base"] → 检索面收口）
from __future__ import annotations

from typing import Any

import pytest

from bandi.company.org_engine import OrgEngine
from bandi.knowledge.scopes import (
    KNOWLEDGE_BASES_PERMISSION_KEY,
    get_runtime_knowledge_bases,
    set_runtime_knowledge_bases,
)
from bandi.knowledge.store import KnowledgeStore


def _org_with_kb_permission(base_ids: list[str] | None) -> OrgEngine:
    """真实 OrgEngine：role 带 knowledge_bases 权限（None=键不存在=不过滤）。"""
    org = OrgEngine()
    org.register_role(
        "analyst2",
        "分析师",
        responsibility="数据分析",
        permissions=(
            {KNOWLEDGE_BASES_PERMISSION_KEY: list(base_ids)} if base_ids is not None else None
        ),
    )
    return org


def _store_with_two_bases() -> KnowledgeStore:
    """真实 KnowledgeStore：kb-public / kb-secret 各一篇文档（含中文命中词）。"""
    store = KnowledgeStore(":memory:")
    store.ingest_document(
        "kb-public", "public.md", "# 公开口径\n产品发布时间是下周一", title="公开口径"
    )
    store.ingest_document(
        "kb-secret", "secret.md", "# 薪酬保密表\n张三薪酬是一百万", title="薪酬保密"
    )
    return store


def _tool() -> Any:
    from bandi.tools.doc.knowledge_search import KnowledgeSearchTool

    return KnowledgeSearchTool()


def _ctx(role: str = "analyst2") -> Any:
    from bandi.tools.base import ToolPermissionContext

    return ToolPermissionContext(role=role)


class TestStoreLevelFilter:
    """store 层：base_ids 参数语义（None=不过滤 / 列表=IN 过滤 / LIKE 回退同收口）。"""

    def test_search_chunks_none_returns_all(self) -> None:
        store = _store_with_two_bases()
        assert len(store.search_chunks("薪酬")) == 1
        assert len(store.search_chunks("产品发布")) == 1

    def test_search_chunks_base_ids_filters(self) -> None:
        store = _store_with_two_bases()
        hits = store.search_chunks("薪酬", base_ids=["kb-public"])
        assert hits == [], "越权 base 的命中不得出现"
        hits = store.search_chunks("薪酬", base_ids=["kb-secret"])
        assert len(hits) == 1 and hits[0].base_id == "kb-secret"

    def test_search_chunks_like_fallback_respects_base_ids(self) -> None:
        """FTS 中文空命中回退 LIKE——回退路径必须同样收口（破坏者：绕 FTS 走 LIKE）。"""
        store = _store_with_two_bases()
        hits = store.search_chunks("薪酬不存在词组组合xyz", base_ids=["kb-public"])
        assert all(h.base_id == "kb-public" for h in hits)

    def test_list_all_documents_and_concepts_filtered(self) -> None:
        store = _store_with_two_bases()
        docs = store.list_all_documents(base_ids=["kb-public"])
        assert [d.base_id for d in docs] == ["kb-public"]
        assert len(store.list_all_documents()) == 2, "None = 不过滤（零回归）"


class TestToolLevelEnforcement:
    """工具面：KnowledgeSearchTool 三动作全收口（真实 store + contextvar 传递）。"""

    @pytest.mark.asyncio
    async def test_search_hides_forbidden_base(self) -> None:
        store = _store_with_two_bases()
        tool = _tool()
        tool._knowledge_store = store
        set_runtime_knowledge_bases(["kb-public"])
        try:
            out = await tool.execute({"action": "search", "query": "薪酬"}, context=_ctx())
        finally:
            set_runtime_knowledge_bases(None)
        assert out["success"] is True
        joined = str(out["results"])
        assert "一百万" not in joined, f"越权内容不得出现: {joined}"
        assert all(
            r.get("source") != "knowledge_base" or "kb-secret" not in str(r) for r in out["results"]
        )

    @pytest.mark.asyncio
    async def test_search_without_scope_unchanged(self) -> None:
        """键不存在（contextvar None）= 现状全量——零回归红线。"""
        store = _store_with_two_bases()
        tool = _tool()
        tool._knowledge_store = store
        assert get_runtime_knowledge_bases() is None
        out = await tool.execute({"action": "search", "query": "薪酬"}, context=_ctx())
        assert "一百万" in str(out["results"])

    @pytest.mark.asyncio
    async def test_catalog_filters_documents(self) -> None:
        store = _store_with_two_bases()
        tool = _tool()
        tool._knowledge_store = store
        set_runtime_knowledge_bases(["kb-public"])
        try:
            out = await tool.execute({"action": "catalog"}, context=_ctx())
        finally:
            set_runtime_knowledge_bases(None)
        filenames = [d["filename"] for d in out["documents"] if d["source"] == "knowledge_base"]
        assert filenames == ["public.md"], f"越权文档不得进 catalog: {filenames}"

    @pytest.mark.asyncio
    async def test_read_forbidden_chunk_is_not_found(self) -> None:
        """破坏者：绕过 search 直接 read 越权 chunk——必须按 not found 处理（不泄露内容）。"""
        store = _store_with_two_bases()
        tool = _tool()
        tool._knowledge_store = store
        secret_chunk = store.search_chunks("薪酬")[0]
        set_runtime_knowledge_bases(["kb-public"])
        try:
            out = await tool.execute({"action": "read", "item_id": secret_chunk.id}, context=_ctx())
        finally:
            set_runtime_knowledge_bases(None)
        assert out["success"] is False, f"越权 read 必须失败: {out}"
        assert "一百万" not in str(out)

    @pytest.mark.asyncio
    async def test_read_allowed_chunk_succeeds(self) -> None:
        store = _store_with_two_bases()
        tool = _tool()
        tool._knowledge_store = store
        public_chunk = store.search_chunks("产品发布")[0]
        set_runtime_knowledge_bases(["kb-public"])
        try:
            out = await tool.execute({"action": "read", "item_id": public_chunk.id}, context=_ctx())
        finally:
            set_runtime_knowledge_bases(None)
        assert out["success"] is True and "下周一" in out["text"]


class TestSeatAssemblyWiring:
    """生产装配路径：seat_executor 从 OrgRole.permissions 读取并经 contextvar 传入执行链。"""

    def test_run_turn_sets_scope_from_role_permissions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bandi.company import seat_executor as seat_mod
        from bandi.company.company_runtime import CompanyRuntime
        from bandi.company.seat_executor import EngineSeatExecutor
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        org = _org_with_kb_permission(["kb-public"])
        executor = EngineSeatExecutor(org_engine=org)
        runtime = CompanyRuntime(
            project_id="p", session_scope_id="s", role_id="analyst2", employee_id="e1"
        )
        item = create_work_item(title="查口径", turn_type=WorkItemTurnType.EXECUTE)

        seen: list[list[str] | None] = []

        async def fake_run_agent(user_input: str, **kwargs: Any) -> tuple[str, None, int, int]:
            seen.append(get_runtime_knowledge_bases())
            return "done", None, 0, 0

        monkeypatch.setattr(seat_mod, "run_agent", fake_run_agent)
        import asyncio

        asyncio.run(executor.run_turn(runtime, item))

        assert seen == [["kb-public"]], (
            f"run_agent 执行期间 contextvar 必须携带角色知识库权限: {seen}"
        )

    def test_run_turn_no_permission_key_leaves_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bandi.company import seat_executor as seat_mod
        from bandi.company.company_runtime import CompanyRuntime
        from bandi.company.seat_executor import EngineSeatExecutor
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        org = _org_with_kb_permission(None)
        executor = EngineSeatExecutor(org_engine=org)
        runtime = CompanyRuntime(
            project_id="p", session_scope_id="s", role_id="analyst2", employee_id="e1"
        )
        item = create_work_item(title="查口径", turn_type=WorkItemTurnType.EXECUTE)

        async def fake_run_agent(user_input: str, **kwargs: Any) -> tuple[str, None, int, int]:
            return "done", None, 0, 0

        monkeypatch.setattr(seat_mod, "run_agent", fake_run_agent)
        import asyncio

        asyncio.run(executor.run_turn(runtime, item))

        assert get_runtime_knowledge_bases() is None, "键不存在=不过滤，且执行后不得残留"

    def test_run_turn_resets_scope_after_execution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """破坏者：contextvar 泄漏会污染同任务后续运行——执行完必须复位。"""
        from bandi.company import seat_executor as seat_mod
        from bandi.company.company_runtime import CompanyRuntime
        from bandi.company.seat_executor import EngineSeatExecutor
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        org = _org_with_kb_permission(["kb-public"])
        executor = EngineSeatExecutor(org_engine=org)
        runtime = CompanyRuntime(
            project_id="p", session_scope_id="s", role_id="analyst2", employee_id="e1"
        )
        item = create_work_item(title="查口径", turn_type=WorkItemTurnType.EXECUTE)

        async def fake_run_agent(user_input: str, **kwargs: Any) -> tuple[str, None, int, int]:
            return "done", None, 0, 0

        monkeypatch.setattr(seat_mod, "run_agent", fake_run_agent)
        import asyncio

        asyncio.run(executor.run_turn(runtime, item))

        assert get_runtime_knowledge_bases() is None

    def test_permission_query_crash_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """破坏者：role 查询崩溃——对齐 tool_tags fail-closed 先例（空名单=全禁）
        不得 fail-open 放行全量。"""
        from bandi.company import seat_executor as seat_mod
        from bandi.company.company_runtime import CompanyRuntime
        from bandi.company.seat_executor import EngineSeatExecutor
        from bandi.company.work_item import WorkItemTurnType, create_work_item

        class _BoomOrg:
            def get_role(self, role_id: str) -> Any:
                raise RuntimeError("boom")

        executor = EngineSeatExecutor(org_engine=_BoomOrg())
        runtime = CompanyRuntime(
            project_id="p", session_scope_id="s", role_id="analyst2", employee_id="e1"
        )
        item = create_work_item(title="查口径", turn_type=WorkItemTurnType.EXECUTE)

        seen: list[list[str] | None] = []

        async def fake_run_agent(user_input: str, **kwargs: Any) -> tuple[str, None, int, int]:
            seen.append(get_runtime_knowledge_bases())
            return "done", None, 0, 0

        monkeypatch.setattr(seat_mod, "run_agent", fake_run_agent)
        import asyncio

        asyncio.run(executor.run_turn(runtime, item))

        assert seen == [[]], "查询异常必须 fail-closed 空名单（全禁），不得 fail-open"


class TestPermissionKeyInterop:
    """口径统一：knowledge_base 键即 resource_type，值列表成员 = 允许访问的库 id。"""

    def test_permission_key_reads_same_config(self) -> None:
        org = _org_with_kb_permission(["kb-public"])
        role = org.get_role("analyst2")
        assert role is not None
        allowed = role.permissions[KNOWLEDGE_BASES_PERMISSION_KEY]
        assert "kb-public" in allowed
        assert "kb-secret" not in allowed, "越权库不得出现在该角色的可见名单"
