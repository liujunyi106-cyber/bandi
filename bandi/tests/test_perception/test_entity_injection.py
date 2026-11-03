# 功能：实体引用注入验证 — @实体 语法必须真实加载实体并注入 system prompt

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
async def test_entity_reference_injected(home: Any) -> None:
    """@实体 引用必须真实加载实体详情并注入 system prompt。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")

    # 建三元组本体 + 写入一个 Project 实体（label=中文名，供 @提及 解析）
    ontology_store = OntologyStore(open_ontology(str(home / "ontology.db")))
    ontology_store.define_class({"id": "Project", "label": "项目"}, "T00:00:00")
    ontology_store.assert_entity(
        {
            "id": "proj-stellar",
            "classes": ["Project"],
            "label": "恒星贸易项目",
            "comment": "恒星贸易的 ERP 迁移项目",
        },
        "T00:00:00",
    )

    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="好的"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="@恒星贸易项目 进展如何",
            session_id=sid,
            session_store=store,
            triple_ontology_store=ontology_store,
        )
    assert result.reply
    system_prompt = "\n".join(
        str(m.get("content") or "") for m in provider.seen_messages[0] if m.get("role") == "system"
    )
    assert "恒星贸易项目" in system_prompt, "实体引用必须注入 system prompt"
    ontology_store.close()
