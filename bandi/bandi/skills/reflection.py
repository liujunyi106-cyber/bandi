# 功能：Skill Card 反思校验器（reflection）——生成后质量门：最多 3 轮 rubric 审阅 +
# 修订回流 + 三态兜底（未返回可修正草稿 / 模型失败 / 达轮次上限均保留当前草稿）
# 防「请稍候」式不可闭环草稿沉淀为可用技能。作为 distill() 末段落盘前接入。
# skill_reflection_prompt.md（7 rubric × MAX_REFLECTION_ROUNDS=3）。
# 提示词内嵌常量（Bandi 无 resource_dir）；rubric 去掉 new_candidate 口径
# 契约增强（有意超集）：修订稿必须经调用方注入的 normalize（与首次生成同一守卫
# 修订不得绕过工具白名单/闭环校验）。
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from bandi.core.exceptions import LLMError
from bandi.skills.skill_guards import compact_warnings, extract_json

ResponseT = TypeVar("ResponseT")
CompleteText = Callable[[str, str], Awaitable[str]]  # 功能：(system_prompt, model_input) → 模型文本

MAX_REFLECTION_ROUNDS = 3

RUBRIC_LABELS: dict[str, str] = {
    "source_alignment": "来源一致性",
    "closed_loop": "闭环能力",
    "adaptive_progression": "自适应推进",
    "tool_grounding": "工具依据",
    "tool_call_format": "工具调用格式",
    "side_effect_confirmation": "副作用确认",
    "interruption_and_recovery": "中断恢复",
}
RUBRICS: list[dict[str, str]] = [
    {"name": name, "label": label} for name, label in RUBRIC_LABELS.items()
]

REFLECTION_PROMPT = """你是企业 Skill Card 反思审阅助手。

你会收到 source、candidate_skill、current_warnings、tool_suggestions 和 rubrics。
请判断 candidate_skill 是否忠实满足 source、是否形成可执行闭环，必要时返回修正后的完整 Skill Card。

反思要求：
- 只基于 source、candidate_skill、available_tools 和已有 tool_suggestions 判断，不要臆造新的业务要求。
- 工具动作只允许引用 available_tools 中已存在的工具；tool_suggestions 中无法与现有工具匹配的条目仅作参考，不得据此保留或新增工具动作。
- 如果问题来自原始文档或原始 Skill 本身，而不是 candidate_skill 的改写错误，请把 origin 标为 source_input。
- 如果问题来自 candidate_skill 的生成或改写，请把 origin 标为 generated_skill。
- 如果不确定来源，请把 origin 标为 unclear。
- 如果 passed=false 且问题可以通过改写 Skill Card 解决，必须返回完整 draft_skill。
- 如果 passed=false 但主要问题来自 source_input，仍可返回一个尽量保守闭环的 draft_skill，同时在 source_warnings 中说明原始输入问题。
- 如果已经通过，draft_skill 可以省略。
- 不要输出 Markdown、解释、注释或代码围栏，只输出 JSON。

Rubric 定义：
1. source_alignment：技能目标、触发意图、graph 节点/边和必要字段是否与用户原始文档/改写要求一致；是否避免添加 source 未要求的无关流程。
2. closed_loop：流程是否能走到明确最终回复；是否避免把“请稍候/正在处理/稍后反馈”作为最终可见结果。
3. adaptive_progression：是否支持一次用户消息抽取多个字段，已满足字段不重复追问，节点是目标而不是固定脚本。
4. tool_grounding：工具调用是否只使用 available_tools 中存在的工具；引用了未配置工具即为 grounding 失败。
5. tool_call_format：allowed_actions 中的工具调用是否完整规范；需要调用工具时必须写成 `call_tool:<tool_name>`，其中 `<tool_name>` 必须是具体工具名；不得只写 `call_tool`、`call_tool:` 或把工具名只写在 instruction 里。
6. side_effect_confirmation：涉及写入、提交、权益/资产/状态变更、不可逆操作时，是否在调用工具或处理前确认关键对象和操作。
7. interruption_and_recovery：中断、切换、恢复和无法闭环场景是否有清晰策略，不会把用户卡在无下一步的状态。

输出格式：
{
  "passed": true,
  "summary": "一句话结论",
  "rubric_results": [
    {"name": "source_alignment", "passed": true, "finding": "", "origin": "generated_skill"}
  ],
  "source_warnings": [],
  "warnings": [],
  "draft_skill": {},
  "tool_mentions": []
}"""

_REVIEW_PAYLOAD_KEYS = (
    "source_kind",
    "source",
    "candidate_skill",
    "current_warnings",
    "tool_suggestions",
    "rubrics",
    "reflection_round",
    "max_reflection_rounds",
    "reflection_history",
)


async def reflect_skill_response(
    *,
    complete: CompleteText,
    normalize: Callable[[dict[str, Any]], ResponseT],
    source_payload: dict[str, Any] | None,
    response: ResponseT,
    source_kind: str = "distill",
) -> ResponseT:
    """功能：反思校验主链——最多 MAX_REFLECTION_ROUNDS 轮；通过即收束，未通过且有可修正
    草稿则经 normalize 修订回流，三态（未返回草稿 / 模型失败 / 达上限）均保留当前草稿
    并追加说明 warning，不抛异常。"""
    reviewed = response
    reviewed_skill = getattr(response, "draft_skill")
    warnings = list(getattr(response, "warnings", []) or [])
    suggestions = list(getattr(response, "tool_suggestions", []) or [])
    reflection_history: list[dict[str, Any]] = []

    for round_index in range(1, MAX_REFLECTION_ROUNDS + 1):
        try:
            review = await _model_review(
                complete,
                {
                    "source_kind": source_kind,
                    "source": source_payload or {},
                    "candidate_skill": reviewed_skill.model_dump(mode="json"),
                    "current_warnings": warnings,
                    "tool_suggestions": [item.model_dump(mode="json") for item in suggestions],
                    "rubrics": RUBRICS,
                    "reflection_round": round_index,
                    "max_reflection_rounds": MAX_REFLECTION_ROUNDS,
                    "reflection_history": reflection_history,
                },
            )
        except (LLMError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return _reviewed(
                normalize,
                reviewed_skill,
                [*warnings, f"反思校验未能完成，已保留当前技能草稿：{exc}"],
                suggestions,
            )

        reflection_history.append(_reflection_history_item(review))
        warnings = compact_warnings([*warnings, *_warnings_from_review(review, source_kind)])

        if bool(review.get("passed")):
            return _reviewed(
                normalize,
                reviewed_skill,
                warnings,
                [*suggestions, *_tool_mentions_from_review(review)],
            )

        revised_skill = review.get("draft_skill")
        if not isinstance(revised_skill, dict):
            return _reviewed(
                normalize,
                reviewed_skill,
                [*warnings, "模型反思未通过，但未返回可修正 Skill Card，已保留当前草稿。"],
                [*suggestions, *_tool_mentions_from_review(review)],
            )

        try:
            reviewed = normalize(
                {
                    "draft_skill": revised_skill,
                    "warnings": warnings,
                    "tool_mentions": [*suggestions, *_tool_mentions_from_review(review)],
                }
            )
        except (TypeError, ValueError) as exc:
            return _reviewed(
                normalize,
                reviewed_skill,
                [*warnings, f"模型返回的修订稿非法，已保留当前草稿：{exc}"],
                [*suggestions, *_tool_mentions_from_review(review)],
            )
        reviewed_skill = getattr(reviewed, "draft_skill")
        warnings = list(getattr(reviewed, "warnings", warnings) or [])
        suggestions = list(getattr(reviewed, "tool_suggestions", suggestions) or [])

    return _reviewed(
        normalize,
        reviewed_skill,
        [
            *warnings,
            f"模型反思已达到 {MAX_REFLECTION_ROUNDS} 轮上限，保留最后一版技能草稿。",
        ],
        suggestions,
    )


def _reviewed(
    normalize: Callable[[dict[str, Any]], ResponseT],
    skill: Any,
    warnings: list[str],
    tool_mentions: list[Any],
) -> ResponseT:
    """功能：统一构造反思收束响应（经调用方 normalize，保证与首次生成同一守卫）。"""
    return normalize(
        {
            "draft_skill": skill.model_dump(mode="json"),
            "warnings": warnings,
            "tool_mentions": [
                item if isinstance(item, dict) else item.model_dump(mode="json")
                for item in tool_mentions
            ],
        }
    )


async def _model_review(complete: CompleteText, payload: dict[str, Any]) -> dict[str, Any]:
    """功能：单轮反思调用——组装输入、解析 JSON、非 object 即判失败。"""
    text = await complete(REFLECTION_PROMPT, _input_from_payload(payload))
    raw = json.loads(extract_json(text))
    if not isinstance(raw, dict):
        raise ValueError("反思模型输出不是 JSON object")
    return raw


def _input_from_payload(payload: dict[str, Any]) -> str:
    """功能：把反思输入字典渲染为分节文本（跳过空值，结构化值走 JSON）。"""
    sections: list[str] = []
    for key in _REVIEW_PAYLOAD_KEYS:
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


def _warnings_from_review(review: dict[str, Any], source_kind: str) -> list[str]:
    """功能：把反思结论转为 warning（来源侧问题带「原始文档/原始技能本身可能存在问题」前缀）。"""
    warnings: list[str] = []
    for item in _string_list(review.get("source_warnings")):
        warnings.append(f"{_source_label(source_kind)}本身可能存在问题：{item}")
    warnings.extend(_string_list(review.get("warnings")))
    for item in _failed_rubrics(review):
        if str(item.get("origin") or "").strip() != "source_input":
            continue
        finding = _finding_text(item)
        if finding:
            warnings.append(
                f"{_source_label(source_kind)}本身可能存在问题：{_rubric_label(item)} - {finding}"
            )
    return compact_warnings(warnings)


def _failed_rubrics(review: dict[str, Any]) -> list[dict[str, Any]]:
    """功能：筛选未通过的 rubric 结果项。"""
    results = review.get("rubric_results")
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict) and not bool(item.get("passed"))]


def _reflection_history_item(review: dict[str, Any]) -> dict[str, Any]:
    """功能：把单轮反思压缩为历史项（供下一轮提示词携带）。"""
    return {
        "passed": bool(review.get("passed")),
        "summary": str(review.get("summary") or ""),
        "failed_rubrics": [
            {
                "name": str(item.get("name") or ""),
                "finding": _finding_text(item),
                "origin": str(item.get("origin") or ""),
            }
            for item in _failed_rubrics(review)
        ],
    }


def _tool_mentions_from_review(review: dict[str, Any]) -> list[dict[str, Any]]:
    """功能：提取反思返回的 tool_mentions 字典项。"""
    value = review.get("tool_mentions")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _source_label(source_kind: str) -> str:
    """功能：来源标签（rewrite=原始技能，其余=原始文档）。"""
    return "原始技能" if source_kind == "rewrite" else "原始文档"


def _rubric_label(item: dict[str, Any]) -> str:
    """功能：rubric 中文标签（未知回退原名）。"""
    name = str(item.get("name") or "")
    return RUBRIC_LABELS.get(name, name or "未知 Rubric")


def _finding_text(item: dict[str, Any]) -> str:
    """功能：rubric 失败描述文本。"""
    return str(item.get("finding") or item.get("issue") or "").strip()


def _string_list(value: Any) -> list[str]:
    """功能：字符串列表归一（去空）。"""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
