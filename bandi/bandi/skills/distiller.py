# 功能：SOP 蒸馏器（NL → SkillCard draft）。四级降级链：正常 JSON →
# 最低可运行草稿；normalize 负责闭环规则注入、未配置工具剔除、node_id 唯一化与
# 契约增强（有意超集）：降级链为独立可复用纯函数；terminals 重推满足闭环验证；
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bandi.core.config import LLMConfig
from bandi.core.exceptions import LLMError
from bandi.llm.base import BaseProvider
from bandi.skills.card import SkillCard, SkillGraphNode
from bandi.skills.llm_limits import skill_model_config
from bandi.skills.reflection import reflect_skill_response
from bandi.skills.serializer import SkillCardSaveError, load_card, save_card
from bandi.skills.skill_guards import (
    ToolSuggestion,
    compact_warnings,
    complete_skill_text,
    extract_json,
    normalize_tool_suggestions,
    sanitize_allowed_actions,
    tool_resolution_warnings,
)
from bandi.skills.step_ids import (
    ensure_unique_node_ids,
    skill_card_with_unique_step_ids,
)

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

MODEL_REPAIR_ATTEMPTS = 2
MODEL_TOOL_LIMIT = 48
MODEL_TOOL_CATALOG_CHAR_LIMIT = 12000
MODEL_TOOL_DESCRIPTION_CHAR_LIMIT = 240


def _existing_card_text(skills_dir: str | Path, skill_id: str) -> str:
    """功能：既有技能卡文本（无卡/读失败回落空串，供审计账本 before 值）。"""
    path = Path(skills_dir).expanduser() / f"{skill_id}.card.json"
    if not path.is_file():
        return ""
    try:
        return load_card(path).model_dump_json()
    except Exception:  # noqa: BLE001 - 旧卡损坏不阻断蒸馏落盘
        return ""


MODEL_TOOL_PARAMETER_LIMIT = 12
ALLOWED_NON_TOOL_ACTIONS = frozenset({"ask_user", "answer_user", "continue_flow", "handoff_human"})
CLOSED_LOOP_RESPONSE_RULE = (
    "流程必须形成闭环：不得把“请稍候/正在处理/稍后反馈”作为最终回复；"
    "需要外部事实、外部状态或外部副作用时必须调用已配置工具或转人工，并向用户给出明确结果。"
)
ADAPTIVE_FLOW_RESPONSE_RULE = (
    "步骤是可自适应推进的目标，不是固定问答脚本；已由当前用户消息、历史信息或路由意图满足的内容"
    "不得重复追问，应直接推进到下一缺失信息、工具调用或最终回复。"
)
CONFIRMATION_FLOW_RESPONSE_RULE = (
    "涉及外部系统写入、用户资产变更、不可逆操作或明确需要确认的处理时，"
    "调用工具或执行处理前必须先让用户确认关键对象、范围和操作内容。"
)
TOOL_STEP_INSTRUCTION_SUFFIX = (
    "工具参数满足时直接调用工具；工具成功后必须基于工具结果进入最终回复，"
    "不要停留在“请稍候”或“正在处理”。"
)
ADAPTIVE_STEP_INSTRUCTION_SUFFIX = (
    "将本步骤作为目标而不是固定话术；如果用户当前消息、历史 slots 或路由意图已满足本步骤，"
    "直接写入对应 slot 并继续到下一缺失信息、工具调用或最终回复，不要重复确认。"
)
FINAL_RESPONSE_INSTRUCTION_SUFFIX = "给用户明确最终回复；无法闭环时转人工，不要只说请稍候。"

_DISTILL_PROMPT = """你是企业级 SOP 流程蒸馏引擎，把用户的「原始流程」改写为一套可直接执行的技能图（Skill Card）。

改写要求：
1. 输出唯一 JSON object，不要任何解释、不要代码围栏、不要 markdown。
2. 结构为 {"draft_skill": {…}, "warnings": [], "tool_mentions": []}。
3. draft_skill 必须包含：skill_id（英文小写+下划线，建议以 sk_ 开头）、name、
   business_domain、description、trigger_intents、user_utterance_examples、goal、
   required_info、slot_filling_policy、response_rules、nodes、edges、start_node_id、
   terminal_node_ids、interruption_policy。
4. nodes 每项：node_id（英文唯一）、type（collect_info/action/decision/response 之一）、
   name（中文）、instruction（中文执行要点）、optional、condition、expected_user_info、
   allowed_actions（只允许 ask_user/answer_user/continue_flow/handoff_human 以及
   call_tool:<可用工具名>）。
5. edges 每项：source_node_id、next_node_id、condition、priority、label。
6. 流程终态必须是明确的最终回复，不能停在"请稍候"。需要外部接口但工具不全时，
   在 tool_mentions 列出，由系统裁决（不臆造工具）。
7. 保留原始流程中的所有业务规则与分支条件，逐字引用原文关键操作，不要丢失信息。
"""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class SkillDistillRequest(BaseModel):
    """功能：蒸馏请求入参（NL → SOP 卡触发源）。"""

    model_config = ConfigDict(extra="forbid")

    title: str = ""  # 功能：技能标题（中文名）
    raw_content: str = ""  # 功能：原始流程文本（NL 源）
    business_domain: str | None = None  # 功能：业务领域
    available_tools: list[dict[str, Any]] = Field(default_factory=list)  # 功能：现有 Tool registry


class SkillDistillResponse(BaseModel):
    """功能：蒸馏响应（草稿 SkillCard + 过程 warning + 工具建议）。"""

    model_config = ConfigDict(extra="forbid")

    draft_skill: SkillCard  # 功能：草稿态（未 publish）技能卡
    warnings: list[str] = Field(default_factory=list)  # 功能：修复/降级过程说明
    tool_suggestions: list[ToolSuggestion] = Field(default_factory=list)  # 功能：工具匹配建议


# ---------------------------------------------------------------------------
# 模块级降级链（为独立可复用纯函数；= _complete_text +
# _response_from_text 组合，distill 仅做编排 + 保留上一轮原文供 repair 使用）
# ---------------------------------------------------------------------------


async def _repair_chain(
    provider: BaseProvider,
    prompt: str,
    payload: dict[str, Any],
    previous_output: str,
    previous_error: str,
    request: SkillDistillRequest,
    *,
    max_tokens: int,
    session_id: str | None = None,
) -> SkillDistillResponse:
    """功能：repair——带上一轮输出与错误上下文修复重试（最多 MODEL_REPAIR_ATTEMPTS）。"""
    output = previous_output
    error = previous_error
    for attempt in range(MODEL_REPAIR_ATTEMPTS):
        repair_payload = {
            **payload,
            "previous_output": output,
            "previous_error": error,
            "repair_attempt": attempt + 1,
            "repair_instruction": (
                "上一次输出无法解析或未通过 Skill Card graph 校验。请修复为完整合法 JSON。"
                "不要解释，不要使用代码围栏。必须保留原始流程中的节点、边、工具建议和闭环约束。"
            ),
        }
        output = await _complete_text(
            provider,
            prompt,
            _input_from_payload(repair_payload),
            max_tokens=max_tokens,
            session_id=session_id,
        )
        try:
            return _response_from_text(output, request)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            error = str(exc)
    raise ValueError(error)


async def _staged_chain(
    provider: BaseProvider,
    prompt: str,
    payload: dict[str, Any],
    request: SkillDistillRequest,
    previous_error: str,
    *,
    max_tokens: int,
    session_id: str | None = None,
) -> SkillDistillResponse:
    """功能：staged——outline → 逐节点扩写 → 终审 分段生成完整卡。"""
    outline_text = await _complete_text(
        provider,
        prompt,
        _input_from_payload(
            {
                **payload,
                "generation_mode": "outline_only",
                "previous_error": previous_error,
                "generation_instruction": (
                    "先生成完整但紧凑的 Skill Card graph 大纲。nodes/edges 必须覆盖原始流程全部节点与条件推进关系，"
                    "每个 instruction 只写一句目标说明；保留 response_rules、slot_filling_policy、"
                    "interruption_policy 和 tool_mentions。只输出 JSON。"
                ),
            }
        ),
        max_tokens=max_tokens,
        session_id=session_id,
    )
    outline = _response_from_text(outline_text, request)
    draft_data = outline.draft_skill.model_dump(mode="json")
    warnings = list(outline.warnings)
    tool_mentions = [item.model_dump(mode="json") for item in outline.tool_suggestions]
    nodes = [node for node in draft_data.get("nodes", []) if isinstance(node, dict)]

    for index, node in enumerate(nodes):
        node_text = await _complete_text(
            provider,
            prompt,
            _input_from_payload(
                {
                    **payload,
                    "generation_mode": "expand_node",
                    "current_draft": draft_data,
                    "target_node_index": index,
                    "target_node": node,
                    "generation_instruction": (
                        '只扩写 target_node。输出 JSON：{"node": {...}, "warnings": [], '
                        '"tool_mentions": []}。node 必须包含 node_id、type、name、instruction、'
                        "expected_user_info、allowed_actions。不要输出完整技能。"
                    ),
                }
            ),
            max_tokens=max_tokens,
            session_id=session_id,
        )
        try:
            node_raw = _raw_json_from_text(node_text)
            node_data = node_raw.get("node") if isinstance(node_raw.get("node"), dict) else node_raw
            nodes[index] = SkillGraphNode.model_validate(node_data).model_dump(mode="json")
            warnings.extend(str(item) for item in node_raw.get("warnings", []) if str(item).strip())
            if isinstance(node_raw.get("tool_mentions"), list):
                tool_mentions.extend(
                    item for item in node_raw["tool_mentions"] if isinstance(item, dict)
                )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            warnings.append(f"模型未能扩写节点 {index + 1}，已保留大纲节点：{exc}")

    draft_data["nodes"] = nodes
    reviewed = _normalize_response(
        {"draft_skill": draft_data, "warnings": warnings, "tool_mentions": tool_mentions},
        request,
    )
    review_text = await _complete_text(
        provider,
        prompt,
        _input_from_payload(
            {
                **payload,
                "generation_mode": "final_review",
                "current_draft": reviewed.draft_skill.model_dump(mode="json"),
                "generation_instruction": (
                    "检查 current_draft 是否遗漏原始流程、闭环回复、工具建议或中断策略。"
                    "如需修正，返回完整 draft_skill；如果无需修正，也返回完整 draft_skill。只输出 JSON。"
                ),
            }
        ),
        max_tokens=max_tokens,
        session_id=session_id,
    )
    try:
        return _response_from_text(review_text, request)
    except (json.JSONDecodeError, TypeError, ValueError):
        return reviewed


def _fallback_response(request: SkillDistillRequest, warning: str) -> SkillDistillResponse:
    """功能：fallback——最低可运行草稿（不抛异常不中断）。"""
    return SkillDistillResponse(
        draft_skill=_fallback_card(request), warnings=compact_warnings([warning])
    )


# ---------------------------------------------------------------------------
# SkillDistiller 主类
# ---------------------------------------------------------------------------


class SkillDistiller:
    """功能：SOP 蒸馏器（NL → SkillCard draft + 落盘）。"""

    def __init__(self, provider: BaseProvider, skills_dir: str = "~/.bandi/skills") -> None:
        self.provider = provider
        self.skills_dir = skills_dir

    async def distill(
        self,
        request: SkillDistillRequest,
        model_config: LLMConfig | None = None,
        *,
        session_id: str | None = None,
    ) -> SkillDistillResponse:
        """功能：蒸馏主链路——四级降级生成→ 反思校验（落盘前质量门）
        → 产物落盘 draft 卡。session_id 透传全链 LLM 调用点（成本按操作归集）。"""
        base_config = model_config or getattr(self.provider, "config", None) or LLMConfig()
        output_budget = skill_model_config(base_config).max_output_tokens
        prompt = _DISTILL_PROMPT
        payload = _payload(request)
        model_input = _distill_model_input(
            title=request.title,
            business_domain=request.business_domain,
            raw_content=request.raw_content,
            available_tools=payload.get("available_tools", []),
            total_tool_count=len(request.available_tools),
            output_budget=output_budget,
        )
        output = ""
        try:
            output = await _complete_text(
                self.provider,
                prompt,
                model_input,
                max_tokens=output_budget,
                session_id=session_id,
            )
            response = _response_from_text(output, request)
        except (LLMError, json.JSONDecodeError, TypeError, ValueError) as exc:
            try:
                response = await _repair_chain(
                    self.provider,
                    prompt,
                    payload,
                    output,
                    str(exc),
                    request,
                    max_tokens=output_budget,
                    session_id=session_id,
                )
            except (LLMError, json.JSONDecodeError, TypeError, ValueError) as repair_exc:
                try:
                    response = await _staged_chain(
                        self.provider,
                        prompt,
                        payload,
                        request,
                        str(repair_exc),
                        max_tokens=output_budget,
                        session_id=session_id,
                    )
                except (LLMError, json.JSONDecodeError, TypeError, ValueError) as staged_exc:
                    response = _fallback_response(
                        request, f"模型多轮生成未能完成，已使用最低可运行草稿：{staged_exc}"
                    )
        response = await reflect_skill_response(
            complete=lambda system_prompt, review_input: _complete_text(
                self.provider,
                system_prompt,
                review_input,
                max_tokens=output_budget,
                session_id=session_id,
            ),
            normalize=lambda raw: _normalize_response(raw, request),
            source_payload=payload,
            response=response,
            source_kind="distill",
        )
        if response.draft_skill.skill_id:
            before_text = _existing_card_text(self.skills_dir, response.draft_skill.skill_id)
            try:
                save_card(response.draft_skill, self.skills_dir)
            except SkillCardSaveError as exc:
                response.warnings.append(f"技能卡已生成但落盘失败（不影响本次返回）：{exc}")
            else:
                from bandi.skills.audit_ledger import record_skill_change

                record_skill_change(
                    action="distill",
                    skill_id=response.draft_skill.skill_id,
                    before=before_text,
                    after=response.draft_skill.model_dump_json(),
                    source="distiller",
                )
        return response


# ---------------------------------------------------------------------------
# provider 文本补全
# ---------------------------------------------------------------------------


async def _complete_text(
    provider: BaseProvider,
    prompt: str,
    model_input: str,
    *,
    max_tokens: int,
    session_id: str | None = None,
) -> str:
    """功能：蒸馏/反思全链 LLM 补全——委托 skill_guards.complete_skill_text
    （统一计费入口 label=skill_distill + 输出预算透传 + session_id 成本归集透传）。"""
    return await complete_skill_text(
        provider,
        prompt,
        model_input,
        max_tokens=max_tokens,
        label="skill_distill",
        session_id=session_id,
    )


def _input_from_payload(payload: dict[str, Any]) -> str:
    """功能：把 repair/staged 的阶段 payload 渲染为模型输入文本。"""
    sections: list[str] = []
    for key in (
        "title",
        "business_domain",
        "raw_content",
        "available_tools",
        "previous_output",
        "previous_error",
        "repair_attempt",
        "repair_instruction",
        "generation_mode",
        "generation_instruction",
        "current_draft",
        "target_node_index",
        "target_node",
    ):
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            try:
                sections.append(f"{key}：{json.dumps(value, ensure_ascii=False)}")
            except (TypeError, ValueError):
                sections.append(f"{key}：{value}")
        else:
            sections.append(f"{key}：{value}")
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# 模型输出解析与归一化
# ---------------------------------------------------------------------------


def _response_from_text(text: str, request: SkillDistillRequest) -> SkillDistillResponse:
    """功能：解析模型文本为响应对象（非法 JSON 冒泡，由降级链接管）。"""
    raw = _raw_json_from_text(text)
    return _normalize_response(raw, request)


def _normalize_response(raw: dict[str, Any], request: SkillDistillRequest) -> SkillDistillResponse:
    """功能：模型原始字典 → 合法 SkillCard 响应（闭环规则 + 工具剔除 + 唯一化 + 终结重推）。"""
    draft = raw.get("draft_skill") if isinstance(raw.get("draft_skill"), dict) else raw
    warnings: list[str] = list(raw.get("warnings") or [])
    fallback = _fallback_card(request)

    required_info = _string_list(draft.get("required_info"), fallback.required_info)
    nodes = _normalize_nodes(draft.get("nodes"), fallback.nodes)
    nodes, node_warnings = _ensure_closed_loop_nodes(nodes, request)
    warnings.extend(node_warnings)
    nodes, unique_node_warnings = ensure_unique_node_ids(nodes)
    warnings.extend(unique_node_warnings)
    edges = _normalize_edges(draft.get("edges"), nodes, fallback.edges)
    edges = _ensure_linear_reachability(nodes, edges)
    node_id_set = {str(node.get("node_id") or "") for node in nodes}
    start_node_id = _string(draft.get("start_node_id"), fallback.start_node_id)
    if start_node_id not in node_id_set:
        start_node_id = nodes[0]["node_id"]
    model_terminals = [
        nid
        for nid in _string_list(draft.get("terminal_node_ids"), fallback.terminal_node_ids)
        if nid in node_id_set
    ]
    terminal_node_ids = _closed_loop_terminals(nodes, edges, model_terminals)
    raw_tool_mentions = (
        raw.get("tool_mentions")
        if isinstance(raw.get("tool_mentions"), list)
        else raw.get("tool_suggestions")
    )

    nodes, missing_tool_names, unknown_actions = sanitize_allowed_actions(
        nodes, request.available_tools
    )
    for tool_name in missing_tool_names:
        warnings.append(
            f"技能草稿引用了未配置工具 {tool_name}，已移出 allowed_actions；"
            "如确需该工具，模型必须在 tool_mentions 中提供来自原文的完整工具提及。"
        )
    for action_name in unknown_actions:
        warnings.append(
            f"技能草稿包含未定义动作 {action_name}，已移出 allowed_actions；"
            "合法动作仅 ask_user/answer_user/continue_flow/handoff_human 与 call_tool:<工具名>。"
        )
    response_rules = _string_list(draft.get("response_rules"), fallback.response_rules)
    if CLOSED_LOOP_RESPONSE_RULE not in response_rules:
        response_rules.append(CLOSED_LOOP_RESPONSE_RULE)
    if ADAPTIVE_FLOW_RESPONSE_RULE not in response_rules:
        response_rules.append(ADAPTIVE_FLOW_RESPONSE_RULE)
    if _steps_declare_confirmation(nodes) and CONFIRMATION_FLOW_RESPONSE_RULE not in response_rules:
        response_rules.append(CONFIRMATION_FLOW_RESPONSE_RULE)
    normalized = {
        "skill_id": _string(draft.get("skill_id"), fallback.skill_id),
        "name": _string(draft.get("name"), fallback.name),
        "version": _string(draft.get("version"), "1.0.0"),
        "business_domain": _string(
            draft.get("business_domain"), fallback.business_domain or "general"
        ),
        "description": _string(draft.get("description"), fallback.description),
        "trigger_intents": _string_list(draft.get("trigger_intents"), fallback.trigger_intents),
        "user_utterance_examples": _string_list(
            draft.get("user_utterance_examples"), fallback.user_utterance_examples
        ),
        "goal": _string_list(draft.get("goal"), fallback.goal),
        "required_info": required_info,
        "slot_filling_policy": _slot_filling_policy(
            draft.get("slot_filling_policy"),
            required_info,
            nodes,
            fallback.slot_filling_policy,
        ),
        "response_rules": response_rules,
        "nodes": nodes,
        "edges": edges,
        "start_node_id": start_node_id,
        "terminal_node_ids": terminal_node_ids,
        "interruption_policy": _string_dict(
            draft.get("interruption_policy"), fallback.interruption_policy
        ),
    }
    draft_skill, card_warnings = skill_card_with_unique_step_ids(normalized)
    warnings.extend(card_warnings)
    tool_suggestions = normalize_tool_suggestions(raw_tool_mentions, request.available_tools)
    warnings.extend(tool_resolution_warnings(tool_suggestions))
    return SkillDistillResponse(
        draft_skill=draft_skill,
        warnings=compact_warnings(warnings),
        tool_suggestions=tool_suggestions,
    )


def _ensure_closed_loop_nodes(
    nodes: list[dict[str, Any]], request: SkillDistillRequest
) -> tuple[list[dict[str, Any]], list[str]]:
    """功能：自适应步骤指令注入 + 工具步骤闭环注入 + 末节点非 answer 时补回复节点。"""
    normalized_nodes = [dict(node) for node in nodes]
    warnings: list[str] = []
    _attach_declared_confirmation_to_tool_steps(normalized_nodes)

    for node in normalized_nodes:
        _ensure_adaptive_step_instruction(node)
        actions = [str(action) for action in node.get("allowed_actions", [])]
        if not any(action.startswith("call_tool:") for action in actions):
            continue
        if "continue_flow" not in actions:
            actions.append("continue_flow")
            node["allowed_actions"] = actions
        _append_instruction_suffix(node, TOOL_STEP_INSTRUCTION_SUFFIX)

    if not _last_step_allows_answer(normalized_nodes):
        normalized_nodes.append(
            {
                "node_id": _unique_step_id(normalized_nodes, "reply_final_result"),
                "type": "response",
                "name": "反馈最终结果",
                "instruction": (
                    "基于已收集信息和工具结果给用户明确最终回复；"
                    "信息不足时追问缺失信息，无法闭环时转人工，不要只说请稍候；"
                    f"{ADAPTIVE_STEP_INSTRUCTION_SUFFIX}"
                ),
                "expected_user_info": [],
                "allowed_actions": ["answer_user", "handoff_human"],
            }
        )
        warnings.append("原始改写缺少最终回复节点，已补充闭环反馈节点。")
    else:
        last_step = normalized_nodes[-1]
        _append_instruction_suffix(last_step, FINAL_RESPONSE_INSTRUCTION_SUFFIX)

    return normalized_nodes, warnings


def _closed_loop_terminals(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    model_terminals: list[str],
) -> list[str]:
    """功能：终结节点重推——模型 given 终结集若不能保证「非终结节点可达 terminal」
    （Bandi 闭环超集），回退到链末端节点，保证图校验通过。"""
    if not nodes:
        return []
    node_ids = [str(node.get("node_id") or "") for node in nodes]
    if not model_terminals:
        return [node_ids[-1]]
    terminal_set = {nid for nid in model_terminals if nid in set(node_ids)}
    if not terminal_set:
        return [node_ids[-1]]
    incoming: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for edge in edges:
        source = str(edge.get("source_node_id") or "")
        target = str(edge.get("next_node_id") or "")
        if source in incoming and target in incoming:
            incoming[target].append(source)
    no_exit = [
        nid
        for nid in node_ids
        if nid not in terminal_set and nid not in _reachable_from(terminal_set, incoming)
    ]
    if no_exit:
        return [node_ids[-1]]
    return sorted(model_terminals)


def _reachable_from(starts: set[str], adjacency: dict[str, list[str]]) -> set[str]:
    """功能：BFS 从 starts 沿邻接表反向可达节点集（含 starts）。"""
    seen = set(starts)
    queue = list(starts)
    while queue:
        current = queue.pop()
        for target in adjacency.get(current, []):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def _normalize_nodes(value: Any, fallback_nodes: list[SkillGraphNode]) -> list[dict[str, Any]]:
    """功能：模型节点列表 → 规范化 dict 列表（逐字段兜底到 fallback 卡）。"""
    if not isinstance(value, list):
        return [node.model_dump() for node in fallback_nodes]
    nodes: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        fallback = fallback_nodes[min(index, len(fallback_nodes) - 1)]
        nodes.append(
            {
                "node_id": _string(item.get("node_id"), fallback.node_id),
                "type": _string(item.get("type"), fallback.type),
                "name": _string(item.get("name"), fallback.name),
                "instruction": _string(item.get("instruction"), fallback.instruction),
                "optional": bool(item.get("optional", fallback.optional)),
                "condition": item.get("condition")
                if isinstance(item.get("condition"), str)
                else fallback.condition,
                "expected_user_info": _string_list(
                    item.get("expected_user_info"), fallback.expected_user_info
                ),
                "allowed_actions": _normalize_actions(
                    _string_list(item.get("allowed_actions"), fallback.allowed_actions)
                ),
                "knowledge_scope": (
                    item.get("knowledge_scope")
                    if isinstance(item.get("knowledge_scope"), dict)
                    else fallback.knowledge_scope
                ),
                "retry_policy": (
                    item.get("retry_policy")
                    if isinstance(item.get("retry_policy"), dict)
                    else fallback.retry_policy
                ),
                "metadata": item.get("metadata")
                if isinstance(item.get("metadata"), dict)
                else fallback.metadata,
            }
        )
    return nodes or [node.model_dump() for node in fallback_nodes]


def _normalize_edges(
    value: Any, nodes: list[dict[str, Any]], fallback_edges: list[Any]
) -> list[dict[str, Any]]:
    """功能：模型边列表 → 规范化边（引用缺失节点即丢弃，空则线性回退）。"""
    node_ids = {str(node.get("node_id") or "") for node in nodes}
    edges: list[dict[str, Any]] = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                continue
            source = _string(item.get("source_node_id"), "")
            target = _string(item.get("next_node_id"), "")
            if source not in node_ids or target not in node_ids:
                continue
            edges.append(
                {
                    "source_node_id": source,
                    "next_node_id": target,
                    "condition": item.get("condition")
                    if isinstance(item.get("condition"), str)
                    else None,
                    "priority": _to_int(item.get("priority"), index),  # 空/非数字回退 index
                    "label": item.get("label") if isinstance(item.get("label"), str) else None,
                }
            )
    if edges:
        return edges
    if fallback_edges:
        fallback: list[dict[str, Any]] = []
        for edge in fallback_edges:
            item = edge.model_dump(mode="json") if hasattr(edge, "model_dump") else dict(edge)
            if item.get("source_node_id") in node_ids and item.get("next_node_id") in node_ids:
                fallback.append(item)
        if fallback:
            return fallback
    return [
        {
            "source_node_id": nodes[index]["node_id"],
            "next_node_id": nodes[index + 1]["node_id"],
            "priority": index,
            "label": "默认推进",
        }
        for index in range(len(nodes) - 1)
    ]


def _ensure_linear_reachability(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """功能：按节点顺序为缺前驱的节点补最小线性边（保证链条可达）。"""
    if len(nodes) < 2:
        return edges
    existing = {
        (str(edge.get("source_node_id") or ""), str(edge.get("next_node_id") or ""))
        for edge in edges
        if isinstance(edge, dict)
    }
    next_edges = [dict(edge) for edge in edges]
    incoming = {str(edge.get("next_node_id") or "") for edge in next_edges}
    for index in range(1, len(nodes)):
        target = str(nodes[index].get("node_id") or "")
        source = str(nodes[index - 1].get("node_id") or "")
        if not target or not source or target in incoming:
            continue
        pair = (source, target)
        if pair in existing:
            continue
        next_edges.append(
            {
                "source_node_id": source,
                "next_node_id": target,
                "priority": index,
                "label": "默认推进",
            }
        )
        existing.add(pair)
        incoming.add(target)
    return next_edges


# ---------------------------------------------------------------------------
# fallback 草稿卡
# ---------------------------------------------------------------------------


def _fallback_card(request: SkillDistillRequest) -> SkillCard:
    """功能：最低可运行草稿卡（双节点线性闭环，适配任意 NL 输入）。"""
    title = request.title.strip() or "新技能"
    raw = request.raw_content
    required_info: list[str] = []
    nodes = [
        SkillGraphNode(
            node_id="understand_request",
            type="decision",
            name="理解原始流程",
            instruction=(
                "根据原始流程文档理解用户目标、缺失信息和下一步处理方式；"
                "不要基于固定话术推进，信息不足时追问，涉及外部事实或外部副作用时转人工或等待人工补充工具配置；"
                f"{ADAPTIVE_STEP_INSTRUCTION_SUFFIX}"
            ),
            expected_user_info=[],
            allowed_actions=["ask_user", "continue_flow", "handoff_human"],
        ),
        SkillGraphNode(
            node_id="reply_result",
            type="response",
            name="反馈结果",
            instruction=(
                "根据已收集的信息和工具结果给用户明确回复；信息不足时继续追问，不要编造事实；"
                f"{ADAPTIVE_STEP_INSTRUCTION_SUFFIX}"
            ),
            expected_user_info=[],
            allowed_actions=["answer_user", "handoff_human"],
        ),
    ]
    return SkillCard(
        skill_id=_slugify(title, raw),
        name=title,
        version="1.0.0",
        business_domain=request.business_domain or "general",
        description=raw[:120] or "根据原始技能文本生成的流程。",
        trigger_intents=[title],
        user_utterance_examples=[title],
        goal=_infer_goals(raw),
        required_info=required_info,
        slot_filling_policy=_default_slot_filling_policy(required_info),
        response_rules=[
            "信息不足时先追问，不要编造事实。",
            ADAPTIVE_FLOW_RESPONSE_RULE,
        ],
        nodes=nodes,
        edges=[
            {
                "source_node_id": "understand_request",
                "next_node_id": "reply_result",
                "priority": 0,
                "label": "默认推进",
            }
        ],
        start_node_id="understand_request",
        terminal_node_ids=["reply_result"],
        interruption_policy={
            "related_question": "回答相关问题后回到当前流程。",
            "unrelated_business": "可切换新流程并保留当前进度。",
            "chitchat": "简短回应后引导用户继续当前流程。",
            "user_wants_human": "直接转人工。",
        },
    )


# ---------------------------------------------------------------------------
# payload / model_input
# ---------------------------------------------------------------------------


def _payload(request: SkillDistillRequest) -> dict[str, Any]:
    """功能：蒸馏请求 payload（模型输入上下文）。"""
    return {
        "title": request.title,
        "business_domain": request.business_domain,
        "raw_content": request.raw_content,
        "available_tools": _compact_available_tools(
            request.available_tools,
            source_text=_request_text(request),
        ),
    }


def _request_text(request: SkillDistillRequest) -> str:
    """功能：source 文本拼接（工具相关性排序输入）。"""
    title = request.title.strip()
    raw = request.raw_content
    return f"{title}\n{raw}"


def _distill_model_input(
    *,
    title: str,
    business_domain: str | None,
    raw_content: str,
    available_tools: Any,
    total_tool_count: int,
    output_budget: int,
) -> str:
    """功能：模型输入渲染（工具目录按相关性压缩 + 输出预算提示）。"""
    sections = [
        f"技能标题：{title.strip() or '新SOP'}",
        f"输出预算：最终 JSON 请控制在 {output_budget} tokens 以内。",
    ]
    if business_domain and business_domain.strip():
        sections.append(f"业务领域：{business_domain.strip()}")
    sections.extend(("原始流程：", raw_content.strip()))

    tools = (
        [item for item in available_tools if isinstance(item, dict)]
        if isinstance(available_tools, list)
        else []
    )
    sections.append("可用工具（只选择与原始流程语义匹配的工具）：")
    if not tools:
        sections.append("无可用工具。流程需要外部接口时，请指出缺少的接口，不要臆造工具。")
        return "\n".join(sections)

    for tool in tools:
        name = str(tool.get("name") or "").strip()
        display_name = str(tool.get("display_name") or "").strip()
        description = str(tool.get("description") or "").strip()
        heading = name
        if display_name and display_name != name:
            heading = f"{name}（{display_name}）"
        line = f"- {heading}"
        if description:
            line += f"：{description}"
        sections.append(line)
        parameter_text = _model_tool_parameter_text(tool.get("input_schema"))
        if parameter_text:
            sections.append(f"  输入参数：{parameter_text}")

    omitted = max(0, total_tool_count - len(tools))
    if omitted:
        sections.append(
            f"另有 {omitted} 个与当前流程相关性较低的工具未展开；不得猜测或调用未列出的工具。"
        )
    return "\n".join(sections)


def _compact_available_tools(
    available_tools: list[dict[str, Any]],
    *,
    source_text: str,
) -> list[dict[str, Any]]:
    """功能：按源文本相关性压缩工具目录（数量 + 字符双上限）。"""
    source_terms = _tool_relevance_terms(source_text)
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    seen_names: set[str] = set()
    for index, tool in enumerate(available_tools):
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        description = _limited_text(tool.get("description"), MODEL_TOOL_DESCRIPTION_CHAR_LIMIT)
        projected: dict[str, Any] = {
            "name": name,
            "display_name": _limited_text(tool.get("display_name"), 120),
            "description": description,
            "input_schema": _compact_tool_input_schema(tool.get("input_schema")),
        }
        projected = {
            key: value for key, value in projected.items() if value not in (None, "", [], {})
        }
        candidate_text = " ".join(
            str(tool.get(key) or "") for key in ("name", "display_name", "description", "bucket")
        )
        score = len(source_terms & _tool_relevance_terms(candidate_text))
        lowered_source = source_text.lower()
        for exact in (name, str(tool.get("display_name") or "").strip()):
            if exact and exact.lower() in lowered_source:
                score += 20
        ranked.append((score, index, projected))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    compacted: list[dict[str, Any]] = []
    catalog_chars = 0
    for _score, _index, projected in ranked:
        if len(compacted) >= MODEL_TOOL_LIMIT:
            break
        projected_chars = len(json.dumps(projected, ensure_ascii=False, separators=(",", ":")))
        if compacted and catalog_chars + projected_chars > MODEL_TOOL_CATALOG_CHAR_LIMIT:
            break
        compacted.append(projected)
        catalog_chars += projected_chars
    return compacted


def _compact_tool_input_schema(value: Any) -> dict[str, Any]:
    """功能：工具输入 schema 压缩（最多前 12 个参数）。"""
    if not isinstance(value, dict):
        return {}
    properties = value.get("properties") if isinstance(value.get("properties"), dict) else {}
    required = [str(item) for item in value.get("required", []) if str(item).strip()]
    ordered_names = [*required, *(str(name) for name in properties if str(name) not in required)]
    selected_names = ordered_names[:MODEL_TOOL_PARAMETER_LIMIT]
    compact_properties: dict[str, Any] = {}
    for name in selected_names:
        raw_property = properties.get(name)
        if not isinstance(raw_property, dict):
            raw_property = {}
        item: dict[str, Any] = {
            "type": _limited_text(raw_property.get("type"), 32),
            "description": _limited_text(raw_property.get("description"), 100),
        }
        enum = raw_property.get("enum")
        if isinstance(enum, list) and enum:
            item["enum"] = enum[:8]
        compact_properties[name] = {
            key: item_value
            for key, item_value in item.items()
            if item_value not in (None, "", [], {})
        }
    result: dict[str, Any] = {"type": "object"}
    if compact_properties:
        result["properties"] = compact_properties
    selected_required = [name for name in required if name in selected_names]
    if selected_required:
        result["required"] = selected_required
    return result


def _model_tool_parameter_text(value: Any) -> str:
    """功能：工具参数渲染文本（模型输入展示）。"""
    if not isinstance(value, dict):
        return ""
    properties = value.get("properties") if isinstance(value.get("properties"), dict) else {}
    required = {str(item) for item in value.get("required", [])}
    parts: list[str] = []
    for name, raw_property in properties.items():
        property_data = raw_property if isinstance(raw_property, dict) else {}
        kind = str(property_data.get("type") or "any")
        marker = "必填" if name in required else "可选"
        description = str(property_data.get("description") or "").strip()
        part = f"{name} ({kind}, {marker})"
        if description:
            part += f" - {description}"
        parts.append(part)
    return "；".join(parts)


def _tool_relevance_terms(value: str) -> set[str]:
    """功能：相关性关键词提取（英文词 + 中文 2/3 元组）。"""
    lowered = value.lower()
    terms = {item for item in re.findall(r"[a-z0-9_]+", lowered) if len(item) >= 2}
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", value):
        for size in (2, 3):
            terms.update(segment[index : index + size] for index in range(len(segment) - size + 1))
    return terms


def _limited_text(value: Any, limit: int) -> str:
    """功能：文本归一化截断（压缩空白）。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


# ---------------------------------------------------------------------------
# 通用文本/结构工具
# ---------------------------------------------------------------------------


def _raw_json_from_text(text: str) -> dict[str, Any]:
    """功能：文本 → JSON object（失败抛 JSONDecodeError 由降级链接管）。"""
    raw = json.loads(extract_json(text))
    if not isinstance(raw, dict):
        raise ValueError("模型输出不是 JSON object")
    return raw


def _to_int(value: Any, fallback: int) -> int:
    """功能：整数字段防御解析（0/空/非数字回退 fallback）。"""
    if not value:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _string(value: Any, fallback: str | None = "") -> str:
    """功能：字符串字段兜底（空白视为无效）。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback or ""


def _string_list(value: Any, fallback: list[str]) -> list[str]:
    """功能：字符串列表兜底。"""
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        if items:
            return items
    return fallback


def _string_dict(value: Any, fallback: dict[str, str]) -> dict[str, str]:
    """功能：字符串字典兜底。"""
    if isinstance(value, dict):
        items = {str(key): str(item) for key, item in value.items() if str(key)}
        if items:
            return items
    return fallback


def _normalize_actions(actions: list[str]) -> list[str]:
    """功能：动作去重保序。"""
    normalized: list[str] = []
    for action in actions:
        if action not in normalized:
            normalized.append(action)
    return normalized


def _slot_filling_policy(
    value: Any,
    required_info: list[str],
    steps: list[dict[str, Any]],
    fallback_policy: dict[str, Any],
) -> dict[str, Any]:
    """功能：槽位填充策略合并（保证 multi_slot 抽取与目标字段完整性）。"""
    has_explicit_policy = isinstance(value, dict)
    policy = dict(value) if has_explicit_policy else dict(fallback_policy or {})
    expected_infos = set(required_info)
    for step in steps:
        expected_infos.update(str(field) for field in step.get("expected_user_info", []))
    if has_explicit_policy and isinstance(policy.get("target_info"), list):
        expected_infos.update(str(field) for field in policy["target_info"] if str(field).strip())
    default_policy = _default_slot_filling_policy(sorted(expected_infos))
    return {
        **default_policy,
        **policy,
        "enabled": True,
        "multi_slot_per_turn": True,
        "extract_scope": "all_skill_expected_user_info",
        "skip_satisfied_steps": True,
        "target_info": sorted(expected_infos),
    }


def _default_slot_filling_policy(expected_infos: list[str]) -> dict[str, Any]:
    """功能：默认槽位填充策略。"""
    return {
        "enabled": True,
        "multi_slot_per_turn": True,
        "extract_scope": "all_skill_expected_user_info",
        "skip_satisfied_steps": True,
        "description": (
            "每轮用户消息都应同时抽取所有可识别的信息；如果用户一次提供多个字段，"
            "必须一次性写入 slot_updates，不要按步骤重复追问。"
        ),
        "target_info": expected_infos,
    }


def _ensure_adaptive_step_instruction(step: dict[str, Any]) -> None:
    """功能：为步骤注入自适应推进指令后缀。"""
    _append_instruction_suffix(step, ADAPTIVE_STEP_INSTRUCTION_SUFFIX)


def _append_instruction_suffix(step: dict[str, Any], suffix: str) -> None:
    """功能：指令后缀注入（幂等，重复则不追加）。"""
    instruction = str(step.get("instruction") or "")
    if suffix in instruction:
        return
    step["instruction"] = f"{instruction}{suffix}"


def _confirmation_fields(steps: list[dict[str, Any]]) -> list[str]:
    """功能：收集 *_confirmed 后缀字段（确认型槽位）。"""
    fields: list[str] = []
    for step in steps:
        expected = [str(field) for field in step.get("expected_user_info", [])]
        for field in expected:
            if field.endswith("_confirmed") and field not in fields:
                fields.append(field)
    return fields


def _steps_declare_confirmation(steps: list[dict[str, Any]]) -> bool:
    """功能：流程是否声明了确认型槽位。"""
    return bool(_confirmation_fields(steps))


def _attach_declared_confirmation_to_tool_steps(steps: list[dict[str, Any]]) -> None:
    """功能：把已声明的确认字段注入到工具步骤的调用前确认指令。"""
    confirmed_fields: list[str] = []
    for step in steps:
        if any(str(action).startswith("call_tool:") for action in step.get("allowed_actions", [])):
            _append_tool_confirmation_instruction(step, confirmed_fields)
        for field in _confirmation_fields([step]):
            if field not in confirmed_fields:
                confirmed_fields.append(field)


def _append_tool_confirmation_instruction(
    step: dict[str, Any], confirmation_fields: list[str]
) -> None:
    """功能：工具调用前确认指令注入。"""
    if not confirmation_fields:
        return
    field_text = "、".join(f"{field}=true" for field in confirmation_fields)
    _append_instruction_suffix(step, f"调用工具前必须确认字段已满足：{field_text}。")


def _last_step_allows_answer(steps: list[dict[str, Any]]) -> bool:
    """功能：末节点是否允许 answer_user（用于闭环回复节点补充判断）。"""
    if not steps:
        return False
    actions = [str(action) for action in steps[-1].get("allowed_actions", [])]
    return "answer_user" in actions


def _unique_step_id(steps: list[dict[str, Any]], base: str) -> str:
    """功能：node_id 冲突时的递增后缀生成。"""
    existing = {str(step.get("node_id") or "") for step in steps}
    if base not in existing:
        return base
    index = 2
    while f"{base}_{index}" in existing:
        index += 1
    return f"{base}_{index}"


def _infer_goals(raw: str) -> list[str]:
    """功能：从原始流程文本推断目标（按标点切分，空则默认）。"""
    clauses = [clause.strip() for clause in _split_clauses(raw) if clause.strip()]
    return clauses or ["理解用户诉求", "收集必要信息", "完成流程处理", "向用户反馈结果"]


def _split_clauses(text: str) -> list[str]:
    """功能：中文标点切分子句。"""
    normalized = (
        text.replace("\n", "，")
        .replace("；", "，")
        .replace(";", "，")
        .replace(",", "，")
        .replace("。", "，")
    )
    return [part.strip() for part in normalized.split("，")]


def _slugify(title: str, raw: str) -> str:
    """功能：标题 → ASCII slug（非 ASCII 则回退 hash 前缀）。"""
    ascii_slug = "".join(
        char.lower() if char.isalnum() else "_" for char in title if ord(char) < 128
    )
    ascii_slug = "_".join(part for part in ascii_slug.split("_") if part)
    if ascii_slug:
        return ascii_slug[:48]
    digest = hashlib.md5(f"{title}:{raw}".encode(), usedforsecurity=False).hexdigest()[:8]
    return f"skill_{digest}"
