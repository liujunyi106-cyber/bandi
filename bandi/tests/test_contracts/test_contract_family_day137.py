from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.company_runtime import CompanyRuntime
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import InvalidPhaseTransitionError, Phase
from bandi.company.seat_executor import EngineSeatExecutor
from bandi.company.seat_progress import EmployeeProgressHandler
from bandi.company.seat_recruiter import Recruiter
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.core.agent_runner import _persist_memory
from bandi.core.config import BandiConfig, LLMConfig
from bandi.core.event_bus import EventBus, reset_event_context, set_event_context
from bandi.core.events import Event, EventType
from bandi.core.message import ToolCall
from bandi.llm.profiles import (
    LlmProfile,
    ProfilesFileError,
    ProfileStore,
    active_profile_summary,
    mask_api_key,
    reset_store_singleton,
    resolve_llm_config,
)
from bandi.tools.base import BaseTool, ToolPermissionContext
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry

# 功能：契约——公司员工执行的记忆维度必须跟随「工作项的项目归属」
#
# ① `company_mode._build_runtime` 取 `self.communication.project_id`，而生产调度器
# 的 CommunicationManager 硬编码 `project_id="company"`（app.py 装配）⇒ 员工执行
# 写入的记忆 project_id 恒为 "company"，真实项目归属（`work_item.metadata
# ["project_id"]`，由 company_chat 写入）被丢弃 ⇒ 跨项目混桶（A 项目员工可召回
# ② `agent_runner._persist_memory` 的 episode_summary 取 `raw_input` 前 200 字
# 而员工链路的 raw_input 以 `## Company Work Item Contract` 契约模板开头
# （seat_executor 把 contract/output_ctx 拼在最前）⇒ 不同员工/不同任务的经历
# 摘要几乎逐字相同，无任务标题、无产出 ⇒ FTS 命中即噪声。
#
# 本契约钉行为：① runtime 项目维度来自工作项元数据（回退 communication，再回退
# "default"）；② 公司派单的经历记忆摘要必须携带任务标题。


@pytest.fixture
def store(tmp_path):
    return WorkItemStore(str(tmp_path / "dispatch.db"))


def _build_dispatcher(store: WorkItemStore, communication: Any = None) -> CompanyDispatcher:
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    return CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=EngineSeatExecutor(),
        config=DispatcherConfig(max_concurrent=3),
        communication=communication,
    )


class TestRuntimeProjectScope:
    """员工执行的 runtime 项目维度必须跟随工作项归属。"""

    def test_runtime_project_scope_follows_work_item_with_fallbacks(
        self, store: WorkItemStore
    ) -> None:
        """核心：work_item.metadata["project_id"] 成为 runtime.project_id；无归属时依次回退
        communication.project_id 与 "default"，不得空串/异常。"""
        dispatcher = _build_dispatcher(store)
        item = create_work_item(
            title="核对价格带",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            metadata={"project_id": "p-e2e-137"},
        )
        store.save(item)
        assert dispatcher.claim("worker")

        with patch.object(dispatcher, "_build_runtime", wraps=dispatcher._build_runtime) as spy:
            asyncio.run(dispatcher.run_work_item(item.id))

        seen = [kwargs.get("project_id") for _, kwargs in spy.call_args_list]
        assert seen, "run_work_item 未调用 _build_runtime（装配路径断了）"
        assert "p-e2e-137" in seen, f"runtime 项目维度未跟随工作项归属：{seen}"

        fallback = _build_dispatcher(store, communication=SimpleNamespace(project_id="company"))
        plain = create_work_item(
            title="无归属任务",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
        )
        store.save(plain)
        assert fallback.claim("worker")

        with patch.object(fallback, "_build_runtime", wraps=fallback._build_runtime) as spy2:
            asyncio.run(fallback.run_work_item(plain.id))

        seen2 = [kwargs.get("project_id") for _, kwargs in spy2.call_args_list]
        assert seen2 and seen2[0] in ("", None), f"无归属时不应伪造项目维度：{seen2}"
        runtimes = [fallback._build_runtime(session_scope_id="probe")]
        assert runtimes[0].project_id == "company"

        bare = _build_dispatcher(store)
        runtime = bare._build_runtime(session_scope_id="probe")
        assert runtime.project_id == "default"


class _RecordingEpisodic:
    """记录写入经历的 episodic 替身。"""

    def __init__(self) -> None:
        self.episodes: list[Any] = []

    def record(self, episode: Any) -> None:
        self.episodes.append(episode)


class _NoopLongTerm:
    """长期层替身（本契约只关心经历通道）。"""

    def store(self, item: Any, **_kwargs: Any) -> None:
        return None

    def check_conflict(self, _item_id: str, _content: str) -> bool:
        return False

    def prune_partitioned(self, **_kwargs: Any) -> None:
        return None


class TestEpisodeSummaryCarriesTask:
    """公司派单的经历记忆必须能区分任务（带标题），不得被契约模板占满。"""

    def test_episode_summary_prefers_work_item_title_then_raw_input(self) -> None:
        """公司派单必须把任务标题写进经历记忆摘要；非公司链路（无 work_item）保持原文行为。"""
        contract = (
            "## Company Work Item Contract\n"
            "- You are executing one projected work item inside company mode, not the whole "
            "project alone.\n"
            "- Stay inside the current work-item boundary and use upstream handoffs.\n"
        )
        episodic = _RecordingEpisodic()
        manager = SimpleNamespace(episodic=episodic, working=None, long_term=_NoopLongTerm())
        ctx = SimpleNamespace(
            raw_input=f"{contract}\n核对价格带",
            turn_index=1,
            terminal_reason=None,
            final_message="已完成核对：主力价格带 99-129，竞品下探至 89。",
            current_content=None,
            tool_results=[],
            messages=[],
            meta={"work_item_title": "核对价格带", "work_item_description": "对比竞品报价"},
        )

        asyncio.run(
            _persist_memory(ctx, manager, "role-session::p::scope::worker::emp", None, "p-1", "emp")
        )

        assert episodic.episodes, "经历通道未写入"
        summary = episodic.episodes[0].summary
        assert "核对价格带" in summary, f"任务标题未进入经历记忆：{summary[:160]}"

        plain = _RecordingEpisodic()
        manager = SimpleNamespace(episodic=plain, working=None, long_term=_NoopLongTerm())
        ctx = SimpleNamespace(
            raw_input="帮我整理部署流程",
            turn_index=1,
            terminal_reason=None,
            final_message="部署流程已整理为五步。",
            current_content=None,
            tool_results=[],
            messages=[],
            meta={},
            work_item=None,
        )

        asyncio.run(_persist_memory(ctx, manager, "sess-1", None, None, None))

        assert plain.episodes, "经历通道未写入"
        assert "帮我整理部署流程" in plain.episodes[0].summary


class TestWorkItemSubjectWiring:
    """接线完整性：run_agent_turn 生产主链路必须把工作项标题送进经历记忆。

    装配链：run_agent_turn(work_item) → ctx.meta["work_item_title"]（ExecutionContext
    无 work_item 字段）→ _persist_memory → episodic.summary。
    """

    @pytest.mark.asyncio
    async def test_run_agent_turn_episode_carries_work_item_title(self, tmp_path: Any) -> None:
        from pathlib import Path

        from bandi.core.agent_runner import run_agent_turn
        from bandi.core.config import BandiConfig
        from bandi.core.message import LLMResponse, TokenUsage
        from bandi.llm.mock import MockProvider
        from bandi.memory.episodic import EpisodicStore
        from bandi.memory.long_term import LongTermMemory
        from bandi.memory.memory_provider import MemoryManager
        from bandi.memory.session.store import SessionStore
        from bandi.memory.working import ScratchpadStore

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        memory = MemoryManager(
            long_term=LongTermMemory(str(tmp_path / "lt.db")),
            episodic=episodic,
            working=ScratchpadStore(str(tmp_path / "wk.db")),
        )
        provider = MockProvider()
        provider.add_response(
            LLMResponse(
                content="价格带核对完成：主力 99-129，竞品下探至 89。",
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=30, completion_tokens=15),
                finish_reason="stop",
            )
        )
        contract = (
            "## Company Work Item Contract\n"
            "- You are executing one projected work item inside company mode, not the whole "
            "project alone.\n"
        )
        work_item = create_work_item(
            title="核对价格带",
            turn_type=WorkItemTurnType.EXECUTE,
            description="对比竞品报价",
            assignee_role="selection-team_market_researcher",
        )

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = SessionStore(str(tmp_path / "sess.db"))
            sid = store.create_session(mode="execute")
            cfg = BandiConfig()
            cfg.llm.api_key = ""
            with patch("bandi.core.agent_runner.build_provider", return_value=provider):
                await run_agent_turn(
                    user_input=f"{contract}\n核对价格带",
                    mode="execute",
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                    memory_manager=memory,
                    project_id="p-e2e-137",
                    employee_id="selection-team_market_researcher",
                    work_item=work_item,
                )

        # 按生产检索维度查（list_recent 无维度时 fail-closed 只查 NULL 分区）
        recent = episodic.list_recent(
            limit=5,
            project_id="p-e2e-137",
            employee_id="selection-team_market_researcher",
        )
        assert recent, "生产链路未按 (project, employee) 维度写入经历记忆"
        titles = [episode.summary or "" for episode in recent]
        assert any("核对价格带" in text for text in titles), (
            f"工作项标题未进入经历记忆（接线断了）：{[t[:90] for t in titles]}"
        )


# ==================== day137_timeline ====================
# 功能：Day137 契约测试——工具时间线数据源（CoT thinking 透传 + tool_input 透传）
# 钉行为族（前端过程块/工具时间线的数据来源，docs/前端设计规划.md §8）：
# ① LLM_STREAM_EVENT(thinking_delta) 经真实 EventBus + _subscribe_handler 转发到
# StreamHandler.on_thinking（CoT 思维链节点数据源）；
# ② thinking 事件按会话归属过滤（其他会话不串流）；
# ④ WS/SSE 两实现发 thinking_delta 帧（text 透传），CLI 实现协议完整；
# ⑤ TOOL_INVOKED 事件 data 携带 tool_input（工具节点输入预览数据源）；
# ⑥ _subscribe_handler 把 tool_input 转给 handler.on_tool_call_start；
# ⑦ WS tool_call_start 帧携带 tool_input。
# 契约只钉行为结果，不钉实现——全部经生产主链路（真实 ToolExecutor + 真实 EventBus
# + 真实 _subscribe_handler）断言最终产物事件/帧。


class R137RecorderHandler:
    """StreamHandler 记录实现（Day137：含 on_thinking）。"""

    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.thinking: list[str] = []
        self.tool_starts: list[tuple[str, dict[str, Any] | None]] = []
        self.skills: list[list[str]] = []
        self.notes: list[str] = []

    async def on_content_delta(self, session_id: str, delta: str) -> None:
        self.deltas.append(delta)

    async def on_thinking(self, session_id: str, delta: str) -> None:
        self.thinking.append(delta)

    async def on_tool_call_start(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        tool_call_id: str = "",
    ) -> None:
        self.tool_starts.append((tool_name, tool_input))

    async def on_tool_result(
        self,
        session_id: str,
        tool_name: str,
        success: bool,
        output_preview: str,
        duration_ms: int,
        tool_call_id: str = "",
    ) -> None: ...

    async def on_tool_progress(
        self, session_id: str, tool_name: str, progress_type: str, data: dict[str, Any]
    ) -> None: ...

    async def on_skill_injected(self, session_id: str, skills: list[str]) -> None:
        self.skills.append(list(skills))

    async def on_note(self, session_id: str, note_type: str) -> None:
        self.notes.append(note_type)

    async def on_done(
        self, session_id: str, terminal_reason: str, total_tokens: int, turn_index: int
    ) -> None: ...

    async def on_error(self, session_id: str, error: str) -> None: ...


class _EchoTool(BaseTool):  # 可实例化的最小工具（携带 arguments 走真实 invocation 路径）
    name = "echo"
    description = "minimal"
    parameters: dict = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        return str(params.get("text", ""))


pytestmark = pytest.mark.asyncio


# ── ①② thinking_delta 经真实 bus + 真实绑定转发（含会话归属过滤）────────────
async def test_thinking_delta_forwarded_with_session_isolation() -> None:
    from bandi.core.agent_runner import _subscribe_handler, _unsubscribe_handler

    bus = EventBus()
    handler = R137RecorderHandler()
    bindings = await _subscribe_handler(bus, handler, "s-1")
    try:
        await bus.emit(
            Event(
                type=EventType.LLM_STREAM_EVENT,
                data={"type": "thinking_delta", "content": "先分析再行动"},
                session_id="s-1",
            )
        )
    finally:
        await _unsubscribe_handler(bus, bindings)

    assert handler.thinking == ["先分析再行动"], "thinking_delta 必须到达 StreamHandler.on_thinking"

    bus = EventBus()
    handler = R137RecorderHandler()
    bindings = await _subscribe_handler(bus, handler, "s-mine")
    try:
        await bus.emit(
            Event(
                type=EventType.LLM_STREAM_EVENT,
                data={"type": "thinking_delta", "content": "别的会话"},
                session_id="s-other",
            )
        )
    finally:
        await _unsubscribe_handler(bus, bindings)

    assert handler.thinking == [], "非本会话 thinking 不得串入前端时间线"


async def test_content_delta_forwarding_unchanged() -> None:
    from bandi.core.agent_runner import _subscribe_handler, _unsubscribe_handler

    bus = EventBus()
    handler = R137RecorderHandler()
    bindings = await _subscribe_handler(bus, handler, "s-1")
    try:
        await bus.emit(
            Event(
                type=EventType.LLM_STREAM_EVENT,
                data={"type": "content_delta", "content": "正文"},
                session_id="s-1",
            )
        )
    finally:
        await _unsubscribe_handler(bus, bindings)

    assert handler.deltas == ["正文"], "content_delta 透传行为不得回归"


# ── ④ WS/SSE/CLI 三实现发 thinking_delta 帧（族级一致）──────────────────────
async def test_all_stream_handlers_emit_thinking_frames() -> None:
    from bandi.api.app import _SSEStreamHandler, _WSStreamHandler
    from bandi.cli.stream_handler import CLIStreamHandler

    sent: list[dict[str, Any]] = []

    class _FakeWS:
        async def send_json(self, data: dict[str, Any]) -> None:
            sent.append(data)

    handler = _WSStreamHandler(_FakeWS())
    await handler.on_thinking("s1", "推理中……")

    assert len(sent) == 1
    assert sent[0]["type"] == "thinking_delta"
    assert sent[0]["data"]["text"] == "推理中……"

    sse = _SSEStreamHandler()
    await sse.on_thinking("s1", "推理中……")
    frame = await sse._queue.get()
    assert frame.startswith("event: thinking_delta")
    assert "推理中……" in frame

    await CLIStreamHandler().on_thinking("s1", "推理中……")


# ── ⑤ TOOL_INVOKED 事件 data 携带 tool_input（无参归一化为空 dict）──────────
async def test_tool_invoked_event_carries_normalized_tool_input() -> None:
    registry = MagicMock(spec=ToolRegistry)
    bus = EventBus()
    executor = ToolExecutor(registry=registry, event_bus=bus)
    tool = _EchoTool()
    registry.get.return_value = tool

    invoked: list[Event] = []

    async def _on_invoked(event: Event) -> None:
        invoked.append(event)

    await bus.subscribe(EventType.TOOL_INVOKED, _on_invoked)

    results = await executor.execute([ToolCall(id="1", name="echo", arguments={"text": "hi"})])
    assert results[0].success is True
    assert invoked, "TOOL_INVOKED 事件必须发射"
    assert invoked[0].data["tool_input"] == {"text": "hi"}, "事件必须携带工具输入参数"

    registry2 = MagicMock(spec=ToolRegistry)
    bus2 = EventBus()
    executor2 = ToolExecutor(registry=registry2, event_bus=bus2)
    registry2.get.return_value = _EchoTool()

    invoked2: list[Event] = []

    async def _on_invoked2(event: Event) -> None:
        invoked2.append(event)

    await bus2.subscribe(EventType.TOOL_INVOKED, _on_invoked2)
    await executor2.execute([ToolCall(id="1", name="echo", arguments={})])

    assert invoked2[0].data["tool_input"] == {}


# ── ⑥ 绑定层把 tool_input 转给 on_tool_call_start（不再是 None）──────────────
async def test_tool_call_start_forwards_tool_input_via_bindings() -> None:
    from bandi.core.agent_runner import _subscribe_handler, _unsubscribe_handler

    registry = MagicMock(spec=ToolRegistry)
    bus = EventBus()
    executor = ToolExecutor(registry=registry, event_bus=bus)
    tool = _EchoTool()
    registry.get.return_value = tool

    handler = R137RecorderHandler()
    bindings = await _subscribe_handler(bus, handler, "s-1")
    try:
        await executor.execute([ToolCall(id="1", name="echo", arguments={"text": "hi"})])
    finally:
        await _unsubscribe_handler(bus, bindings)

    assert handler.tool_starts, "on_tool_call_start 必须被调用"
    assert handler.tool_starts[0][0] == "echo"
    assert handler.tool_starts[0][1] == {"text": "hi"}, "工具输入必须透传到 StreamHandler"


# ── ⑦ WS/SSE tool_call_start 帧携带 tool_input───────────────
async def test_ws_and_sse_tool_call_start_frames_carry_tool_input() -> None:
    from bandi.api.app import _SSEStreamHandler, _WSStreamHandler

    sent: list[dict[str, Any]] = []

    class _FakeWS:
        async def send_json(self, data: dict[str, Any]) -> None:
            sent.append(data)

    handler = _WSStreamHandler(_FakeWS())
    await handler.on_tool_call_start("s1", "read", {"path": "a.txt"})
    await handler.on_tool_call_start("s1", "read")

    assert sent[0]["data"]["tool_input"] == {"path": "a.txt"}
    assert sent[1]["data"]["tool_input"] == {}, "缺省输入归一化为空 dict（对齐 SSE）"

    sse = _SSEStreamHandler()
    await sse.on_tool_call_start("s1", "read", {"path": "a.txt"})
    frame = await sse._queue.get()
    assert frame.startswith("event: tool_call_start")
    assert '"tool_input"' in frame
    assert "a.txt" in frame


# ── ⑧⑨ 技能注入事件经真实 bus + 真实绑定转发（含会话归属过滤）──────────────
async def test_skill_injected_forwarded_with_session_isolation() -> None:
    from bandi.core.agent_runner import _subscribe_handler, _unsubscribe_handler

    bus = EventBus()
    handler = R137RecorderHandler()
    bindings = await _subscribe_handler(bus, handler, "s-1")
    try:
        await bus.emit(
            Event(
                type=EventType.SKILL_INJECTED,
                data={"skills": ["git-workflow", "code-review"]},
                session_id="s-1",
            )
        )
    finally:
        await _unsubscribe_handler(bus, bindings)

    assert handler.skills == [["git-workflow", "code-review"]]

    bus = EventBus()
    handler = R137RecorderHandler()
    bindings = await _subscribe_handler(bus, handler, "s-mine")
    try:
        await bus.emit(
            Event(
                type=EventType.SKILL_INJECTED,
                data={"skills": ["other"]},
                session_id="s-other",
            )
        )
    finally:
        await _unsubscribe_handler(bus, bindings)

    assert handler.skills == [], "非本会话技能注入不得串入前端过程块"


# ── ⑩ WS/SSE/CLI 三实现发 skill_injected 帧（族级一致）──────────────────────
async def test_all_stream_handlers_emit_skill_frames() -> None:
    from bandi.api.app import _SSEStreamHandler, _WSStreamHandler
    from bandi.cli.stream_handler import CLIStreamHandler

    sent: list[dict[str, Any]] = []

    class _FakeWS:
        async def send_json(self, data: dict[str, Any]) -> None:
            sent.append(data)

    handler = _WSStreamHandler(_FakeWS())
    await handler.on_skill_injected("s1", ["git-workflow"])

    assert len(sent) == 1
    assert sent[0]["type"] == "skill_injected"
    assert sent[0]["data"]["skills"] == ["git-workflow"]

    sse = _SSEStreamHandler()
    await sse.on_skill_injected("s1", ["git-workflow"])
    frame = await sse._queue.get()
    assert frame.startswith("event: skill_injected")
    assert "git-workflow" in frame

    await CLIStreamHandler().on_skill_injected("s1", ["git-workflow"])


# ── ⑬⑭ 压缩事件经真实 bus + 真实绑定转发为 on_note（含会话归属过滤）──────────
async def test_compression_note_forwarded_with_session_isolation() -> None:
    from bandi.core.agent_runner import _subscribe_handler, _unsubscribe_handler

    bus = EventBus()
    handler = R137RecorderHandler()
    bindings = await _subscribe_handler(bus, handler, "s-1")
    try:
        await bus.emit(
            Event(
                type=EventType.COMPRESSION_COMPLETED,
                data={"stage": "micro", "before": 40, "after": 12},
                session_id="s-1",
            )
        )
    finally:
        await _unsubscribe_handler(bus, bindings)

    assert handler.notes == ["compaction"], "压缩完成必须到达 StreamHandler.on_note"

    bus = EventBus()
    handler = R137RecorderHandler()
    bindings = await _subscribe_handler(bus, handler, "s-mine")
    try:
        await bus.emit(
            Event(
                type=EventType.COMPRESSION_COMPLETED,
                data={"stage": "micro"},
                session_id="s-other",
            )
        )
    finally:
        await _unsubscribe_handler(bus, bindings)

    assert handler.notes == []


# ── ⑮ WS/SSE/CLI 三实现发 note 帧（族级一致）────────────────────────────────
async def test_all_stream_handlers_emit_note_frames() -> None:
    from bandi.api.app import _SSEStreamHandler, _WSStreamHandler
    from bandi.cli.stream_handler import CLIStreamHandler

    sent: list[dict[str, Any]] = []

    class _FakeWS:
        async def send_json(self, data: dict[str, Any]) -> None:
            sent.append(data)

    handler = _WSStreamHandler(_FakeWS())
    await handler.on_note("s1", "compaction")

    assert len(sent) == 1
    assert sent[0]["type"] == "note"
    assert sent[0]["data"]["note_type"] == "compaction"

    sse = _SSEStreamHandler()
    await sse.on_note("s1", "compaction")
    frame = await sse._queue.get()
    assert frame.startswith("event: note")
    assert "compaction" in frame

    await CLIStreamHandler().on_note("s1", "compaction")


# ==================== day137_user_rework ====================
# 功能：契约——用户在对话中修正「某次已派发任务」的执行细节（MEM-SCOPE-4）
#
# 现状：用户在单聊/群聊补充要求只会**新建** work_item；已派发/已 APPROVED 的卡
# 用户碰不到（`READY_FOR_REWORK` 只有「评审失败」自动入口；APPROVED 仅允许回
# `READY` 且走内部邮件复活）。对标 OpenOPC：
# `_prepare_company_followup_target`（写 latest_user_directive）
# → `reopen_approved_delegation_work_item_for_rework`（named bypass 受控回转）
# → `build_rework_feedback_context`（把修正指令 + 上一版产出注入 brief
# 注释原话「否则第二次尝试既看不到被拒原因，要么重复同样的错、要么盲改」）
#
# 本契约钉三层行为：
# ① r2xday137_store 侧：APPROVED → READY_FOR_REWORK 的**唯一受控入口**（非 APPROVED 一律
# fail-closed 拒绝），且必须释放执行租约 + 清理上一版产出；
# ② 领域侧：`prepare_user_rework_target` 把用户修正指令写进 work_item metadata；
# ③ 执行侧：rework 轮的 brief 必须携带「用户修正指令 + 上一版产出」（走
# `EngineSeatExecutor.run_turn` 生产装配路径）。


@pytest.fixture
def r2xday137_store(tmp_path):
    return WorkItemStore(str(tmp_path / "user-rework.db"))


def _approved_item(**extra: Any):
    return create_work_item(
        title="核对价格带",
        turn_type=WorkItemTurnType.EXECUTE,
        description="对比竞品报价",
        assignee_role="worker",
        phase=Phase.APPROVED,
        **extra,
    )


class TestReopenApprovedForRework:
    """r2xday137_store 侧：受控回转入口（named bypass，不放开状态机全表）。"""

    def test_approved_reopen_returns_ready_for_rework_and_releases_claim(
        self, r2xday137_store: WorkItemStore
    ) -> None:
        """核心：APPROVED 卡经该入口回转 READY_FOR_REWORK，且执行租约必须释放
        （残留租约会让 gate 永久跳过该卡）。"""
        item = _approved_item()
        r2xday137_store.save(item)

        reopened = r2xday137_store.reopen_approved_work_item_for_rework(item.id)

        assert reopened is not None, "已存在卡必须返回回转后的工作项"
        assert r2xday137_store.get(item.id).phase == Phase.READY_FOR_REWORK

        claimed = _approved_item()
        claimed.metadata["execution_claim"] = {
            "instance_id": "stale-instance",
            "lease_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
        r2xday137_store.save(claimed)

        r2xday137_store.reopen_approved_work_item_for_rework(claimed.id)

        claim = (r2xday137_store.get(claimed.id).metadata or {}).get("execution_claim")
        assert not claim, f"回转后不得残留执行租约：{claim}"

    def test_reopen_rejects_non_approved_unknown_target_and_missing_item(
        self, r2xday137_store: WorkItemStore
    ) -> None:
        """破坏者：非 APPROVED / 非法 target 一律 fail-closed；不存在的卡返回 None
        （不抛，调用方可据此判空）。"""
        for phase in (Phase.RUNNING, Phase.QUEUED, Phase.READY_FOR_REWORK):
            item = create_work_item(
                title=f"卡-{phase.value}",
                turn_type=WorkItemTurnType.EXECUTE,
                assignee_role="worker",
                phase=phase,
            )
            r2xday137_store.save(item)
            with pytest.raises(InvalidPhaseTransitionError):
                r2xday137_store.reopen_approved_work_item_for_rework(item.id)

        item = _approved_item()
        r2xday137_store.save(item)
        with pytest.raises(InvalidPhaseTransitionError):
            r2xday137_store.reopen_approved_work_item_for_rework(
                item.id, target_phase=Phase.RUNNING
            )

        assert r2xday137_store.reopen_approved_work_item_for_rework("no-such-item") is None


class TestPrepareUserReworkTarget:
    """领域侧：把用户修正指令落到工作项上（执行侧据此注入 brief）。"""

    def test_prepare_user_rework_target_writes_directive_and_clears_output(
        self, r2xday137_store: WorkItemStore
    ) -> None:
        """核心：修正指令写进 metadata 且卡回到 READY_FOR_REWORK；上一版产出必须清理
        （否则校验/回写会拿旧产出冒充新结果）。"""
        from bandi.company.user_rework import prepare_user_rework_target

        item = _approved_item()
        r2xday137_store.save(item)

        prepare_user_rework_target(item, "价格带改成 89-129，并补一句竞品来源", r2xday137_store)

        refreshed = r2xday137_store.get(item.id)
        assert refreshed.metadata["user_rework_directive"] == "价格带改成 89-129，并补一句竞品来源"
        assert refreshed.phase == Phase.READY_FOR_REWORK

        stale = _approved_item()
        stale.runtime_metadata["_last_output"] = "上一版：主力价格带 99-129"
        r2xday137_store.save(stale)

        prepare_user_rework_target(stale, "改成 89-129", r2xday137_store)

        refreshed = r2xday137_store.get(stale.id)
        assert not (refreshed.runtime_metadata or {}).get("_last_output")


class TestChatEntryUserRework:
    """入口：对话带目标卡时必须走修正分支（受控回转原卡，不新建卡）。"""

    def test_chat_entry_rework_reopens_target_and_guards_other_paths(
        self, r2xday137_store: WorkItemStore
    ) -> None:
        """入口：带目标卡走修正分支（受控回转原卡，不新建卡）；无目标卡不动既有卡
        （日常对话语义不变）；目标卡不存在显式报错。"""
        from bandi.api.company_chat import handle_chat_message

        item = _approved_item(metadata={"project_id": "p-e2e-137"})
        r2xday137_store.save(item)

        created = handle_chat_message(
            content="把价格带改成 89-129",
            sender_role="user",
            communication=None,
            store=r2xday137_store,
            target_work_item_id=item.id,
        )

        assert [c.id for c in created] == [item.id], (
            f"修正分支不得新建卡：{[c.id for c in created]}"
        )
        refreshed = r2xday137_store.get(item.id)
        assert refreshed.phase == Phase.READY_FOR_REWORK
        assert refreshed.metadata["user_rework_directive"] == "把价格带改成 89-129"

        untouched = _approved_item()
        r2xday137_store.save(untouched)

        handle_chat_message(
            content="今天天气不错",
            sender_role="user",
            communication=None,
            store=r2xday137_store,
            send_dm=False,
        )

        assert r2xday137_store.get(untouched.id).phase == Phase.APPROVED, (
            "无目标卡的对话不得改动既有卡"
        )

        with pytest.raises(ValueError):
            handle_chat_message(
                content="改成 89-129",
                sender_role="user",
                communication=None,
                store=r2xday137_store,
                target_work_item_id="no-such-item",
            )


class _CaptureRunAgent:
    """捕获 seat 执行器交给 run_agent 的 user_input（生产装配路径）。"""

    def __init__(self) -> None:
        self.user_inputs: list[str] = []

    async def __call__(self, **kwargs: Any) -> tuple[str, str, int, int]:
        self.user_inputs.append(str(kwargs.get("user_input") or ""))
        return ("已完成核对。", "COMPLETED", 12, 1)


class TestReworkBriefInjection:
    """执行侧：rework 轮 brief 必须带用户修正指令 + 上一版产出。"""

    def _runtime(self) -> CompanyRuntime:
        return CompanyRuntime(
            project_id="p-e2e-137",
            session_scope_id="item-1",
            role_id="worker",
            employee_id="worker",
        )

    def test_rework_brief_carries_directive_and_previous_output(
        self, r2xday137_store: WorkItemStore, monkeypatch: Any
    ) -> None:
        """核心：rework 轮 brief 必须携带用户修正指令 + 上一版产出（经 prepare 回转后
        仍须看得到——「据此修改，不要从零重写」否则退化盲改）。"""
        import bandi.company.seat_executor as seat_mod
        from bandi.company.user_rework import prepare_user_rework_target

        capture = _CaptureRunAgent()
        monkeypatch.setattr(seat_mod, "run_agent", capture)

        item = create_work_item(
            title="核对价格带",
            turn_type=WorkItemTurnType.EXECUTE,
            description="对比竞品报价",
            assignee_role="worker",
            phase=Phase.READY_FOR_REWORK,
            metadata={
                "project_id": "p-e2e-137",
                "user_rework_directive": "价格带改成 89-129，并补一句竞品来源",
            },
        )
        item.runtime_metadata["_last_output"] = "上一版：主力价格带 99-129"

        asyncio.run(EngineSeatExecutor().run_turn(self._runtime(), item))

        assert capture.user_inputs, "seat 执行器未调用 run_agent（装配路径断了）"
        brief = capture.user_inputs[0]
        assert "价格带改成 89-129" in brief, f"用户修正指令未进入 brief：{brief[:200]}"
        assert "上一版" in brief, f"上一版产出未进入 brief：{brief[:200]}"

        approved = _approved_item(metadata={"project_id": "p-e2e-137"})
        approved.runtime_metadata["_last_output"] = "上一版：主力价格带 99-129"
        r2xday137_store.save(approved)

        reopened = prepare_user_rework_target(approved, "改成 89-129", r2xday137_store)
        assert reopened is not None
        asyncio.run(EngineSeatExecutor().run_turn(self._runtime(), reopened))

        assert capture.user_inputs, "seat 执行器未调用 run_agent"
        brief = capture.user_inputs[-1]
        assert "改成 89-129" in brief
        assert "主力价格带 99-129" in brief, f"上一版产出在回转后丢失：{brief[:220]}"

    def test_plain_turn_has_no_rework_block(self, monkeypatch: Any) -> None:
        """破坏者：无修正指令的普通轮不得凭空出现返工段（不污染正常 brief）。"""
        import bandi.company.seat_executor as seat_mod

        capture = _CaptureRunAgent()
        monkeypatch.setattr(seat_mod, "run_agent", capture)

        item = create_work_item(
            title="核对价格带",
            turn_type=WorkItemTurnType.EXECUTE,
            description="对比竞品报价",
            assignee_role="worker",
            metadata={"project_id": "p-e2e-137"},
        )

        asyncio.run(EngineSeatExecutor().run_turn(self._runtime(), item))

        assert capture.user_inputs
        assert "修正" not in capture.user_inputs[0]


@pytest.fixture
def dispatcher(r2xday137_store: WorkItemStore):
    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.gate_harness import GateHarness
    from bandi.company.org_engine import OrgEngine
    from bandi.company.recruiter import Recruiter

    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    return CompanyDispatcher(
        store=r2xday137_store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        config=DispatcherConfig(max_concurrent=3),
    )


class TestReworkLifecycleCleanup:
    """审查修复：用户修正指令/上一版产出必须在「使命完成」时清理——残留会让
    `directive or feedback` 遮蔽后续评审驳回反馈（brief 里旧指令取代新反馈）
    并污染该卡所有后续执行轮。"""

    def test_review_rework_clears_stale_user_directive(
        self, r2xday137_store: WorkItemStore, dispatcher: Any
    ) -> None:
        """评审驳回轮：旧用户指令必须清除（来源切换），brief 呈现评审反馈。"""
        item = create_work_item(
            title="核对价格带",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={
                "review_verdict": "rework",
                "structured_review_verdict": {"reason": "评审：缺竞品来源引用"},
                "user_rework_directive": "旧用户指令：改成 89-129",
                "rework_previous_output": "旧产出快照",
                "review_rework_count": 0,
            },
        )
        r2xday137_store.save(item)

        asyncio.run(dispatcher.tick())

        loaded = r2xday137_store.get(item.id)
        assert loaded is not None and loaded.phase == Phase.READY_FOR_REWORK
        assert loaded.metadata.get("rework_feedback") == "评审：缺竞品来源引用"
        assert not str(loaded.metadata.get("user_rework_directive") or "").strip(), (
            "评审驳回轮必须清除旧用户指令——否则遮蔽本轮评审反馈"
        )
        block = EngineSeatExecutor()._render_rework_feedback(loaded)
        assert "评审：缺竞品来源引用" in block, f"评审反馈未进入 brief：{block[:200]}"
        assert "旧用户指令" not in block, f"旧用户指令遮蔽了评审反馈：{block[:200]}"

    def test_user_rework_residue_cleared_on_pass_approve_and_cap_paths(
        self, r2xday137_store: WorkItemStore, dispatcher: Any
    ) -> None:
        """执行完成（gate PASS）/审批通过/rework 达上限 auto_done 三条使命完成路径：
        用户修正指令与上一版产出同批清理（残留污染后续所有执行轮）。"""
        from bandi.company.work_item import transition_work_item

        pass_item = create_work_item(
            title="核对价格带",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            phase=Phase.READY,
            metadata={
                "user_rework_directive": "改成 89-129",
                "user_rework_count": 1,
                "rework_previous_output": "旧产出快照",
                "rework_feedback": "改成 89-129",
            },
        )
        r2xday137_store.save(pass_item)
        running = r2xday137_store.get(pass_item.id)
        assert running is not None
        transition_work_item(running, Phase.RUNNING, r2xday137_store, reason="test: seed running")
        running.runtime_metadata["_last_output"] = "新产出（价格带 89-129）"
        r2xday137_store.save(running)

        asyncio.run(dispatcher.tick())

        loaded = r2xday137_store.get(pass_item.id)
        assert loaded is not None and loaded.phase == Phase.APPROVED
        assert not str(loaded.metadata.get("user_rework_directive") or "").strip(), (
            "gate PASS 后旧用户指令必须清理（否则污染后续所有执行轮）"
        )
        assert not str(loaded.metadata.get("rework_previous_output") or "").strip()

        approve_item = create_work_item(
            title="核对价格带",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={
                "review_verdict": "approve",
                "user_rework_directive": "改成 89-129",
                "rework_previous_output": "旧产出快照",
                "rework_feedback": "改成 89-129",
                "review_rework_count": 1,
            },
        )
        r2xday137_store.save(approve_item)

        asyncio.run(dispatcher.tick())

        loaded = r2xday137_store.get(approve_item.id)
        assert loaded is not None and loaded.phase == Phase.APPROVED
        assert not str(loaded.metadata.get("user_rework_directive") or "").strip()
        assert not str(loaded.metadata.get("rework_previous_output") or "").strip()

        dispatcher.config.max_review_reworks = 2
        cap_item = create_work_item(
            title="核对价格带",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={
                "review_verdict": "rework",
                "structured_review_verdict": {"reason": "again"},
                "review_rework_count": 2,
                "user_rework_directive": "旧用户指令",
                "rework_previous_output": "旧产出快照",
            },
        )
        r2xday137_store.save(cap_item)

        asyncio.run(dispatcher.tick())

        loaded = r2xday137_store.get(cap_item.id)
        assert loaded is not None and loaded.phase == Phase.APPROVED
        assert not str(loaded.metadata.get("user_rework_directive") or "").strip()
        assert not str(loaded.metadata.get("rework_previous_output") or "").strip()


class TestUserReworkEntryNoHalfCommit:
    """审查修复：非 APPROVED 卡被拒时不得留半提交（线程消息先落盘但卡未回转）。"""

    def test_rejected_rework_leaves_no_thread_message(
        self, r2xday137_store: WorkItemStore, tmp_path: Any
    ) -> None:
        from bandi.api.company_chat import handle_chat_message as _handle
        from bandi.company.discussion_thread import ThreadManager

        mgr = ThreadManager(workspace=str(tmp_path), project_id="p1", session_id="s1")
        item = create_work_item(
            title="进行中的任务",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            phase=Phase.RUNNING,
        )
        r2xday137_store.save(item)

        with pytest.raises(InvalidPhaseTransitionError):
            _handle(
                content="改成 89-129",
                sender_role="user",
                communication=None,
                store=r2xday137_store,
                thread_manager=mgr,
                target_work_item_id=item.id,
                project_id="p1",
            )

        assert mgr.get_threads_for_agent("user") == [], (
            "资格预检必须在线程/DM 写入之前——被拒的修正不得留下半提交消息"
        )


class TestReopenCasUnderConcurrency:
    """审查修复：reopen 的 CAS 必须验证 UPDATE 命中——并发窗口不得返回「成功」。"""

    def test_concurrent_reopen_fails_explicitly(self, tmp_path: Any, monkeypatch: Any) -> None:
        db = str(tmp_path / "cas.db")
        store_a = WorkItemStore(db)
        store_b = WorkItemStore(db)
        item = _approved_item()
        store_a.save(item)
        item_id = item.id

        class _ConnProxy:
            """在 store_a 的 UPDATE 之前让 store_b 完成回转——模拟跨进程并发窗口。"""

            def __init__(self, real: Any, hook: Any) -> None:
                self._real = real
                self._hook = hook
                self._hooked = False

            def execute(self, sql: str, *args: Any) -> Any:
                if not self._hooked and sql.strip().startswith("UPDATE"):
                    self._hooked = True
                    self._hook()
                return self._real.execute(sql, *args)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._real, name)

        monkeypatch.setattr(
            store_a,
            "_conn",
            _ConnProxy(
                store_a._conn,
                lambda: store_b.reopen_approved_work_item_for_rework(item_id),
            ),
        )

        with pytest.raises(InvalidPhaseTransitionError):
            store_a.reopen_approved_work_item_for_rework(
                item_id, metadata_updates={"probe": "mine"}
            )

        final = store_b.get(item_id)
        assert final is not None and final.metadata.get("probe") is None, (
            "本方未命中的 metadata_updates 不得落库（他方结果不被覆盖）"
        )


# ==================== day137b_employee_progress ====================
# 功能：Day137b-1 插队批契约——员工执行过程流式（thread_progress 帧数据源）
# 钉行为族（员工单聊/群聊/团队会话的实时过程块）：
# ① EmployeeProgressHandler 帧映射族（run_agent_turn 过程帧 → thread_progress 事件：
# content/thinking/tool_start/tool_progress/tool_result/skill/note/memory/task_ledger/done/error）；
# ② 归属字段（project_id/thread_id/role_id）逐帧携带（前端按项目/员工过滤依据）；
# ③ 破坏者：广播回调异常不得中断员工执行（帧转发 best-effort）；
# ④ seat_executor 装配：progress_callback → run_agent 收到 stream_handler（含 thread_id 透传）
# + 执行结束转发 done 帧；未装配 callback 时 stream_handler 为 None（fail-closed 不广播）；
# ⑤ 端到端（生产主链路）：真实 run_agent + MockProvider + 真实 _subscribe_handler 桥接 →
# 广播回调收到 content_delta（断言最终帧产物）；
# ⑥ api 广播出口：_broadcast_thread_progress 出 thread_progress 帧（fake 客户端收到）。
# ⑩ 作用域归一化：空 project 帧按 "company" 匹配（与前端 normalizeProjectScope 同口径）。


pytestmark = pytest.mark.asyncio


def _collector() -> tuple[list[dict[str, Any]], Any]:
    frames: list[dict[str, Any]] = []
    return frames, frames.append


def _event_types(frames: list[dict[str, Any]]) -> list[str]:
    return [f["data"]["event"]["type"] for f in frames]


# ── ① 帧映射族（方法 → event.type/data 结构；② 归属断言合并覆盖）────────────
async def test_handler_frame_family_with_scope() -> None:
    frames, callback = _collector()
    handler = EmployeeProgressHandler(callback, project_id="p1", thread_id="t1", role_id="r1")
    await handler.on_content_delta("s", "正文")
    await handler.on_thinking("s", "想一下")
    await handler.on_tool_call_start("s", "web_search", {"query": "x"})
    await handler.on_tool_progress("s", "web_search", "status", {"step": "fetch"})
    await handler.on_tool_result("s", "web_search", True, "ok", 5)
    await handler.on_skill_injected("s", ["git-workflow"])
    await handler.on_note("s", "compaction")
    await handler.on_memory_written("s", ["long_term", "episodic"])
    await handler.on_task_ledger("s", [{"content": "a"}])
    await handler.on_done("s", "VERIFIED", 10, 1)
    await handler.on_error("s", "boom")

    assert _event_types(frames) == [
        "content_delta",
        "thinking_delta",
        "tool_call_start",
        "tool_progress",
        "tool_result",
        "skill_injected",
        "note",
        "memory_written",
        "task_ledger",
        "done",
        "error",
    ]
    assert all(f["type"] == "thread_progress" for f in frames)
    assert all(
        f["data"]["project_id"] == "p1"
        and f["data"]["thread_id"] == "t1"
        and f["data"]["role_id"] == "r1"
        for f in frames
    )
    assert frames[2]["data"]["event"]["data"] == {
        "tool_name": "web_search",
        "tool_input": {"query": "x"},
        "tool_call_id": "",
    }
    assert frames[7]["data"]["event"]["data"] == {
        "channels": ["long_term", "episodic"],
        "count": 2,
    }
    assert frames[9]["data"]["event"]["data"]["terminal_reason"] == "VERIFIED"


# ── ③ 破坏者：广播回调异常不中断员工执行 ────────────────────────────────────
async def test_callback_failure_does_not_break_execution() -> None:
    def boom(_frame: dict[str, Any]) -> None:
        raise RuntimeError("ws broadcast down")

    handler = EmployeeProgressHandler(boom, project_id="p", thread_id="t", role_id="r")
    await handler.on_thinking("s", "x")
    await handler.on_done("s", "", 0, 0)


# ── ④ seat_executor 装配（stream_handler 注入 + done 转发 + fail-closed）────
async def test_seat_executor_wires_progress_handler_and_fails_closed(monkeypatch) -> None:
    from bandi.company.company_runtime import CompanyRuntime
    from bandi.company.seat_executor import EngineSeatExecutor
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    captured: dict[str, Any] = {}

    async def fake_run_agent(**kwargs: Any):
        captured.update(kwargs)
        return "out", "VERIFIED", 1, 1

    monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)
    frames, callback = _collector()
    executor = EngineSeatExecutor(progress_callback=callback)
    runtime = CompanyRuntime(project_id="p1", session_scope_id="s", role_id="r1", employee_id="e1")
    item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    item.metadata = {**(item.metadata or {}), "thread_id": "t-9"}
    await executor.run_turn(runtime, item)

    handler = captured.get("stream_handler")
    assert handler is not None, "装配 progress_callback 后必须给 run_agent 传 stream_handler"
    await handler.on_thinking("s", "x")
    assert frames[-1]["data"]["thread_id"] == "t-9", "thread_id 必须从 work_item metadata 透传"
    assert frames[-1]["data"]["role_id"] == "r1"
    assert frames[-1]["data"]["project_id"] == "p1"
    assert "done" in _event_types(frames), "执行结束必须转发 done 帧（前端清过程槽依据）"

    captured.clear()
    plain_executor = EngineSeatExecutor()
    plain_runtime = CompanyRuntime(
        project_id="p", session_scope_id="s", role_id="r", employee_id="e"
    )
    plain_item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    await plain_executor.run_turn(plain_runtime, plain_item)

    assert captured.get("stream_handler") is None, "未装配 callback 不得广播（fail-closed）"


# ── ⑤ 端到端（生产主链路）：真实 run_agent + MockProvider + 桥接 ────────────
async def test_run_agent_emits_progress_frames_end_to_end(tmp_path) -> None:
    from bandi.agent.factory import run_agent
    from bandi.core.config import BandiConfig
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    frames, callback = _collector()
    handler = EmployeeProgressHandler(callback, project_id="p", thread_id="t", role_id="r")
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    cfg.agent.max_iterations = 1
    r3xday137b_store = SessionStore(str(tmp_path / "s.db"))
    try:
        with patch("bandi.core.agent_runner.build_provider", return_value=MockProvider()):
            await run_agent(
                user_input="过程流端到端",
                session_id="emp-progress-e2e",
                session_store=r3xday137b_store,
                bandi_config=cfg,
                stream_handler=handler,
            )
    finally:
        r3xday137b_store.close()

    assert "content_delta" in _event_types(frames), (
        f"真实执行链必须把 content_delta 桥接到广播回调（实际 {_event_types(frames)}）"
    )


# ── ⑥ api 广播出口：_broadcast_thread_progress 发帧且不进回放缓冲 ────────────
async def test_broadcast_thread_progress_pushes_without_replay_buffer() -> None:
    """广播出口：thread_progress 帧实时推送（data 不二次包装）；过程帧中间态不得写入
    公司 WS 回放缓冲（否则挤出未 ack 的业务帧 → resume 丢消息）。"""
    import importlib

    # 注意：`bandi.api.__init__` 以 app 名导出 FastAPI 实例——必须经 importlib
    # 从 sys.modules 直取子模块（`import ... as` 会被包属性遮蔽）
    app_module = importlib.import_module("bandi.api.app")

    sent: list[dict[str, Any]] = []

    class _FakeWS:
        async def send_json(self, data: dict[str, Any]) -> None:
            sent.append(data)

    fake = _FakeWS()
    original_history = list(app_module._company_ws_history)
    app_module._company_ws_clients.add(fake)
    probe = "fm8-probe-137b1-r1"

    def _recorded() -> list[dict[str, Any]]:
        # 只认本用例的探针帧——不依赖缓冲绝对长度（pytest-xdist 并行/游离广播任务不会干扰）
        with app_module._company_ws_history_lock:
            return [
                f
                for f in app_module._company_ws_history
                if (f.get("data") or {}).get("thread_id") == probe
            ]

    try:
        app_module._broadcast_thread_progress(
            {
                "type": "thread_progress",
                "data": {
                    "project_id": "p",
                    "thread_id": "t",
                    "role_id": "r",
                    "event": {"type": "done", "data": {}},
                },
            }
        )
        for _ in range(25):
            await asyncio.sleep(0.02)
            if sent:
                break

        assert sent, "广播必须发出 thread_progress 帧"
        assert sent[0]["type"] == "thread_progress"
        assert sent[0]["data"]["role_id"] == "r"
        assert sent[0]["data"]["event"]["type"] == "done"
        assert "type" not in sent[0]["data"], "帧不得二次包装（data 内不应再出现外层 type）"

        sent.clear()
        app_module._broadcast_thread_progress(
            {
                "type": "thread_progress",
                "data": {
                    "project_id": "p",
                    "thread_id": probe,
                    "role_id": "r",
                    "event": {"type": "thinking_delta", "data": {"text": "…"}},
                },
            }
        )
        for _ in range(25):
            await asyncio.sleep(0.02)
            if sent:
                break

        assert sent, "过程帧仍须实时推送（只是不入缓冲）"
        assert _recorded() == [], (
            "过程帧中间态不得写入公司 WS 回放缓冲（否则挤出未 ack 的业务帧 → resume 丢消息）"
        )

        # 对照组：业务帧仍入缓冲（证明上面的断言不是恒真）
        app_module._record_company_event(
            {"type": "thread_message", "data": {"thread_id": probe, "content": "x"}}
        )
        assert len(_recorded()) == 1, "业务帧仍须入缓冲（对照组）"
    finally:
        app_module._company_ws_clients.discard(fake)
        app_module._company_ws_history.clear()
        app_module._company_ws_history.extend(original_history)


async def test_terminal_progress_frame_recorded_others_not() -> None:
    """终态帧（done/error）每次执行仅 1 帧——必须入缓冲，客户端重连后靠它把过程块
    收敛到真实终态（否则断线期间结束的执行只能停在「执行中」）。
    非终态中间态仍不入缓冲（的窗口保护不变）。"""
    import importlib

    app_module = importlib.import_module("bandi.api.app")

    sent: list[dict[str, Any]] = []

    class _FakeWS:
        async def send_json(self, data: dict[str, Any]) -> None:
            sent.append(data)

    fake = _FakeWS()
    original_history = list(app_module._company_ws_history)
    app_module._company_ws_clients.add(fake)
    probe = "fm8-probe-137b2-r1"
    try:
        for event_type in ("thinking_delta", "done", "error", "tool_result"):
            sent.clear()
            app_module._broadcast_thread_progress(
                {
                    "type": "thread_progress",
                    "data": {
                        "project_id": "p",
                        "thread_id": probe,
                        "role_id": "r",
                        "event": {"type": event_type, "data": {}},
                    },
                }
            )
            for _ in range(25):
                await asyncio.sleep(0.02)
                if sent:
                    break
            assert sent, f"{event_type} 帧仍须实时推送"

        # 中间态 2 帧（thinking_delta / tool_result）不入缓冲；终态 2 帧（done/error）入缓冲
        with app_module._company_ws_history_lock:
            recorded = [
                f
                for f in app_module._company_ws_history
                if (f.get("data") or {}).get("thread_id") == probe
            ]
        kinds = [f["data"]["event"]["type"] for f in recorded]
        assert kinds == ["done", "error"], (
            f"只有 done/error 入回放缓冲（中间态不入：防挤爆业务帧窗口；实际 {kinds}）"
        )
        assert all(isinstance(f.get("seq"), int) for f in recorded), "入缓冲帧必须带 seq"
    finally:
        app_module._company_ws_clients.discard(fake)
        app_module._company_ws_history.clear()
        app_module._company_ws_history.extend(original_history)


async def test_progress_frames_scoped_by_project_subscription() -> None:
    """已订阅连接只收本项目的过程帧；未订阅连接 fail-open 全收（兼容旧包）。
    业务帧与过程帧的差别只在这里——本用例只覆盖过程帧出口。"""
    import importlib

    app_module = importlib.import_module("bandi.api.app")

    class _FakeWS:
        def __init__(self) -> None:
            self.frames: list[dict[str, Any]] = []

        async def send_json(self, data: dict[str, Any]) -> None:
            self.frames.append(data)

    ws_a, ws_b, ws_open = _FakeWS(), _FakeWS(), _FakeWS()
    for ws in (ws_a, ws_b, ws_open):
        app_module._company_ws_clients.add(ws)
    app_module._company_ws_scopes[id(ws_a)] = {"pA"}
    app_module._company_ws_scopes[id(ws_b)] = {"pB"}

    def _frame(project: str) -> dict[str, Any]:
        return {
            "type": "thread_progress",
            "data": {
                "project_id": project,
                "thread_id": "t",
                "role_id": "r",
                "event": {"type": "thinking_delta", "data": {"text": "…"}},
            },
        }

    async def _wait(ws: _FakeWS, expected: int, tries: int = 25) -> None:
        for _ in range(tries):
            await asyncio.sleep(0.02)
            if len(ws.frames) >= expected:
                return

    try:
        app_module._broadcast_thread_progress(_frame("pA"))
        await _wait(ws_a, 1)
        await _wait(ws_open, 1)
        assert len(ws_a.frames) == 1, "订阅了 pA 的连接必须收到 pA 的过程帧"
        assert len(ws_b.frames) == 0, "只订阅 pB 的连接不得收到 pA 的过程帧（定向生效）"
        assert len(ws_open.frames) == 1, "未订阅连接 fail-open——必须照旧全收（兼容旧包）"

        app_module._broadcast_thread_progress(_frame("pB"))
        await _wait(ws_b, 1)
        await _wait(ws_open, 2)
        assert len(ws_b.frames) == 1, "订阅了 pB 的连接必须收到 pB 的过程帧"
        assert len(ws_a.frames) == 1, "只订阅 pA 的连接不得收到 pB 的过程帧"
        assert len(ws_open.frames) == 2, "未订阅连接继续全收"
    finally:
        for ws in (ws_a, ws_b, ws_open):
            app_module._company_ws_clients.discard(ws)
            app_module._company_ws_scopes.pop(id(ws), None)


async def test_progress_scope_normalizes_empty_to_company() -> None:
    """空/缺失 project_id 的过程帧按 "company" 匹配——与前端 `normalizeProjectScope()`
    同口径；否则前端订阅 "company" 时后端判成「不在订阅集合」而静默丢弃。"""
    import importlib

    app_module = importlib.import_module("bandi.api.app")
    assert app_module._progress_scope({"data": {"project_id": "pA"}}) == "pA"
    assert app_module._progress_scope({"data": {"project_id": "  pA  "}}) == "pA"
    assert app_module._progress_scope({"data": {"project_id": ""}}) == "company"
    assert app_module._progress_scope({"data": {}}) == "company"
    assert app_module._progress_scope({}) == "company"
    # 事件类型解析
    assert app_module._progress_event_type({"data": {"event": {"type": "done"}}}) == "done"
    assert app_module._progress_event_type({"data": {}}) == ""


# ==================== day137b_process_frames ====================
# 功能：Day137b-1 契约——过程消息数据源补全（记忆写入行 / 会话任务条 / 挂起倒计时）
# 钉行为族（docs/前端设计规划.md §8 过程块规范）：
# ① MEMORY_WRITTEN 经真实 _emit_memory_written + 真实 EventBus + _subscribe_handler
# 转发到 StreamHandler.on_memory_written（前端「记入记忆 xN」数据源）；
# ② MEMORY_WRITTEN 按会话归属过滤（loop 外发射，data.session_id 兜底——不串会话）；
# ③ TASK_LEDGER_UPDATED 经真实 TaskLedger.persist 转发到 on_task_ledger（会话任务条数据源）；
# ④ task_ledger 事件按 EventBus 溯源 session_id 过滤（不串会话）；
# ⑤ WS/SSE 两实现发 memory_written / task_ledger 帧（族级一致），CLI 协议完整；
# ⑥ 交互帧携带 expires_in（挂起倒计时数据源；用实际挂起超时，过期 clamp 0）。
# 契约只钉行为结果，不钉实现——经生产主链路（真实 TaskLedger/真实 EventBus/
# 真实 _subscribe_handler/真实 HTTPUserInteraction）断言最终产物帧与字段。


class R137bRecorderHandler:
    """StreamHandler 记录实现（Day137b-1：含 on_memory_written / on_task_ledger）。"""

    def __init__(self) -> None:
        self.memory_channels: list[list[str]] = []
        self.task_ledgers: list[list[dict[str, Any]]] = []

    async def on_content_delta(self, session_id: str, delta: str) -> None: ...

    async def on_thinking(self, session_id: str, delta: str) -> None: ...

    async def on_tool_call_start(
        self, session_id: str, tool_name: str, tool_input: dict[str, Any] | None = None
    ) -> None: ...

    async def on_tool_result(
        self, session_id: str, tool_name: str, success: bool, output_preview: str, duration_ms: int
    ) -> None: ...

    async def on_tool_progress(
        self, session_id: str, tool_name: str, progress_type: str, data: dict[str, Any]
    ) -> None: ...

    async def on_skill_injected(self, session_id: str, skills: list[str]) -> None: ...

    async def on_note(self, session_id: str, note_type: str) -> None: ...

    async def on_memory_written(self, session_id: str, channels: list[str]) -> None:
        self.memory_channels.append(list(channels))

    async def on_task_ledger(self, session_id: str, items: list[dict[str, Any]]) -> None:
        self.task_ledgers.append(list(items))

    async def on_done(
        self, session_id: str, terminal_reason: str, total_tokens: int, turn_index: int
    ) -> None: ...

    async def on_error(self, session_id: str, error: str) -> None: ...


pytestmark = pytest.mark.asyncio


# ── ①② MEMORY_WRITTEN 经真实发射函数 + 真实绑定转发（含会话归属过滤）────────
async def test_memory_written_forwarded_with_session_isolation() -> None:
    from bandi.core.agent_runner import (
        _emit_memory_written,
        _subscribe_handler,
        _unsubscribe_handler,
    )

    bus = EventBus()
    handler = R137bRecorderHandler()
    bindings = await _subscribe_handler(bus, handler, "s-1")
    ctx = MagicMock()
    ctx.bus = bus
    try:
        await _emit_memory_written(ctx, "s-1", 2, ["long_term", "episodic"])
    finally:
        await _unsubscribe_handler(bus, bindings)

    assert handler.memory_channels == [["long_term", "episodic"]], (
        "MEMORY_WRITTEN 必须转发到 StreamHandler.on_memory_written（通道明细透传）"
    )

    bus = EventBus()
    handler = R137bRecorderHandler()
    bindings = await _subscribe_handler(bus, handler, "s-mine")
    try:
        await bus.emit(
            Event(
                type=EventType.MEMORY_WRITTEN,
                data={"session_id": "s-other", "turn_index": 1, "channels": ["long_term"]},
            )
        )
    finally:
        await _unsubscribe_handler(bus, bindings)

    assert handler.memory_channels == [], "非本会话记忆写入事件不得串入前端过程块"


# ── ③④ 真实 TaskLedger.persist → on_task_ledger（含会话归属过滤）────────────
async def test_task_ledger_updates_forwarded_with_session_isolation() -> None:
    from bandi.core.agent_runner import _subscribe_handler, _unsubscribe_handler
    from bandi.core.config import TaskLedgerConfig
    from bandi.tools.task_ledger import TaskLedger

    bus = EventBus()
    handler = R137bRecorderHandler()
    bindings = await _subscribe_handler(bus, handler, "s-1")
    ledger = TaskLedger(
        config=TaskLedgerConfig(persist_to_work_item=False),
        session_id="s-1",
        session_store=MagicMock(),
        event_bus=bus,
    )
    ledger.items = [
        {"content": "写契约", "status": "in_progress"},
        {"content": "跑回归", "status": "pending"},
    ]
    token = set_event_context(session_id="s-1")
    try:
        await ledger.persist()
    finally:
        reset_event_context(token)
        await _unsubscribe_handler(bus, bindings)

    assert handler.task_ledgers, "TASK_LEDGER_UPDATED 必须转发到 on_task_ledger"
    assert handler.task_ledgers[0][0]["content"] == "写契约", (
        "任务条目原样透传（前端任务条渲染数据源）"
    )
    assert len(handler.task_ledgers[0]) == 2

    bus = EventBus()
    handler = R137bRecorderHandler()
    bindings = await _subscribe_handler(bus, handler, "s-mine")
    try:
        await bus.emit(
            Event(
                type=EventType.TASK_LEDGER_UPDATED,
                data={"task_ledger": [{"content": "别人的任务"}], "item_count": 1},
                session_id="s-other",
            )
        )
    finally:
        await _unsubscribe_handler(bus, bindings)

    assert handler.task_ledgers == [], "非本会话任务台账不得串入前端任务条"


# ── ⑤ WS/SSE/CLI 三实现发 memory_written / task_ledger 帧（族级一致）────────
async def test_all_stream_handlers_emit_memory_and_task_frames() -> None:
    from bandi.api.app import _SSEStreamHandler, _WSStreamHandler
    from bandi.cli.stream_handler import CLIStreamHandler

    sent: list[dict[str, Any]] = []

    class _FakeWS:
        async def send_json(self, data: dict[str, Any]) -> None:
            sent.append(data)

    handler = _WSStreamHandler(_FakeWS())
    await handler.on_memory_written("s1", ["long_term", "working"])
    await handler.on_task_ledger("s1", [{"content": "a", "status": "pending"}])

    assert sent[0]["type"] == "memory_written"
    assert sent[0]["data"]["channels"] == ["long_term", "working"]
    assert sent[0]["data"]["count"] == 2
    assert sent[1]["type"] == "task_ledger"
    assert sent[1]["data"]["items"][0]["content"] == "a"
    assert sent[1]["data"]["count"] == 1

    sse = _SSEStreamHandler()
    await sse.on_memory_written("s1", ["episodic"])
    await sse.on_task_ledger("s1", [{"content": "b"}])
    frame_memory = await sse._queue.get()
    frame_task = await sse._queue.get()

    assert frame_memory.startswith("event: memory_written")
    assert "episodic" in frame_memory
    assert frame_task.startswith("event: task_ledger")
    assert "b" in frame_task

    cli = CLIStreamHandler()
    await cli.on_memory_written("s1", ["long_term"])
    await cli.on_task_ledger("s1", [])


# ── ⑥⑥b 交互帧携带 expires_in（默认 TTL 内；实际挂起超时优先——倒计时不虚标）──
async def test_interaction_frame_expires_in_uses_actual_timeout() -> None:
    from bandi.api.app import _interaction_frame
    from bandi.core.user_interaction import HTTPUserInteraction, InteractionStore

    r4xday137b_store = InteractionStore(ttl_seconds=300)
    item = r4xday137b_store.register(session_id="s1", kind="confirm", payload={"prompt": "?"})
    frame = _interaction_frame(item)

    assert 0 < frame["expires_in"] <= 300, "交互帧必须携带剩余秒数供前端倒计时"
    assert frame["interaction_id"] == item.interaction_id

    r4xday137b_store = InteractionStore(ttl_seconds=300)
    captured: list[Any] = []

    async def _on_interaction(item: Any) -> None:
        captured.append(item)
        r4xday137b_store.reject(item.interaction_id)

    ui = HTTPUserInteraction(r4xday137b_store, session_id="s1", on_interaction=_on_interaction)
    decided = await ui.ask_confirmation("确认？")  # 默认 timeout=30
    assert decided is False
    assert captured, "挂起必须推帧"
    frame = _interaction_frame(captured[0])
    assert 0 < frame["expires_in"] <= 30, (
        "倒计时必须用实际挂起超时（confirm=30s），不是存储默认 300s"
    )


# ── ⑥c 破坏者：已过期交互 → expires_in 归零（不是负数）──────────────────────
async def test_interaction_frame_expired_clamped_to_zero() -> None:
    from bandi.api.app import _interaction_frame
    from bandi.core.user_interaction import InteractionStore

    r4xday137b_store = InteractionStore(ttl_seconds=1)
    item = r4xday137b_store.register(session_id="s1", kind="input", payload={"prompt": "?"})
    item.created_at -= 10  # 已过期（超过 TTL）
    frame = _interaction_frame(item)

    assert frame["expires_in"] == 0, "过期交互不得出现负数倒计时"


# ── ⑥d 过期判定按 item 级 ttl（不是 r4xday137b_store 级）——倒计时与真实过期同源 ──────────
async def test_expiry_uses_item_level_ttl() -> None:
    from bandi.core.user_interaction import InteractionStore

    r4xday137b_store = InteractionStore(ttl_seconds=300)
    short = r4xday137b_store.register(
        session_id="s1", kind="confirm", payload={"prompt": "?"}, ttl_seconds=30
    )
    default_ttl = r4xday137b_store.register(
        session_id="s1", kind="confirm", payload={"prompt": "?"}
    )
    short.created_at -= 40  # 超过自身 30s 超时，但远小于 r4xday137b_store 级 300s

    pending = r4xday137b_store.list_pending(session_id="s1")  # 触发 _expire_stale

    assert [item.interaction_id for item in pending] == [default_ttl.interaction_id], (
        "短超时项必须按 item 级 ttl 过期（否则前端倒计时归零后后端仍认为可作答）"
    )
    assert r4xday137b_store.get(short.interaction_id) is not None
    assert r4xday137b_store.get(short.interaction_id).status == "expired"
    assert short.remaining_seconds() == 0


# ── ⑥e 通知类交互不带倒计时（不是待决策项，不得伪装成待审批卡）──────────────
async def test_notification_frame_has_no_countdown() -> None:
    from bandi.api.app import _interaction_frame
    from bandi.core.user_interaction import HTTPUserInteraction, InteractionStore

    r4xday137b_store = InteractionStore(ttl_seconds=300)
    captured: list[Any] = []

    async def _on_interaction(item: Any) -> None:
        captured.append(item)

    ui = HTTPUserInteraction(r4xday137b_store, session_id="s1", on_interaction=_on_interaction)
    await ui.show_notification("构建完成", level="info")

    assert captured, "通知必须推帧"
    assert "expires_in" not in _interaction_frame(captured[0]), "通知类交互不得携带倒计时"
    # 对照组：普通确认交互必须带倒计时（防过度收紧）
    plain = r4xday137b_store.register(session_id="s1", kind="confirm", payload={"prompt": "?"})
    assert "expires_in" in _interaction_frame(plain)


# ==================== day137b2_attachments ====================
# 功能：Day137b-2 契约——对话附件通道（消息绑定落库 + 文本类内容注入）


def r5xday137b2__isolate_home(monkeypatch, tmp_path: Path) -> None:
    """功能：隔离 HOME/USERPROFILE（记忆/日志/checkpoint 全落 tmp，不碰真实 ~/.bandi）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def _session_store(tmp_path: Path):
    from bandi.memory.session.store import SessionStore

    return SessionStore(str(tmp_path / "sessions.db"))


# ── A 元数据落库 ────────────────────────────────────────────────────────────


class TestAttachmentMetadataPersistence:
    def test_attachment_meta_roundtrip_and_absent_without_attachments(self, tmp_path: Path) -> None:
        """attachments 元数据随 user 消息落库并可读回（前端 chips 数据源）；
        无附件消息不写 attachments 键（旧库形态零变化）。"""
        from bandi.core.message import Message, MessageRole

        r5xday137b2_store = _session_store(tmp_path)
        r5xday137b2_store.create_session(session_id="s1", mode="execute")
        msg = Message(role=MessageRole.USER, content="看看附件")
        msg.meta["attachments"] = [{"upload_id": "u1", "name": "报告.csv", "size": 42}]
        r5xday137b2_store.append_message("s1", msg)

        entries = [
            e for e in r5xday137b2_store.get_branch("s1") if e.get("entry_type") == "message"
        ]
        meta = json.loads(entries[-1]["metadata"])
        assert meta["attachments"] == [{"upload_id": "u1", "name": "报告.csv", "size": 42}]
        assert entries[-1]["content"] == "看看附件"

        r5xday137b2_store.append_message("s1", Message(role=MessageRole.USER, content="普通消息"))

        entries = [
            e for e in r5xday137b2_store.get_branch("s1") if e.get("entry_type") == "message"
        ]
        assert "attachments" not in json.loads(entries[-1]["metadata"])


# ── B 文本类内容提取 ────────────────────────────────────────────────────────


class TestExtractAttachmentText:
    def test_text_file_content(self, tmp_path: Path) -> None:
        """文本类扩展名提取全文内容。"""
        from bandi.company.attachments import extract_attachment_text

        path = tmp_path / "notes.md"
        path.write_bytes("第一行\n第二行".encode())
        assert extract_attachment_text(path) == "第一行\n第二行"

    def test_extract_attachment_text_returns_none_for_unsupported_or_missing(
        self, tmp_path: Path
    ) -> None:
        """二进制类/白名单外扩展名/不存在文件一律返回 None（仅文件名进注记）不抛。"""
        from bandi.company.attachments import extract_attachment_text

        path = tmp_path / "image.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        assert extract_attachment_text(path) is None

        path = tmp_path / "tool.exe"
        path.write_bytes(b"MZ" + b"\x00" * 32)
        assert extract_attachment_text(path) is None

        assert extract_attachment_text(tmp_path / "none.md") is None

    def test_extract_attachment_text_truncates_and_tolerates_invalid_utf8(
        self, tmp_path: Path
    ) -> None:
        """超长文本截断到上限并带截断标记；非法 UTF-8 字节容错解码不抛。"""
        from bandi.company.attachments import (
            MAX_ATTACHMENT_TEXT_BYTES,
            extract_attachment_text,
        )

        path = tmp_path / "big.txt"
        path.write_text("长" * (MAX_ATTACHMENT_TEXT_BYTES + 5000), encoding="utf-8")
        text = extract_attachment_text(path)
        assert text is not None
        assert "截断" in text
        assert len(text.encode("utf-8")) < MAX_ATTACHMENT_TEXT_BYTES + 5000

        broken = tmp_path / "broken.txt"
        broken.write_bytes(b"abc\xff\xfe\x00def")
        text = extract_attachment_text(broken)
        assert text is not None and "abc" in text


# ── C run_agent_turn 请求注入 + 落库 ────────────────────────────────────────


class _RecordingMock:
    """功能：MockProvider 子类——记录请求 messages（complete + stream 双通道）。"""

    def __new__(cls, captured: list[list[dict]]):
        from bandi.llm.mock import MockProvider

        class _Mock(MockProvider):
            async def complete(self, messages, tools=None, **kwargs):  # type: ignore[override]
                captured.append([dict(m) for m in messages])
                return await super().complete(messages, tools, **kwargs)

            async def stream(self, messages, tools=None, **kwargs):  # type: ignore[override]
                captured.append([dict(m) for m in messages])
                async for event in super().stream(messages, tools, **kwargs):
                    yield event

        return _Mock()


@pytest.mark.asyncio
async def test_run_agent_turn_injects_attachment_notes_only_when_present(
    monkeypatch, tmp_path: Path
) -> None:
    """attachments 随回合——entry 落库（text 剔除）+ LLM 请求含【附件】注记；
    无附件回合零注入（请求内容 == 用户原文）。"""
    r5xday137b2__isolate_home(monkeypatch, tmp_path)
    from bandi.core import agent_runner as runner_mod

    captured: list[list[dict]] = []
    monkeypatch.setattr(runner_mod, "build_provider", lambda *_a, **_k: _RecordingMock(captured))
    r5xday137b2_store = _session_store(tmp_path)

    await runner_mod.run_agent_turn(
        user_input="看看附件",
        session_id="s1",
        session_store=r5xday137b2_store,
        attachments=[{"upload_id": "u1", "name": "notes.md", "size": 3, "text": "附件正文"}],
    )

    entries = [e for e in r5xday137b2_store.get_branch("s1") if e.get("entry_type") == "message"]
    user_entry = [e for e in entries if e.get("role") == "user"][-1]
    meta = json.loads(user_entry["metadata"])
    assert meta["attachments"] == [{"upload_id": "u1", "name": "notes.md", "size": 3}]
    assert "text" not in meta["attachments"][0], "附件正文不得落库（体积防护）"
    assert user_entry["content"] == "看看附件", "entry 内容必须是用户原文（不得含注记）"

    assert captured, "LLM 必须被调用"
    last_user = [m for m in captured[0] if m.get("role") == "user"][-1]
    content = str(last_user.get("content") or "")
    assert "【附件】notes.md" in content, "请求必须含附件注记（名称）"
    assert "附件正文" in content, "文本类附件内容必须注入请求"

    captured.clear()
    await runner_mod.run_agent_turn(
        user_input="普通消息",
        session_id="s2",
        session_store=r5xday137b2_store,
    )

    assert captured, "LLM 必须被调用"
    last_user = [m for m in captured[0] if m.get("role") == "user"][-1]
    assert str(last_user.get("content") or "").endswith("普通消息")
    assert "【附件】" not in str(last_user.get("content") or "")


# ── D WS 层附件解析（_handle_ws_chat 载荷 → run_agent_turn 附件列表） ─────────


class TestResolveWsAttachments:
    def _store(self, tmp_path: Path, monkeypatch) -> object:
        import importlib

        app_mod = importlib.import_module("bandi.api.app")
        from bandi.company.attachments import AttachmentStore

        r5xday137b2_store = AttachmentStore(str(tmp_path))
        monkeypatch.setattr(app_mod, "_attachment_store", r5xday137b2_store)
        return r5xday137b2_store

    def test_valid_text_attachment(self, tmp_path: Path, monkeypatch) -> None:
        """有效引用 → upload_id/name/size + 文本类内容提取。"""
        r5xday137b2_store = self._store(tmp_path, monkeypatch)
        rec = r5xday137b2_store.save(
            "hello 内容".encode(), filename="报告.md", project_id="company"
        )
        from bandi.api.app import _resolve_ws_attachments

        out = _resolve_ws_attachments([{"upload_id": rec.upload_id, "name": "报告.md"}], "")
        assert out[0]["upload_id"] == rec.upload_id
        assert out[0]["name"] == "报告.md"
        assert out[0]["size"] == len("hello 内容".encode())
        assert out[0]["text"] == "hello 内容"

    def test_binary_attachment_no_text(self, tmp_path: Path, monkeypatch) -> None:
        """二进制附件不带 text 字段（仅名称进注记）。"""
        r5xday137b2_store = self._store(tmp_path, monkeypatch)
        rec = r5xday137b2_store.save(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, filename="图.png", project_id="company"
        )
        from bandi.api.app import _resolve_ws_attachments

        out = _resolve_ws_attachments([{"upload_id": rec.upload_id, "name": "图.png"}], "")
        assert out and "text" not in out[0]

    def test_resolve_ws_attachments_drops_invalid_and_guards_shape(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """（破坏者）：不存在的 upload_id 剔除（消息本体照发）；非 list / 非 dict 元素 /
        空 id 全容错为空。"""
        self._store(tmp_path, monkeypatch)
        from bandi.api.app import _resolve_ws_attachments

        assert _resolve_ws_attachments([{"upload_id": "nonexistent"}], "") == []
        assert _resolve_ws_attachments(None, "") == []
        assert _resolve_ws_attachments("bad", "") == []
        assert _resolve_ws_attachments([42, {"upload_id": ""}], "") == []


class TestWsProviderConfig:
    def test_ws_provider_config_maps_overrides_else_none(self) -> None:
        """无 model/thinking_mode → None（跟随全局，零行为变化）；model + thinking_mode →
        provider_config 键（会话级覆盖）；仅 model → 单键（thinking 缺省不改）。"""
        from bandi.api.app import _ws_provider_config

        assert _ws_provider_config({}) is None
        assert _ws_provider_config({"model": "  ", "thinking_mode": ""}) is None
        assert _ws_provider_config({"model": "deepseek-v4-flash", "thinking_mode": "high"}) == {
            "default_model": "deepseek-v4-flash",
            "thinking_mode": "high",
        }
        assert _ws_provider_config({"model": "x"}) == {"default_model": "x"}


# ==================== day137b2_compact ====================
# 功能：Day137b-2 契约——会话压缩服务（compact：单块全量摘要 + fork 新会话）


def r6xday137b2__isolate_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def _seed_session(r6xday137b2_store, session_id: str = "s1", pairs: int = 3) -> None:
    from bandi.core.message import Message, MessageRole

    r6xday137b2_store.create_session(session_id=session_id, mode="execute", title="测试会话")
    for i in range(pairs):
        r6xday137b2_store.append_message(
            session_id,
            Message(role=MessageRole.USER, content=f"用户问题{i} " + "细节内容" * 30),
        )
        r6xday137b2_store.append_message(
            session_id,
            Message(role=MessageRole.ASSISTANT, content=f"助手回答{i} " + "回答正文" * 60),
        )


class TestCompactService:
    @pytest.mark.asyncio
    async def test_compact_session_rejects_missing_and_short_sessions(self, tmp_path: Path) -> None:
        """会话不存在 → session not found；消息过少 → too few messages（不抛异常）。"""
        from bandi.core.session_compact import compact_session
        from bandi.llm.mock import MockProvider
        from bandi.memory.session.store import SessionStore

        r6xday137b2_store = SessionStore(str(tmp_path / "s.db"))
        result = await compact_session(r6xday137b2_store, "nope", MockProvider())
        assert result.success is False and result.reason == "session not found"

        _seed_session(r6xday137b2_store, "s0", pairs=1)
        result = await compact_session(r6xday137b2_store, "s0", MockProvider())
        assert result.success is False and result.reason == "too few messages"

    @pytest.mark.asyncio
    async def test_compact_success_forks_and_keeps_original(self, tmp_path: Path) -> None:
        """+压缩成功 fork 新会话（含摘要），原会话消息数不变。"""
        from bandi.core.session_compact import compact_session
        from bandi.llm.mock import MockProvider
        from bandi.memory.session.store import SessionStore

        r6xday137b2_store = SessionStore(str(tmp_path / "s.db"))
        _seed_session(r6xday137b2_store, "s1", pairs=3)
        result = await compact_session(r6xday137b2_store, "s1", MockProvider())

        assert result.success is True
        assert result.new_session_id and result.new_session_id != "s1"
        assert result.after_messages < result.before_messages
        assert result.has_summary is True
        assert result.stage and result.stage != "none"

        from bandi.core.message import message_from_entry

        new_entries = r6xday137b2_store.get_branch(result.new_session_id)
        new_messages = [
            message_from_entry(e) for e in new_entries if e.get("entry_type") == "message"
        ]
        assert any("[上下文摘要]" in (m.content or "") for m in new_messages)

        original = [
            e for e in r6xday137b2_store.get_branch("s1") if e.get("entry_type") == "message"
        ]
        assert len(original) == 6, "append-only：原会话消息必须原样保留"


class TestCompactEndpoint:
    def _wire(self, tmp_path: Path, monkeypatch):
        """功能：端点直调装配（monkeypatch r6xday137b2_store 单例——隔离；鉴权在 HTTP 层，直调不经过）。"""
        r6xday137b2__isolate_home(monkeypatch, tmp_path)
        app_mod = importlib.import_module("bandi.api.app")
        from bandi.memory.session.store import SessionStore

        r6xday137b2_store = SessionStore(str(tmp_path / "api.db"))
        monkeypatch.setattr(app_mod, "_store", r6xday137b2_store)
        return app_mod, r6xday137b2_store

    @pytest.mark.asyncio
    async def test_compact_endpoint_returns_404_or_success_payload(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """不存在会话 → 404（HTTPException）；成功响应含 compacted/new_session_id/
        before/after 且产物可读回。"""
        from fastapi import HTTPException

        app_mod, r6xday137b2_store = self._wire(tmp_path, monkeypatch)
        with pytest.raises(HTTPException) as excinfo:
            await app_mod.compact_session_endpoint("nope")
        assert excinfo.value.status_code == 404

        _seed_session(r6xday137b2_store, "s1", pairs=3)
        body = await app_mod.compact_session_endpoint("s1")
        assert body["compacted"] is True
        assert body["new_session_id"]
        assert body["after"] < body["before"]
        assert body["has_summary"] is True


# ==================== day137b2_files_models ====================
# 功能：Day137b-2 契约——模型候选列表（控制条数据源）+ 项目文件列表（@文件引用数据源）


class TestAvailableModels:
    @pytest.mark.asyncio
    async def test_available_models_fields(self) -> None:
        """候选模型列表字段齐备（前端按 supports_reasoning 显示思考模式）。"""
        app_mod = importlib.import_module("bandi.api.app")
        body = await app_mod.get_model()
        models = body["available_models"]
        assert isinstance(models, list) and models, "候选列表非空"
        first = models[0]
        assert {"name", "provider", "supports_reasoning", "reasoning_efforts"} <= set(first)
        assert all(item["name"] != "mock" for item in models), "mock 占位不进候选"


class TestProjectFiles:
    def _tree(self, tmp_path: Path) -> Path:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x", encoding="utf-8")
        (tmp_path / "README.md").write_text("x", encoding="utf-8")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "lib.js").write_text("x", encoding="utf-8")
        return tmp_path

    def test_list_project_files_lists_and_filters(self, tmp_path: Path) -> None:
        """遍历含文件 + 忽略目录剪枝（node_modules 不进结果）；query 子串过滤
        （不区分大小写）。"""
        from bandi.company.projects import list_project_files

        root = self._tree(tmp_path)
        paths = {item["path"] for item in list_project_files(str(root))}
        assert "src/app.py" in paths and "README.md" in paths
        assert not any("node_modules" in p for p in paths)

        filtered = list_project_files(str(root), query="APP")
        assert filtered and all("app" in item["path"].lower() for item in filtered)

    def test_empty_or_missing_root(self, tmp_path: Path) -> None:
        """（破坏者）：root 空白 / 目录不存在 → 空列表不抛。"""
        from bandi.company.projects import list_project_files

        assert list_project_files("") == []
        assert list_project_files(str(tmp_path / "nope")) == []


class TestProjectFilesEndpoint:
    def _app(self, root_dir: Path | None):
        app_mod = importlib.import_module("bandi.api.app")

        class _Record:
            def __init__(self) -> None:
                self.root_dir = str(root_dir) if root_dir is not None else ""

        class _FakeStore:
            def get_project(self, project_id: str):
                return _Record() if project_id == "p1" else None

        return app_mod, _FakeStore

    @pytest.mark.asyncio
    async def test_project_files_endpoint_lists_or_404(self, tmp_path: Path, monkeypatch) -> None:
        """端点返回 files + root_dir（含真实文件）；项目不存在 → 404。"""
        from fastapi import HTTPException

        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        app_mod, fake = self._app(tmp_path)
        monkeypatch.setattr(app_mod, "_get_project_store", lambda: fake())
        body = await app_mod.list_project_files_endpoint("p1")
        assert body["root_dir"] == str(tmp_path)
        assert any(item["name"] == "a.py" for item in body["files"])

        with pytest.raises(HTTPException) as excinfo:
            await app_mod.list_project_files_endpoint("nope")
        assert excinfo.value.status_code == 404


# ==================== day137b2b_llm_profiles ====================
# 功能：Day137b-2b 契约——多 Provider 档案（LlmProfile/ProfileStore/resolve 唯一解析入口）
# 钉行为族（不钉实现）：
# ① 档案 CRUD + 首次创建自动激活 + 删除激活档案后激活指针清空；
# ② resolve_llm_config 覆盖语义（有档案覆盖模型/凭证/地址/白名单高级项；无档案原样返回）；
# ③ 破坏者：API 出参与摘要**绝不泄露明文 key**（掩码），落盘为 密文；
# ④ 破坏者：坏文件（非 JSON / 非对象）→ resolve fail-open 回退 base（不得阻断对话主链路）；
# ⑤ 破坏者：profile_id 路径穿越与 extra 未知字段被构造期拒绝；
# ⑥ 生产装配路径端到端：激活档案 → ProviderRegistry 构造出的 provider 真实使用档案凭证；
# ⑧ 离线兜底：内置目录中「适配层确认接受 effort」的模型有非空挡位。


client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 档案路径隔离到 tmp（单例重置——防测试写到真实 ~/.bandi）
    reset_store_singleton(tmp_path / "llm_profiles.json")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with patch("bandi.api.app._API_KEY", "test"):
        yield
    reset_store_singleton()


def _make_profile(
    r8xday137b2b_store: ProfileStore, profile_id: str = "p1", **overrides: object
) -> LlmProfile:
    data = {
        "profile_id": profile_id,
        "name": "深寻主力",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "api_key": "sk-abcdefgh12345678",
        "base_url": "",
        "extra": {"thinking_mode": "high", "max_output_tokens": 4096},
    }
    data.update(overrides)
    profile = LlmProfile(**data)
    r8xday137b2b_store.upsert(profile)
    return profile


# ── ① CRUD / 激活语义 ────────────────────────────────────────────────────
def test_profile_crud_and_activation(tmp_path: Path) -> None:
    r8xday137b2b_store = ProfileStore(tmp_path / "llm_profiles.json")
    assert r8xday137b2b_store.list_profiles() == []
    assert r8xday137b2b_store.active_profile() is None

    _make_profile(r8xday137b2b_store)
    assert [item.profile_id for item in r8xday137b2b_store.list_profiles()] == ["p1"]
    # 首次创建即自动激活（「配置完就能用」）
    assert r8xday137b2b_store.active_profile_id() == "p1"

    _make_profile(r8xday137b2b_store, "p2", model="gpt-5-mini", provider="openai")
    r8xday137b2b_store.activate("p2")
    assert r8xday137b2b_store.active_profile_id() == "p2"
    # 两档案并存（B 方案：跨 provider 不丢 key）
    assert {item.profile_id for item in r8xday137b2b_store.list_profiles()} == {"p1", "p2"}
    assert (
        r8xday137b2b_store.get("p1") is not None
        and r8xday137b2b_store.get("p1").api_key == "sk-abcdefgh12345678"
    )  # type: ignore[union-attr]

    assert r8xday137b2b_store.delete("p1") is True
    assert r8xday137b2b_store.delete("p1") is False
    assert r8xday137b2b_store.active_profile_id() == "p2"

    # 删除**激活**档案 → 激活指针清空（回落到 config.yaml，不静默改指他人）
    assert r8xday137b2b_store.delete("p2") is True
    assert r8xday137b2b_store.active_profile_id() == ""
    assert r8xday137b2b_store.active_profile() is None


# ── ② resolve 覆盖语义 ───────────────────────────────────────────────────
def test_resolve_llm_config_applies_profile_and_keeps_base_blanks(tmp_path: Path) -> None:
    """resolve 覆盖语义：无档案原样返回；有档案覆盖模型/凭证/白名单高级项且不改原对象；
    档案字段为空白不得把 base 里的值清掉（破坏者视角反面）。"""
    r8xday137b2b_store = ProfileStore(tmp_path / "llm_profiles.json")
    base = LLMConfig(api_key="base-key", default_model="deepseek-v4-flash", timeout=60)

    # 无档案 → 原样返回（零迁移：跟随 config.yaml）
    assert resolve_llm_config(base) is base

    _make_profile(r8xday137b2b_store)
    resolved = resolve_llm_config(base)
    assert resolved.default_model == "deepseek-v4-pro"
    assert resolved.api_key == "sk-abcdefgh12345678"
    assert resolved.thinking_mode == "high"  # extra 白名单项覆盖
    assert resolved.max_output_tokens == 4096
    assert resolved.timeout == 60  # 档案未提供的字段保持 base
    assert base.api_key == "base-key"  # 不改原对象

    r8xday137b2b_store.deactivate()
    assert resolve_llm_config(base) is base

    _make_profile(r8xday137b2b_store, base_url="")
    blank_base = LLMConfig(api_key="k", base_url="https://gw.example/v1")
    resolved_blank = resolve_llm_config(blank_base)
    assert resolved_blank.base_url == "https://gw.example/v1"


# ── ③ 破坏者：明文 key 不得泄露 ──────────────────────────────────────────
def test_plain_key_never_leaks_and_mask_put_keeps_stored_key(tmp_path: Path) -> None:
    """破坏者：档案文件/API 出参/摘要绝不泄露明文 key（掩码）；PUT 携带掩码时真实 key
    不得被掩码覆盖（用户只改备注）。"""
    r8xday137b2b_store = ProfileStore(tmp_path / "llm_profiles.json")
    _make_profile(r8xday137b2b_store)
    plain = "sk-abcdefgh12345678"

    # 落盘是密文（可用时）；不可用时至少不得直接等于明文
    raw = (tmp_path / "llm_profiles.json").read_text(encoding="utf-8")
    assert plain not in raw, "档案文件不得出现明文 key"

    listed = client.get("/api/settings/llm/profiles").json()
    assert listed["profiles"][0]["api_key"] == mask_api_key(plain)
    assert plain not in json.dumps(listed, ensure_ascii=False)
    assert listed["profiles"][0]["has_api_key"] is True

    summary = active_profile_summary()
    assert summary is not None and summary["api_key_masked"] == mask_api_key(plain)
    assert plain not in json.dumps(summary, ensure_ascii=False)

    body = {
        "name": "改名了",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "api_key": mask_api_key("sk-abcdefgh12345678"),
        "extra": {},
    }
    resp = client.put("/api/settings/llm/profiles/p1", json=body)
    assert resp.status_code == 200, resp.text
    assert ProfileStore(tmp_path / "llm_profiles.json").get("p1").api_key == "sk-abcdefgh12345678"  # type: ignore[union-attr]


# ── ④ 破坏者：坏文件 fail-open + API 显式报错 ─────────────────────────────
def test_corrupt_file_fails_open_for_resolve_and_400_for_api(tmp_path: Path) -> None:
    path = tmp_path / "llm_profiles.json"
    path.write_text("{not json", encoding="utf-8")
    base = LLMConfig(api_key="base-key", default_model="deepseek-v4-flash")

    # 对话主链路不得因坏文件中断
    assert resolve_llm_config(base) is base
    assert active_profile_summary() is None

    with pytest.raises(ProfilesFileError):
        ProfileStore(path).list_profiles()

    resp = client.get("/api/settings/llm/profiles")
    assert resp.status_code == 400
    assert "llm_profiles.json" in resp.json()["message"]


# ── ⑤ 破坏者：构造期拒绝非法输入 ─────────────────────────────────────────
@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "", "x" * 65, "有中文"])
def test_reject_path_traversal_and_bad_profile_id(bad_id: str) -> None:
    with pytest.raises(ValueError):
        LlmProfile(profile_id=bad_id, model="deepseek-v4-flash")


def test_profile_constructor_rejects_unknown_extra_and_empty_model() -> None:
    with pytest.raises(ValueError):
        LlmProfile(profile_id="ok", model="m", extra={"not_a_llm_field": 1})
    with pytest.raises(ValueError):
        LlmProfile(profile_id="ok", model="   ")


# ── ⑥ 生产装配路径端到端：provider 真的吃到档案凭证 ──────────────────────
def test_registry_uses_active_profile_credentials(tmp_path: Path) -> None:
    from bandi.llm.registry import ProviderRegistry

    r8xday137b2b_store = ProfileStore(tmp_path / "llm_profiles.json")
    _make_profile(r8xday137b2b_store, base_url="https://proxy.example/v1", provider="openai_compat")

    cfg = BandiConfig()
    cfg.llm.api_key = "config-key"
    cfg.llm.default_model = "deepseek-v4-flash"
    cfg.llm.base_url = ""

    registry = ProviderRegistry(cfg)
    provider = registry.get_provider("deepseek-v4-pro", cfg)
    assert getattr(provider.config, "api_key", "") == "sk-abcdefgh12345678"
    assert getattr(provider.config, "default_model", "") == "deepseek-v4-pro"
    assert getattr(provider.config, "base_url", "") == "https://proxy.example/v1"


def test_build_provider_uses_profile_key_without_config_key(tmp_path: Path) -> None:
    from bandi.agent.factory import build_provider
    from bandi.llm.mock import MockProvider

    r8xday137b2b_store = ProfileStore(tmp_path / "llm_profiles.json")
    _make_profile(r8xday137b2b_store)

    cfg = BandiConfig()
    cfg.llm.api_key = ""  # config.yaml 没配 key → 旧实现必然回退 Mock

    provider = build_provider(cfg)
    assert not isinstance(provider, MockProvider), "档案里有 key 时不得回退 MockProvider"
    assert getattr(provider.config, "api_key", "") == "sk-abcdefgh12345678"


def test_builtin_reasoning_efforts_offline_fallback() -> None:
    from bandi.llm.model_catalog import BUILTIN_MODELS
    from bandi.llm.providers.deepseek import DeepSeekProvider

    deepseek = BUILTIN_MODELS["deepseek-v4-pro"]
    assert deepseek.supports_reasoning is True
    # 兜底挡位必须是适配层真接受的值域（否则滑杆会给出打挂请求的档位）
    assert set(deepseek.reasoning_efforts) <= set(DeepSeekProvider._EFFORT_MODES)
    assert deepseek.reasoning_efforts, "离线（未联网同步）时挡位不得为空"

    flash_v41 = BUILTIN_MODELS["deepseek-flash"]
    assert flash_v41.supports_reasoning is True
    assert set(flash_v41.reasoning_efforts) <= set(DeepSeekProvider._EFFORT_MODES)
    assert flash_v41.reasoning_efforts, "离线（未联网同步）时挡位不得为空"

    from bandi.llm.providers.openai import OpenAIProvider

    for name in (
        "gpt-5.2",
        "gpt-5.2-pro",
        "gpt-5.1",
        "o3",
        "gpt-6-astra",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "gpt-5.6-luna",
    ):
        efforts = BUILTIN_MODELS[name].reasoning_efforts
        assert efforts, f"{name} 是推理模型，离线兜底不得为空"
        assert set(efforts) <= set(OpenAIProvider._EFFORT_MAP), f"{name} 挡位超出适配层值域"

    # 非推理模型不得被编造挡位
    assert BUILTIN_MODELS["gpt-4.1"].reasoning_efforts == []

    # 未接线 provider 一律不得**被兜底表编造挡位**（防「滑杆给了档位但请求根本不带 thinking」
    # 的假支持）。
    # 断言对象 = **离线兜底源表** `_BUILTIN_REASONING_EFFORTS`，而不是运行时 `BUILTIN_MODELS`：
    # models.dev 同步会**原地覆写**目录实例（设计如此——拿到真实数据才覆写），若断言运行时目录
    from bandi.llm.model_catalog import _BUILTIN_REASONING_EFFORTS

    thinking_wired = ("deepseek", "openai", "anthropic", "gemini", "moonshot", "zhipu")
    for name in _BUILTIN_REASONING_EFFORTS:
        assert BUILTIN_MODELS[name].provider in thinking_wired, (
            f"{name}（{BUILTIN_MODELS[name].provider}）适配层尚未接线 thinking ⇒ 不得进入兜底表"
        )

    # -C 加固：新接线四家的挡位必须落在**各自适配层映射表值域内**——
    # 防「滑杆给了档位但适配层不认（或静默丢弃）」，与 deepseek/openai 同款断言
    from bandi.llm.adapters.anthropic import _ANTHROPIC_EFFORT
    from bandi.llm.adapters.google import _GEMINI_LEVEL
    from bandi.llm.providers.moonshot import _KIMI_EFFORT
    from bandi.llm.providers.zhipu import _GLM_EFFORT

    domains = {
        "anthropic": _ANTHROPIC_EFFORT,
        "gemini": _GEMINI_LEVEL,
        "moonshot": _KIMI_EFFORT,
        "zhipu": _GLM_EFFORT,
    }
    checked = 0
    for name, efforts in _BUILTIN_REASONING_EFFORTS.items():
        provider = BUILTIN_MODELS[name].provider
        domain = domains.get(provider)
        if domain is None:
            continue
        assert set(efforts) <= set(domain), (
            f"{name} 挡位超出 {provider} 适配层值域：{efforts} ⊄ {sorted(domain)}"
        )
        checked += 1
    assert checked >= 10, f"新接线四家必须真被覆盖（防断言空转）：checked={checked}"


def test_model_endpoint_exposes_active_profile(tmp_path: Path) -> None:
    r8xday137b2b_store = ProfileStore(tmp_path / "llm_profiles.json")
    _make_profile(r8xday137b2b_store)
    payload = client.get("/api/settings/model").json()
    assert payload["active_profile"]["profile_id"] == "p1"
    assert payload["effective_model"] == "deepseek-v4-pro"
    assert payload["effective_has_api_key"] is True
