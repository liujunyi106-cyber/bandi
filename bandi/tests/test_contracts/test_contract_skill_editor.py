# 功能：契约——Skill Card 局部改写器（SkillEditor：patch 式局部改写 + 越界忽略 +
# 钉行为不钉实现：契约断言「最终产物」——目标区域被改写而无关区域逐字保持、越界 patch
# 模型失败保留原卡、改写结果不得绕过工具白名单/动作白名单（破坏者）
# rewrite_and_persist 落盘读回等于改写后卡（DoD-12）。
# 生产路径族：SkillEditor.rewrite（纯函数，调用方持卡）+ rewrite_and_persist
# （load_card 读回 → 改写 → save_card 回写，serializer 新生产消费路径）。
from __future__ import annotations

import json
from typing import Any

import pytest

from bandi.core.config import LLMConfig
from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider
from bandi.skills.card import SkillCard
from bandi.skills.serializer import load_card, save_card


def _current_card(node_id: str = "collect_order") -> dict[str, Any]:
    """功能：契约基准——待改写的既有技能卡（两节点 + 一条边，末节点 response 保证闭环）。"""
    return {
        "skill_id": "sk_ship_order",
        "name": "发货 SOP",
        "version": "1.0.0",
        "business_domain": "仓储物流",
        "description": "处理发货流程。",
        "goal": ["确认订单信息", "反馈单号"],
        "required_info": ["订单号"],
        "nodes": [
            {
                "node_id": node_id,
                "type": "collect_info",
                "name": "收集订单信息",
                "instruction": "收集订单号。",
                "expected_user_info": ["订单号"],
                "allowed_actions": ["ask_user"],
            },
            {
                "node_id": "reply_final_result",
                "type": "response",
                "name": "反馈最终结果",
                "instruction": "给出明确最终回复。",
                "expected_user_info": [],
                "allowed_actions": ["answer_user", "handoff_human"],
            },
        ],
        "edges": [{"source_node_id": node_id, "next_node_id": "reply_final_result", "priority": 0}],
        "start_node_id": node_id,
        "terminal_node_ids": ["reply_final_result"],
    }


def _rewrite_payload(
    *,
    patches: list[dict[str, Any]] | None = None,
    draft_skill: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    assistant_message: str = "已完成选中部分的改写。",
) -> str:
    """功能：改写模型返回体 JSON（patches / draft_skill 二者按需给）。"""
    payload: dict[str, Any] = {
        "assistant_message": assistant_message,
        "warnings": warnings or [],
    }
    if patches is not None:
        payload["patches"] = patches
    if draft_skill is not None:
        payload["draft_skill"] = draft_skill
    return json.dumps(payload, ensure_ascii=False)


def _review_payload(passed: bool = True, draft_skill: dict[str, Any] | None = None) -> str:
    """功能：反思模型返回体 JSON（默认通过，避免干扰改写断言）。"""
    payload: dict[str, Any] = {
        "passed": passed,
        "summary": "审阅结论",
        "rubric_results": [
            {"name": "closed_loop", "passed": passed, "finding": "", "origin": "generated_skill"}
        ],
        "source_warnings": [],
        "warnings": [],
    }
    if draft_skill is not None:
        payload["draft_skill"] = draft_skill
    return json.dumps(payload, ensure_ascii=False)


class _RecordingProvider(MockProvider):
    """功能：记录每次 complete 的 messages/kwargs，其余行为同 MockProvider。"""

    def __init__(self, config: LLMConfig | None = None) -> None:
        super().__init__(config)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict] | None = None, **kwargs: Any
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return await super().complete(messages, tools=tools, **kwargs)


def _request(**overrides: Any) -> Any:
    from bandi.skills.editor import SkillRewriteRequest

    params: dict[str, Any] = {
        "current_skill": SkillCard.model_validate(_current_card()),
        "instruction": "把第一步的说明写得更明确",
        "available_tools": [{"name": "create_shipping_order", "description": "创建运单"}],
    }
    params.update(overrides)
    return SkillRewriteRequest(**params)


def _editor(provider: MockProvider, skills_dir: str = "~/.bandi/skills") -> Any:
    from bandi.skills.editor import SkillEditor

    return SkillEditor(provider=provider, skills_dir=skills_dir)


# ---------------------------------------------------------------------------
# patch 局部改写 + 越界守卫
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_basic_field_changes_only_target() -> None:
    """契约（patch 局部改写）：target_paths=["basic"] + patches 改 basic.name
    → 仅该字段变化，nodes/edges/start/terminal 与其余 basic 字段逐字保持。"""
    provider = _RecordingProvider()
    provider.add_response(
        LLMResponse(
            content=_rewrite_payload(patches=[{"path": "basic.name", "value": "发货 SOP（新版）"}])
        )
    )
    provider.add_response(LLMResponse(content=_review_payload()))
    response = await _editor(provider).rewrite(_request(target_paths=["basic"]))

    assert response.draft_skill.name == "发货 SOP（新版）"
    assert response.draft_skill.description == "处理发货流程。"
    assert [n.node_id for n in response.draft_skill.nodes] == [
        "collect_order",
        "reply_final_result",
    ]
    assert response.draft_skill.start_node_id == "collect_order"
    assert "basic" in response.changed_paths


@pytest.mark.asyncio
async def test_out_of_scope_patch_ignored_with_warning() -> None:
    """契约（破坏者）：target_paths=["basic"] 却 patch `nodes[0].instruction`
    → 越界 patch 被忽略（节点逐字不变）并出「已忽略越界改写路径」warning。"""
    provider = _RecordingProvider()
    provider.add_response(
        LLMResponse(
            content=_rewrite_payload(
                patches=[{"path": "nodes[0].instruction", "value": "越界改写的内容"}]
            )
        )
    )
    provider.add_response(LLMResponse(content=_review_payload()))
    response = await _editor(provider).rewrite(_request(target_paths=["basic"]))

    assert response.draft_skill.nodes[0].instruction == "收集订单号。"
    assert any("越界改写路径" in w for w in response.warnings)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_field_patch_reports_changed_path() -> None:
    """契约（节点级改写）：target_paths=["nodes[0]"] + patch 该节点 instruction
    → 该节点被改写、另一节点与图结构不变、changed_paths 报 `nodes[0]`。"""
    provider = _RecordingProvider()
    provider.add_response(
        LLMResponse(
            content=_rewrite_payload(
                patches=[{"path": "nodes[0].instruction", "value": "先收集订单号与收货地址。"}]
            )
        )
    )
    provider.add_response(LLMResponse(content=_review_payload()))
    response = await _editor(provider).rewrite(_request(target_paths=["nodes[0]"]))

    assert response.draft_skill.nodes[0].instruction == "先收集订单号与收货地址。"
    assert response.draft_skill.nodes[1].instruction == "给出明确最终回复。"
    assert "nodes[0]" in response.changed_paths


@pytest.mark.asyncio
async def test_node_rename_remaps_graph_references() -> None:
    """契约（破坏者）：节点改名（返回整卡 draft_skill，node_id 变）→ start/edges
    """
    renamed = dict(_current_card()["nodes"][0], node_id="collect_order_v2")
    draft = _current_card()
    draft["nodes"][0] = renamed
    draft["edges"] = [
        {"source_node_id": "collect_order_v2", "next_node_id": "reply_final_result", "priority": 0}
    ]
    draft["start_node_id"] = "collect_order_v2"
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content=_rewrite_payload(draft_skill=draft)))
    provider.add_response(LLMResponse(content=_review_payload()))
    response = await _editor(provider).rewrite(_request(target_paths=["nodes[0]"]))

    node_ids = [n.node_id for n in response.draft_skill.nodes]
    assert "collect_order_v2" in node_ids and "collect_order" not in node_ids
    assert response.draft_skill.start_node_id == "collect_order_v2"
    assert {e.source_node_id for e in response.draft_skill.edges} == {"collect_order_v2"}


# ---------------------------------------------------------------------------
# 降级与兜底
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_json_repaired_once() -> None:
    """契约（repair）：首轮输出非 JSON → 一次修复重试成功（patch 生效）。"""
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="这不是 JSON"))
    provider.add_response(
        LLMResponse(
            content=_rewrite_payload(
                patches=[{"path": "basic.description", "value": "改写后的描述"}]
            )
        )
    )
    provider.add_response(LLMResponse(content=_review_payload()))
    response = await _editor(provider).rewrite(_request(target_paths=["basic"]))

    assert response.draft_skill.description == "改写后的描述"
    assert provider.call_count == 3  # 改写(1) + 修复(1) + 反思(1)


@pytest.mark.asyncio
async def test_total_failure_keeps_current_card() -> None:
    """契约（兜底）：改写与修复均失败 → **保留原卡**（逐字等于 current_skill）
    + 明确 warning，不抛异常、不改动卡片。"""
    current = SkillCard.model_validate(_current_card())
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="不是 JSON"))
    provider.add_response(LLMResponse(content="还不是 JSON"))
    response = await _editor(provider).rewrite(_request(current_skill=current))

    assert response.draft_skill.model_dump() == current.model_dump()
    assert any("未能完成局部改写" in w for w in response.warnings)


@pytest.mark.asyncio
async def test_rewrite_then_reflection_runs_with_rewrite_source() -> None:
    """契约（反思接线）：改写成功后必须再走反思校验（source_kind="rewrite"）
    即改写(1) + 反思(1) = 2 次调用；反思提示词含 rubric 词表。"""
    provider = _RecordingProvider()
    provider.add_response(
        LLMResponse(content=_rewrite_payload(patches=[{"path": "basic.name", "value": "改名"}]))
    )
    provider.add_response(LLMResponse(content=_review_payload()))
    await _editor(provider).rewrite(_request(target_paths=["basic"]))

    assert provider.call_count == 2
    assert "closed_loop" in provider.calls[1]["messages"][0]["content"]


# ---------------------------------------------------------------------------
# 守卫不可绕过（破坏者）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rewrite_cannot_bypass_tool_whitelist() -> None:
    """契约（破坏者）：改写结果 allowed_actions 臆造 `call_tool:ghost_tool`
    → 必须被同一守卫剔除 + warning（改写通道不得成为工具白名单绕过路径，同族）。"""
    draft = _current_card()
    draft["nodes"][0]["allowed_actions"] = ["ask_user", "call_tool:ghost_tool"]
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content=_rewrite_payload(draft_skill=draft)))
    provider.add_response(LLMResponse(content=_review_payload()))
    response = await _editor(provider).rewrite(_request(target_paths=["all"]))

    actions: list[str] = []
    for node in response.draft_skill.nodes:
        actions.extend(node.allowed_actions)
    assert "call_tool:ghost_tool" not in actions
    assert any("ghost_tool" in w for w in response.warnings)


@pytest.mark.asyncio
async def test_rewrite_cannot_bypass_action_whitelist() -> None:
    """契约（破坏者）：改写结果含未定义动作（`ghost_action`）→ 剔除 + warning
    （动作白名单与蒸馏同源，同族）。"""
    draft = _current_card()
    draft["nodes"][0]["allowed_actions"] = ["ask_user", "ghost_action"]
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content=_rewrite_payload(draft_skill=draft)))
    provider.add_response(LLMResponse(content=_review_payload()))
    response = await _editor(provider).rewrite(_request(target_paths=["all"]))

    actions: list[str] = []
    for node in response.draft_skill.nodes:
        actions.extend(node.allowed_actions)
    assert "ghost_action" not in actions
    assert any("ghost_action" in w for w in response.warnings)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rewrite_and_persist_round_trip(tmp_path) -> None:
    """契约（DoD-12 端到端）：`rewrite_and_persist` 从真实目录 load_card 读回
    既有卡 → 改写 → save_card 回写；重新 load_card 读回的卡逐字等于本次改写结果
    （serializer 新生产消费路径，wiring 可证）。"""
    skills_dir = str(tmp_path / "skills")
    save_card(SkillCard.model_validate(_current_card()), skills_dir)

    provider = _RecordingProvider()
    provider.add_response(
        LLMResponse(
            content=_rewrite_payload(
                patches=[{"path": "basic.name", "value": "发货 SOP（已改写）"}]
            )
        )
    )
    provider.add_response(LLMResponse(content=_review_payload()))
    response = await _editor(provider, skills_dir).rewrite_and_persist(
        "sk_ship_order",
        "把技能名改得更明确",
        target_paths=["basic"],
        available_tools=[{"name": "create_shipping_order", "description": "创建运单"}],
    )

    assert response.draft_skill.name == "发货 SOP（已改写）"
    persisted = load_card(str(tmp_path / "skills" / "sk_ship_order.card.json"))
    assert persisted.model_dump() == response.draft_skill.model_dump()
