# 功能：注入全景综合验证 — 单次真实 turn 的 system prompt 必须包含全部注入项

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
async def test_full_injection_panorama_single_turn(home: Any, tmp_path: Any) -> None:
    """单次真实 turn：system prompt 必须同时包含 schema/项目指令/实体/技能/模式/记忆。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.long_term import MemoryItem
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore
    from bandi.skills.manager import create_skill_registry
    from bandi.skills.models import SkillDef

    # 项目指令
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("项目规则：回答必须用简体中文", encoding="utf-8")

    # 实体
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
    entity_store = ontology_store

    # 记忆
    manager = MemoryManager()
    manager.long_term.store(
        MemoryItem(item_id="k1", content="恒星贸易预算五十万元", importance=0.9, metadata={})
    )

    # 技能
    registry = create_skill_registry()
    registry.register(
        SkillDef(
            name="qa",
            description="QA 技能",
            instructions="回答质量问题时必须引用测试结果。",
            triggers=["QA", "质量检查"],
        )
    )

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="好的"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        with patch("bandi.core.agent_runner.create_skill_registry", return_value=registry):
            with patch("bandi.perception.pipeline._detect_project_root", return_value=proj):
                result = await run_agent_turn(
                    user_input="@恒星贸易项目 做实体数据 QA 检查，预算如何",
                    session_id=sid,
                    session_store=store,
                    triple_ontology_store=entity_store,
                    memory_manager=manager,
                )
    assert result.reply
    sent = provider.seen_messages[0]
    all_system = "".join(str(m.get("content", "")) for m in sent if m.get("role") == "system")
    tail_user = [str(m.get("content", "")) for m in sent if m.get("role") == "user"][-2:]

    # 全景断言（注入项必须全部在）
    assert "本体" in all_system, "① Ontology schema 未注入"
    assert "项目规则：回答必须用简体中文" in all_system, "② 项目 AGENTS.md 指令未注入"
    assert "恒星贸易项目" in all_system, "③ @实体引用未注入"
    assert "**qa**" in all_system, "④ Skill 目录未注入"
    assert "必须引用测试结果" not in all_system, "④ F 模式不得自动注入技能正文"
    assert "[技能指令]" not in all_system, "④ F 模式不再插入技能指令消息"
    assert any("预算五十万元" in text for text in tail_user), (
        "⑤ 记忆必须位于末尾 user 段（不进 system 前缀）"
    )

    # 工具列表必须传入 LLM
    tool_names = _tool_names(provider.seen_tools[0])
    assert "read" in tool_names, "⑦ 工具 schema 未传给 LLM"
    assert "ontology_query" in tool_names, "⑧ 实体查询工具未注册"
    assert len(tool_names) >= 10, f"工具数量过少: {len(tool_names)}"

    entity_store.close()
    store.close()


@pytest.mark.asyncio
async def test_injection_persists_in_second_turn(home: Any) -> None:
    """多轮：第二轮 system prompt 仍含 schema/实体引用（注入不随轮次丢失）。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore

    entity_store = OntologyStore(open_ontology(str(home / "ontology.db")))
    entity_store.define_class({"id": "Project", "label": "项目"}, "T00:00:00")
    entity_store.assert_entity(
        {
            "id": "proj-stellar",
            "classes": ["Project"],
            "label": "恒星贸易项目",
            "comment": "恒星贸易的 ERP 迁移项目",
        },
        "T00:00:00",
    )

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    manager = MemoryManager()
    provider = _RecordingProvider()
    provider.add_response(
        LLMResponse(content="第一轮回答内容足够长以触发记忆写入流程包含有效信息用于验证多轮注入。")
    )
    provider.add_response(LLMResponse(content="第二轮回答"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        r1 = await run_agent_turn(
            user_input="@恒星贸易项目 进展如何",
            session_id=sid,
            session_store=store,
            triple_ontology_store=entity_store,
            memory_manager=manager,
        )
        r2 = await run_agent_turn(
            user_input="@恒星贸易项目 继续",
            session_id=sid,
            session_store=store,
            triple_ontology_store=entity_store,
            memory_manager=manager,
        )
    assert r1.reply and r2.reply
    for idx, msgs in enumerate(provider.seen_messages):
        system_prompt = "".join(
            str(m.get("content", "")) for m in msgs if m.get("role") == "system"
        )
        assert "本体" in system_prompt, f"第 {idx + 1} 轮 schema 未注入"
        assert "恒星贸易项目" in system_prompt, f"第 {idx + 1} 轮实体引用未注入"
    entity_store.close()
    store.close()
