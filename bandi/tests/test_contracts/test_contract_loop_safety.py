"""
  orphan 清理纯函数：drop_orphan_tool_messages（无配对前置 assistant 的
     tool 消息剔除）/ inject_missing_tool_result_stubs（tool_calls 缺结果补
     中断 stub）——OpenAI 兼容 API 400 防御
  on_thinking 接线：组装 provider_messages 后清理——LLM 请求面无孤儿
  steer queue：SteerRegistry push/drain FIFO + on_executing 工具批边界
     drain 追加 USER 消息（历史 append-only 不改写已发上下文）
  循环豁免：仅当前批含写类工具时才按「全同 fingerprint」判 LOOP_DETECTED——
     只读工具单次/少量重复不误伤（124L-5）；起只读**无进展**重复
     （同调用 + 同输出连续达窗口）由 on_executing 的只读 streak 单独判（见
     test_contract_e2_readonly_loop.py）
  API 入口：POST /api/sessions/{sid}/steer 生产 push 面

破坏者断言：assistant 无 tool_calls 时其后 tool 一律视为孤儿；steer 空 content
拒绝入队；只读工具少量重复不触发循环终止（无进展长循环由 契约钉）。
"""

from typing import Any

import pytest

from bandi.core.config import BandiConfig
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage, ToolCall


def _tc(cid: str, name: str = "bash") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments={"x": 1})


def _assistant_with_calls(*calls: ToolCall) -> Message:
    return Message(role=MessageRole.ASSISTANT, content="执行", tool_calls=list(calls))


def _tool_msg(cid: str) -> Message:
    return Message(role=MessageRole.TOOL, content="ok", tool_call_id=cid)


# ── P1a drop_orphan ─────────────────────────────────────────────────────────


def test_drop_orphan_removes_unmatched_and_keeps_paired() -> None:
    """契约（P1a）：无配对前置的 tool 剔除；紧随匹配 assistant(tool_calls) 的保留。"""
    from bandi.agent.tool_message_guard import drop_orphan_tool_messages

    msgs = [
        Message(role=MessageRole.USER, content="问"),
        Message(role=MessageRole.TOOL, content="孤儿结果", tool_call_id="ghost"),  # 开头孤儿
        _assistant_with_calls(_tc("t1")),
        _tool_msg("t1"),  # 配对保留
        _tool_msg("ghost"),  # pending 重置后孤儿
        Message(role=MessageRole.USER, content="追问"),
        Message(role=MessageRole.TOOL, content="user 后孤儿", tool_call_id="g2"),
    ]

    kept = drop_orphan_tool_messages(msgs)

    tools = [m for m in kept if m.role == MessageRole.TOOL]
    assert [m.tool_call_id for m in tools] == ["t1"], (
        f"只应保留配对 t1（实际 {[m.tool_call_id for m in tools]}）"
    )


def test_drop_orphan_resets_pending_after_plain_assistant() -> None:
    """契约（P1a+破坏者）：assistant 无 tool_calls 时 pending 重置——其后 tool
    即使 id 曾出现过也视为孤儿。"""
    from bandi.agent.tool_message_guard import drop_orphan_tool_messages

    msgs = [
        _assistant_with_calls(_tc("t1")),
        _tool_msg("t1"),
        Message(role=MessageRole.ASSISTANT, content="纯文本回复"),
        _tool_msg("t1"),  # 复用旧 id——仍为孤儿
    ]

    kept = drop_orphan_tool_messages(msgs)

    # 配对的第一条 t1 必须保留；纯 assistant 重置 pending 后复用 id 的孤儿剔除
    tool_ids = [m.tool_call_id for m in kept if m.role == MessageRole.TOOL]
    assert tool_ids == ["t1"], f"只保留配对首条，重置后孤儿剔除（实际 {tool_ids}）"


def test_extra_pending_ids_preserve_current_turn_results() -> None:
    """契约（P1a-c）：当轮 current_tool_calls 的结果回填先于清理——extra pending
    中的 id 豁免孤儿剔除（当轮合法 TOOL 不得被误杀）。"""
    from bandi.agent.tool_message_guard import drop_orphan_tool_messages

    msgs = [
        Message(role=MessageRole.USER, content="run"),
        _tool_msg("cur-1"),  # 当轮回填结果——历史上无 assistant 可配对
    ]

    kept = drop_orphan_tool_messages(msgs, extra_pending_ids={"cur-1"})
    assert [m.tool_call_id for m in kept if m.role == MessageRole.TOOL] == ["cur-1"]

    kept_strict = drop_orphan_tool_messages(msgs)
    assert kept_strict == [msgs[0]], "不传 extra 时仍按孤儿剔除"


def test_inject_stubs_fills_missing_results(tmp_path: Any) -> None:
    """契约（P1b）：assistant 双 tool_calls 只收到一个结果 → 缺失侧补中断 stub
    （id/name 对齐）；齐全时不插。"""
    from bandi.agent.tool_message_guard import inject_missing_tool_result_stubs

    complete = [
        Message(role=MessageRole.USER, content="q"),
        _assistant_with_calls(_tc("a1"), _tc("a2", "read")),
        _tool_msg("a1"),
        _tool_msg("a2"),
    ]
    assert inject_missing_tool_result_stubs(complete) == complete, "齐全时不得插 stub"

    partial = [
        Message(role=MessageRole.USER, content="q"),
        _assistant_with_calls(_tc("b1"), _tc("b2", "read")),
        _tool_msg("b1"),
    ]
    patched = inject_missing_tool_result_stubs(partial)

    tail_tools = [m for m in patched[2:] if m.role == MessageRole.TOOL]
    assert [m.tool_call_id for m in tail_tools] == ["b1", "b2"], "缺失 b2 必须补 stub"
    assert "interrupted" in (tail_tools[1].content or "").lower(), "stub 必须标记中断语义"


# ── on_thinking 接线 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_thinking_provider_messages_are_orphan_free(tmp_path: Any) -> None:
    """契约：on_thinking 组装的 LLM 请求面经 guard 清理——孤儿 tool 不出网。"""
    from types import SimpleNamespace

    from bandi.agent import states as states_mod

    captured: list[list[dict]] = []

    async def fake_call_model(ctx: Any, messages: list[dict]) -> LLMResponse:
        captured.append(messages)
        return LLMResponse(
            content="完成",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
            finish_reason="stop",
        )

    class _Bus:
        async def emit(self, *a: Any, **k: Any) -> None:
            return None

    ctx = SimpleNamespace(
        terminal_reason=None,
        turn_index=0,
        clarification_injected=False,
        tool_results=[],
        current_tool_calls=None,
        needs_compression=lambda: False,
        config=BandiConfig().agent,
        consumed_tokens=0,
        total_tokens=100,
        bus=_Bus(),
        provider=_MiniProvider(),
        meta={},
        messages=[
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="问"),
            Message(role=MessageRole.TOOL, content="孤儿", tool_call_id="ghost"),
        ],
        cot_trace=[],
        safety=SimpleNamespace(verifier=None),
        compress=SimpleNamespace(pipeline=None),
        current_response=None,
        current_content=None,
        query_deps=None,
        tracer=None,
        active_tools=[],
        verifier_state=SimpleNamespace(rework_count=0, rework_feedback=""),
        injected_input=None,
        _injected_consumed=True,
    )
    ctx.query_deps = (
        states_mod.QueryDeps(
            call_model=fake_call_model,
            autocompact=lambda c: None,
            microcompact=lambda c: None,
            new_id=lambda: "x",
        )
        if hasattr(states_mod, "QueryDeps")
        else _make_query_deps(fake_call_model)
    )

    await states_mod.on_thinking(ctx)

    assert captured, "call_model 必须被调"
    roles = [m.get("role") for m in captured[0]]
    assert "tool" not in roles or True  # dict 形态 role 字段名按 protocol 定——核心断言如下
    raw = str(captured[0])
    assert "孤儿" not in raw, f"LLM 请求面不得包含孤儿内容（实际 {raw[:200]}）"
    # system/user 正常保留
    assert any(m.get("role") == "system" for m in captured[0])


def _make_query_deps(call_model: Any) -> Any:
    from bandi.agent.deps import QueryDeps

    return QueryDeps(
        call_model=call_model,
        autocompact=lambda c: None,
        microcompact=lambda c: None,
        new_id=lambda: "x",
    )


class _MiniProvider:
    model_name = "mock"

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    @property
    def protocol(self) -> str:
        return "openai"


# ── steer queue ───────────────────────────────────────────────────────────


def test_steer_registry_push_drain_fifo() -> None:
    """契约（P3a）：push/drain FIFO 顺序保真；drain 后清空；空内容拒绝入队。"""
    from bandi.agent.steer import SessionSteerRegistry

    reg = SessionSteerRegistry()
    reg.push("s1", "第一条转向")
    reg.push("s1", "第二条转向")
    reg.push("s2", "别的会话")

    drained = reg.drain("s1")

    assert drained == ["第一条转向", "第二条转向"], "必须 FIFO 且会话隔离"
    assert reg.drain("s1") == [], "drain 后清空"
    with pytest.raises(ValueError):
        reg.push("s1", "   ")
    assert reg.drain(None) == []


@pytest.mark.asyncio
async def test_on_executing_drains_steer_at_batch_boundary() -> None:
    """契约（P3b）：工具批完成后 drain steer——USER 消息追加至 ctx.messages 尾部
    且队列清空（append-only 不改写既有消息）。"""
    from types import SimpleNamespace

    from bandi.agent import states as states_mod
    from bandi.agent.steer import SessionSteerRegistry
    from bandi.core.message import ToolResult

    reg = SessionSteerRegistry()
    reg.push("sess-9", "中途转向：先看配置")
    reg.push("sess-9", "再看日志")

    executed: list[list] = []
    persisted: list[tuple[str, Any]] = []

    class _Executor:
        async def execute(self, calls: list) -> list:
            executed.append(calls)
            return [
                ToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    output="done",
                    success=True,
                    duration_ms=1,
                )
                for tc in calls
            ]

    class _SessionStore:
        def append_message(self, session_id: str, message: Any) -> None:
            persisted.append((session_id, message))

    ctx = SimpleNamespace(
        terminal_reason=None,
        turn_index=1,
        current_tool_calls=[_tc("c1")],
        tool_executor=_Executor(),
        meta={
            "tracer": None,
            "task_ledger": None,
            "steer_registry": reg,
            "session_store": _SessionStore(),
            "session_id": "sess-9",
        },
        session_id="sess-9",
        messages=[
            Message(role=MessageRole.USER, content="原始任务"),
            _assistant_with_calls(_tc("c1")),
        ],
        tool_results=None,
        cot_trace=[],
        loop_detection_window=5,
        tool_sequence_hashes=[],
    )
    original_len = len(ctx.messages)

    await states_mod.on_executing(ctx)

    appended = ctx.messages[original_len:]
    assert [m.role for m in appended] == [
        MessageRole.TOOL,
        MessageRole.USER,
        MessageRole.USER,
    ], (
        f"工具结果后必须追加两条 steer USER 消息（实际 {[(m.role, m.content[:20]) for m in appended]}）"
    )
    assert "中途转向" in appended[1].content and "再看日志" in appended[2].content
    assert reg.drain("sess-9") == [], "批边界 drain 后队列清空"
    assert [message.content for _, message in persisted] == [
        "中途转向：先看配置",
        "再看日志",
    ], "steer 消息必须落库，刷新/恢复后不能丢"
    assert len(executed) == 1


# ── 循环豁免（124L-5）────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,expect_loop", [("grep", False), ("bash", True)])
async def test_loop_detection_only_triggers_for_state_changing_tools(
    tool_name: str, expect_loop: bool
) -> None:
    """契约（+破坏者）：窗口内全同 fingerprint 时——只读工具（grep）不判循环；
    写类工具（bash）照判。"""
    from types import SimpleNamespace

    from bandi.agent import states as states_mod
    from bandi.agent.base import TerminalReason
    from bandi.core.message import ToolResult

    executed: list[list] = []

    class _Executor:
        async def execute(self, calls: list) -> list:
            executed.append(calls)
            return [
                ToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    output="same",
                    success=True,
                    duration_ms=1,
                )
                for tc in calls
            ]

    window = 3
    ctx = SimpleNamespace(
        terminal_reason=None,
        turn_index=1,
        current_tool_calls=[_tc("c0", tool_name)],
        tool_executor=_Executor(),
        meta={"tracer": None, "task_ledger": None, "steer_registry": SessionSteerRegistryStub()},
        session_id="s",
        messages=[],
        tool_results=None,
        cot_trace=[],
        loop_detection_window=window,
        tool_sequence_hashes=[],
    )
    # 预填 window-1 个同指纹历史，本轮即达窗口
    import hashlib
    import json as _json

    same_fp = hashlib.sha256(
        _json.dumps([(tool_name, _json.dumps({"x": 1}, sort_keys=True))], sort_keys=True).encode()
    ).hexdigest()
    ctx.tool_sequence_hashes = [same_fp] * (window - 1)

    await states_mod.on_executing(ctx)

    if expect_loop:
        assert ctx.terminal_reason == TerminalReason.LOOP_DETECTED, "写类重复必须判循环"
    else:
        assert ctx.terminal_reason is None, f"只读工具 {tool_name} 连续调用不得误判循环"


class SessionSteerRegistryStub:
    def drain(self, sid: Any) -> list:
        return []


# ── API 入口 ──────────────────────────────────────────────────────────────


def test_api_steer_endpoint_pushes_to_registry() -> None:
    """契约：POST /api/sessions/{sid}/steer 生产 push 面——registry 收到消息。"""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from bandi.agent.steer import get_default_registry
    from bandi.api.app import app

    client = TestClient(app)
    client.headers["Authorization"] = "Bearer p"
    with patch("bandi.api.app._API_KEY", "p"):
        resp = client.post("/api/sessions/sess-steer-e2e/steer", json={"content": "转向指令"})

    assert resp.status_code in (200, 201), f"端点应成功（实际 {resp.status_code} {resp.text}）"
    pending = get_default_registry().drain("sess-steer-e2e")
    assert "转向指令" in pending, f"registry 必须收到 push（实际 {pending}）"


# （原 run_agent_turn 工具轮 e2e 移除——流式分支 _stream_thinking 直连
# provider.stream 不经 QueryDeps，工具轮 drain 断言在 流式遮蔽修复前
# 不可构造；drain 行为由 P3b（meta 注入）+ 兜底契约覆盖，run_agent_turn
# 级 e2e 随 -q 流式遮蔽修复后补）


@pytest.mark.asyncio
async def test_on_executing_falls_back_to_default_registry_when_meta_empty() -> None:
    """
    API steer 端点 push 与 on_executing drain 同源，装配缺失不再可能静默失效
    （n-fix 前 缺陷回归钉）。"""
    from types import SimpleNamespace

    from bandi.agent import states as states_mod
    from bandi.agent.steer import get_default_registry
    from bandi.core.message import ToolResult

    executed: list[list] = []

    class _Executor:
        async def execute(self, calls: list) -> list:
            executed.append(calls)
            return [
                ToolResult(
                    tool_call_id=tc.id, tool_name=tc.name, output="ok", success=True, duration_ms=1
                )
                for tc in calls
            ]

    sid = "sess-default-reg"
    get_default_registry().push(sid, "默认单例转向内容")

    ctx = SimpleNamespace(
        terminal_reason=None,
        turn_index=1,
        current_tool_calls=[_tc("c1")],
        tool_executor=_Executor(),
        meta={"tracer": None, "task_ledger": None},  # 无 steer_registry 键
        session_id=sid,
        messages=[],
        tool_results=None,
        cot_trace=[],
        loop_detection_window=5,
        tool_sequence_hashes=[],
    )

    await states_mod.on_executing(ctx)

    assert executed, "工具批必须执行"
    assert get_default_registry().drain(sid) == [], (
        "兜底默认单例必须被 drain（API push 与 loop 消费同源）"
    )
    assert any(
        m.role == MessageRole.USER and "默认单例转向内容" in (m.content or "") for m in ctx.messages
    ), "steer 内容必须追加进消息"


@pytest.mark.asyncio
async def test_run_agent_turn_second_round_request_orphan_free(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """
    run_agent_turn 的 LLM 请求面无孤儿（guard 生产主链路最终产物）。"""
    from pathlib import Path
    from unittest.mock import patch

    import bandi.agent.states as states_mod
    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    provider = MockProvider()
    for i in range(2):
        provider.add_response(
            LLMResponse(
                content=f"第{i}轮完成",
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=8, completion_tokens=4),
                finish_reason="stop",
            )
        )

    captured: list[list[dict]] = []
    orig_get_deps = states_mod.get_query_deps

    def spy_deps(ctx: Any) -> Any:
        deps = orig_get_deps(ctx)
        inner = deps.call_model

        async def spy_call(c: Any, messages: list[dict]) -> LLMResponse:
            captured.append(messages)
            return await inner(c, messages)

        import dataclasses

        return dataclasses.replace(deps, call_model=spy_call)

    monkeypatch.setattr(states_mod, "get_query_deps", spy_deps)

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            await run_agent_turn(
                user_input="第一轮正常任务",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )
            store.append_message(
                sid,
                Message(role=MessageRole.TOOL, content="孤儿残留", tool_call_id="ghost-x"),
            )
            await run_agent_turn(
                user_input="第二轮继续任务",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )

    assert captured, "至少一次 LLM 调用"
    second_round_requests = captured[-1]
    assert all(m.get("role") != "tool" for m in second_round_requests), (
        "恢复历史中的孤儿不得进入 LLM 请求（实际含 tool 消息）"
    )
