"""契约（端到端）：扇出 3 张子卡 → 依赖屏障 → 父卡综合轮汇总；真实 store/调度器/执行器，仅替换 Provider。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.discussion_thread import ThreadManager
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase, is_terminal
from bandi.company.recruiter import Recruiter
from bandi.company.seat_executor import EngineSeatExecutor
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItem, WorkItemTurnType, create_work_item
from bandi.core.message import LLMResponse, ToolCall
from bandi.llm.mock import MockProvider
from bandi.llm.streaming import StreamEvent, StreamEventType

_PARENT_ROLE = "pm"
_PARENT_TITLE = "扇出父卡：发布准备三路并行交付"
# 只出现在父卡自述里，用于把「父卡两轮（派单/收口）」与子卡轮区分开
_PLAN_MARKER = "PLAN-DISPATCH-MARKER-Q7"
_PLAN_OUTPUT = "计划已派发：writer / designer / analyst 三路并行"
_SUMMARY_PREFIX = "综合汇总："
_ROLES: tuple[tuple[str, str], ...] = (
    ("writer", "文案"),
    ("designer", "视觉"),
    ("analyst", "数据分析"),
)
_CHILD_PROMPTS = {
    "writer": "子卡甲：产出发布文案要点",
    "designer": "子卡乙：产出视觉稿要点",
    "analyst": "子卡丙：产出数据口径要点",
}
_ARTIFACTS = {
    "writer": "DELIVERABLE-WRITER-V1",
    "designer": "DELIVERABLE-DESIGNER-V1",
    "analyst": "DELIVERABLE-ANALYST-V1",
}
_DESIGNER_SCOPE_KEY = "designer-card"


def _prompt_text(messages: list[dict[str, Any]]) -> str:
    """把消息列表拍平成提示词全文（用于按内容路由脚本响应）。"""
    parts: list[str] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, dict) and isinstance(piece.get("text"), str):
                    parts.append(piece["text"])
    return "\n".join(parts)


def _events_for(response: LLMResponse) -> list[StreamEvent]:
    """把一条预设响应转成流式事件序列（与 MockProvider 同一事件口径）。"""
    events: list[StreamEvent] = []
    if response.content:
        events.append(
            StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta=response.content)
        )
    for call in response.tool_calls or []:
        events.append(
            StreamEvent(
                type=StreamEventType.TOOL_CALL_START,
                tool_call_id=call.id,
                tool_name=call.name,
            )
        )
        events.append(
            StreamEvent(
                type=StreamEventType.TOOL_CALL_DELTA,
                tool_call_id=call.id,
                arguments_delta=json.dumps(call.arguments, ensure_ascii=False),
            )
        )
        events.append(StreamEvent(type=StreamEventType.TOOL_CALL_END, tool_call_id=call.id))
    events.append(
        StreamEvent(
            type=StreamEventType.DONE,
            finish_reason=response.finish_reason or "stop",
            usage=response.usage,
        )
    )
    return events


class _FanOutScriptProvider(MockProvider):
    """脚本化 Provider：按提示词内容路由固定响应，并记录调用序列与并发区间。"""

    def __init__(
        self,
        spawn_arguments: dict[str, Any],
        *,
        claim_sampler: Any = None,
        latency: float = 0.02,
    ) -> None:
        super().__init__()
        self._spawn_arguments = spawn_arguments
        self._claim_sampler = claim_sampler
        self._latency = latency
        self.tick = 0
        self.calls: list[dict[str, Any]] = []
        self.inflight = 0
        self.max_inflight = 0
        self.max_live_claims = 0
        self._plan_calls = 0

    # -- 路由 --

    def _route(self, prompt: str) -> str:
        # 功能：按提示词内容判定本次调用属于哪张卡的哪一轮
        if _PLAN_MARKER in prompt and "## 子任务结果" in prompt:
            return "parent:synthesis"
        if _PLAN_MARKER in prompt:
            self._plan_calls += 1
            return "parent:plan" if self._plan_calls == 1 else "parent:plan_close"
        matched = [role for role, marker in _CHILD_PROMPTS.items() if marker in prompt]
        if matched:
            # 功能：依赖注入块排在本卡任务之前，故「最后出现」的子卡自述即本卡
            return f"child:{max(matched, key=lambda role: prompt.rfind(_CHILD_PROMPTS[role]))}"
        return "unrouted"

    def _response(self, route: str, prompt: str) -> LLMResponse:
        # 功能：把路由结果映射成预设响应
        if route == "parent:plan":
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="tc-fanout-1",
                        name="spawn_thread_task",
                        arguments=self._spawn_arguments,
                    )
                ],
                finish_reason="tool_calls",
            )
        if route == "parent:plan_close":
            return LLMResponse(content=_PLAN_OUTPUT, finish_reason="stop")
        if route == "parent:synthesis":
            found = [artifact for artifact in _ARTIFACTS.values() if artifact in prompt]
            return LLMResponse(content=_SUMMARY_PREFIX + "、".join(found), finish_reason="stop")
        if route.startswith("child:"):
            return LLMResponse(content=_ARTIFACTS[route.split(":", 1)[1]], finish_reason="stop")
        return LLMResponse(content="未匹配到脚本响应", finish_reason="stop")

    # -- 观测 --

    @contextlib.contextmanager
    def _observe(self, route: str, prompt: str):
        # 功能：记录单次模型调用的并发区间与调用时在跑的卡数
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        if self._claim_sampler is not None:
            self.max_live_claims = max(self.max_live_claims, int(self._claim_sampler() or 0))
        self.calls.append({"tick": self.tick, "route": route, "prompt": prompt})
        try:
            yield
        finally:
            self.inflight -= 1

    async def stream(self, messages, tools=None, **kwargs):
        prompt = _prompt_text(messages)
        route = self._route(prompt)
        with self._observe(route, prompt):
            if self._latency:
                await asyncio.sleep(self._latency)
            for event in _events_for(self._response(route, prompt)):
                yield event

    async def complete(self, messages, tools=None, **kwargs):
        prompt = _prompt_text(messages)
        route = self._route(prompt)
        with self._observe(route, prompt):
            if self._latency:
                await asyncio.sleep(self._latency)
            return self._response(route, prompt)

    # -- 查询 --

    def routes_for_tick(self, tick: int) -> list[str]:
        return [call["route"] for call in self.calls if call["tick"] == tick]

    def prompts_for(self, route: str) -> list[str]:
        return [call["prompt"] for call in self.calls if call["route"] == route]


def _fan_out_arguments(*, depend_on: str = "") -> dict[str, Any]:
    """构造一次扇出 3 张子卡的 spawn_thread_task 参数。"""
    tasks: list[dict[str, Any]] = [
        {
            "target_role": "writer",
            "task_prompt": _CHILD_PROMPTS["writer"],
            "deliverables": ["发布文案要点"],
        },
        {
            "target_role": "designer",
            "task_prompt": _CHILD_PROMPTS["designer"],
            "scope_key": _DESIGNER_SCOPE_KEY,
            "deliverables": ["视觉稿要点"],
        },
        {
            "target_role": "analyst",
            "task_prompt": _CHILD_PROMPTS["analyst"],
            "deliverables": ["数据口径要点"],
        },
    ]
    if depend_on:
        tasks[2]["depends_on"] = [depend_on]
    return {
        "tasks": tasks,
        "sequence": False,
        "upstream_intent_summary": "发布准备三路并行交付",
    }


class _Fleet:
    """一次端到端装配：真实 store / 调度器 / 执行器 + 脚本化 Provider。"""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        max_concurrent: int = 2,
    ) -> None:
        self._monkeypatch = monkeypatch
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        from bandi.tools import fs_tool

        monkeypatch.setattr(fs_tool, "_WORKSPACE_ROOT", self.workspace)

        self.store = WorkItemStore(str(tmp_path / "company.db"))
        self.org = OrgEngine()
        self.org.register_role(
            _PARENT_ROLE,
            "产品经理",
            "拆解需求并派单",
            can_spawn=[role_id for role_id, _ in _ROLES],
        )
        for role_id, name in _ROLES:
            self.org.register_role(role_id, name, f"{name}交付")
        self.recruiter = Recruiter()
        for role_id in [_PARENT_ROLE, *[role_id for role_id, _ in _ROLES]]:
            self.recruiter.register_seat(role_id)
        self.threads = ThreadManager(
            workspace=str(tmp_path / "comms"),
            project_id="proj-fanout",
            session_id="sess-fanout",
        )
        self.executor = EngineSeatExecutor(
            store=self.store,
            org_engine=self.org,
            thread_manager=self.threads,
        )
        self.dispatcher = CompanyDispatcher(
            store=self.store,
            recruiter=self.recruiter,
            org_engine=self.org,
            gate_harness=GateHarness(),
            seat_executor=self.executor,
            config=DispatcherConfig(max_concurrent=max_concurrent, auto_approve_reviews=True),
            thread_manager=self.threads,
        )
        self.dispatcher.session_writer = lambda *args, **kwargs: None
        self.provider: _FanOutScriptProvider | None = None
        self.tick = 0
        self.history: list[dict[str, Any]] = []
        self.initial_phases: dict[str, Phase] = {}

    def attach_provider(self, provider: _FanOutScriptProvider) -> None:
        # 功能：把脚本化 Provider 装到真实执行链的构建点
        self.provider = provider
        import bandi.core.agent_runner as runner

        self._monkeypatch.setattr(runner, "build_provider", lambda *_a, **_k: provider)

    def live_claim_count(self) -> int:
        # 功能：当前持有执行租约的卡数（真实执行中的卡）
        return sum(
            1 for item in self.store.list_all() if (item.metadata or {}).get("execution_claim")
        )

    def create_parent(self) -> WorkItem:
        parent = create_work_item(
            title=_PARENT_TITLE,
            turn_type=WorkItemTurnType.PLAN,
            description=f"{_PLAN_MARKER}｜把发布准备拆成文案、视觉、数据三路并行交付",
            assignee_role=_PARENT_ROLE,
            phase=Phase.QUEUED,
            metadata={
                "origin_session_id": "sess-fanout",
                "project_id": "proj-fanout",
                "round_id": "round-fanout",
                "allowed_delegate_role_ids": [role_id for role_id, _ in _ROLES],
            },
        )
        self.store.save(parent)
        return parent

    def children_of(self, parent_id: str) -> list[WorkItem]:
        return [item for item in self.store.list_all() if item.parent_id == parent_id]

    def run_ticks(self, count: int) -> None:
        for _ in range(count):
            if not self.history:
                self.initial_phases = {item.id: item.phase for item in self.store.list_all()}
            self.tick += 1
            assert self.provider is not None
            self.provider.tick = self.tick
            asyncio.run(self.dispatcher.tick())
            self.history.append(self._snapshot())

    def _snapshot(self) -> dict[str, Any]:
        assert self.provider is not None
        return {
            "tick": self.tick,
            "phases": {item.id: item.phase for item in self.store.list_all()},
            "routes": self.provider.routes_for_tick(self.tick),
        }

    def phases_before_tick(self, tick: int) -> dict[str, Phase]:
        # 功能：某一轮开始前的相位快照（第 1 轮取初始相位）
        if tick <= 1:
            return dict(self.initial_phases)
        return dict(self.history[tick - 2]["phases"])

    def first_tick_of_route(self, route: str) -> int | None:
        for snapshot in self.history:
            if route in snapshot["routes"]:
                return int(snapshot["tick"])
        return None


def _build_fleet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_concurrent: int = 2,
    depend_on: str = "",
) -> tuple[_Fleet, _FanOutScriptProvider, WorkItem]:
    fleet = _Fleet(tmp_path, monkeypatch, max_concurrent=max_concurrent)
    provider = _FanOutScriptProvider(
        _fan_out_arguments(depend_on=depend_on),
        claim_sampler=fleet.live_claim_count,
    )
    fleet.attach_provider(provider)
    parent = fleet.create_parent()
    return fleet, provider, parent


def test_fanout_spawns_three_children_and_caps_parallel_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """扇出：父卡一次派生 3 张子卡，同一 tick 内并行执行且受 max_concurrent 钳制。"""
    fleet, provider, parent = _build_fleet(tmp_path, monkeypatch, max_concurrent=2)

    fleet.run_ticks(1)
    children = fleet.children_of(parent.id)
    assert len(children) == 3, f"父卡一次扇出必须恰好派生 3 张子卡，实际 {len(children)}"
    assert {child.assignee_role for child in children} == {role_id for role_id, _ in _ROLES}, (
        "3 张子卡必须分派给 3 个不同承接员工"
    )
    assert len({child.metadata.get("thread_id") for child in children}) == 1, (
        "一次扇出的子卡必须共享同一条对话链"
    )
    waiting_parent = fleet.store.get(parent.id)
    assert waiting_parent is not None
    assert waiting_parent.phase == Phase.WAITING_FOR_CHILDREN, (
        f"派发子卡后父卡必须挂起等子卡，实际 {waiting_parent.phase}"
    )
    assert set(waiting_parent.metadata.get("dependency_work_item_ids") or []) == {
        child.id for child in children
    }, "父卡必须登记全部子卡的依赖（屏障）"

    fleet.run_ticks(1)
    routes = provider.routes_for_tick(2)
    assert sorted(route for route in routes if route.startswith("child:")) == [
        "child:analyst",
        "child:designer",
        "child:writer",
    ], f"同一 tick 内 3 张子卡都必须被 claim 执行，实际调用 {routes}"
    assert provider.max_inflight == 2, (
        f"并发上限 2 时必须真的并行且不超过 2，实际峰值 {provider.max_inflight}"
    )
    # 卡在执行结束后仍停在 RUNNING 相位等下一轮 gate，故并发度按「执行中持租约」度量
    assert provider.max_live_claims <= 2, (
        f"同时持有执行租约的卡数必须 ≤ max_concurrent，实际 {provider.max_live_claims}"
    )

    fleet.run_ticks(3)
    final_children = fleet.children_of(parent.id)
    assert all(child.phase == Phase.APPROVED for child in final_children), (
        f"3 张子卡最终都必须完成，实际 {[(c.assignee_role, c.phase) for c in final_children]}"
    )
    for role_id, artifact in _ARTIFACTS.items():
        child = next(c for c in final_children if c.assignee_role == role_id)
        assert str((child.runtime_metadata or {}).get("_last_output") or "").strip() == artifact


def test_dependency_barrier_blocks_dispatch_until_upstream_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """依赖屏障：被依赖卡未完成时依赖卡不得派发，完成后下一 tick 自动释放。"""
    fleet, provider, parent = _build_fleet(
        tmp_path, monkeypatch, max_concurrent=2, depend_on=_DESIGNER_SCOPE_KEY
    )

    fleet.run_ticks(1)
    children = {child.assignee_role: child for child in fleet.children_of(parent.id)}
    designer, analyst = children["designer"], children["analyst"]
    assert analyst.phase == Phase.WAITING_DEPENDENCIES, (
        f"带 depends_on 的子卡必须以等待依赖态起步，实际 {analyst.phase}"
    )
    assert analyst.metadata.get("dependency_work_item_ids") == [designer.id], (
        "依赖字段必须指向被依赖的子卡"
    )

    fleet.run_ticks(1)
    assert provider.routes_for_tick(2).count("child:analyst") == 0, (
        "被依赖卡未完成时依赖卡不得被派发"
    )
    pending = fleet.store.get(analyst.id)
    assert pending is not None and pending.phase == Phase.WAITING_DEPENDENCIES
    assert pending.metadata.get("waiting_on_work_item_ids") == [designer.id], (
        f"等待中的卡必须记录当前阻塞来源，实际 {pending.metadata.get('waiting_on_work_item_ids')}"
    )

    fleet.run_ticks(6)
    designer_done_tick = next(
        tick
        for tick in range(1, len(fleet.history) + 1)
        if fleet.history[tick - 1]["phases"].get(designer.id) == Phase.APPROVED
    )
    analyst_tick = fleet.first_tick_of_route("child:analyst")
    assert analyst_tick is not None, "被依赖卡完成后依赖卡必须被派发"
    assert analyst_tick == designer_done_tick + 1, (
        f"依赖卡必须在被依赖卡完成后的下一 tick 自动释放，"
        f"实际 依赖完成于 tick{designer_done_tick}、执行于 tick{analyst_tick}"
    )
    released = fleet.store.get(analyst.id)
    assert released is not None
    assert released.metadata.get("waiting_on_work_item_ids") == [], (
        f"释放后不得残留阻塞来源，实际 {released.metadata.get('waiting_on_work_item_ids')}"
    )
    assert released.metadata.get("dependency_work_item_ids") == [designer.id], (
        "依赖字段必须保留（审计/重放依据），只清阻塞来源"
    )
    assert released.phase == Phase.APPROVED
    assert provider.prompts_for("child:analyst"), "依赖卡必须真的执行过一轮"


def test_join_synthesis_contains_all_children_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """join：3 张子卡全部完成后父卡进入综合轮，产出包含三张子卡的关键产物标识。"""
    fleet, provider, parent = _build_fleet(tmp_path, monkeypatch, max_concurrent=2)

    fleet.run_ticks(1)
    child_ids = {child.id for child in fleet.children_of(parent.id)}
    assert len(child_ids) == 3

    for _ in range(8):
        fleet.run_ticks(1)
        current = fleet.store.get(parent.id)
        assert current is not None
        children = fleet.children_of(parent.id)
        if not all(is_terminal(child.phase) for child in children):
            assert current.phase == Phase.WAITING_FOR_CHILDREN, (
                f"子卡未全部完成时父卡不得进入综合轮，实际 {current.phase} "
                f"（子卡 {[(c.assignee_role, c.phase.value) for c in children]}）"
            )
        if current.phase == Phase.APPROVED:
            break

    final = fleet.store.get(parent.id)
    assert final is not None
    assert final.phase == Phase.APPROVED, f"父卡综合轮必须收敛到终态，实际 {final.phase}"
    assert final.metadata.get("synthesis_turn_started") is True, (
        f"父卡必须走过综合轮，实际 metadata={final.metadata.get('synthesis_turn_started')}"
    )
    assert set(final.metadata.get("synthesis_source_work_item_ids") or []) == child_ids, (
        "综合轮必须登记全部子卡为扇入来源"
    )
    assert final.metadata.get("delegation_turn_kind") == "synthesize"

    synthesis_tick = fleet.first_tick_of_route("parent:synthesis")
    assert synthesis_tick is not None, "父卡必须真的执行过综合轮（模型被调用）"
    phases_before = fleet.phases_before_tick(synthesis_tick)
    assert all(phases_before.get(child_id) == Phase.APPROVED for child_id in child_ids), (
        f"综合轮必须在子卡全部完成之后才开始，实际开始前相位 {phases_before}"
    )
    assert provider.routes_for_tick(synthesis_tick).count("parent:synthesis") == 1, (
        "综合轮必须只跑一轮"
    )

    synthesis_prompt = provider.prompts_for("parent:synthesis")[0]
    last_output = str((final.runtime_metadata or {}).get("_last_output") or "")
    for role_id, artifact in _ARTIFACTS.items():
        assert artifact in synthesis_prompt, (
            f"综合轮提示词必须注入 {role_id} 的产物，实际缺失 {artifact}"
        )
        assert artifact in last_output, (
            f"父卡汇总产出必须含 {role_id} 的关键产物标识 {artifact}，实际 {last_output[:160]!r}"
        )
    assert _SUMMARY_PREFIX in last_output, "父卡汇总产出必须是综合轮的新产出"
    assert _PLAN_OUTPUT not in last_output, "父卡最终产出不得残留计划轮旧文本"
