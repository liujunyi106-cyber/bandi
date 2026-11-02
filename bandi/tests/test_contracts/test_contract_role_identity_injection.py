# 功能：契约 — 角色定义注入员工执行链路（断链接线：org_engine → seat_executor → company_source → system prompt）
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from bandi.company.org_engine import OrgEngine, OrgRole
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.context.sources.company_source import ROLE_IDENTITY_CONTEXT_KEY


def _org_with_responsibility() -> OrgEngine:
    org = OrgEngine()
    org.register_role(
        "db_engineer",
        "数据库工程师",
        responsibility="负责数据库设计与 SQL 优化，产出口径以 SQL 为准",
        reports_to="cto",
    )
    return org


def _employee_registered(org: OrgEngine, *, persona: str = "") -> str:
    from bandi.company.org_engine import OrgEmployee

    metadata: dict[str, object] = {"persona_prompt": persona} if persona else {}
    org.register_employee(
        OrgEmployee(
            employee_id="emp-db-1",
            name="老王",
            role_id="db_engineer",
            metadata=metadata,
        )
    )
    return "emp-db-1"


class TestRolePromptContextRendering:
    """org_engine.get_role_prompt_context：真实 OrgEngine 内容级渲染。"""

    def test_renders_responsibility_and_reporting_line(self) -> None:
        org = _org_with_responsibility()
        text = org.get_role_prompt_context("db_engineer")
        assert "负责数据库设计与 SQL 优化" in text, f"职责必须渲染: {text}"
        assert "数据库工程师" in text
        assert "CTO" in text, "汇报对象必须渲染角色名"

    def test_employee_name_and_persona_rendered(self) -> None:
        org = _org_with_responsibility()
        _employee_registered(org, persona="回复风格：直接给 SQL，不给废话")
        text = org.get_role_prompt_context("db_engineer", employee_id="emp-db-1")
        assert "老王" in text, "员工名必须渲染"
        assert "直接给 SQL，不给废话" in text, "员工 persona 必须渲染"

    def test_persona_absent_not_rendered_as_empty_section(self) -> None:
        org = _org_with_responsibility()
        _employee_registered(org)
        text = org.get_role_prompt_context("db_engineer", employee_id="emp-db-1")
        assert "补充要求" not in text, "无 persona 不得渲染空段"

    def test_unknown_role_returns_empty(self) -> None:
        assert OrgEngine().get_role_prompt_context("ghost") == ""

    def test_self_referencing_role_no_hang(self) -> None:
        """破坏者：绕过 register_role 直接注入自引用角色——查询不得死循环/崩溃。"""
        org = OrgEngine()
        org._roles["loop"] = OrgRole(id="loop", name="Loop", reports_to="loop", responsibility="x")
        text = org.get_role_prompt_context("loop")
        assert "x" in text

    def test_single_arg_backward_compatible(self) -> None:
        org = _org_with_responsibility()
        assert org.get_role_prompt_context("db_engineer")

    def test_oversized_responsibility_capped_not_dropped(self) -> None:
        """DoD-5 超长形态：5000 字职责必须截断进身份段——渲染层不收口时 planner
        会对超预算 item 整条丢弃（静默消失），此处钉「身份段恒在且体积有界」。"""
        org = OrgEngine()
        org.register_role("writer", "写手", responsibility="长" * 5000)
        text = org.get_role_prompt_context("writer")
        assert "长" * 100 in text, "截断后仍须保留职责前缀（非整段消失）"
        assert len(text) <= 1200, f"身份段必须有界: {len(text)}"

        from bandi.context.item import ContextItem
        from bandi.context.planner import ContextPlanner
        from bandi.utils.token_counter import TokenCounter

        item = ContextItem(
            id="identity",
            source_type="company",
            content=text,
            priority=9,
            token_estimate=TokenCounter.estimate(text),
        )
        planned = ContextPlanner.plan([item], max_tokens=1024)
        assert planned, "截断后的身份段必须能在最小注册表预算（1024 tokens）下存活"


class TestSeatExecutorWiresIdentity:
    """生产装配路径：真实 OrgEngine + 真实 EngineSeatExecutor（LLM 边界 stub）。"""

    def _runtime(self, org: OrgEngine, employee_id: str = "emp-db-1") -> Any:
        from bandi.company.company_runtime import CompanyRuntime

        return CompanyRuntime(
            project_id="proj-1",
            session_scope_id="scope-1",
            role_id="db_engineer",
            employee_id=employee_id,
        )

    def test_run_turn_stashes_identity_into_runtime_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bandi.company import seat_executor as seat_mod
        from bandi.company.seat_executor import EngineSeatExecutor

        org = _org_with_responsibility()
        _employee_registered(org)
        executor = EngineSeatExecutor(org_engine=org)
        runtime = self._runtime(org)
        item = create_work_item(title="优化慢查询", turn_type=WorkItemTurnType.EXECUTE)

        async def fake_run_agent(user_input: str, **kwargs: Any) -> tuple[str, None, int, int]:
            return "done", None, 0, 0

        monkeypatch.setattr(seat_mod, "run_agent", fake_run_agent)
        result = asyncio.run(executor.run_turn(runtime, item))

        assert result.success, f"执行必须成功: {result.output}"
        identity = runtime.metadata(ROLE_IDENTITY_CONTEXT_KEY)
        assert identity, "run_turn 必须把身份段写入 runtime.metadata"
        assert "负责数据库设计与 SQL 优化" in str(identity), "职责必须随身份段下发"
        assert "老王" in str(identity), "员工名必须随身份段下发"

    def test_no_org_engine_no_identity_key_execution_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bandi.company import seat_executor as seat_mod
        from bandi.company.seat_executor import EngineSeatExecutor

        executor = EngineSeatExecutor()
        runtime = self._runtime(OrgEngine())
        item = create_work_item(title="整理报告", turn_type=WorkItemTurnType.EXECUTE)

        async def fake_run_agent(user_input: str, **kwargs: Any) -> tuple[str, None, int, int]:
            return "done", None, 0, 0

        monkeypatch.setattr(seat_mod, "run_agent", fake_run_agent)
        result = asyncio.run(executor.run_turn(runtime, item))

        assert result.success
        assert runtime.metadata(ROLE_IDENTITY_CONTEXT_KEY) in (None, ""), (
            "无 org_engine 不得写入身份段（无数据不硬造）"
        )

    def test_org_engine_crash_fail_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """破坏者：org_engine 查询崩溃——身份段丢弃，主链路不得被拖死。"""
        from bandi.company import seat_executor as seat_mod
        from bandi.company.seat_executor import EngineSeatExecutor

        class _BoomOrg:
            def get_role_prompt_context(self, role_id: str, employee_id: str = "") -> str:
                raise RuntimeError("boom")

        executor = EngineSeatExecutor(org_engine=_BoomOrg())
        runtime = self._runtime(OrgEngine())
        item = create_work_item(title="整理报告", turn_type=WorkItemTurnType.EXECUTE)

        async def fake_run_agent(user_input: str, **kwargs: Any) -> tuple[str, None, int, int]:
            return "done", None, 0, 0

        monkeypatch.setattr(seat_mod, "run_agent", fake_run_agent)
        result = asyncio.run(executor.run_turn(runtime, item))

        assert result.success, "身份段渲染失败不得阻断执行主链路"
        assert not runtime.metadata(ROLE_IDENTITY_CONTEXT_KEY)


class TestCompanySourceRelaysIdentity:
    """company_source 转发身份段进 system 注入通道。"""

    def _runtime_with_identity(self, text: str) -> Any:
        from bandi.company.company_runtime import CompanyRuntime

        runtime = CompanyRuntime(
            project_id="p", session_scope_id="s", role_id="db_engineer", employee_id="e1"
        )
        runtime.set_metadata(ROLE_IDENTITY_CONTEXT_KEY, text)
        return runtime

    @pytest.mark.asyncio
    async def test_identity_emitted_even_without_member_sessions(self) -> None:
        from bandi.context.sources.company_source import CompanyContextSource
        from bandi.core.message import Message

        source = CompanyContextSource(company_runtime=self._runtime_with_identity("职责：X"))
        items = await source.collect(Message(role="user", content="hi"), session_id="s-1")
        assert items, "身份段必须在无成员会话时也产出（执行身份不依赖群聊状态）"
        identity_items = [i for i in items if "职责：X" in i.content]
        assert identity_items, f"身份段必须出现在 collect 结果: {[i.content for i in items]}"
        assert identity_items[0].source_type == "company"
        assert identity_items[0].priority > 4, (
            "身份段 priority 必须高于成员状态项（裁剪时优先保留）"
        )

    @pytest.mark.asyncio
    async def test_identity_first_before_status_items(self) -> None:
        from bandi.context.sources.company_source import CompanyContextSource
        from bandi.core.message import Message

        runtime = self._runtime_with_identity("职责：X")
        peer = runtime.ensure_role_member_session("engineer", "e-eng")
        runtime.set_member_session_status(peer, "running", focused_work_item_id="w-9")
        source = CompanyContextSource(company_runtime=runtime)
        items = await source.collect(Message(role="user", content="hi"), session_id="s-1")
        assert items, "必须产出 items"
        assert "职责：X" in items[0].content, "身份段必须排在成员状态之前"

    @pytest.mark.asyncio
    async def test_no_metadata_no_identity_item(self) -> None:
        from bandi.company.company_runtime import CompanyRuntime
        from bandi.context.sources.company_source import CompanyContextSource
        from bandi.core.message import Message

        runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        source = CompanyContextSource(company_runtime=runtime)
        items = await source.collect(Message(role="user", content="hi"), session_id="s-1")
        assert all("执行身份" not in i.content for i in items), "无身份段不得硬造"

    @pytest.mark.asyncio
    async def test_stub_runtime_without_metadata_method_safe(self) -> None:
        """破坏者：duck-typing runtime（无 metadata 方法，旧契约 _StubRuntime 形态）——不得崩。"""
        from bandi.context.sources.company_source import CompanyContextSource
        from bandi.core.message import Message

        class _StubRuntime:
            def sorted_member_sessions(self) -> list[dict[str, Any]]:
                return []

        source = CompanyContextSource(company_runtime=_StubRuntime())
        items = await source.collect(Message(role="user", content="hi"), session_id="s-1")
        assert items == []


class TestIdentityReachesSystemPrompt:
    """端到端：run_agent_turn 生产链路（MockProvider 录制）断言 LLM 请求 system 区。"""

    @pytest.mark.asyncio
    async def test_identity_rendered_into_system_message(self, tmp_path: Any) -> None:
        from bandi.core.agent_runner import run_agent_turn
        from bandi.core.message import LLMResponse
        from bandi.llm.mock import MockProvider
        from bandi.memory.session.store import SessionStore

        # 生产装配：真实 OrgEngine 渲染 → runtime.metadata → company_source → system
        org = _org_with_responsibility()
        _employee_registered(org, persona="回复风格：直接给 SQL")
        identity_text = org.get_role_prompt_context("db_engineer", employee_id="emp-db-1")
        assert "负责数据库设计与 SQL 优化" in identity_text, "前置：真实渲染必须含职责"

        from bandi.company.company_runtime import CompanyRuntime

        runtime = CompanyRuntime(
            project_id="p-e2e", session_scope_id="s-e2e", role_id="db_engineer", employee_id="e1"
        )
        runtime.set_metadata(ROLE_IDENTITY_CONTEXT_KEY, identity_text)

        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(title="t")

        seen: list[list[dict[str, Any]]] = []

        class _Recording(MockProvider):
            async def complete(
                self, messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any
            ) -> LLMResponse:
                seen.append(messages)
                return await super().complete(messages, tools=tools, **kwargs)

            async def stream(self, messages: list, tools: Any = None, **kwargs: Any) -> Any:
                seen.append(messages)
                async for ev in super().stream(messages, tools=tools, **kwargs):
                    yield ev

        provider = _Recording()
        provider.add_response(LLMResponse(content="收到"))
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            await run_agent_turn(
                user_input="开始优化慢查询",
                session_id=sid,
                session_store=store,
                company_runtime=runtime,
            )
        assert seen, "LLM 必须被调用"
        injected_content = "\n".join(str(m.get("content") or "") for m in seen[0])
        assert "负责数据库设计与 SQL 优化" in injected_content, (
            f"角色职责必须进 LLM 请求（易变上下文段）: {injected_content[:400]}"
        )
        assert "直接给 SQL" in injected_content, "员工 persona 必须进 LLM 请求"
        store.close()
