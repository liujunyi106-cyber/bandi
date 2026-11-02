# 功能：契约——SOP 蒸馏器（四级降级链 + 闭环注入 + 未知工具剔除 +
# step_ids/llm_limits 内部件 + save_card 生产接线）。
# 钉行为不钉实现：契约断言「最终产物」——合法 SkillCard 可构建且字段中文本真
# 降级链到达级别由调用次数与草稿结构反推、闭环不变式（CLOSED_LOOP_RESPONSE_RULE +
# 自动补 response 节点）、模型臆造工具被移出 allowed_actions 并产生 warning
# 生产路径族：distill() 同步调用（四级降级：正常 → repair → staged → fallback）
# 四级各为独立可复用降级路径（流式外壳未来零重写复用）；step_ids/llm_limits 为 A 档独立模块。
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

# pylint: disable=wrong-import-position # 未实现模块在红灯阶段后随实现补充导入


def _valid_card_payload() -> dict[str, Any]:
    """功能：契约基准——合法模型输出草稿卡（语义字段覆盖 StartDeck 蒸馏器产物形状）。"""
    return {
        "skill_id": "sk_ship_order",
        "name": "发货 SOP",
        "business_domain": "仓储物流",
        "description": "处理发货流程。",
        "trigger_intents": ["发货", "安排配送"],
        "user_utterance_examples": ["我要发货", "安排配送"],
        "goal": ["确认订单信息", "安排物流", "反馈单号"],
        "required_info": ["订单号", "收货地址"],
        "slot_filling_policy": {"enabled": True, "target_info": ["订单号", "收货地址"]},
        "response_rules": ["信息不足时先追问，不要编造事实。"],
        "nodes": [
            {
                "node_id": "collect_order",
                "type": "collect_info",
                "name": "收集订单信息",
                "instruction": "收集订单号和收件人。",
                "optional": False,
                "condition": None,
                "expected_user_info": ["订单号", "收货地址"],
                "allowed_actions": ["ask_user"],
            },
            {
                "node_id": "arrange_ship",
                "type": "action",
                "name": "安排发货",
                "instruction": "调用物流接口创建运单。",
                "optional": False,
                "condition": None,
                "expected_user_info": [],
                "allowed_actions": ["call_tool:create_shipping_order"],
            },
        ],
        "edges": [
            {"source_node_id": "collect_order", "next_node_id": "arrange_ship", "priority": 0}
        ],
        "start_node_id": "collect_order",
        "terminal_node_ids": ["arrange_ship"],
        "interruption_policy": {
            "related_question": "回答相关问题后回到当前流程。",
            "unrelated_business": "可切换新流程并保留当前进度。",
        },
    }


def _valid_response_payload(raw_card: dict[str, Any] | None = None) -> str:
    """功能：把草稿卡包装成模型返回体 JSON（draft_skill 键）。"""
    return json.dumps(
        {
            "draft_skill": raw_card if raw_card is not None else _valid_card_payload(),
            "warnings": [],
        },
        ensure_ascii=False,
    )


def _request(
    title: str = "发货 SOP",
    raw_content: str = "收到发货指令后，先核对订单与收货地址，再调用物流系统创建运单，把单号反馈用户。",
    business_domain: str = "仓储物流",
    available_tools: list[dict[str, Any]] | None = None,
) -> Any:
    """功能：构造 SkillDistillRequest（红灯阶段先用伪造 shape，实现后切换真实类型）。"""
    from bandi.skills.distiller import SkillDistillRequest

    return SkillDistillRequest(
        title=title,
        raw_content=raw_content,
        business_domain=business_domain,
        available_tools=available_tools
        or [
            {
                "name": "create_shipping_order",
                "display_name": "创建运单",
                "description": "创建物流运单",
                "input_schema": {},
            }
        ],
    )


def _mock(content: str) -> LLMResponse:
    return LLMResponse(content=content)


class _RecordingProvider(MockProvider):
    """功能：记录每次 complete 的 messages/kwargs（契约观测通道），其余行为同 MockProvider。"""

    def __init__(self, config: LLMConfig | None = None) -> None:
        super().__init__(config)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict] | None = None, **kwargs: Any
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return await super().complete(messages, tools=tools, **kwargs)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestStepIdsUniqueNodeIds:
    def test_duplicate_node_ids_remapped_unique_with_warning(self) -> None:
        """契约（A 档模块①）：模型输出重复 node_id → 逐节点唯一化
        （`_2` 后缀递增），并产生修正 warning——最终产物不可含重复节点标识。"""
        from bandi.skills.step_ids import ensure_unique_node_ids

        nodes = [
            {"node_id": "dup", "name": "甲"},
            {"node_id": "dup", "name": "乙"},
            {"node_id": "dup", "name": "丙"},
        ]
        normalized, warnings = ensure_unique_node_ids(nodes)
        ids = [node["node_id"] for node in normalized]
        assert len(ids) == len(set(ids)) == 3
        assert ids[0] == "dup"
        assert ids[1] in {"dup_2", "dup_3"}
        assert ids[2] in {"dup_2", "dup_3"}
        assert any("已修正" in w or "重复" in w for w in warnings)

    def test_missing_node_id_backfilled_with_warning(self) -> None:
        """契约：节点缺 node_id → 按顺序补全 `node_N` 并保留 name。"""
        from bandi.skills.step_ids import ensure_unique_node_ids

        nodes = [{"name": "甲"}, {"name": "乙"}]
        normalized, warnings = ensure_unique_node_ids(nodes)
        assert [node["node_id"] for node in normalized] == ["node_1", "node_2"]
        assert [node["name"] for node in normalized] == ["甲", "乙"]
        assert any("补全" in w for w in warnings)

    def test_card_remap_start_terminal_edges(self) -> None:
        """契约：整卡字典唯一化后 start/terminal/edges 引用随之重映射
        返回的 SkillCard 通过 闭环校验（唯一性 + 顶点存在 + reachable）。
        注意：Bandi SkillCard 构造即执行唯一性校验（超集），因此唯一化必须
        发生在 SkillCard 构造之前，契约输入为「模型原始字典」而非已构造的卡。"""
        from bandi.skills.step_ids import skill_card_with_unique_step_ids

        raw: dict[str, Any] = _valid_card_payload()
        for node in raw["nodes"]:
            node["node_id"] = "dup"
        raw["start_node_id"] = "dup"
        raw["terminal_node_ids"] = ["dup"]
        for edge in raw["edges"]:
            edge["source_node_id"] = "dup"
            edge["next_node_id"] = "dup"
        normalized, warnings = skill_card_with_unique_step_ids(raw)
        assert isinstance(normalized, SkillCard)
        ids = [node.node_id for node in normalized.nodes]
        assert len(ids) == len(set(ids)) == len(raw["nodes"])
        assert normalized.start_node_id in set(ids)
        assert [t in set(ids) for t in normalized.terminal_node_ids].count(True) == len(
            normalized.terminal_node_ids
        )
        assert [e.next_node_id in set(ids) for e in normalized.edges].count(True) == len(
            normalized.edges
        )
        assert any("已修正" in w for w in warnings)

    def test_duplicate_node_ids_references_point_to_first_copy(self) -> None:
        """契约（破坏者）：重复 node_id 时引用重映射必须指向「保留原名」的
        首个副本（首见优先）——不得被后写覆盖指向最后一个重命名副本。"""
        from bandi.skills.step_ids import skill_card_with_unique_step_ids

        raw: dict[str, Any] = _valid_card_payload()
        raw["nodes"] = [
            {
                "node_id": "a",
                "type": "collect_info",
                "name": "甲",
                "instruction": "甲步。",
                "expected_user_info": [],
                "allowed_actions": ["ask_user"],
            },
            {
                "node_id": "a",
                "type": "action",
                "name": "乙",
                "instruction": "乙步。",
                "expected_user_info": [],
                "allowed_actions": ["continue_flow"],
            },
            {
                "node_id": "b",
                "type": "response",
                "name": "丙",
                "instruction": "丙步。",
                "expected_user_info": [],
                "allowed_actions": ["answer_user"],
            },
        ]
        raw["start_node_id"] = "a"
        raw["terminal_node_ids"] = ["b"]
        raw["edges"] = [{"source_node_id": "a", "next_node_id": "b", "priority": 0}]

        normalized, _ = skill_card_with_unique_step_ids(raw)

        assert normalized.nodes[0].node_id == "a", "首个副本必须保留原名"
        assert normalized.start_node_id == "a"
        assert normalized.edges[0].source_node_id == "a"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestLLMLimitsSkillModelConfig:
    def test_derived_config_raises_output_budget_without_mutating_source(self) -> None:
        """契约：skill_model_config 派生 max_output_tokens <= 8000 的配置
        → 上调至蒸馏预算上限（>= 8192）；源配置对象零改动。"""
        from bandi.skills.llm_limits import skill_model_config

        base = LLMConfig(max_output_tokens=2048, default_model="deepseek-v4-flash")
        derived = skill_model_config(base)
        assert derived.max_output_tokens >= 8192
        assert derived is not base
        assert base.max_output_tokens == 2048
        assert derived.default_model == base.default_model

    def test_already_large_budget_kept(self) -> None:
        """契约：源配置 max_output_tokens 已 >= 8192 → 不缩小。"""
        from bandi.skills.llm_limits import skill_model_config

        base = LLMConfig(max_output_tokens=16384)
        derived = skill_model_config(base)
        assert derived.max_output_tokens == 16384

    def test_zero_output_size_rejected_like_level_config(self) -> None:
        """契约（破坏者）：max_output_tokens=0 不可派生（LLMConfig gt=0
        门禁仍生效——派生不绕过既有校验）。"""
        from pydantic import ValidationError

        from bandi.skills.llm_limits import skill_model_config

        with pytest.raises(ValidationError):
            skill_model_config(LLMConfig(max_output_tokens=0))


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_skills_dir(tmp_path):
    """功能：蒸馏落盘目录（别名化语义，避免契约与 pytest 内部命名耦合）。"""
    return str(tmp_path / "skills")


@pytest.mark.asyncio
async def test_distill_level1_normal_json_builds_draft_and_persists(
    tmp_skills_dir,
) -> None:
    """契约（正常路径 + 接线）：
    模型返回完整合法 JSON → distill() 返回可构建 SkillCard、中文逐字透传；
    产物已按 落盘 draft 到 skills_dir/<skill_id>.card.json，load_card 逐字读回。"""
    from bandi.skills.distiller import SkillDistiller

    provider = MockProvider()
    provider.add_response(_mock(_valid_response_payload()))
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    response = await distiller.distill(_request(), model_config=LLMConfig())

    assert response.draft_skill.name == "发货 SOP"
    assert response.draft_skill.business_domain == "仓储物流"
    assert response.draft_skill.goal == ["确认订单信息", "安排物流", "反馈单号"]
    # 闭环注入：模型两个流程节点 + 自动补最终回复节点
    assert [n.name for n in response.draft_skill.nodes] == [
        "收集订单信息",
        "安排发货",
        "反馈最终结果",
    ]
    assert response.draft_skill.nodes[-1].type == "response"
    assert provider.call_count == 2  # distill L1(1) + 反思(1)，反思失败时保留草稿

    saved = os.path.join(tmp_skills_dir, "sk_ship_order.card.json")
    assert os.path.isfile(saved), f"draft 未落盘: {saved}"
    reread = load_card(saved)
    assert reread.model_dump() == response.draft_skill.model_dump()


@pytest.mark.asyncio
async def test_distill_level1_injects_closed_loop_rule_and_reply_node(tmp_skills_dir) -> None:
    """契约（闭环注入）：draft 的 response_rules 必须注入
    CLOSED_LOOP_RESPONSE_RULE 与自适应推进规则；末节点是 action（非 answer_user）
    → 自动补充最终回复 response 节点并产生 warning——防「请稍候」死循环。"""
    from bandi.skills.distiller import SkillDistiller

    provider = MockProvider()
    provider.add_response(_mock(_valid_response_payload()))
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    response = await distiller.distill(_request(), model_config=LLMConfig())

    rules = " ".join(response.draft_skill.response_rules)
    assert "闭环" in rules and "请稍候" in rules
    assert "自适应" in rules or "重复追问" in rules or "既" in rules
    assert any(node.type == "response" for node in response.draft_skill.nodes)
    assert any(node.node_id.startswith("reply_final_result") for node in response.draft_skill.nodes)


@pytest.mark.asyncio
async def test_distill_level2_repair_succeeds_after_bad_json(tmp_skills_dir) -> None:
    """契约（repair）：首轮输出非法 JSON → 带上一轮上下文修复重试
    （最多 MODEL_REPAIR_ATTEMPTS=2 次）→ 修复轮产物可构建且走完整卡（非 fallback 草稿）。"""
    from bandi.skills.distiller import SkillDistiller

    provider = MockProvider()
    provider.add_response(_mock("这段不是 JSON，模型没按要求输出。"))
    provider.add_response(_mock(_valid_response_payload()))
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    response = await distiller.distill(_request(), model_config=LLMConfig())

    assert provider.call_count == 3  # L1(1) + repair(1) + 反思(1)
    assert response.draft_skill.skill_id == "sk_ship_order"
    assert len(response.draft_skill.nodes) >= 3  # 两个流程节点 + 闭环 response 节点


@pytest.mark.asyncio
async def test_distill_level3_staged_builds_outline_after_two_failures(tmp_skills_dir) -> None:
    """契约（staged）：正常+repair 均失败 → 分段生成
    staged 阶段输出合法大纲 → 产物仍为可构建完整卡。"""
    from bandi.skills.distiller import SkillDistiller

    outline_payload: dict[str, Any] = {
        "draft_skill": {
            **{k: v for k, v in _valid_card_payload().items() if k != "nodes"},
            "nodes": [
                {"node_id": "o_collect", "type": "collect_info", "name": "收集信息底座"},
                {"node_id": "o_ship", "type": "action", "name": "发货底座"},
            ],
        },
        "warnings": ["分段生成：大纲阶段完成。"],
    }
    provider = MockProvider()
    provider.add_response(_mock("###### 非法"))
    provider.add_response(_mock("###### 非法"))
    provider.add_response(_mock("###### 非法"))
    provider.add_response(_mock(json.dumps(outline_payload, ensure_ascii=False)))
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    response = await distiller.distill(_request(), model_config=LLMConfig())

    assert (
        provider.call_count >= 5
    )  # L1(1) + repair(2) + outline(1) 起步 + 反思(1)；后续扩写/若成功继续累加
    assert response.draft_skill.skill_id == "sk_ship_order"
    assert len(response.draft_skill.nodes) >= 2
    assert response.draft_skill.start_node_id in {n.node_id for n in response.draft_skill.nodes}
    assert [
        t in {n.node_id for n in response.draft_skill.nodes}
        for t in response.draft_skill.terminal_node_ids
    ]


@pytest.mark.asyncio
async def test_distill_level4_fallback_draft_on_total_failure(tmp_skills_dir) -> None:
    """契约（fallback）：全部降级路径失败 → 返回最低可运行草稿
    （固定 understand_request/reply_result 节点 + 闭环规则），不抛异常不中断。"""
    from bandi.skills.distiller import SkillDistiller

    provider = MockProvider()
    for _ in range(6):
        provider.add_response(_mock("错误输出：根本不是 JSON"))
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    response = await distiller.distill(_request(), model_config=LLMConfig())

    assert isinstance(response.draft_skill, SkillCard)
    node_ids = [n.node_id for n in response.draft_skill.nodes]
    assert "understand_request" in node_ids and "reply_result" in node_ids
    assert [n.type for n in response.draft_skill.nodes][-1] == "response"
    assert any("最低可运行草稿" in w for w in response.warnings)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distill_removes_unknown_tool_action_with_warning(tmp_skills_dir) -> None:
    """契约：模型在 allowed_actions 臆造工具
    `call_tool:ghost_tool`（不在 available_tools＝现有 Tool registry）→ 移出该动作 +
    warning 点名；registry 内已配置工具的动作原样保留。"""
    from bandi.skills.distiller import SkillDistiller

    payload = _valid_response_payload()
    raw = json.loads(payload)
    node = raw["draft_skill"]["nodes"][1]
    node["allowed_actions"] = ["call_tool:create_shipping_order", "call_tool:ghost_tool"]
    provider = MockProvider()
    provider.add_response(_mock(json.dumps(raw, ensure_ascii=False)))
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    response = await distiller.distill(_request(), model_config=LLMConfig())

    allowed: list[str] = []
    for n in response.draft_skill.nodes:
        allowed.extend(n.allowed_actions)
    assert "call_tool:ghost_tool" not in allowed
    assert "call_tool:create_shipping_order" in allowed
    assert any("ghost_tool" in w for w in response.warnings)


@pytest.mark.asyncio
async def test_distill_chinese_and_punctuation_passthrough(tmp_skills_dir) -> None:
    """契约（真实数据形态）：中文标题/业务领域/节点名/指令含标点与
    引号 → 蒸馏产物逐字保留不丢截。"""
    from bandi.skills.distiller import SkillDistiller

    title = "「售后」：退换货处理"
    raw = _valid_card_payload()
    raw["name"] = "「售后」：退换货处理"
    raw["business_domain"] = "客服中心（含引号与括号）"
    raw["nodes"][0]["name"] = "第一步：登记工单「原始单号」与原因；若跨省，标注“偏远”"
    provider = MockProvider()
    provider.add_response(
        _mock(json.dumps({"draft_skill": raw, "warnings": []}, ensure_ascii=False))
    )
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    response = await distiller.distill(
        _request(
            title=title, raw_content="售后流程：登记、审核、换货。", business_domain="客服中心"
        ),
        model_config=LLMConfig(),
    )

    assert response.draft_skill.name == "「售后」：退换货处理"
    assert response.draft_skill.business_domain == "客服中心（含引号与括号）"
    assert (
        response.draft_skill.nodes[0].name
        == "第一步：登记工单「原始单号」与原因；若跨省，标注“偏远”"
    )


@pytest.mark.asyncio
async def test_distill_empty_and_oversized_raw_content_not_crash(tmp_skills_dir) -> None:
    """契约（破坏者/边界）：空 raw_content、超长 raw_content（1 万字符）
    蒸馏均不崩——空输入落到 fallback 最低草稿；超长直接走模型（契约不因长度自杀）。"""
    from bandi.skills.distiller import SkillDistiller

    provider = MockProvider()
    provider.add_response(_mock(""))
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    empty = await distiller.distill(
        _request(title="新技能", raw_content="", business_domain=""),
        model_config=LLMConfig(),
    )
    assert isinstance(empty.draft_skill, SkillCard)
    assert len(empty.draft_skill.nodes) >= 2

    provider2 = MockProvider()
    provider2.add_response(_mock(_valid_response_payload()))
    distiller2 = SkillDistiller(provider=provider2, skills_dir=tmp_skills_dir)
    big = await distiller2.distill(
        _request(title="长流程", raw_content="第一步。" * 2000),
        model_config=LLMConfig(),
    )
    assert isinstance(big.draft_skill, SkillCard)


@pytest.mark.asyncio
async def test_distill_provider_raises_still_falls_back(tmp_skills_dir) -> None:
    """契约（破坏者）：Provider 连续抛 LLMError（模拟服务不可用）
    → 蒸馏不冒泡异常，降级到 fallback 草稿并记录 warning。"""
    from bandi.skills.distiller import SkillDistiller

    provider = MockProvider()
    for _ in range(6):
        provider.add_response(LLMError)
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    response = await distiller.distill(_request(), model_config=LLMConfig())

    assert isinstance(response.draft_skill, SkillCard)
    assert [n.node_id for n in response.draft_skill.nodes][0] == "understand_request"
    assert any("最低可运行草稿" in w for w in response.warnings)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distill_level1_survives_non_numeric_edge_priority(tmp_skills_dir) -> None:
    """契约（回归/破坏者）：模型合法 JSON 但边 priority 为字符串
    （如 "urgent"）→ 蒸馏不得烧穿降级链落到 fallback；多轮语义应正常、call_count==1
    （成功即收）。此用例在修复前会以 call_count>=4 + fallback 草稿失败。"""
    from bandi.skills.distiller import SkillDistiller

    payload = _valid_response_payload()
    raw = json.loads(payload)
    raw["draft_skill"]["edges"][0]["priority"] = "urgent"
    provider = MockProvider()
    provider.add_response(_mock(json.dumps(raw, ensure_ascii=False)))
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    response = await distiller.distill(_request(), model_config=LLMConfig())

    assert provider.call_count == 2  # L1(1) + 反思(1)，L1 成功即不再走降级链
    assert response.draft_skill.skill_id == "sk_ship_order"
    assert not any("最低可运行草稿" in w for w in response.warnings)
    assert all(
        e.source_node_id in {n.node_id for n in response.draft_skill.nodes}
        for e in response.draft_skill.edges
    )


@pytest.mark.asyncio
async def test_distill_save_failure_warns_but_returns_draft(tmp_path) -> None:
    """契约（回归/破坏者）：skills_dir 指向一个**已存在文件**的路径
    → save_card mkdir 抛 OSError → 蒸馏不裸抛 SkillCardSaveError，返回内存态 draft
    + 落盘失败 warning。"""
    from bandi.skills.distiller import SkillDistiller

    blocker = tmp_path / "skills_blocked"
    blocker.write_text("占用文件而非目录", encoding="utf-8")
    provider = MockProvider()
    provider.add_response(_mock(_valid_response_payload()))
    distiller = SkillDistiller(provider=provider, skills_dir=str(blocker))
    response = await distiller.distill(_request(), model_config=LLMConfig())

    assert isinstance(response.draft_skill, SkillCard)
    assert response.draft_skill.skill_id == "sk_ship_order"
    assert any("落盘" in w or "无法创建" in w for w in response.warnings)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_removed_even_when_mentioned_in_tool_mentions(tmp_skills_dir) -> None:
    """契约（回归/破坏者）：模型既臆造 `call_tool:ghost_tool`，又在
    tool_mentions 里提到同名工具（未匹配 registry → incomplete）→ 仍必须移出
    allowed_actions（「只引现有 registry」，不因「提及过」放行）；
    契约内已配置工具的同类动作原样保留。"""
    from bandi.skills.distiller import SkillDistiller

    raw = json.loads(_valid_response_payload())
    raw["draft_skill"]["nodes"][1]["allowed_actions"] = [
        "call_tool:create_shipping_order",
        "call_tool:ghost_tool",
    ]
    raw["tool_mentions"] = [{"name": "ghost_tool", "description": "模型臆造的外部接口"}]
    provider = MockProvider()
    provider.add_response(_mock(json.dumps(raw, ensure_ascii=False)))
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    response = await distiller.distill(_request(), model_config=LLMConfig())

    allowed: list[str] = []
    for node in response.draft_skill.nodes:
        allowed.extend(node.allowed_actions)
    assert "call_tool:ghost_tool" not in allowed, "未匹配 registry 的臆造工具不得留在卡上"
    assert "call_tool:create_shipping_order" in allowed
    assert any("ghost_tool" in w for w in response.warnings)


@pytest.mark.asyncio
async def test_repair_receives_previous_raw_output(tmp_skills_dir) -> None:
    """契约（回归）：输出非法 JSON → repair 首轮输入必须携带上一轮
    **原始文本**（previous_output 非空），否则「带上一轮输出修复」承诺落空
    。"""
    from bandi.skills.distiller import SkillDistiller

    bad_output = "这段不是 JSON，模型没按要求输出。"
    provider = _RecordingProvider()
    provider.add_response(_mock(bad_output))
    provider.add_response(_mock(_valid_response_payload()))
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    response = await distiller.distill(_request(), model_config=LLMConfig())

    assert provider.call_count == 3  # L1(1) + repair(1) + 反思(1)
    repair_input = provider.calls[1]["messages"][1]["content"]
    assert bad_output in repair_input, "repair 首轮必须携带上一轮原始输出"
    assert response.draft_skill.skill_id == "sk_ship_order"


@pytest.mark.asyncio
async def test_distill_passes_derived_output_budget_to_provider(tmp_skills_dir) -> None:
    """契约（回归）：distill 必须把 `skill_model_config` 派生的输出预算
    作为 max_tokens 传给 provider（≥ SKILL_MAX_OUTPUT_TOKENS），且 prompt 文案预算与
    实际请求预算一致——杜绝「文案 8192 / 实际 2048」的假接线（provider 默认 2048 场景）。"""
    from bandi.skills.distiller import SkillDistiller
    from bandi.skills.llm_limits import SKILL_MAX_OUTPUT_TOKENS

    provider = _RecordingProvider(LLMConfig(max_output_tokens=2048))
    provider.add_response(_mock(_valid_response_payload()))
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    await distiller.distill(_request(), model_config=LLMConfig(max_output_tokens=2048))

    sent = provider.calls[0]["kwargs"].get("max_tokens")
    assert sent is not None, "provider 必须收到 max_tokens（预算接线）"
    assert sent >= SKILL_MAX_OUTPUT_TOKENS, (
        f"蒸馏预算应抬升至 >= {SKILL_MAX_OUTPUT_TOKENS}，实际 {sent}"
    )
    prompt_text = provider.calls[0]["messages"][1]["content"]
    assert f"{sent} tokens" in prompt_text, "prompt 文案预算必须与实际请求预算一致"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distill_strips_non_whitelisted_actions(tmp_skills_dir) -> None:
    """契约（回归/破坏者）：allowed_actions 出现未定义动作（非四类合法动作
    非 call_tool）→ 移出 allowed_actions + warning 点名；四类合法动作与 registry
    工具动作原样保留——动作词表与蒸馏提示词声明一致。"""
    from bandi.skills.distiller import SkillDistiller

    raw = json.loads(_valid_response_payload())
    raw["draft_skill"]["nodes"][0]["allowed_actions"] = ["ask_user", "ghost_action", "wait_user"]
    provider = MockProvider()
    provider.add_response(_mock(json.dumps(raw, ensure_ascii=False)))
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    response = await distiller.distill(_request(), model_config=LLMConfig())

    allowed: list[str] = []
    for node in response.draft_skill.nodes:
        allowed.extend(node.allowed_actions)
    assert "ghost_action" not in allowed, "未定义动作不得留在卡上"
    assert "wait_user" not in allowed, "未定义动作不得留在卡上"
    assert "ask_user" in allowed, "白名单动作必须保留"
    assert "call_tool:create_shipping_order" in allowed, "registry 工具动作必须保留"
    assert any("ghost_action" in w for w in response.warnings)
    assert any("wait_user" in w for w in response.warnings)


@pytest.mark.asyncio
async def test_distill_llm_calls_route_through_accounted_entry(tmp_skills_dir, monkeypatch) -> None:
    """契约（回归）：蒸馏/反思的全部 LLM 调用必须经 accounted_complete
    统一入口（label=skill_distill），携带 per-call 预算——不得游离于预算闸门/成本台账。
    注：统一入口实现在 `skill_guards.complete_skill_text`（抽公共件后迁移）
    故 patch 目标为该模块符号。"""
    from bandi.skills import skill_guards as guards_mod
    from bandi.skills.distiller import SkillDistiller

    captured: list[dict[str, Any]] = []
    real_accounted = guards_mod.accounted_complete

    async def spy(provider, messages, **kwargs):
        captured.append(dict(kwargs))
        return await real_accounted(provider, messages, **kwargs)

    monkeypatch.setattr(guards_mod, "accounted_complete", spy)

    provider = MockProvider()
    provider.add_response(_mock(_valid_response_payload()))
    distiller = SkillDistiller(provider=provider, skills_dir=tmp_skills_dir)
    response = await distiller.distill(_request(), model_config=LLMConfig())

    assert response.draft_skill.skill_id == "sk_ship_order"
    assert captured, "蒸馏 LLM 调用必须经 accounted_complete 统一入口"
    assert all(kwargs.get("label") == "skill_distill" for kwargs in captured), (
        f"全部调用点必须带统一 label，实际 {[k.get('label') for k in captured]}"
    )
    assert all(kwargs.get("max_tokens") for kwargs in captured), "每次调用必须携带输出预算"
