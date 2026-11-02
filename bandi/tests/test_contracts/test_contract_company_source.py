# 功能：契约 — CompanyContextSource 接线（群聊上下文注入其他员工状态）
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.context.item import ContextItem
from bandi.core.message import Message


def _member(role_id: str, status: str, focused: str = "", employee_id: str = "") -> dict[str, Any]:
    return {
        "role_id": role_id,
        "employee_id": employee_id,
        "status": status,
        "focused_work_item_id": focused,
    }


class _StubRuntime:
    """duck-typing CompanyRuntime——只暴露 sorted_member_sessions 面。"""

    def __init__(self, sessions: list[dict[str, Any]] | None) -> None:
        self._sessions = sessions if sessions is not None else []

    def sorted_member_sessions(self) -> list[dict[str, Any]]:
        return self._sessions


class TestProductionDataclassShape:
    @pytest.mark.asyncio
    async def test_real_runtime_dataclass_sessions_render(self) -> None:
        """
        （非 dict）——collect 必须按属性访问渲染，不得因形态不符静默失效。"""
        from bandi.company.company_runtime import CompanyRuntime
        from bandi.context.sources.company_source import CompanyContextSource

        runtime = CompanyRuntime(
            project_id="p-fix",
            session_scope_id="scope",
            role_id="cto",
            employee_id="e-cto",
        )
        peer = runtime.ensure_role_member_session("engineer", "e-eng")
        runtime.set_member_session_status(peer, "running", focused_work_item_id="w-9")
        sessions = runtime.sorted_member_sessions()
        assert sessions and hasattr(sessions[0], "role_id"), "前置：生产返回 dataclass 形态"

        source = CompanyContextSource(company_runtime=runtime)
        items = await source.collect(Message(role="user", content="hi"), session_id="s-1")
        assert items, "真实 dataclass 形态必须产出 company 上下文项（dict stub 假阳性回归）"
        joined = "\n".join(i.content for i in items)
        assert "engineer" in joined and "执行中" in joined and "w-9" in joined


class TestCompanyContextSourceUnit:
    @pytest.mark.asyncio
    async def test_renders_other_members_as_company_items(self) -> None:
        from bandi.context.sources.company_source import CompanyContextSource

        runtime = _StubRuntime(
            [
                _member("ceo", "running", focused="w-1", employee_id="e1"),
                _member("engineer", "idle", employee_id="e2"),
            ]
        )
        source = CompanyContextSource(company_runtime=runtime)
        items = await source.collect(Message(role="user", content="hi"), session_id="s-1")
        assert items, "有其他成员时必须产出 company 上下文项"
        assert all(isinstance(i, ContextItem) and i.source_type == "company" for i in items)
        joined = "\n".join(i.content for i in items)
        assert "ceo" in joined and "engineer" in joined
        assert "执行中" in joined and "空闲" in joined

    @pytest.mark.asyncio
    async def test_provider_failure_returns_empty(self) -> None:
        from bandi.context.sources.company_source import CompanyContextSource

        class _Broken:
            def sorted_member_sessions(self) -> list[dict[str, Any]]:
                raise RuntimeError("boom")

        source = CompanyContextSource(company_runtime=_Broken())
        items = await source.collect(Message(role="user", content="hi"), session_id="s-1")
        assert items == [], "provider 异常必须 fail-safe 空 List"

    @pytest.mark.asyncio
    async def test_no_runtime_returns_empty(self) -> None:
        from bandi.context.sources.company_source import CompanyContextSource

        source = CompanyContextSource(company_runtime=None)
        items = await source.collect(Message(role="user", content="hi"), session_id="s-1")
        assert items == []


class TestPipelineRendersCompanyItems:
    @pytest.mark.asyncio
    async def test_company_items_rendered_into_extra_context(self) -> None:
        """pipeline extra_context 渲染扩展——company 类型进「公司上下文」节。"""
        from bandi.context.planner import ContextPlanner
        from bandi.context.source import ContextSourceRegistry
        from bandi.perception.pipeline import PerceptionPipeline

        class _FixedSource:
            async def collect(
                self,
                message: Message,
                session_id: str,
                budget: int = 4096,
                intent_hint: str | None = None,
            ) -> list[ContextItem]:
                return [
                    ContextItem(
                        id="c1",
                        source_type="company",
                        content="CEO 正在处理 w-1",
                        priority=4,
                        token_estimate=10,
                    )
                ]

        registry = ContextSourceRegistry()
        registry.register("company", _FixedSource())
        pipeline = PerceptionPipeline(context_registry=registry, context_planner=ContextPlanner())
        result = await pipeline.process("你好", session_id="s-test")
        first = "\n".join(str(message.content or "") for message in result.messages)
        assert "公司上下文" in first, f"company 类型 item 必须渲染进请求: {first[:200]}"
        assert "CEO 正在处理 w-1" in first


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_company_runtime_injects_context(home: Any) -> None:
    """生产主链路：run_agent_turn 传 company_runtime → LLM 请求 system 区"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.message import LLMResponse
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
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
            user_input="汇报一下团队状态",
            session_id=sid,
            session_store=store,
            company_runtime=_StubRuntime([_member("ceo", "running", focused="w-1")]),
        )
    assert seen, "LLM 必须被调用"
    injected_content = "\n".join(str(m.get("content") or "") for m in seen[0])
    assert "公司上下文" in injected_content, (
        f"company_runtime 注入必须在 LLM 请求体现: {injected_content[:300]}"
    )
    store.close()
