# 功能：F 模式技能注入验证——目录常驻、正文只在 skill_invoke 时加载

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


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_skill_instructions_injected(home: Any, tmp_path: Any) -> None:
    """F 模式：注册技能只进目录，正文不得自动进入 system prompt。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore
    from bandi.skills.manager import create_skill_registry
    from bandi.skills.models import SkillDef

    # 直接注册一个 SkillDef（避免依赖 ~/.bandi/skills 目录）
    skill_def = SkillDef(
        name="qa",
        description="QA 检查技能",
        instructions="回答质量问题时必须引用测试结果。",
        constraints=["必须使用简体中文"],
        triggers=["QA", "质量检查"],
    )
    registry = create_skill_registry()
    registry.register(skill_def)

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="好的"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        with patch("bandi.core.agent_runner.create_skill_registry", return_value=registry):
            result = await run_agent_turn(
                user_input="帮我做 QA 检查",
                session_id=sid,
                session_store=store,
            )
    assert result.reply
    all_system = "".join(
        str(m.get("content", "")) for m in provider.seen_messages[0] if m.get("role") == "system"
    )
    assert "## 可用技能" in all_system, "技能目录必须常驻（模型据此调用 skill_invoke）"
    assert "**qa**" in all_system, "注册技能必须出现在目录"
    assert "必须引用测试结果" not in all_system, "F 模式不得自动注入技能正文"
    assert "必须使用简体中文" not in all_system, "F 模式不得自动注入技能约束"
    assert "[技能指令]" not in all_system, "F 模式不再插入技能指令消息"
