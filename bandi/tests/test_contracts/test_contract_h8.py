from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall
from bandi.llm.mock import MockProvider
from bandi.memory.compression_pipeline import AutoCompactScheduler
from bandi.tools.base import BaseTool, ToolPermissionContext
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


class _PayloadTool(BaseTool):
    name = "payload_tool"
    description = "returns configurable payload without raising"
    is_concurrency_safe = True

    def __init__(self, payload: str | dict[str, Any]) -> None:
        self._payload = payload

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str | dict[str, Any]:
        return self._payload


# ── 压缩分级阈值可达性 ────────────────────────────────────


@pytest.mark.parametrize(
    ("threshold", "ratio", "expect"),
    [
        (0.85, 0.50, "none"),
        (0.85, 0.70, "none"),
        (0.85, 0.86, "micro"),
        (0.85, 0.90, "micro_progressive"),
        (0.85, 0.95, "progressive"),
        (0.90, 0.91, "micro"),
        (0.90, 0.94, "micro_progressive"),
        (0.90, 0.97, "progressive"),
    ],
)
def test_contract_select_strategy_graduated_tiers(
    threshold: float, ratio: float, expect: str
) -> None:
    """契约：进入压缩区后按相对区间分级——轻度过载且增速平缓走免费
    micro，中度叠加 progressive 摘要，重度直接 progressive。
    （防阈值区间互斥导致 micro/micro_progressive 成为不可达死分支。）"""
    cw = 128000
    scheduler = AutoCompactScheduler()
    # 增速平缓历史（相邻记录差 < 10% 窗口），排除 fast_growth 干扰
    scheduler.record_tokens(int(threshold * cw))
    tokens = int(ratio * cw)
    assert scheduler.select_strategy(tokens, cw, threshold) == expect


def test_contract_select_strategy_fast_growth_skips_micro() -> None:
    """契约（补充）：增速过快（单步涨幅 > 10% 窗口）时不得走免费的
    micro 层——同一轻度过载点，平缓时 micro、过快时必须升级。"""
    cw = 128000
    slow = AutoCompactScheduler()
    slow.record_tokens(int(0.84 * cw))
    slow.record_tokens(int(0.86 * cw))
    assert slow.select_strategy(int(0.86 * cw), cw, 0.85) == "micro"

    fast = AutoCompactScheduler()
    fast.record_tokens(int(0.60 * cw))
    fast.record_tokens(int(0.86 * cw))
    assert fast.select_strategy(int(0.86 * cw), cw, 0.85) != "micro"
    assert fast.select_strategy(int(0.86 * cw), cw, 0.85) in (
        "micro_progressive",
        "progressive",
    )


def _make_executor(
    payload: str | dict[str, Any],
    *,
    read_only: bool = False,
) -> tuple[ToolExecutor, EventBus, list[dict[str, Any]]]:
    bus = EventBus()
    received: list[dict[str, Any]] = []

    async def _record(e: Event) -> None:
        received.append(e.data)

    registry = ToolRegistry()
    tool = _PayloadTool(payload)
    tool.is_read_only = read_only
    registry.register_instance(tool)
    executor = ToolExecutor(registry, event_bus=bus)

    async def _wire() -> None:
        await bus.subscribe(EventType.TOOL_ERROR, _record)

    asyncio.run(_wire())
    return executor, bus, received


def test_contract_executor_dict_error_payload_fails_tool() -> None:
    """契约：工具不抛异常但返回 {"error": ...} / success=False 时
    ToolResult.success 必须为 False 且发出 TOOL_ERROR、连续失败记账 +1。"""
    executor, _bus, received = _make_executor({"error": "boom"})
    results = asyncio.run(executor.execute([ToolCall(id="t1", name="payload_tool", arguments={})]))
    assert len(results) == 1
    r = results[0]
    assert r.success is False, f"错误形态 dict 不得记为成功: {r!r}"
    assert "boom" in (r.error or "")
    assert executor._failure_count.get("payload_tool", 0) == 1, "失败必须计入连续失败记账"
    assert any("boom" in (d.get("error_message") or "") for d in received), (
        "必须发出 TOOL_ERROR 事件"
    )


def test_contract_executor_success_false_payload_fails_tool() -> None:
    """契约（补充）：{"success": False} 形态同样按失败处理。"""
    executor, _bus, _received = _make_executor({"success": False, "message": "上游拒绝"})
    results = asyncio.run(executor.execute([ToolCall(id="t2", name="payload_tool", arguments={})]))
    assert results[0].success is False
    assert "上游拒绝" in (results[0].error or "")


@pytest.mark.parametrize(
    "payload",
    [
        "plain string output",
        {"result": "ok"},
        {"error": None},
        {"success": True},
        {"items": [1, 2, 3]},
    ],
)
def test_contract_executor_normal_payload_still_success(payload: str | dict[str, Any]) -> None:
    """契约（防误伤）：正常输出（字符串/普通 dict/error 为空/显式成功）
    必须仍记为 success=True——错误形态检测不得扩大化。"""
    executor, _bus, received = _make_executor(payload)
    results = asyncio.run(executor.execute([ToolCall(id="t3", name="payload_tool", arguments={})]))
    assert results[0].success is True, f"正常输出被误判失败: {payload!r}"
    assert received == [], "正常输出不得触发 TOOL_ERROR"


def _make_ctx(provider: MockProvider, messages: list[Message]) -> Any:
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig, BandiConfig

    ctx = ExecutionContext(
        messages=messages,
        provider=provider,
        config=AgentConfig(stream_to_ui=False),
        bus=EventBus(),
    )
    ctx.meta["bandi_config"] = BandiConfig()
    ctx.meta["bandi_config"].llm.api_key = ""
    return ctx


def test_contract_restored_completed_session_not_rerun() -> None:
    """
    必须直接返回 COMPLETED——LLM 零调用、final_message 保持快照值。"""
    from bandi.agent.base import QueryState, TerminalReason
    from bandi.agent.loop import AgentLoop

    provider = MockProvider()
    provider.add_response(LLMResponse(content="不应被调用"))
    calls = {"n": 0}
    orig_complete = provider.complete

    async def _spy(*args: Any, **kwargs: Any) -> LLMResponse:
        calls["n"] += 1
        return await orig_complete(*args, **kwargs)

    provider.complete = _spy  # type: ignore[method-assign]

    ctx = _make_ctx(provider, [Message(role=MessageRole.USER, content="历史问题")])
    ctx.apply_query_state(QueryState(terminal_reason="COMPLETED", final_message="done"))

    reason = asyncio.run(AgentLoop(max_iterations=3).run(ctx))
    assert reason == TerminalReason.COMPLETED
    assert calls["n"] == 0, "已完成会话恢复后不得重新调用 LLM"
    assert ctx.final_message == "done", "final_message 必须保持快照值"


def test_contract_restored_completed_with_new_input_processes_turn() -> None:
    """
    终止标记并正常处理本轮——快照中的 COMPLETED 属于上一轮，不得吞掉新输入。"""
    from bandi.agent.base import QueryState, TerminalReason
    from bandi.agent.loop import AgentLoop

    provider = MockProvider()
    provider.add_response(LLMResponse(content="第二轮回复"))
    calls = {"n": 0}
    orig_complete = provider.complete

    async def _spy(*args: Any, **kwargs: Any) -> LLMResponse:
        calls["n"] += 1
        return await orig_complete(*args, **kwargs)

    provider.complete = _spy  # type: ignore[method-assign]

    ctx = _make_ctx(provider, [Message(role=MessageRole.USER, content="新一轮问题")])
    ctx.apply_query_state(QueryState(terminal_reason="COMPLETED", final_message="done"))
    ctx.raw_input = "新一轮问题"

    reason = asyncio.run(AgentLoop(max_iterations=3).run(ctx))
    assert reason == TerminalReason.COMPLETED
    assert calls["n"] == 1, "带新输入的已完成会话恢复必须正常调用 LLM 处理本轮"
    assert ctx.current_content == "第二轮回复"
    assert any(
        m.role == MessageRole.ASSISTANT and "第二轮回复" in (m.content or "") for m in ctx.messages
    ), "本轮回复必须进入消息历史"


def test_contract_restored_pending_tools_resume_from_executing() -> None:
    """
    续跑执行工具（消息中出现 tool 结果），而非清空调用后重问 LLM。
    起只读工具在旧快照下可安全重放；副作用工具的旧快照 provenance 未知
    走 unknown 不重放（见 test_contract_d2_recovery_no_replay）。"""
    from bandi.agent.base import QueryState, TerminalReason
    from bandi.agent.loop import AgentLoop

    provider = MockProvider()
    provider.add_response(LLMResponse(content="收尾"))
    calls = {"n": 0}
    orig_complete = provider.complete

    async def _spy(*args: Any, **kwargs: Any) -> LLMResponse:
        calls["n"] += 1
        return await orig_complete(*args, **kwargs)

    provider.complete = _spy  # type: ignore[method-assign]

    ctx = _make_ctx(provider, [Message(role=MessageRole.USER, content="列出目录")])
    ctx.apply_query_state(
        QueryState(pending_tool_calls=[{"id": "c1", "name": "payload_tool", "arguments": {}}])
    )
    executor, _bus, _received = _make_executor({"result": "done-by-tool"}, read_only=True)
    ctx.tool_executor = executor

    reason = asyncio.run(AgentLoop(max_iterations=3).run(ctx))
    assert reason == TerminalReason.COMPLETED
    tool_msgs = [m for m in ctx.messages if m.role == MessageRole.TOOL]
    assert tool_msgs, "挂起工具必须在续跑中被真实执行并产生 tool 消息"
    assert calls["n"] == 1, "续跑只应补一次收尾 LLM 调用"
    assert (
        ctx.current_tool_calls is None
        or not getattr(ctx.current_tool_calls, "__len__", lambda: 1)()
    ), "执行完成后挂起调用应被消费"


# ── explain 响应含 checkpoints 字段 ───────────────────────


def test_contract_explain_response_contains_checkpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约：POST /api/sessions/{id}/explain 响应必须含 checkpoints
    时间线字段且透传 store.list_checkpoint_entries 结果（每步持久化的读侧观测面）。"""
    import sys
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from bandi.api.app import app as fastapi_app

    app_module = sys.modules["bandi.api.app"]
    assert fastapi_app is app_module.app

    marker = [{"id": "cp-1", "turn_index": 2, "created_at": "2026-01-01T00:00:00Z"}]
    store = MagicMock()
    store.get_session.return_value = {"session_id": "s-explain"}
    store.get_branch.return_value = []
    store.list_checkpoint_entries.return_value = marker
    monkeypatch.setattr(app_module, "_get_store", lambda: store)

    orig_key = app_module._API_KEY
    app_module._API_KEY = "test-key"
    try:
        client = TestClient(fastapi_app)
        resp = client.post(
            "/api/sessions/s-explain/explain",
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["checkpoints"] == marker, "checkpoints 必须透传 store 查询结果"
        assert body["explanation"]
    finally:
        app_module._API_KEY = orig_key
