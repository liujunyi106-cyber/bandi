# 功能：契约——checkpoint 恢复不盲目重放副作用工具（并发工程 批次4 / ）
#
# 直接回 **重放**——副作用工具靠 idempotency store 兜底而非 exactly-once
# 审批 JSON 重放/重复执行有实录。本契约钉：
# ① 只读判定 fail-closed——registry 元数据（executor 去重同源 is_read_only）优先
# 无 registry 回落显式 RECOVERY_SAFE_READONLY_TOOLS；名字不在只读安全集合
# （或查不到）一律按副作用处理（进入派发记录判定，不得视为只读）；
# ② 精确「未启动可重放」——副作用调用已记录派发（dispatched）→ 结果未知不重放；
# 未记录派发（未启动）→ 可安全重放（首次真实执行，保持恢复续跑语义）；
# 空 id 无法匹配派发记录 → fail-closed 不重放；
# ③ 派发记录与只读循环计数跨 checkpoint 序列化往返不丢。
from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

import pytest

from bandi.agent.base import TerminalReason
from bandi.core.message import Message, MessageRole, ToolCall
from bandi.tools.base import BaseTool


def _ctx_with_pending(calls: list[ToolCall], *, raw_input: str = ""):
    from bandi.agent.base import ExecutionContext
    from bandi.core.event_bus import EventBus

    ctx = ExecutionContext(messages=[], provider=object(), config=object(), bus=EventBus())
    ctx.messages.append(
        Message(role=MessageRole.ASSISTANT, content="调用工具", tool_calls=list(calls))
    )
    ctx.current_tool_calls = list(calls)
    ctx.raw_input = raw_input
    return ctx


def _stub_loop(monkeypatch: Any):
    from bandi.agent.loop import AgentLoop

    loop = AgentLoop(max_iterations=3)
    seen: dict[str, Any] = {}

    async def fake_perception(ctx: Any) -> None:
        return None

    async def fake_run_loop(ctx: Any, max_turns: int, turns_taken: int) -> TerminalReason:
        seen["state"] = ctx.current_state
        seen["calls"] = [tc.id for tc in (ctx.current_tool_calls or [])]
        return TerminalReason.COMPLETED

    monkeypatch.setattr(loop, "_wire_safety", lambda ctx: None)
    monkeypatch.setattr(loop, "_run_perception", fake_perception)
    monkeypatch.setattr(loop, "_run_loop", fake_run_loop)
    return loop, seen


def _tool_msgs(ctx: Any) -> list[Message]:
    return [m for m in ctx.messages if m.role == MessageRole.TOOL]


@pytest.mark.asyncio
async def test_side_effect_pending_not_replayed(monkeypatch: Any) -> None:
    """核心：已记录派发的副作用工具 pending → 不重放；注入「结果未知」交模型决策。"""
    loop, seen = _stub_loop(monkeypatch)
    ctx = _ctx_with_pending([ToolCall(id="t1", name="bash", arguments={"cmd": "rm -rf data"})])
    ctx.meta["dispatched_tool_call_ids"] = {"t1"}

    await loop.run(ctx)

    assert seen["state"] == "init", "已派发的副作用 pending 不得直接进 EXECUTING 重放"
    assert seen["calls"] == [], "副作用调用不得保留在重放集合"
    msgs = _tool_msgs(ctx)
    assert [m.tool_call_id for m in msgs] == ["t1"], "必须为副作用调用合成结果消息"
    assert "结果未知" in (msgs[0].content or ""), "文案必须显式告知结果未知"
    assert (msgs[0].meta or {}).get("outcome_unknown") is True
    assert ctx.current_state == "init"


@pytest.mark.asyncio
async def test_readonly_pending_still_replayed(monkeypatch: Any) -> None:
    """回归：只读工具 pending → 仍安全重试（保持既有恢复语义）。"""
    loop, seen = _stub_loop(monkeypatch)
    ctx = _ctx_with_pending([ToolCall(id="t2", name="read", arguments={"path": "a.txt"})])

    await loop.run(ctx)

    assert seen["state"] == "executing", "只读工具应保持重放路径"
    assert seen["calls"] == ["t2"]
    assert not _tool_msgs(ctx), "只读重放不得注入未知结果消息"


@pytest.mark.asyncio
async def test_mixed_pending_partitions(monkeypatch: Any) -> None:
    """混合批：已派发的副作用注入 UNKNOWN，只读保留重放（互不牵连）。"""
    loop, seen = _stub_loop(monkeypatch)
    ctx = _ctx_with_pending(
        [
            ToolCall(id="w1", name="write", arguments={"path": "x", "content": "y"}),
            ToolCall(id="r1", name="read", arguments={"path": "a.txt"}),
        ]
    )
    ctx.meta["dispatched_tool_call_ids"] = {"w1"}

    await loop.run(ctx)

    assert seen["calls"] == ["r1"], "只读调用应重放"
    msgs = _tool_msgs(ctx)
    assert [m.tool_call_id for m in msgs] == ["w1"], "副作用调用注入未知结果"
    assert seen["state"] == "executing"


@pytest.mark.asyncio
async def test_new_input_recovery_goes_init(monkeypatch: Any) -> None:
    """带新输入恢复：清 pending 走 INIT 正常路径（既有语义保持）。"""
    loop, seen = _stub_loop(monkeypatch)
    ctx = _ctx_with_pending([ToolCall(id="t1", name="bash", arguments={})], raw_input="换个问题")

    await loop.run(ctx)

    assert seen["state"] == "init"
    assert seen["calls"] == []
    assert not _tool_msgs(ctx), "新输入路径不得注入未知结果（pending 被正常清除）"


@pytest.mark.asyncio
async def test_unknown_outcome_injection_is_idempotent(monkeypatch: Any) -> None:
    """幂等：同 id 的未知结果消息不得重复注入（恢复重跑场景）。"""
    loop, _ = _stub_loop(monkeypatch)
    ctx = _ctx_with_pending([ToolCall(id="t1", name="bash", arguments={})])
    ctx.meta["dispatched_tool_call_ids"] = {"t1"}
    ctx.messages.append(
        Message(
            role=MessageRole.TOOL,
            content="该工具可能已执行，结果未知，请先核实外部状态或询问用户，不要盲目重试。",
            tool_call_id="t1",
            name="bash",
            meta={"outcome_unknown": True},
        )
    )

    await loop.run(ctx)

    assert len(_tool_msgs(ctx)) == 1, "同 id 未知结果不得重复注入"


# ── 升级：只读安全集合 fail-closed + 精确「未启动可重放」+ 派发记录持久化 ──


@pytest.fixture(scope="module")
def real_registry(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """功能：隔离 home 下构建真实生产注册表（全量工具名 + is_read_only 元数据）。"""
    import pathlib

    import bandi.knowledge.store as knowledge_store_mod
    from bandi.agent.factory import build_tool_registry

    monkeypatch = pytest.MonkeyPatch()
    home = tmp_path_factory.mktemp("d2-home")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(knowledge_store_mod, "_SHARED_STORE", None)
    try:
        from bandi.ontology.spec import open_ontology
        from bandi.ontology.triple_store import OntologyStore

        ontology = OntologyStore(open_ontology(str(home / "ontology.db")))
        yield build_tool_registry(triple_ontology_store=ontology)
    finally:
        monkeypatch.undo()


def test_registry_partition_matches_readonly_metadata(real_registry: Any) -> None:
    """参数化枚举注册表全部工具名：只有「元数据只读且恢复安全」的工具可重放
    其余一律 unknown（含元数据只读但恢复不安全的 mode 切换工具）。

    「已派发」集合成员条件（最保守场景）——凡非只读工具，恢复时不得重放。
    """
    from bandi.agent.loop import RECOVERY_UNSAFE_READONLY_TOOLS, partition_recovery_tool_calls

    tools = real_registry.list_tools()
    assert tools, "生产注册表不得为空（fixture 构建失败会掩盖全部成员）"
    wrong_replayable: list[str] = []
    wrong_unknown: list[str] = []
    for tool in tools:
        call = ToolCall(id=f"c-{tool.name}", name=tool.name, arguments={})
        replayable, unknown = partition_recovery_tool_calls(
            [call], dispatched_ids={call.id}, registry=real_registry
        )
        safe_readonly = (
            getattr(tool, "is_read_only", False) and tool.name not in RECOVERY_UNSAFE_READONLY_TOOLS
        )
        if safe_readonly:
            if replayable != [call] or unknown:
                wrong_replayable.append(tool.name)
        elif unknown != [call] or replayable:
            wrong_unknown.append(tool.name)
    assert not wrong_replayable, f"只读工具被误判副作用（不得重放）: {wrong_replayable}"
    assert not wrong_unknown, f"副作用工具被误判可重放（重复执行风险）: {wrong_unknown}"


SIDE_EFFECT_PROBES = (
    "send_dm",
    "broadcast_issue",
    "start_meeting",
    "close_human_review",
    "spawn_thread_task",
    "cron_create",
    "db_execute",
    "canvas_publish",
    "knowledge_ingest",
    "memory_forget",
    "folder_operations",
    "web_login",
    "image_generate",
    "subagent_spawn",
)


def test_side_effect_probes_fail_closed_without_registry() -> None:
    """破坏者视角：无 registry 时副作用工具仍按「已派发 → 不重放」处理——
    sender/cron/db 类（原 8 名单之外）不得被旧名单逻辑放行重放。"""
    from bandi.agent.loop import partition_recovery_tool_calls

    for name in SIDE_EFFECT_PROBES:
        call = ToolCall(id=f"c-{name}", name=name, arguments={})
        replayable, unknown = partition_recovery_tool_calls([call], dispatched_ids={call.id})
        assert unknown == [call] and replayable == [], f"{name} 非只读且已派发——恢复不得重放"


def test_readonly_probes_replay_without_registry() -> None:
    """无 registry 回落显式只读集合：常见只读工具（read/grep/web_search）可重放。"""
    from bandi.agent.loop import partition_recovery_tool_calls

    for name in ("read", "grep", "web_search", "list_dir"):
        call = ToolCall(id=f"c-{name}", name=name, arguments={})
        replayable, unknown = partition_recovery_tool_calls([call], dispatched_ids={call.id})
        assert replayable == [call] and unknown == [], f"{name} 是只读工具，应可重放"


def test_recovery_readonly_names_are_real_registered_readonly_tools(real_registry: Any) -> None:
    """只读集合成员必须真实注册且声明 is_read_only（防拼错名/名单死条目）；
    恢复不安全子集（元数据只读但会切模式/再审批）不得留在只读集合；
    反向绑定：内置只读工具（排除不安全子集）都应在集合内。"""
    from bandi.agent.loop import RECOVERY_SAFE_READONLY_TOOLS, RECOVERY_UNSAFE_READONLY_TOOLS

    registered = {t.name: t for t in real_registry.list_tools()}
    missing = sorted(RECOVERY_SAFE_READONLY_TOOLS - registered.keys())
    assert not missing, f"只读集合死条目（registry 无此注册名）: {missing}"
    not_readonly = sorted(
        n for n in RECOVERY_SAFE_READONLY_TOOLS if not getattr(registered[n], "is_read_only", False)
    )
    assert not not_readonly, (
        f"只读集合成员未声明 is_read_only（恢复重放有副作用风险）: {not_readonly}"
    )
    unsafe_in_set = sorted(RECOVERY_SAFE_READONLY_TOOLS & RECOVERY_UNSAFE_READONLY_TOOLS)
    assert not unsafe_in_set, f"恢复不安全工具不得留在只读集合: {unsafe_in_set}"
    not_listed = sorted(
        n
        for n, tool in registered.items()
        if getattr(tool, "is_read_only", False)
        and n not in RECOVERY_SAFE_READONLY_TOOLS
        and n not in RECOVERY_UNSAFE_READONLY_TOOLS
    )
    assert not not_listed, f"内置只读工具未进恢复只读集合（恢复降级为不重放，需同步）: {not_listed}"


def test_mode_switching_tools_are_not_replay_safe(real_registry: Any) -> None:
    """破坏者视角：enter/exit_plan_mode 元数据虽是只读，但会切模式/可能再次审批——
    恢复重放有副作用风险，必须判 unknown（有 registry 元数据也不放行）。"""
    from bandi.agent.loop import RECOVERY_SAFE_READONLY_TOOLS, partition_recovery_tool_calls

    for name in ("enter_plan_mode", "exit_plan_mode"):
        assert name not in RECOVERY_SAFE_READONLY_TOOLS, f"{name} 不得留在只读集合"
        call = ToolCall(id=f"c-{name}", name=name, arguments={})
        replayable, unknown = partition_recovery_tool_calls(
            [call], dispatched_ids={call.id}, registry=real_registry
        )
        assert unknown == [call] and replayable == [], (
            f"{name} 恢复重放会再次切模式——必须判 unknown 不重放"
        )


@pytest.mark.asyncio
async def test_undispatched_side_effect_is_replayed(monkeypatch: Any) -> None:
    """精确「未启动可重放」：派发记录存在且本批从未派发 → 副作用调用走重放（真执行）。"""
    loop, seen = _stub_loop(monkeypatch)
    ctx = _ctx_with_pending([ToolCall(id="t1", name="bash", arguments={})])
    ctx.meta["dispatched_tool_call_ids"] = set()

    await loop.run(ctx)

    assert seen["state"] == "executing", "明确未派发的副作用调用应重放"
    assert seen["calls"] == ["t1"]
    assert not _tool_msgs(ctx), "未派发重放不得注入未知结果消息"


@pytest.mark.asyncio
async def test_dispatched_side_effect_is_not_replayed(monkeypatch: Any) -> None:
    """精确「已启动不重放」：派发记录包含该 id → unknown stub，交模型决策。"""
    loop, seen = _stub_loop(monkeypatch)
    ctx = _ctx_with_pending([ToolCall(id="t1", name="bash", arguments={})])
    ctx.meta["dispatched_tool_call_ids"] = {"t1"}

    await loop.run(ctx)

    assert seen["state"] == "init"
    assert seen["calls"] == []
    msgs = _tool_msgs(ctx)
    assert [m.tool_call_id for m in msgs] == ["t1"]
    assert (msgs[0].meta or {}).get("outcome_unknown") is True


@pytest.mark.asyncio
async def test_empty_id_side_effect_cannot_replay_and_warns(monkeypatch: Any, caplog: Any) -> None:
    """空 id 调用无法匹配派发记录（fail-closed 不重放）；注入跳过必须告警。"""
    loop, seen = _stub_loop(monkeypatch)
    ctx = _ctx_with_pending([ToolCall(id="", name="bash", arguments={})])
    ctx.meta["dispatched_tool_call_ids"] = set()

    with caplog.at_level(logging.WARNING):
        await loop.run(ctx)

    assert seen["state"] == "init"
    assert seen["calls"] == [], "空 id 副作用调用不得重放"
    assert not _tool_msgs(ctx), "空 id 合成消息无法配对，不得注入"
    assert any("empty tool_call_id" in r.getMessage() for r in caplog.records), (
        "空 id 未知结果跳过必须 warning"
    )


@pytest.mark.asyncio
async def test_query_state_roundtrip_preserves_recovery_keys() -> None:
    """/dispatched 集合与只读循环计数跨 serialize→deserialize 保留。"""
    from bandi.agent.base import ExecutionContext, QueryState
    from bandi.core.event_bus import EventBus

    ctx = ExecutionContext(messages=[], provider=object(), config=object(), bus=EventBus())
    ctx.meta["dispatched_tool_call_ids"] = {"t1", "t2"}
    ctx.meta["_readonly_loop_sig"] = ("fp-1", "rh-1")
    ctx.meta["_readonly_loop_streak"] = 4

    state = json.loads(json.dumps(dataclasses.asdict(ctx.to_query_state()), ensure_ascii=False))
    restored = ExecutionContext(messages=[], provider=object(), config=object(), bus=EventBus())
    restored.apply_query_state(QueryState(**state))

    assert restored.meta.get("dispatched_tool_call_ids") == {"t1", "t2"}
    assert tuple(restored.meta.get("_readonly_loop_sig") or ()) == ("fp-1", "rh-1")
    assert int(restored.meta.get("_readonly_loop_streak") or 0) == 4


# ── 执行中崩溃 → 恢复不重放副作用（真实 CheckpointStore + 真实 ToolExecutor）──


class _SimulatedCrashSignal(BaseException):
    """模拟进程在工具执行中被杀死——BaseException 不被 except Exception 捕获。"""


class _CountingSideEffectTool(BaseTool):
    """真实注册表内的计数副作用工具：首次执行后模拟崩溃，再次执行即重放。"""

    name = "side_effect_probe"
    description = "test-only counting side-effect probe"
    parameters = {"type": "object", "properties": {}}
    is_read_only = False
    is_concurrency_safe = False
    calls = 0

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        type(self).calls += 1
        if type(self).calls == 1:
            raise _SimulatedCrashSignal()
        return "side-effect-done"


class _ProbeProvider:
    """最小 provider：返回预设响应（首次可带 tool_calls，之后纯文本完成）。"""

    supports_streaming = False

    def __init__(self, tool_calls: list[ToolCall] | None = None, content: str = "完成") -> None:
        self._tool_calls = tool_calls
        self._content = content

    @property
    def model_name(self) -> str:
        return "probe"

    @property
    def protocol(self) -> str:
        return "openai"

    async def complete(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any):
        from bandi.core.message import LLMResponse

        if self._tool_calls is not None:
            calls, self._tool_calls = self._tool_calls, None
            return LLMResponse(content="", tool_calls=calls)
        return LLMResponse(content=self._content)


@pytest.fixture()
def crash_env(tmp_path: Any):
    """功能：真实 registry + 真实 ToolExecutor + 真实 CheckpointStore（tmp 隔离）。"""
    from types import SimpleNamespace

    from bandi.agent.checkpoint import CheckpointStore
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.registry import ToolRegistry

    _CountingSideEffectTool.calls = 0
    registry = ToolRegistry()
    registry.register_instance(_CountingSideEffectTool())
    yield SimpleNamespace(
        registry=registry,
        executor=ToolExecutor(registry=registry, temp_dir=str(tmp_path / "tmp")),
        store=CheckpointStore(db_path=str(tmp_path / "cp.db")),
        sid="sess-f1-crash",
    )


def _crash_ctx(env: Any):
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="执行副作用工具")],
        provider=_ProbeProvider(
            tool_calls=[ToolCall(id="side-1", name="side_effect_probe", arguments={})]
        ),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.session_id = env.sid
    ctx.meta["session_id"] = env.sid
    ctx.tool_executor = env.executor
    ctx.active_tools = env.registry.list_schemas()
    return ctx


@pytest.mark.asyncio
async def test_executing_barrier_checkpoint_contains_dispatched_ids(
    crash_env: Any, monkeypatch: Any
) -> None:
    """红点：executing 入口的执行前屏障 snapshot 必须已含本批派发记录——
    否则工具执行中崩溃后任何 checkpoint 都不含该批 id，恢复会重放副作用。"""
    from bandi.agent.base import QueryState
    from bandi.agent.loop import AgentLoop

    loop = AgentLoop(max_iterations=5, checkpoint_store=crash_env.store)
    monkeypatch.setattr(loop, "_wire_safety", lambda ctx: None)
    with pytest.raises(_SimulatedCrashSignal):
        await loop.run(_crash_ctx(crash_env))
    assert _CountingSideEffectTool.calls == 1, "前置：副作用工具已真实执行一次"

    rows = crash_env.store.list_by_session(crash_env.sid)
    assert rows, "executing 入口必须有 checkpoint（执行前屏障）"
    cp = crash_env.store.load(rows[-1]["id"])
    qs = QueryState(**json.loads(cp.state_json))
    assert "side-1" in (qs.dispatched_tool_call_ids or ()), (
        "执行前屏障 snapshot 必须包含本批派发记录，否则崩溃恢复重放副作用"
    )


@pytest.mark.asyncio
async def test_crash_during_execution_then_recovery_runs_side_effect_once(
    crash_env: Any, monkeypatch: Any
) -> None:
    """核心：工具执行中崩溃 → 从 checkpoint 恢复 → 副作用工具执行次数 == 1。"""
    from bandi.agent.base import ExecutionContext, QueryState
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus

    loop = AgentLoop(max_iterations=5, checkpoint_store=crash_env.store)
    monkeypatch.setattr(loop, "_wire_safety", lambda ctx: None)
    with pytest.raises(_SimulatedCrashSignal):
        await loop.run(_crash_ctx(crash_env))
    assert _CountingSideEffectTool.calls == 1

    cp = crash_env.store.load(crash_env.store.list_by_session(crash_env.sid)[-1]["id"])
    restored = ExecutionContext(
        messages=[
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="side-1", name="side_effect_probe", arguments={})],
            )
        ],
        provider=_ProbeProvider(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    restored.session_id = crash_env.sid
    restored.meta["session_id"] = crash_env.sid
    restored.tool_executor = crash_env.executor
    restored.apply_query_state(QueryState(**json.loads(cp.state_json)))

    loop2 = AgentLoop(max_iterations=5, checkpoint_store=crash_env.store)
    monkeypatch.setattr(loop2, "_wire_safety", lambda ctx: None)
    await loop2.run(restored)

    assert _CountingSideEffectTool.calls == 1, (
        "执行中崩溃恢复后副作用工具不得重放（必须合成 unknown 结果）"
    )


# ── 未知结果注入必须落在 assistant 的 tool 段内（USER 插队不被 guard 丢弃）──


@pytest.mark.asyncio
async def test_unknown_outcome_inserted_into_assistant_tool_segment(monkeypatch: Any) -> None:
    """恢复注入的未知结果 TOOL 消息紧跟 assistant tool 段插入——
    append 到末尾遇 USER 插队会被请求面 guard 判孤儿丢弃（模型看不到未知文案）。"""
    from bandi.agent.tool_message_guard import guard_messages

    loop, _ = _stub_loop(monkeypatch)
    ctx = _ctx_with_pending([ToolCall(id="t1", name="bash", arguments={})])
    ctx.meta["dispatched_tool_call_ids"] = {"t1"}
    ctx.messages.append(Message(role=MessageRole.USER, content="插队消息"))

    await loop.run(ctx)

    roles = [m.role for m in ctx.messages]
    assert roles == [MessageRole.ASSISTANT, MessageRole.TOOL, MessageRole.USER], (
        f"未知结果必须紧跟 assistant 的 tool 段插入（实际 {roles}）"
    )
    guarded = guard_messages(ctx.messages)
    assert any(
        m.role == MessageRole.TOOL and m.tool_call_id == "t1" and "结果未知" in (m.content or "")
        for m in guarded
    ), "请求面前未知结果不得被判孤儿丢弃"


def test_unknown_dispatch_provenance_fails_closed() -> None:
    """契约（五审 ）：旧快照（无派发记录字段）不得被当作未启动——
    副作用调用 provenance 未知时 fail-closed 判 unknown（宁可注入未知也不重放）。"""
    from bandi.agent.loop import partition_recovery_tool_calls

    side = ToolCall(id="s1", name="db_execute", arguments={})
    replayable, unknown = partition_recovery_tool_calls([side], dispatched_ids=None)
    assert unknown == [side] and replayable == []
    replayable, unknown = partition_recovery_tool_calls([side], dispatched_ids=set())
    assert replayable == [side] and unknown == []
    replayable, unknown = partition_recovery_tool_calls([side], dispatched_ids={"s1"})
    assert unknown == [side] and replayable == []
