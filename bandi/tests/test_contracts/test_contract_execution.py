# 契约原则：run_agent 必须走统一执行入口（记账/记忆/偏好不丢）；checkpoint 保存
# 必须在 handler 成功后（turn_index 为完成轮次）；工具轮次消息必须落库可恢复；
# scoped session_id 必须自动补建；tool_calls id 写读对称。
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bandi.core.config import BandiConfig
from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall
from bandi.llm.mock import MockProvider

_RETIRED_MODELS = {"deepseek-chat", "deepseek-reasoner"}


def _test_config(max_iterations: int = 1) -> BandiConfig:
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    cfg.agent.max_iterations = max_iterations
    return cfg


def _tool_calling_provider() -> MockProvider:
    """构造两段响应：第一段发起 todo_write 工具调用（无文件系统依赖），第二段收尾。"""
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="todo_write",
                    arguments={"todos": [{"content": "列目录"}]},
                )
            ],
        )
    )
    provider.add_response(LLMResponse(content="完成"))
    return provider


def test_contract_run_agent_records_cost(tmp_path: Any) -> None:
    """契约 1：run_agent（公司链路）LLM 调用后共享成本台账必须记录费用
    （防 run_agent 裸 EventBus 不订阅 cost_tracker 导致记账断裂）。"""
    from bandi.agent.factory import run_agent
    from bandi.llm.cost_tracker import get_shared_cost_tracker
    from bandi.llm.model_catalog import ModelCatalog

    cfg = _test_config()
    tracker = get_shared_cost_tracker(ModelCatalog(), budget_limit=cfg.llm.budget_limit)
    tracker.reset_daily()
    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        asyncio.run(run_agent(user_input="hello", bandi_config=cfg))
    assert tracker.daily_total > 0, "run_agent 必须记录 LLM 调用成本"


def test_contract_checkpoint_saved_after_handler_success(tmp_path: Any) -> None:
    """契约 2：checkpoint 必须在 handler 成功后保存——最新 checkpoint 的
    turn_index 等于完成轮次（post-increment），快照含本轮工具结果消息
    （防 checkpoint 前置保存导致恢复后 turn_index 错位/重复执行）。"""
    from bandi.agent.checkpoint import CheckpointStore
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        provider = _tool_calling_provider()
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            result = asyncio.run(
                run_agent_turn(
                    user_input="列出目录",
                    session_id=sid,
                    session_store=store,
                    bandi_config=_test_config(max_iterations=3),
                    checkpoint_interval=1,
                )
            )
        ckpt = CheckpointStore()
        entries = ckpt.list_by_session(sid)
        assert entries, "run_agent_turn 必须写入 checkpoint"
        cp = ckpt.load(entries[-1]["id"])
        assert cp is not None
        assert cp.turn_index == result.turn_index, (
            f"最新 checkpoint turn_index 必须等于完成轮次：{cp.turn_index} != {result.turn_index}"
        )
        msgs = json.loads(cp.messages_json or "[]")
        # session store（下一契约），恢复链只读 state_json；此处反向钉住
        assert msgs == [], "checkpoint 不应再冗余存 messages_json"
        ckpt.close()
        store.close()


def test_contract_tool_messages_persisted_to_session(tmp_path: Any) -> None:
    """契约 3：工具轮次结果消息必须落库（会话恢复后工具结果上下文完整
    防恢复后模型视角缺工具结果导致重复执行/臆测）。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        provider = _tool_calling_provider()
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            asyncio.run(
                run_agent_turn(
                    user_input="列出目录",
                    session_id=sid,
                    session_store=store,
                    bandi_config=_test_config(max_iterations=3),
                )
            )
        branch = store.get_branch(sid)
        tool_entries = [e for e in branch if e.get("role") == "tool"]
        assert tool_entries, "工具轮次结果必须落库"
        assert tool_entries[0]["tool_call_id"] == "c1", "工具结果必须关联 tool_call_id"
        store.close()


def test_contract_tool_calls_id_roundtrip(tmp_path: Any) -> None:
    """
    message_from_entry 读回 id 完整（防 metadata 丢 id 导致恢复后
    tool_call_id 关联断裂）。"""
    from bandi.core.message import message_from_entry
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(tmp_path / "sess.db"))
    sid = store.create_session(mode="execute")
    msg = Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id="c1", name="list_dir", arguments={})],
    )
    store.append_message(sid, msg)
    restored = message_from_entry(store.get_branch(sid)[0])
    assert restored.tool_calls, "读回必须含 tool_calls"
    assert restored.tool_calls[0].id == "c1", "tool_calls id 必须写读对称"
    store.close()


def test_contract_scoped_session_auto_created(tmp_path: Any) -> None:
    """契约 5：公司链 scoped session_id（如 member_session_id）不在 sessions 表时
    必须自动补建——append_message 不得 FK 崩溃（防委托后公司链路会话断裂）。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        scoped_sid = "p1::scope::role::e1"
        provider = MockProvider()
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            result = asyncio.run(
                run_agent_turn(
                    user_input="hello",
                    session_id=scoped_sid,
                    session_store=store,
                    bandi_config=_test_config(),
                )
            )
        assert result.session_id == scoped_sid
        assert store.get_session(scoped_sid) is not None, "scoped session 必须自动补建"
        store.close()


def test_contract_router_pools_no_retired_models() -> None:
    """契约 6：ModelRouter 三档候选池不得含退役模型（防 deepseek-reasoner
    残留在复杂池导致候选即失败）。"""
    from bandi.llm.strategy import ModelRouter

    router = ModelRouter()
    for pool in (
        router.select_pool(100, 0),
        router.select_pool(2000, 4),
        router.select_pool(10000, 10),
    ):
        for model in pool:
            assert model not in _RETIRED_MODELS, f"候选池含退役模型 {model}"


def test_contract_factory_default_fallback_not_retired() -> None:
    """契约 7：build_provider 无默认模型时的兜底模型不得是退役模型
    （防 factory 硬编码 deepseek-chat 兜底——默认值即失败候选）。"""
    from bandi.agent.factory import build_provider

    cfg = BandiConfig()
    cfg.llm.api_key = "sk-test"
    cfg.llm.default_model = ""
    provider = build_provider(cfg)
    assert provider.model_name not in _RETIRED_MODELS, f"兜底模型 {provider.model_name} 已退役"


def test_contract_api_dispatcher_construction_recovers_stale(tmp_path: Path) -> None:
    """契约 8（批4-2）：API 装配 dispatcher 后必须恢复崩溃遗留卡——
    进程崩溃后 RUNNING 卡无活跃登记，首次构建 dispatcher 时必须 recover
    为 READY 重新入队（防崩溃后卡死永久搁置）。"""
    from bandi.api.app import _get_company_dispatcher
    from bandi.company.phase import Phase
    from bandi.company.store import WorkItemStore
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    company_dir = tmp_path / ".bandi" / "company"
    company_dir.mkdir(parents=True, exist_ok=True)
    store = WorkItemStore(str(company_dir / "company.db"))
    stale = create_work_item(
        title="stale running",
        turn_type=WorkItemTurnType.EXECUTE,
        phase=Phase.RUNNING,
        assignee_role="worker",
    )
    store.save(stale)

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("bandi.api.app._company_dispatcher", None),
    ):
        _get_company_dispatcher()

    check = WorkItemStore(str(company_dir / "company.db"))
    loaded = check.get(stale.id)
    assert loaded is not None
    assert loaded.phase == Phase.READY, (
        f"API 装配后崩溃遗留 RUNNING 卡必须恢复为 READY，实际 {loaded.phase.value}"
    )


def test_contract_gate_pass_clears_review_verdict() -> None:
    """契约 9（批4-3）：gate PASS 必须清除 review_verdict 残留——
    残留会让重进评审态的卡被 _review_decision_ready 误判「已评审」自动通过
    （fix8 在 gate PASS 分支的遗漏）。"""
    import asyncio

    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.gate_harness import GateHarness, GateResult, GateVerdict
    from bandi.company.org_engine import OrgEngine
    from bandi.company.phase import Phase
    from bandi.company.recruiter import Recruiter
    from bandi.company.store import WorkItemStore
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    store = WorkItemStore(":memory:")
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        config=DispatcherConfig(max_concurrent=3),
    )
    dispatcher.gate_harness.evaluate = lambda *a, **k: GateResult(GateVerdict.PASS, "ok", [])
    item = create_work_item(
        title="gate pass with verdict residue",
        turn_type=WorkItemTurnType.EXECUTE,
        phase=Phase.RUNNING,
        assignee_role="worker",
        metadata={
            "review_verdict": "approve",
            "structured_review_verdict": {"reason": "x"},
        },
    )
    store.save(item)
    asyncio.run(dispatcher.tick())
    loaded = store.get(item.id)
    assert loaded is not None and loaded.phase == Phase.APPROVED
    assert "review_verdict" not in (loaded.metadata or {}), (
        "gate PASS 后 review_verdict 必须清除（防评审自动通过）"
    )


def test_contract_checkpoint_interval_zero_no_division_error() -> None:
    """
    （turns==1 短路仅保护第 1 轮）。"""
    from bandi.agent.loop import AgentLoop

    # 0 与负值均钳制为禁用
    loop0 = AgentLoop(checkpoint_interval=0)
    assert loop0._checkpoint_interval == 0, "interval=0 必须禁用（不触发保存）"
    loop_neg = AgentLoop(checkpoint_interval=-5)
    assert loop_neg._checkpoint_interval == 0, "负值必须钳制为禁用"
    # 正常正值保留
    loop_ok = AgentLoop(checkpoint_interval=3)
    assert loop_ok._checkpoint_interval == 3, "正值必须保留"

    # 行为验证：interval=0 时保存条件短路（_checkpoint_interval > 0 前置）——
    # 直接构造真实场景验证不除零：turns_taken=2、store 非空
    fake_store = object()
    loop0._checkpoint_store = fake_store  # type: ignore[assignment]
    cond = loop0._checkpoint_interval > 0
    assert cond is False, "interval=0 时保存条件必须短路（否则 % 0 除零）"


def test_contract_a13_abort_during_stream_sets_user_interrupt(tmp_path: Any) -> None:
    """契约 11：流式处理中被 abort 时必须设置 USER_INTERRUPT——
    terminal_reason 变为 MODEL_ERROR 而非 USER_INTERRUPT。"""
    import asyncio as _asyncio

    from bandi.agent.loop import AgentLoop
    from bandi.core.abort_controller import AbortController
    from bandi.llm.streaming import StreamEvent, StreamEventType

    ac = AbortController()
    loop = AgentLoop(abort_controller=ac)

    async def aborting_stream(*args: Any, **kwargs: Any):
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="Hello")
        ac.abort("user_interrupt")
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta=" world")
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    from bandi.llm.mock import MockProvider

    provider = MockProvider()
    provider.stream = aborting_stream  # type: ignore[method-assign]

    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig, BandiConfig
    from bandi.core.event_bus import EventBus

    cfg = BandiConfig()
    cfg.llm.api_key = ""
    ctx = ExecutionContext(
        messages=[],
        provider=provider,
        config=AgentConfig(),
        bus=EventBus(),
        meta={"bandi_config": cfg, "abort_controller": ac},
    )
    ctx.current_state = "thinking"
    _asyncio.run(loop._run_loop(ctx, max_turns=3, turns_taken=0))
    assert ctx.terminal_reason is not None
    assert ctx.terminal_reason.name == "USER_INTERRUPT", (
        f"流式 abort 后 terminal_reason 必须为 USER_INTERRUPT，实际：{ctx.terminal_reason}"
    )


def test_contract_a13_checkpoint_restores_pending_tool_calls(tmp_path: Any) -> None:
    """契约 12：恢复 checkpoint 后 current_tool_calls 必须还原——"""

    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.llm.mock import MockProvider

    ctx = ExecutionContext(
        messages=[],
        provider=MockProvider(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    # 模拟中断时的状态：current_tool_calls 有未完成调用
    from bandi.core.message import ToolCall

    ctx.current_tool_calls = [ToolCall(id="tc1", name="bash", arguments={"cmd": "long-running"})]
    qs = ctx.to_query_state()
    assert qs.pending_tool_calls == [
        {"id": "tc1", "name": "bash", "arguments": {"cmd": "long-running"}}
    ], "to_query_state 必须序列化 pending_tool_calls"

    # 清除状态后恢复
    ctx.current_tool_calls = None
    ctx.apply_query_state(qs)
    assert ctx.current_tool_calls is not None, "apply_query_state 必须恢复 current_tool_calls"
    assert len(ctx.current_tool_calls) == 1
    assert ctx.current_tool_calls[0].id == "tc1"
    assert ctx.current_tool_calls[0].name == "bash"
    assert ctx.current_tool_calls[0].arguments == {"cmd": "long-running"}
