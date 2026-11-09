# 功能：模式注入与生效验证 — plan/ask 模式必须在 system prompt 体现 + 工具限制真实生效

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


class _RecordingProvider(MockProvider):
    def __init__(self) -> None:
        super().__init__()
        self.seen_messages: list[list[dict[str, Any]]] = []
        self.seen_tools: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.seen_messages.append(messages)
        self.seen_tools.append(tools or [])
        return await super().complete(messages, tools=tools, **kwargs)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.seen_messages.append(messages)
        self.seen_tools.append(tools or [])
        async for ev in super().stream(messages, tools=tools, **kwargs):
            yield ev


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _tool_names(tools: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for t in tools:
        fn = t.get("function") or t
        name = fn.get("name")
        if name:
            names.add(name)
    return names


@pytest.mark.asyncio
async def test_plan_mode_restricts_tools(home: Any) -> None:
    """plan 模式：system prompt 体现计划模式 + 工具列表只含只读工具。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="plan")
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="计划完成"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="制定计划",
            session_id=sid,
            session_store=store,
            mode="plan",
        )
    assert result.reply
    system_prompt = "\n".join(
        str(m.get("content") or "") for m in provider.seen_messages[0] if m.get("role") == "system"
    )
    assert "计划" in system_prompt, "plan 模式应在 system prompt 体现"
    tool_names = _tool_names(provider.seen_tools[0])
    assert "write" not in tool_names, "plan 模式不得暴露写工具"
    assert "bash" not in tool_names, "plan 模式不得暴露 bash"


@pytest.mark.asyncio
async def test_question_input_keeps_tools_in_execute(home: Any) -> None:
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="回答"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="问个问题：这个工具怎么用",
            session_id=sid,
            session_store=store,
            mode="execute",
        )
    assert result.reply
    assert provider.seen_tools[0], "问句输入不得清空 execute 会话的工具面"
    system_prompt = "\n".join(
        str(m.get("content") or "") for m in provider.seen_messages[0] if m.get("role") == "system"
    )
    assert "问答模式" not in system_prompt, "system prompt 不得渲染已移除的 ask 模式段"
