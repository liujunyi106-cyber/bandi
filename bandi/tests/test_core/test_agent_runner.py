from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bandi.agent.loop import AgentLoop
from bandi.core.agent_runner import (
    AgentTurnResult,
    _assistant_persist_content,
    _HandlerBindings,
    _subscribe_handler,
    _unsubscribe_handler,
    run_agent_turn,
)
from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


class TestAssistantPersistContent:
    """落库内容选取：系统替代文案不进历史，但用户已看到的模型原话必须留存。

    内容抑制类终态（安全/隐私/幻觉拦截）在 states.py 里先清空 current_content
    再设 final_message，故这些路径下本函数返回 None——历史不会被拦截文案污染。
    """

    def test_normal_reply_persisted(self) -> None:
        """正常对话：final_message 为 None → 落模型真实输出。"""
        assert _assistant_persist_content(None, "pineapple-77") == "pineapple-77"

    def test_safety_blocked_cleared_content_not_persisted(self) -> None:
        """安全拦截：current_content 已被 states.py 清空 → 不落库任何内容。"""
        assert _assistant_persist_content("输出存在幻觉风险，已拦截", None) is None

    def test_model_error_no_content_not_persisted(self) -> None:
        """MODEL_ERROR：final_message=错误文案、current_content 为空 → 不落库。"""
        assert _assistant_persist_content("LLM returned empty response", None) is None

    def test_final_message_equals_content_persisted(self) -> None:
        """final_message 与模型原文相同（无替代）→ 落库不误伤。"""
        assert _assistant_persist_content("pineapple-77", "pineapple-77") == "pineapple-77"

    def test_tool_abort_keeps_model_output(self) -> None:
        """工具中止终态（BC-006）：系统文案不落库，模型已送达的原话要留存。"""
        got = _assistant_persist_content(
            "工具 read 已被暂停——已中止本轮。", "好的老板，我已经记住了！"
        )
        assert got == "好的老板，我已经记住了！"

    def test_incomplete_stream_keeps_partial_output(self) -> None:
        """流式未完整结束：用户已看到部分输出 → 落库保留。"""
        got = _assistant_persist_content(
            "模型流式响应未完整结束（incomplete），请重试或继续对话。", "前半句"
        )
        assert got == "前半句"


class TestAgentTurnResult:
    def test_create(self) -> None:
        r = AgentTurnResult(
            reply="hello",
            terminal_reason="COMPLETED",
            total_tokens=15,
            turn_index=1,
            session_id="s-1",
        )
        assert r.reply == "hello"
        assert r.total_tokens == 15
        assert r.turn_index == 1


class TestHandlerBindings:
    @pytest.mark.asyncio
    async def test_bind_covers_production_event_types(self) -> None:
        handler = MagicMock()
        bindings = _HandlerBindings(handler, "s-1")
        result = bindings.bind()
        assert set(result.keys()) == {
            EventType.LLM_STREAM_EVENT,
            EventType.TOOL_INVOKED,
            EventType.TOOL_COMPLETED,
            EventType.TOOL_ERROR,
            EventType.TOOL_PROGRESS,
            EventType.SKILL_INJECTED,
            EventType.COMPRESSION_COMPLETED,
            EventType.MEMORY_WRITTEN,
            EventType.TASK_LEDGER_UPDATED,
        }

    @pytest.mark.asyncio
    async def test_on_stream_event_content_delta(self) -> None:
        handler = AsyncMock()
        bindings = _HandlerBindings(handler, "s-1")
        bindings.bind()
        event = Event(
            type=EventType.LLM_STREAM_EVENT, data={"type": "content_delta", "content": "hello"}
        )
        await bindings._on_stream_event(event)
        handler.on_content_delta.assert_awaited_once_with("s-1", "hello")

    @pytest.mark.asyncio
    async def test_on_stream_event_no_data(self) -> None:
        handler = AsyncMock()
        bindings = _HandlerBindings(handler, "s-1")
        event = Event(type=EventType.LLM_STREAM_EVENT, data=None)
        await bindings._on_stream_event(event)
        handler.on_content_delta.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_stream_event_thinking_delta(self) -> None:
        handler = AsyncMock()
        bindings = _HandlerBindings(handler, "s-1")
        bindings.bind()
        event = Event(
            type=EventType.LLM_STREAM_EVENT, data={"type": "thinking_delta", "content": "想"}
        )
        await bindings._on_stream_event(event)
        handler.on_thinking.assert_awaited_once_with("s-1", "想")

    @pytest.mark.asyncio
    async def test_on_tool_invoked(self) -> None:
        handler = AsyncMock()
        bindings = _HandlerBindings(handler, "s-1")
        bindings.bind()
        event = Event(
            type=EventType.TOOL_INVOKED,
            data={"tool_name": "bash", "tool_input": {"cmd": "ls"}},
        )
        await bindings._on_tool_invoked(event)
        handler.on_tool_call_start.assert_awaited_once_with("s-1", "bash", {"cmd": "ls"}, "")

    @pytest.mark.asyncio
    async def test_on_skill_injected(self) -> None:
        handler = AsyncMock()
        bindings = _HandlerBindings(handler, "s-1")
        bindings.bind()
        event = Event(
            type=EventType.SKILL_INJECTED,
            data={"skills": ["git-workflow", "code-review"]},
        )
        await bindings._on_skill_injected(event)
        handler.on_skill_injected.assert_awaited_once_with("s-1", ["git-workflow", "code-review"])

    @pytest.mark.asyncio
    async def test_on_tool_completed_success(self) -> None:
        handler = AsyncMock()
        bindings = _HandlerBindings(handler, "s-1")
        bindings.bind()
        event = Event(
            type=EventType.TOOL_COMPLETED,
            data={
                "tool_name": "bash",
                "success": True,
                "output_preview": "ok",
                "duration_ms": 100,
            },
        )
        await bindings._on_tool_completed(event)
        handler.on_tool_result.assert_awaited_once_with(
            "s-1", "bash", True, "ok", 100, "", citations=[]
        )

    @pytest.mark.asyncio
    async def test_on_tool_error(self) -> None:
        handler = AsyncMock()
        bindings = _HandlerBindings(handler, "s-1")
        bindings.bind()
        event = Event(
            type=EventType.TOOL_ERROR,
            data={
                "tool_name": "bash",
                "error_message": "failed",
                "duration_ms": 50,
            },
        )
        await bindings._on_tool_error(event)
        handler.on_tool_result.assert_awaited_once_with("s-1", "bash", False, "failed", 50, "")


class TestSubscribeUnsubscribe:
    @pytest.mark.asyncio
    async def test_subscribe(self) -> None:
        bus = EventBus()
        handler = AsyncMock()
        bindings = await _subscribe_handler(bus, handler, "s-1")
        assert bindings is not None
        assert len(bindings.bindings) == 9

    @pytest.mark.asyncio
    async def test_subscribe_none_handler(self) -> None:
        bus = EventBus()
        bindings = await _subscribe_handler(bus, None, "s-1")
        assert bindings is not None

    @pytest.mark.asyncio
    async def test_unsubscribe(self) -> None:
        bus = EventBus()
        handler = AsyncMock()
        bindings = await _subscribe_handler(bus, handler, "s-1")
        await _unsubscribe_handler(bus, bindings)
        assert bindings is not None
        for et, fn in bindings.bindings.items():
            handlers = bus._handlers.get(et, [])
            assert fn not in handlers

    @pytest.mark.asyncio
    async def test_unsubscribe_none(self) -> None:
        await _unsubscribe_handler(EventBus(), None)


class _MockSessionStore:
    def __init__(self) -> None:
        self.sid: str = ""
        self.messages: list = []
        self.last_title: str = ""

    def create_session(
        self,
        mode: str = "execute",
        project_root: str | None = None,
        project_id: str | None = None,
        title: str = "",
        origin: str | None = None,
    ) -> str:
        self.sid = f"test-{mode}"
        self.last_title = title
        return self.sid

    def append_message(self, session_id: str, msg: Any) -> None:
        self.messages.append(msg)
        self.sid = session_id

    def get_branch(self, session_id: str, *, full: bool = False) -> list:
        return []

    def get_full_branch(self, session_id: str) -> list:
        return []

    def get_session(self, session_id: str) -> Any:
        return {"session_id": session_id}

    async def on_state_change(self, event: Any) -> None:
        pass

    def save_checkpoint_entry(self, session_id: str, turn_index: int) -> None:
        pass

    def list_checkpoint_entries(self, session_id: str) -> list:
        return []

    async def on_terminated(self, event: Any) -> None:
        pass


class _RecordingProvider(MockProvider):
    """MockProvider 变体：记录每次 complete/stream 收到的 messages。"""

    def __init__(self) -> None:
        super().__init__()
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.seen_messages.append(messages)
        return await super().complete(messages, tools=tools, **kwargs)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.seen_messages.append(messages)
        async for ev in super().stream(messages, tools=tools, **kwargs):
            yield ev


class TestMultiTurnSystemPrompt:
    @pytest.mark.asyncio
    async def test_second_turn_request_starts_with_system(self, tmp_path: Any) -> None:
        from pathlib import Path

        from bandi.memory.session.store import SessionStore

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = SessionStore(str(tmp_path / "sess.db"))
            sid = store.create_session(mode="execute")
            provider = _RecordingProvider()
            provider.add_response(LLMResponse(content="first reply"))
            provider.add_response(LLMResponse(content="second reply"))
            cfg = _test_config()
            with patch("bandi.core.agent_runner.build_provider", return_value=provider):
                r1 = await run_agent_turn(
                    user_input="hello",
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                )
                r2 = await run_agent_turn(
                    user_input="follow up",
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                )
            store.close()
        assert r1.reply
        assert r2.reply
        assert len(provider.seen_messages) >= 2
        for msgs in provider.seen_messages:
            assert msgs[0]["role"] == "system", "LLM 请求首条必须是 system prompt"

    @pytest.mark.asyncio
    async def test_second_turn_keeps_history_after_system(self, tmp_path: Any) -> None:
        from pathlib import Path

        from bandi.memory.session.store import SessionStore

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = SessionStore(str(tmp_path / "sess.db"))
            sid = store.create_session(mode="execute")
            provider = _RecordingProvider()
            provider.add_response(LLMResponse(content="first reply"))
            provider.add_response(LLMResponse(content="second reply"))
            cfg = _test_config()
            with patch("bandi.core.agent_runner.build_provider", return_value=provider):
                await run_agent_turn(
                    user_input="hello",
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                )
                await run_agent_turn(
                    user_input="follow up",
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                )
            store.close()
        second_turn = provider.seen_messages[-1]
        roles = [m["role"] for m in second_turn]
        assert roles[0] == "system"
        assert "user" in roles and "assistant" in roles
        user_contents = [m["content"] for m in second_turn if m["role"] == "user"]
        # 状态块注入在前（请求视图层），剥离后必须恰好是历史+当前，无重复
        cleaned = [
            content.partition("\n\n")[2] if content.startswith("[任务状态]") else content
            for content in user_contents
        ]
        assert cleaned == ["hello", "follow up"], (
            "第二轮应恰好含两个 user 消息（历史+当前），无重复"
        )

    @pytest.mark.asyncio
    async def test_second_turn_restores_tool_calls(self, tmp_path: Any) -> None:
        from pathlib import Path

        from bandi.core.message import Message, MessageRole, ToolCall
        from bandi.memory.session.store import SessionStore

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = SessionStore(str(tmp_path / "sess.db"))
            sid = store.create_session(mode="execute")
            # 预置第一轮：user + assistant（含 tool_calls）
            store.append_message(sid, Message(role=MessageRole.USER, content="第一问"))
            store.append_message(
                sid,
                Message(
                    role=MessageRole.ASSISTANT,
                    content="调用工具",
                    tool_calls=[ToolCall(id="tc9", name="bash", arguments={"cmd": "ls"})],
                ),
            )
            provider = _RecordingProvider()
            provider.add_response(LLMResponse(content="second reply"))
            cfg = _test_config()
            with patch("bandi.core.agent_runner.build_provider", return_value=provider):
                r2 = await run_agent_turn(
                    user_input="第二问",
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                )
            store.close()
        assert r2.reply
        assert len(provider.seen_messages) >= 1
        restored = provider.seen_messages[-1]
        assistant_msgs = [m for m in restored if m.get("role") == "assistant"]
        assert assistant_msgs, "恢复的会话历史必须含 assistant 消息"
        has_tc = any(m.get("tool_calls") for m in assistant_msgs)
        assert has_tc, "恢复的 assistant 消息必须还原 tool_calls（写读对称）"


class TestRunAgentTurn:
    @pytest.mark.asyncio
    async def test_basic_turn(self, tmp_path: Any) -> None:
        from pathlib import Path

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = _MockSessionStore()
            cfg = _test_config()
            result = await run_agent_turn(
                user_input="hello",
                session_id="test-001",
                session_store=store,
                bandi_config=cfg,
            )
        assert result.session_id == "test-001"
        assert result.turn_index >= 0
        assert result.reply
        assert result.terminal_reason is not None

    @pytest.mark.asyncio
    async def test_badcase_connection_closed_after_turn(self, tmp_path: Any) -> None:
        """run_agent_turn 结束后 badcase_tracker 的 SQLite 连接必须关闭（防句柄泄漏）。"""
        import sqlite3
        from pathlib import Path

        from bandi.eval.badcase_tracker import BadCaseTracker

        closed = {"n": 0}
        original_close = BadCaseTracker.close

        def _spy_close(self):
            closed["n"] += 1
            return original_close(self)

        with (
            patch("pathlib.Path.home", return_value=Path(str(tmp_path))),
            patch.object(BadCaseTracker, "close", _spy_close),
        ):
            # 预建 bandi.db 使 factory 创建 badcase_tracker
            db_path = Path(str(tmp_path)) / ".bandi" / "data" / "bandi.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)")
            conn.commit()
            conn.close()
            store = _MockSessionStore()
            cfg = _test_config()
            result = await run_agent_turn(
                user_input="hello",
                session_id="test-badcase-close",
                session_store=store,
                bandi_config=cfg,
            )
        assert result.reply
        assert closed["n"] >= 1, "badcase_tracker 连接必须在轮次结束后关闭"

    @pytest.mark.asyncio
    async def test_generates_session_id(self, tmp_path: Any) -> None:
        from pathlib import Path

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = _MockSessionStore()
            cfg = _test_config()
            result = await run_agent_turn(
                user_input="hi",
                session_store=store,
                bandi_config=cfg,
            )
        assert result.session_id
        assert result.reply

    @pytest.mark.asyncio
    async def test_session_title_passed_to_store(self, tmp_path: Any) -> None:
        """Day136：run_agent_turn 的 session_title 透传到 create_session（新会话标题一次写入）。"""
        from pathlib import Path

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = _MockSessionStore()
            cfg = _test_config()
            await run_agent_turn(
                user_input="hi",
                session_store=store,
                bandi_config=cfg,
                session_title="首条消息标题",
            )
        assert store.last_title == "首条消息标题"

    @pytest.mark.asyncio
    async def test_with_stream_handler(self, tmp_path: Any) -> None:
        from pathlib import Path

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = _MockSessionStore()
            handler = AsyncMock()
            cfg = _test_config()
            result = await run_agent_turn(
                user_input="hello",
                session_id="s-1",
                session_store=store,
                bandi_config=cfg,
                stream_handler=handler,
            )
        assert result.reply is not None

    @pytest.mark.asyncio
    async def test_user_and_assistant_messages_stored(self, tmp_path: Any) -> None:
        from pathlib import Path

        from bandi.core.message import MessageRole

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = _MockSessionStore()
            cfg = _test_config()
            await run_agent_turn(
                user_input="hello world",
                session_id="s-msg-1",
                session_store=store,
                bandi_config=cfg,
            )
        assert len(store.messages) >= 2
        roles = {m.role for m in store.messages}
        assert MessageRole.USER in roles
        assert MessageRole.ASSISTANT in roles

    @pytest.mark.asyncio
    async def test_passes_content_filter(self, tmp_path: Any) -> None:
        from pathlib import Path

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = _MockSessionStore()
            cfg = _test_config()
            result = await run_agent_turn(
                user_input="hello",
                session_id="s-1",
                session_store=store,
                bandi_config=cfg,
            )
        assert result.reply is not None
        assert len(store.messages) >= 2

    @pytest.mark.asyncio
    async def test_exception_propagation(self, tmp_path: Any) -> None:
        from pathlib import Path

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = _MockSessionStore()
            cfg = _test_config()
            with patch.object(AgentLoop, "run", side_effect=RuntimeError("loop crashed")):
                with pytest.raises(RuntimeError, match="loop crashed"):
                    await run_agent_turn(
                        user_input="hello",
                        session_id="test-001",
                        session_store=store,
                        bandi_config=cfg,
                    )

    @pytest.mark.asyncio
    async def test_cleanup_on_setup_failure(self, tmp_path: Any) -> None:
        from pathlib import Path

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = _MockSessionStore()
            cfg = _test_config()
            with patch(
                "bandi.core.agent_runner.build_agent_context",
                side_effect=ValueError("setup failed"),
            ):
                with pytest.raises(ValueError, match="setup failed"):
                    await run_agent_turn(
                        user_input="hello",
                        session_id="test-001",
                        session_store=store,
                        bandi_config=cfg,
                    )
            # despite the exception, the message was already stored before the try block
            assert len(store.messages) == 1

    @pytest.mark.asyncio
    async def test_checkpoint_persisted_via_event_bus(self, tmp_path: Any) -> None:
        from pathlib import Path

        from bandi.memory.session.store import SessionStore

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = SessionStore(str(tmp_path / "sess.db"))
            sid = store.create_session(mode="execute")
            cfg = _test_config()
            result = await run_agent_turn(
                user_input="checkpoint me",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )
            assert result.reply is not None
            branch = store.get_branch(sid)
            checkpoint_entries = [e for e in branch if e.get("entry_type") == "checkpoint"]
            assert len(checkpoint_entries) >= 1, (
                "A4：每步持久化必须在 session_entries 中写入 checkpoint entry"
            )
            message_entries = [e for e in branch if e.get("entry_type") == "message"]
            assert len(message_entries) >= 2, "历史恢复依赖 message 条目（user+assistant）"
            session = store.get_session(sid)
            metadata = json.loads(session.get("metadata") or "{}")
            assert "last_terminal_reason" in metadata, (
                "终止元数据必须由 on_terminated 写入 session metadata"
            )
            store.close()

    @pytest.mark.asyncio
    async def test_tracer_wired_into_context(self, tmp_path: Any) -> None:
        from pathlib import Path

        from bandi.agent.factory import build_agent_context as _real_build_agent_context
        from bandi.observability.tracer import BandiTracer

        captured: list[Any] = []

        def _fake_build_agent_context(**kwargs: Any) -> Any:
            ctx = _real_build_agent_context(**kwargs)
            captured.append(ctx)
            return ctx

        with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
            store = _MockSessionStore()
            cfg = _test_config()
            with patch(
                "bandi.core.agent_runner.build_agent_context",
                side_effect=_fake_build_agent_context,
            ):
                result = await run_agent_turn(
                    user_input="hello",
                    session_id="s-1",
                    session_store=store,
                    bandi_config=cfg,
                )
        assert result.reply is not None
        assert captured
        assert isinstance(captured[0].meta.get("tracer"), BandiTracer)


def _test_config():
    from bandi.core.config import BandiConfig

    cfg = BandiConfig()
    cfg.llm.api_key = ""
    cfg.agent.max_iterations = 1
    return cfg
