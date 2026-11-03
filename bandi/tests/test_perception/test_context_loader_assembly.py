# 功能：ContextLoader 装配验证 — Ontology schema/项目指令/实体引用必须注入 system prompt

from __future__ import annotations

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
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_ontology_schema_injected_into_system_prompt(home: Any) -> None:
    """本体词表（核心差异化）必须进入 system prompt。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore

    ontology = OntologyStore(open_ontology(str(home / "ontology.db")))
    ontology.define_class({"id": "Project", "label": "项目"}, "2026-01-01T00:00:00")
    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="好的"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="hello",
            session_id=sid,
            session_store=store,
            triple_ontology_store=ontology,
        )
    assert result.reply
    system_prompt = "\n".join(
        str(m.get("content") or "") for m in provider.seen_messages[0] if m.get("role") == "system"
    )
    assert "本体" in system_prompt, "本体词表必须注入 system prompt"
    assert "Project" in system_prompt or "项目" in system_prompt


@pytest.mark.asyncio
async def test_project_instructions_injected(home: Any, tmp_path: Any) -> None:
    """项目级 AGENTS.md 指令必须进入 system prompt。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("项目规则：回答必须用简体中文", encoding="utf-8")

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="好的"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        with patch("bandi.perception.pipeline._detect_project_root", return_value=proj):
            result = await run_agent_turn(
                user_input="hello",
                session_id=sid,
                session_store=store,
            )
    assert result.reply
    system_prompt = "\n".join(
        str(m.get("content") or "") for m in provider.seen_messages[0] if m.get("role") == "system"
    )
    assert "项目规则：回答必须用简体中文" in system_prompt, (
        "项目 AGENTS.md 指令必须注入 system prompt（ContextLoader 未创建时注入失效）"
    )
