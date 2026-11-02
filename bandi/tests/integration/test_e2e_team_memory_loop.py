# 功能：E2E 冒烟 — 真实 seat 链：员工工作项 → 反思门控 → 提名暂存 → 确认落库团队分区
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bandi.core.account import DEFAULT_LOCAL_USER_ID
from bandi.core.message import LLMResponse, ToolCall
from bandi.llm.base import BaseProvider
from bandi.llm.mock import MockProvider
from bandi.memory.episodic import EpisodicStore

_PROJECT_ID = "proj-e2e"
_TEAM_ID = "team-a"
_ROLE_ID = "dba"


class _ScriptedProvider(BaseProvider):
    """脚本化 provider——`isinstance(..., MockProvider)=False`，工厂因此装配真实
    Verifier（judge 真调用）与 provider_registry；judge 请求按 system 提示词识别
    并按 `judge_actions` 序列返回 verdict，执行请求走 Mock 队列。"""

    def __init__(self, judge_actions: list[str] | None = None) -> None:
        from bandi.core.config import LLMConfig

        super().__init__(LLMConfig())
        self._mock = MockProvider()
        self._judge_actions = list(judge_actions or ["pass"])
        self.judge_calls = 0

    def add_response(self, response: LLMResponse) -> None:
        self._mock.add_response(response)

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict] | None = None, **kw: Any
    ) -> LLMResponse:
        from bandi.agent.verifier import VER_JUDGE_SYSTEM_PROMPT

        sys_text = str((messages or [{}])[0].get("content") or "")
        if sys_text.startswith(VER_JUDGE_SYSTEM_PROMPT):
            self.judge_calls += 1
            action = self._judge_actions.pop(0) if self._judge_actions else "pass"
            return LLMResponse(
                content=json.dumps(
                    {"action": action, "reason": "核对工具结果后判定"}, ensure_ascii=False
                )
            )
        if not self._mock._responses:
            return LLMResponse(content="任务完成。")  # 队列兜底防空转
        return await self._mock.complete(messages, tools=tools, **kw)

    def stream(
        self, messages: list[dict[str, Any]], tools: list[dict] | None = None, **kw: Any
    ) -> Any:
        return self._mock.stream(messages, tools=tools, **kw)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # 隔离 ~/.bandi（EventBus 日志 / checkpoint / 层级库 / 成本台账全走 Path.home）。
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    return tmp_path


def _work_item() -> Any:
    from bandi.company.phase import Phase
    from bandi.company.work_item import WorkItem, WorkItemTurnType

    return WorkItem(
        id="wi-1",
        title="优化慢查询",
        description="定位并优化慢 SQL",
        phase=Phase.RUNNING,
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role=_ROLE_ID,
        parent_id=None,
        children_ids=[],
        links={},
        metadata={},
        runtime_metadata={},
    )


async def _run_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _ScriptedProvider,
    *,
    user_input: str,
    with_work_item: bool,
) -> dict[str, Any]:
    """走生产装配：run_agent_turn + 真实 registry/loop/verifier/提名钩子。

    两处脚本化接入（均为生产唯一入口）：
    ① `build_provider` —— ctx.provider 与 Verifier.provider 的来源，脚本化
       provider 非 MockProvider，工厂因此装配真 Verifier（judge 真调用）；
    ② `deps.make_default_deps` —— Loop 的 LLM 效果边界（deps.py docstring 指定的
       测试接缝）。生产 `call_model` 走 capability/fallback 策略层自建 provider
       与 ctx.provider 无关，故必须在此打桩，否则整链回真实 API。
    """
    import bandi.memory.episodic as epi_mod
    from bandi.agent import deps as deps_mod
    from bandi.company.company_runtime import CompanyRuntime
    from bandi.company.org_engine import OrgEngine
    from bandi.core import agent_runner as runner_mod
    from bandi.core.agent_runner import _build_memory_manager, run_agent_turn
    from bandi.core.config import BandiConfig
    from bandi.memory import staging as staging_mod
    from bandi.memory.session.store import SessionStore

    staging_dir = tmp_path / "pending"
    monkeypatch.setattr(staging_mod, "default_memory_store_dir", lambda: staging_dir)
    epi = EpisodicStore(str(tmp_path / "epi.db"))
    monkeypatch.setattr(epi_mod, "get_shared_episodic", lambda: epi)

    monkeypatch.setattr(runner_mod, "build_provider", lambda _cfg: provider)

    async def _scripted_call_model(ctx: Any, messages: list[dict[str, Any]]) -> LLMResponse:
        return await provider.complete(messages, tools=getattr(ctx, "active_tools", None))

    _base_deps = deps_mod.make_default_deps()
    monkeypatch.setattr(
        deps_mod,
        "make_default_deps",
        lambda: deps_mod.QueryDeps(
            call_model=_scripted_call_model,
            autocompact=_base_deps.autocompact,
            microcompact=_base_deps.microcompact,
            new_id=_base_deps.new_id,
        ),
    )

    bandi_config = BandiConfig()
    bandi_config.verification.min_todos_for_verification = 1

    org = OrgEngine()
    org.register_role(_ROLE_ID, "数据库工程师", responsibility="SQL 优化", team_id=_TEAM_ID)
    runtime = CompanyRuntime(
        project_id=_PROJECT_ID, session_scope_id="s-e2e", role_id=_ROLE_ID, employee_id="emp-1"
    )
    store = SessionStore(str(tmp_path / "sess.db"))
    sid = store.create_session(title="e2e")

    result = await run_agent_turn(
        user_input=user_input,
        bandi_config=bandi_config,
        mode="execute",
        session_id=sid,
        session_store=store,
        project_id=_PROJECT_ID,
        employee_id="emp-1",
        agent_role=_ROLE_ID,
        company_runtime=runtime,
        company_org_engine=org,
        memory_manager=_build_memory_manager(tmp_path / "memdb", project_id=_PROJECT_ID),
        work_item=_work_item() if with_work_item else None,
    )
    store.close()
    return {"staging_dir": staging_dir, "epi": epi, "result": result}


def _pending(staging_dir: Path) -> list[Any]:
    from bandi.memory.team_staging import TeamMemoryStaging

    return TeamMemoryStaging(staging_dir).list_pending()


@pytest.mark.asyncio
async def test_tool_turn_judged_nominated_and_confirmed_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E 主链：工具轮（3 次 memory_search）→ judge 判 rework → 返工后 pass →
    教训提名暂存 → 用户确认 → 团队分区可查回。断言最终产物（暂存内容 + episodic 读回）。"""
    provider = _ScriptedProvider(judge_actions=["rework", "pass"])
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="c1", name="memory_search", arguments={"query": "执行计划"}),
                ToolCall(id="c2", name="memory_search", arguments={"query": "索引优化"}),
                ToolCall(id="c3", name="memory_search", arguments={"query": "慢查询"}),
            ],
        )
    )
    provider.add_response(LLMResponse(content="已检索执行计划口径，任务完成。"))
    provider.add_response(LLMResponse(content="补充索引建议后完成。"))

    ctx_out = await _run_turn(
        tmp_path,
        monkeypatch,
        provider,
        user_input="请用 memory_search 工具搜索'执行计划'，然后总结你查到的内容",
        with_work_item=True,
    )

    # 断言 1：judge 真被调用（工具证据达阈值 → 走 LLM-as-Judge，非跳过）
    assert provider.judge_calls >= 1, "工具轮必须真调 judge，否则验证闸门形同虚设"
    # 断言 2：返工结论进暂存，维度齐全（pass 轮不重复提名）
    pending = _pending(ctx_out["staging_dir"])
    assert len(pending) == 1, f"返工轮反思后暂存必须出现提名: {pending}"
    payload = pending[0].payload
    assert payload["team_id"] == _TEAM_ID and payload["project_id"] == _PROJECT_ID
    assert payload["employee_id"] == "emp-1"
    assert "rework" in pending[0].summary

    # 断言 3：用户确认 → 落库团队分区（最终产物读回）
    from bandi.memory.team_staging import TeamMemoryStaging

    confirmed = TeamMemoryStaging(ctx_out["staging_dir"]).confirm(
        pending[0].id8, confirmer=DEFAULT_LOCAL_USER_ID
    )
    assert confirmed["team_id"] == _TEAM_ID
    from bandi.memory.episodic import Episode

    ctx_out["epi"].record(
        Episode(
            summary=confirmed["summary"],
            project_id=confirmed["project_id"],
            team_id=confirmed["team_id"],
        )
    )
    hits = ctx_out["epi"].list_recent(project_id=_PROJECT_ID, team_id=_TEAM_ID)
    assert len(hits) == 1 and "rework" in hits[0].summary


@pytest.mark.asyncio
async def test_pure_text_work_turn_not_nominated_without_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """纯文本工作轮（模型不调工具）→ 不跑 judge（零 token）且不提名
    （显著性门控：仅返工沉淀）；非工作轮同一路径零提名。"""
    provider = _ScriptedProvider()
    provider.add_response(LLMResponse(content="慢 SQL 主要是缺索引，建议加复合索引。"))

    ctx_out = await _run_turn(
        tmp_path,
        monkeypatch,
        provider,
        user_input="这个慢查询你怎么看？",
        with_work_item=True,
    )

    assert provider.judge_calls == 0, "纯文本轮无工具证据，不应花 judge 的钱"
    pending = _pending(ctx_out["staging_dir"])
    assert len(pending) == 0, f"纯文本轮不满足显著性门控，不应提名: {pending}"
