# 功能：契约——技能蒸馏/反思/改写 LLM 调用 session_id 全链透传。
# 钉行为：显式 session_id（真实操作 id）透传至统一计费入口（成本台账可按操作归集）；
# 生产路径族：complete_skill_text（统一入口）→ distill 四级降级链 + 反思 →
# editor 改写链 + 反思 → 全部调用点。
from __future__ import annotations

import json
from typing import Any

import pytest

from bandi.core.config import LLMConfig
from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


@pytest.fixture
def captured_sessions(monkeypatch):
    """功能：捕获 accounted_complete 的 kwargs（session_id 观测通道），真实调用链保持。"""
    from bandi.skills import skill_guards as guards_mod

    captured: list[dict[str, Any]] = []
    real_accounted = guards_mod.accounted_complete

    async def spy(provider, messages, **kwargs):
        captured.append(dict(kwargs))
        return await real_accounted(provider, messages, **kwargs)

    monkeypatch.setattr(guards_mod, "accounted_complete", spy)
    return captured


def _valid_card_payload() -> dict[str, Any]:
    """功能：契约基准——合法两节点闭环草稿卡。"""
    return {
        "skill_id": "sk_session_case",
        "name": "记账用例",
        "business_domain": "测试",
        "description": "用例。",
        "nodes": [
            {
                "node_id": "n1",
                "type": "collect_info",
                "name": "收集",
                "instruction": "收集信息。",
                "expected_user_info": ["订单号"],
                "allowed_actions": ["ask_user"],
            },
            {
                "node_id": "n2",
                "type": "response",
                "name": "回复",
                "instruction": "回复用户。",
                "allowed_actions": ["answer_user"],
            },
        ],
        "edges": [{"source_node_id": "n1", "next_node_id": "n2", "priority": 0}],
        "start_node_id": "n1",
        "terminal_node_ids": ["n2"],
    }


# 统一入口显式 session_id 透传
@pytest.mark.asyncio
async def test_complete_skill_text_forwards_explicit_session_id(captured_sessions) -> None:
    from bandi.skills.skill_guards import complete_skill_text

    provider = MockProvider()
    await complete_skill_text(
        provider,
        "系统提示",
        "用户输入",
        max_tokens=128,
        label="skill_distill",
        session_id="run-abc",
    )

    assert captured_sessions[-1]["session_id"] == "run-abc"


# 缺省回退 label（兼容既有行为）
@pytest.mark.asyncio
async def test_complete_skill_text_defaults_to_label(captured_sessions) -> None:
    from bandi.skills.skill_guards import complete_skill_text

    provider = MockProvider()
    await complete_skill_text(
        provider, "系统提示", "用户输入", max_tokens=128, label="skill_distill"
    )

    assert captured_sessions[-1]["session_id"] == "skill_distill"


# 蒸馏全链（生成 + 反思）全部调用携带显式 session_id
@pytest.mark.asyncio
async def test_distill_forwards_session_id_to_every_llm_call(captured_sessions, tmp_path) -> None:
    from bandi.skills.distiller import SkillDistiller, SkillDistillRequest

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content=json.dumps(
                {"draft_skill": _valid_card_payload(), "warnings": []}, ensure_ascii=False
            )
        )
    )
    distiller = SkillDistiller(provider=provider, skills_dir=str(tmp_path / "skills"))
    request = SkillDistillRequest(
        title="记账用例",
        raw_content="收到请求后先收集订单号，再回复用户。",
        business_domain="测试",
        available_tools=[],
    )

    await distiller.distill(request, model_config=LLMConfig(), session_id="run-distill")

    assert captured_sessions, "蒸馏必须发生 LLM 调用"
    assert {kw.get("session_id") for kw in captured_sessions} == {"run-distill"}


# 改写全链（改写 + 反思）全部调用携带显式 session_id
@pytest.mark.asyncio
async def test_editor_rewrite_forwards_session_id_to_every_llm_call(
    captured_sessions, tmp_path
) -> None:
    from bandi.skills.card import SkillCard
    from bandi.skills.editor import SkillEditor, SkillRewriteRequest

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content=json.dumps(
                {"draft_skill": _valid_card_payload(), "warnings": []}, ensure_ascii=False
            )
        )
    )
    editor = SkillEditor(provider=provider, skills_dir=str(tmp_path / "skills"))
    request = SkillRewriteRequest(
        current_skill=SkillCard(**_valid_card_payload()),
        instruction="把第一步说明写得更明确",
    )

    await editor.rewrite(request, session_id="run-edit")

    assert captured_sessions, "改写必须发生 LLM 调用"
    assert {kw.get("session_id") for kw in captured_sessions} == {"run-edit"}


# 破坏者：缺省 session_id 时蒸馏全链回退 label（不因新参数破坏统一入口语义）
@pytest.mark.asyncio
async def test_distill_without_session_id_falls_back_to_label(captured_sessions, tmp_path) -> None:
    from bandi.skills.distiller import SkillDistiller, SkillDistillRequest

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content=json.dumps(
                {"draft_skill": _valid_card_payload(), "warnings": []}, ensure_ascii=False
            )
        )
    )
    distiller = SkillDistiller(provider=provider, skills_dir=str(tmp_path / "skills"))
    request = SkillDistillRequest(
        title="记账用例",
        raw_content="收到请求后先收集订单号，再回复用户。",
        business_domain="测试",
        available_tools=[],
    )

    await distiller.distill(request, model_config=LLMConfig())

    assert captured_sessions
    assert {kw.get("session_id") for kw in captured_sessions} == {"skill_distill"}
