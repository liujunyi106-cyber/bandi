# 功能：契约 — 团队记忆注入（Phase3：episodic team 分区 → metadata → system prompt Team 层）
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from bandi.company.org_engine import OrgEngine
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.context.sources.company_source import (
    ROLE_IDENTITY_CONTEXT_KEY,
    TEAM_MEMORY_CONTEXT_KEY,
)
from bandi.memory.episodic import Episode, EpisodicStore


def _org_with_team(team_id: str) -> OrgEngine:
    org = OrgEngine()
    org.register_role(
        "db_engineer", "数据库工程师", responsibility="负责 SQL 优化", team_id=team_id
    )
    return org


def _store_with_team_memory() -> EpisodicStore:
    store = EpisodicStore(":memory:")
    store.record(
        Episode(summary="本团队约定：慢查询必须先看执行计划", project_id="proj-1", team_id="team-a")
    )
    store.record(Episode(summary="他队经验：不要引用", project_id="proj-1", team_id="team-b"))
    return store


def _runtime(org: OrgEngine, project_id: str = "proj-1") -> Any:
    from bandi.company.company_runtime import CompanyRuntime

    return CompanyRuntime(
        project_id=project_id, session_scope_id="s", role_id="db_engineer", employee_id="e1"
    )


def _executor(org: OrgEngine, store: EpisodicStore | None) -> Any:
    from bandi.company.seat_executor import EngineSeatExecutor

    return EngineSeatExecutor(org_engine=org, team_memory_store=store)


def _run_turn(executor: Any, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from bandi.company import seat_executor as seat_mod

    async def fake_run_agent(user_input: str, **kwargs: Any) -> tuple[str, None, int, int]:
        return "done", None, 0, 0

    monkeypatch.setattr(seat_mod, "run_agent", fake_run_agent)
    asyncio.run(
        executor.run_turn(
            runtime, create_work_item(title="优化查询", turn_type=WorkItemTurnType.EXECUTE)
        )
    )


class TestSeatTeamMemoryWiring:
    """生产装配路径：真实 episodic + 真实 seat_executor（LLM 边界 stub）。"""

    def test_run_turn_stashes_team_memory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        org = _org_with_team("team-a")
        executor = _executor(org, _store_with_team_memory())
        runtime = _runtime(org)
        _run_turn(executor, runtime, monkeypatch)

        digest = runtime.metadata(TEAM_MEMORY_CONTEXT_KEY)
        assert digest, "run_turn 必须把团队记忆摘要写入 metadata"
        assert "慢查询必须先看执行计划" in str(digest), "本团队记忆必须入摘要"
        assert "不要引用" not in str(digest), "他团队记忆不得入摘要（分区隔离）"
        assert "【团队记忆】" in str(digest), "必须带来源标签"

    def test_role_without_team_no_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        org = OrgEngine()  # db_engineer 未注册（无 team_id）
        executor = _executor(org, _store_with_team_memory())
        runtime = _runtime(org)
        _run_turn(executor, runtime, monkeypatch)
        assert not runtime.metadata(TEAM_MEMORY_CONTEXT_KEY)

    def test_empty_team_partition_no_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        org = _org_with_team("team-a")
        empty_store = EpisodicStore(":memory:")
        executor = _executor(org, empty_store)
        runtime = _runtime(org)
        _run_turn(executor, runtime, monkeypatch)
        assert not runtime.metadata(TEAM_MEMORY_CONTEXT_KEY), "空团队分区不得硬造摘要"

    def test_episodic_crash_fail_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """破坏者：episodic 查询崩溃——摘要丢弃，主链路不得被拖死。"""

        class _BoomStore:
            def list_recent(self, **kwargs: Any) -> list[Any]:
                raise RuntimeError("boom")

        org = _org_with_team("team-a")
        executor = _executor(org, _BoomStore())
        runtime = _runtime(org)
        _run_turn(executor, runtime, monkeypatch)
        assert not runtime.metadata(TEAM_MEMORY_CONTEXT_KEY)


class TestCompanySourceRelayAndOrder:
    """转发 + 注入序：Team 层在 Role 身份段之前（Org→Team→…→Role）。"""

    @pytest.mark.asyncio
    async def test_team_memory_before_identity(self) -> None:
        from bandi.company.company_runtime import CompanyRuntime
        from bandi.context.sources.company_source import CompanyContextSource
        from bandi.core.message import Message

        runtime = CompanyRuntime(
            project_id="p", session_scope_id="s", role_id="db_engineer", employee_id="e1"
        )
        runtime.set_metadata(TEAM_MEMORY_CONTEXT_KEY, "【团队记忆】\n- 团队经验 A")
        runtime.set_metadata(ROLE_IDENTITY_CONTEXT_KEY, "职责：SQL 优化")
        source = CompanyContextSource(company_runtime=runtime)
        items = await source.collect(Message(role="user", content="hi"), session_id="s-1")
        contents = [i.content for i in items]
        team_idx = next(i for i, c in enumerate(contents) if "团队记忆" in c)
        idy_idx = next(i for i, c in enumerate(contents) if "SQL 优化" in c)
        assert team_idx < idy_idx, f"Team 层必须先于 Role 身份段: {contents}"
        assert items[team_idx].priority > items[idy_idx].priority

    @pytest.mark.asyncio
    async def test_no_team_key_no_item(self) -> None:
        from bandi.company.company_runtime import CompanyRuntime
        from bandi.context.sources.company_source import CompanyContextSource
        from bandi.core.message import Message

        runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
        source = CompanyContextSource(company_runtime=runtime)
        items = await source.collect(Message(role="user", content="hi"), session_id="s-1")
        assert all("团队记忆" not in i.content for i in items), "无团队记忆不得硬造"


class TestSystemPromptE2E:
    """端到端：MockProvider 录制断言团队记忆进 system prompt。"""

    @pytest.mark.asyncio
    async def test_team_memory_reaches_system_prompt(self, tmp_path: Path) -> None:
        from bandi.company.company_runtime import CompanyRuntime
        from bandi.core.agent_runner import run_agent_turn
        from bandi.core.message import LLMResponse
        from bandi.llm.mock import MockProvider
        from bandi.memory.session.store import SessionStore

        runtime = CompanyRuntime(
            project_id="p-e2e", session_scope_id="s-e2e", role_id="db_engineer", employee_id="e1"
        )
        runtime.set_metadata(
            TEAM_MEMORY_CONTEXT_KEY, "【团队记忆】\n- 本团队约定：慢查询必须先看执行计划"
        )

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
        with patch_build_provider(provider):
            await run_agent_turn(
                user_input="开始优化",
                session_id=sid,
                session_store=store,
                company_runtime=runtime,
            )
        assert seen, "LLM 必须被调用"
        injected_content = "\n".join(str(m.get("content") or "") for m in seen[0])
        assert "慢查询必须先看执行计划" in injected_content, (
            f"团队记忆必须进 LLM 请求（易变上下文段）: {injected_content[:400]}"
        )
        store.close()


def patch_build_provider(provider: Any) -> Any:
    from unittest.mock import patch

    return patch("bandi.core.agent_runner.build_provider", return_value=provider)
