"""
  工具返回快照内容：ToolPermissionContext.extra["runtime_snapshot"] →
     概览 dict 含 token/预算/消息数/压缩历史等关键字段
  无快照降级：standalone 调用（无 context/extra）→ available=False 不炸
  on_executing 接线：每轮工具批执行前 executor 收到当轮最新快照（防御式
     测试 stub executor 无 setter 时不受影响）
     工具结果含真实 session_id 与 token 数并进入对话

破坏者断言：无快照绑定时不返回伪造数据。
"""

from typing import Any

import pytest

from bandi.core.config import BandiConfig
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage, ToolCall

# ── /工具行为 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ctx_inspect_returns_bound_snapshot() -> None:
    """契约：extra 绑定快照时返回内容级概览（token/预算/压缩历史）。"""
    from bandi.tools.base import ToolPermissionContext
    from bandi.tools.ctx_inspect import CtxInspectTool

    snapshot = {
        "session_id": "sess-1",
        "mode": "execute",
        "turn_index": 3,
        "consumed_tokens": 1200,
        "total_tokens": 5000,
        "context_window": 128000,
        "compression_threshold": 0.85,
        "token_budget_limit": -1,
        "message_count": 12,
        "active_tool_count": 30,
        "compress_token_history": [100, 200, 400],
    }
    tool = CtxInspectTool()
    result = await tool.execute(
        {}, context=ToolPermissionContext(extra={"runtime_snapshot": snapshot})
    )

    assert result["available"] is True
    assert result["consumed_tokens"] == 1200
    assert result["session_id"] == "sess-1"
    assert result["message_count"] == 12
    assert result["compress_token_history"] == [100, 200, 400]


@pytest.mark.asyncio
async def test_ctx_inspect_degrades_without_snapshot() -> None:
    """契约（+破坏者）：无快照绑定时不得伪造数据——available=False。"""
    from bandi.tools.ctx_inspect import CtxInspectTool

    tool = CtxInspectTool()

    standalone = await tool.execute({}, context=None)
    assert standalone["available"] is False, f"无 context 必须降级（实际 {standalone}）"
    assert "reason" in standalone

    no_extra = await tool.execute({}, context=type("C", (), {"extra": None})())
    assert no_extra["available"] is False


# ── on_executing 接线 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_executing_binds_runtime_snapshot_to_executor() -> None:
    """契约：on_executing 每轮把运行时快照注入 executor；stub executor
    （无 setter）不受影响。"""
    from types import SimpleNamespace

    from bandi.agent import states as states_mod
    from bandi.core.message import ToolResult

    received: list[dict] = []

    class _Executor:
        def set_runtime_snapshot(self, snap: dict) -> None:
            received.append(snap)

        async def execute(self, calls: list) -> list:
            return [
                ToolResult(
                    tool_call_id=tc.id, tool_name=tc.name, output="ok", success=True, duration_ms=1
                )
                for tc in calls
            ]

    class _BareExecutor:
        async def execute(self, calls: list) -> list:
            return [
                ToolResult(
                    tool_call_id=tc.id, tool_name=tc.name, output="ok", success=True, duration_ms=1
                )
                for tc in calls
            ]

    cfg = BandiConfig()

    def _mk_ctx(executor: Any) -> Any:
        return SimpleNamespace(
            terminal_reason=None,
            turn_index=2,
            current_tool_calls=[ToolCall(id="c1", name="read", arguments={})],
            tool_executor=executor,
            meta={"tracer": None, "task_ledger": None, "session_id": "s-snap", "mode": "execute"},
            session_id="s-snap",
            messages=[Message(role=MessageRole.USER, content="q")],
            tool_results=None,
            cot_trace=[],
            loop_detection_window=5,
            tool_sequence_hashes=[],
            config=cfg.agent,
            consumed_tokens=77,
            total_tokens=200,
            active_tools=[{"name": "read"}],
            compress=SimpleNamespace(pipeline=None),
            provider=SimpleNamespace(model_name="m"),
            bus=SimpleNamespace(emit=lambda *a, **k: None),
        )

    await states_mod.on_executing(_mk_ctx(_Executor()))
    assert received, "executor 必须收到运行时快照"
    snap = received[0]
    assert snap["consumed_tokens"] == 77 and snap["session_id"] == "s-snap"
    assert snap["message_count"] == 1 and snap["active_tool_count"] == 1

    bare_ctx = _mk_ctx(_BareExecutor())  # stub 无 setter——不得抛错
    await states_mod.on_executing(bare_ctx)


@pytest.mark.asyncio
async def test_run_agent_turn_ctx_inspect_tool_roundtrip(tmp_path: Any, monkeypatch: Any) -> None:
    """
    快照（session_id/token 数），结果进入对话，第二轮正常收尾。"""
    from pathlib import Path
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t1", name="ctx_inspect", arguments={})],
            usage=TokenUsage(prompt_tokens=20, completion_tokens=5),
            finish_reason="tool_calls",
        )
    )
    provider.add_response(
        LLMResponse(
            content="上下文检查完成",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=40, completion_tokens=8),
            finish_reason="stop",
        )
    )

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            await run_agent_turn(
                user_input="检查一下当前上下文状态",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )

        entries = store.get_branch(sid)
        tool_texts = [
            str(m.get("content") or "")
            for m in entries
            if m.get("role") == "tool" or getattr(m, "role", "") == "tool"
        ]
        blob = "\n".join(tool_texts)
        assert "ctx_inspect" in blob, f"工具输出必须落库（实际 {blob[:200]}）"
        assert sid in blob, f"快照 session_id 必须为真实值 {sid}（实际 {blob[:300]}）"
