# 功能：契约——反思校验器（reflect_skill_response：最多 3 轮 rubric 审阅 +
# 修订回流 + 三态兜底，作为 distill() 末段接入质量门）。
# 钉行为不钉实现：契约断言「最终产物」——通过则原样保留草稿、未通过且返回可修正草稿
# 则修订生效并落盘、未返回草稿/模型失败/达上限三态均不崩且带对应 warning；
# 破坏者断言「修订稿不得绕过 normalize 守卫」（臆造工具仍被剔除）。
# 生产路径族：SkillDistiller.distill() 尾部调用（reflect_skill_response 注入
# complete + normalize，见 distiller.py 生产装配点）；reflect_skill_response 亦可独立复用。
# 契约增强（有意超集）：async + provider 注入（Bandi 无 sync LLMClient）；
from __future__ import annotations

import json
import os
from typing import Any

import pytest

from bandi.core.config import LLMConfig
from bandi.core.exceptions import LLMError
from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider
from bandi.skills.card import SkillCard
from bandi.skills.serializer import load_card

# ---------------------------------------------------------------------------
# 基准数据
# ---------------------------------------------------------------------------


def _draft_card(node_one_name: str = "收集订单信息") -> dict[str, Any]:
    """功能：契约基准——合法草稿卡（两节点 + 一条边，末节点为 response 保证闭环）。"""
    return {
        "skill_id": "sk_ship_order",
        "name": "发货 SOP",
        "business_domain": "仓储物流",
        "description": "处理发货流程。",
        "goal": ["确认订单信息", "反馈单号"],
        "required_info": ["订单号"],
        "nodes": [
            {
                "node_id": "collect_order",
                "type": "collect_info",
                "name": node_one_name,
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
        "edges": [
            {"source_node_id": "collect_order", "next_node_id": "reply_final_result", "priority": 0}
        ],
        "start_node_id": "collect_order",
        "terminal_node_ids": ["reply_final_result"],
    }


def _draft_response_text(card: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {"draft_skill": card if card is not None else _draft_card(), "warnings": []},
        ensure_ascii=False,
    )


def _review_text(
    *,
    passed: bool,
    summary: str = "审阅结论",
    rubric_results: list[dict[str, Any]] | None = None,
    source_warnings: list[str] | None = None,
    warnings: list[str] | None = None,
    draft_skill: dict[str, Any] | None = None,
) -> str:
    """功能：反思模型返回体 JSON（passed/rubric_results/draft_skill 可选键）。"""
    payload: dict[str, Any] = {
        "passed": passed,
        "summary": summary,
        "rubric_results": rubric_results
        if rubric_results is not None
        else [
            {"name": "closed_loop", "passed": passed, "finding": "", "origin": "generated_skill"}
        ],
        "source_warnings": source_warnings or [],
        "warnings": warnings or [],
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


@pytest.fixture()
def tmp_skills_dir(tmp_path):
    return str(tmp_path / "skills")


def _request() -> Any:
    from bandi.skills.distiller import SkillDistillRequest

    return SkillDistillRequest(
        title="发货 SOP",
        raw_content="收到发货指令后核对订单，再反馈单号。",
        business_domain="仓储物流",
        available_tools=[{"name": "create_shipping_order", "description": "创建运单"}],
    )


async def _distill(provider: MockProvider, skills_dir: str) -> Any:
    from bandi.skills.distiller import SkillDistiller

    distiller = SkillDistiller(provider=provider, skills_dir=skills_dir)
    return await distiller.distill(_request(), model_config=LLMConfig())


def _all_actions(card: SkillCard) -> list[str]:
    actions: list[str] = []
    for node in card.nodes:
        actions.extend(node.allowed_actions)
    return actions


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_pass_keeps_draft_and_consumes_one_review(tmp_skills_dir) -> None:
    """契约（通过路径）：反思返回 passed=true → 草稿原样保留（节点不增不减
    不重复注入闭环节点），且只消耗 1 次反思调用（distill 1 + review 1 = 2）。"""
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content=_draft_response_text()))
    provider.add_response(LLMResponse(content=_review_text(passed=True, summary="满足要求")))
    response = await _distill(provider, tmp_skills_dir)

    assert provider.call_count == 2, "通过路径应恰为 distill 1 次 + 反思 1 次"
    names = [node.name for node in response.draft_skill.nodes]
    assert names == ["收集订单信息", "反馈最终结果"], "反思通过不得改动/重复注入节点"
    assert not any("未通过" in w for w in response.warnings)


@pytest.mark.asyncio
async def test_reflection_prompt_carries_seven_rubrics_and_no_new_candidate(tmp_skills_dir) -> None:
    """契约（对齐）：反思提示词必须声明 7 项 rubric
    且不得出现参考源的 new_candidate 保留口径（Bandi 只认 existing registry）。"""
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content=_draft_response_text()))
    provider.add_response(LLMResponse(content=_review_text(passed=True)))
    await _distill(provider, tmp_skills_dir)

    system_prompt = provider.calls[1]["messages"][0]["content"]
    for name in (
        "source_alignment",
        "closed_loop",
        "adaptive_progression",
        "tool_grounding",
        "tool_call_format",
        "side_effect_confirmation",
        "interruption_and_recovery",
    ):
        assert name in system_prompt, f"反思提示词缺少 rubric：{name}"
    assert "new_candidate" not in system_prompt, "DP-SD02-1：不得保留 new_candidate 口径"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_revision_applied_and_persisted(tmp_skills_dir) -> None:
    """契约（修订路径 + DoD-12）：首轮未通过并返回可修正草稿 → 修订生效进入
    下一轮；次轮通过后，落盘的 `sk_ship_order.card.json` 读回等于**反思后**的草稿
    （distill 与反思同链，落盘发生在反思之后）。"""
    revised = _draft_card(node_one_name="收集订单号与收货地址")
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content=_draft_response_text()))
    provider.add_response(
        LLMResponse(
            content=_review_text(
                passed=False,
                summary="缺少收货地址收集",
                draft_skill=revised,
            )
        )
    )
    provider.add_response(LLMResponse(content=_review_text(passed=True, summary="已修正")))
    response = await _distill(provider, tmp_skills_dir)

    assert provider.call_count == 3
    assert response.draft_skill.nodes[0].name == "收集订单号与收货地址"
    saved = os.path.join(tmp_skills_dir, "sk_ship_order.card.json")
    assert os.path.isfile(saved), "反思后的草稿必须已落盘"
    assert load_card(saved).model_dump() == response.draft_skill.model_dump()


@pytest.mark.asyncio
async def test_reflection_revision_cannot_bypass_tool_guard(tmp_skills_dir) -> None:
    """契约（破坏者）：修订稿的 allowed_actions 臆造 `call_tool:ghost_tool`
    → 修订必须经同一 normalize 守卫（未登记 registry 一律剔除）
    不得因「来自反思修订」而绕过工具白名单。"""
    revised = _draft_card()
    revised["nodes"][0]["allowed_actions"] = ["ask_user", "call_tool:ghost_tool"]
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content=_draft_response_text()))
    provider.add_response(
        LLMResponse(content=_review_text(passed=False, draft_skill=revised, summary="补充工具动作"))
    )
    provider.add_response(LLMResponse(content=_review_text(passed=True)))
    response = await _distill(provider, tmp_skills_dir)

    actions = _all_actions(response.draft_skill)
    assert "call_tool:ghost_tool" not in actions, "反思修订不得绕过工具白名单"
    assert any("ghost_tool" in w for w in response.warnings)


# ---------------------------------------------------------------------------
# 三态兜底（未返回草稿 / 达上限 / 模型失败）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_not_passed_without_draft_keeps_current(tmp_skills_dir) -> None:
    """契约：未通过但模型未返回可修正草稿 → 保留当前草稿 + 明确 warning
    不得空转重试（立即收束，调用数不增长）。"""
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content=_draft_response_text()))
    provider.add_response(LLMResponse(content=_review_text(passed=False, summary="待改进")))
    response = await _distill(provider, tmp_skills_dir)

    assert provider.call_count == 2, "无草稿返回应立即收束，不得继续下一轮"
    assert any("未返回可修正" in w for w in response.warnings)
    assert isinstance(response.draft_skill, SkillCard)


@pytest.mark.asyncio
async def test_reflection_round_limit_keeps_last_revision(tmp_skills_dir) -> None:
    """契约（上限）：连续 MAX_REFLECTION_ROUNDS 轮未通过且每轮返回可修正草稿
    → 收束于上限轮，保留最后一版修订 + 「达到 3 轮上限」warning（不无限循环）。"""
    revised = _draft_card(node_one_name="最后一版节点名")
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content=_draft_response_text()))
    for _ in range(3):
        provider.add_response(
            LLMResponse(content=_review_text(passed=False, draft_skill=revised, summary="仍未通过"))
        )
    response = await _distill(provider, tmp_skills_dir)

    assert provider.call_count == 4, "1 次 distill + 3 轮反思 = 4"
    assert any("3 轮上限" in w for w in response.warnings)
    assert response.draft_skill.nodes[0].name == "最后一版节点名"


@pytest.mark.asyncio
async def test_reflection_model_failure_keeps_draft_without_crash(tmp_skills_dir) -> None:
    """契约（破坏者）：反思调用抛 LLMError（服务不可用）→ 蒸馏不崩
    保留当前草稿并记「反思校验未能完成」warning。"""
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content=_draft_response_text()))
    provider.add_response(LLMError)
    response = await _distill(provider, tmp_skills_dir)

    assert isinstance(response.draft_skill, SkillCard)
    assert any("反思校验未能完成" in w for w in response.warnings)


@pytest.mark.asyncio
async def test_reflection_non_json_review_keeps_draft(tmp_skills_dir) -> None:
    """契约（破坏者）：反思返回非 JSON 文本 → 不烧穿主链，保留草稿 + warning。"""
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content=_draft_response_text()))
    provider.add_response(LLMResponse(content="这不是 JSON"))
    response = await _distill(provider, tmp_skills_dir)

    assert isinstance(response.draft_skill, SkillCard)
    assert any("反思校验未能完成" in w for w in response.warnings)


@pytest.mark.asyncio
async def test_reflection_invalid_revision_keeps_draft_without_crash(tmp_skills_dir) -> None:
    """契约（破坏者）：反思返回可修正草稿但版本非法（非三段式 semver
    normalize 校验抛错）→ 不得穿透主链，保留当前草稿 + warning（兜底第四态）。"""
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content=_draft_response_text()))
    invalid = _draft_card()
    invalid["version"] = "v2"
    provider.add_response(
        LLMResponse(content=_review_text(passed=False, summary="需修正", draft_skill=invalid))
    )

    response = await _distill(provider, tmp_skills_dir)

    assert isinstance(response.draft_skill, SkillCard)
    assert response.draft_skill.version == "1.0.0", "非法修订稿不得进入产物"
    assert any("非法" in w or "保留当前" in w for w in response.warnings)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_source_input_findings_become_source_warnings(tmp_skills_dir) -> None:
    """契约（来源归因）：仅 origin=source_input 的失败 rubric 与 source_warnings
    降级为「原始文档本身可能存在问题」前缀 warning；origin=generated_skill 的 finding
    由修订稿承载，不得混入同前缀 warning（区分来源侧缺陷与生成侧缺陷）。"""
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content=_draft_response_text()))
    provider.add_response(
        LLMResponse(
            content=_review_text(
                passed=True,
                source_warnings=["原文未说明异常件处理"],
                rubric_results=[
                    {
                        "name": "closed_loop",
                        "passed": False,
                        "finding": "原文缺少失败分支",
                        "origin": "source_input",
                    },
                    {
                        "name": "tool_grounding",
                        "passed": False,
                        "finding": "生成侧工具名不规范",
                        "origin": "generated_skill",
                    },
                ],
            )
        )
    )
    response = await _distill(provider, tmp_skills_dir)

    joined = " | ".join(response.warnings)
    assert "原始文档本身可能存在问题" in joined
    assert "原文未说明异常件处理" in joined
    assert "原文缺少失败分支" in joined
    assert "生成侧工具名不规范" not in joined, "generated_skill 的 finding 不得降级为来源侧 warning"
