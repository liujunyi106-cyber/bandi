# 功能：Skill Card 公共件（蒸馏 / 反思 / 改写三方共用）——allowed_actions 白名单清洗
# warning 归一、模型 JSON 文本抽取、LLM 文本补全（经统一计费入口）。
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bandi.core.exceptions import LLMError
from bandi.core.message import LLMResponse
from bandi.llm.accounted import BudgetExhaustedError, accounted_complete
from bandi.llm.base import BaseProvider

ALLOWED_NON_TOOL_ACTIONS = frozenset({"ask_user", "answer_user", "continue_flow", "handoff_human"})


class ToolSuggestion(BaseModel):
    """功能：蒸馏/改写时识别或匹配到的工具建议（只引用现有 registry，不发明新工具）。"""

    model_config = ConfigDict(extra="forbid")

    name: str  # 功能：工具名
    display_name: str = ""  # 功能：展示名
    description: str = ""  # 功能：用途描述
    url: str = ""  # 功能：接口地址
    method: str = "POST"  # 功能：HTTP 方法
    input_schema: dict[str, Any] = Field(default_factory=dict)  # 功能：输入参数结构
    output_schema: dict[str, Any] = Field(default_factory=dict)  # 功能：返回结果结构
    source_excerpt: str | None = None  # 功能：原文提及片段
    reason: str = ""  # 功能：识别理由
    resolution_status: str = "incomplete"  # 功能：裁决状态（existing/incomplete）
    matched_tool_name: str | None = None  # 功能：匹配到的 registry 工具名
    missing_reason: str | None = None  # 功能：未匹配时的缺项说明


def extract_json(text: str) -> str:
    """功能：从模型文本中截取 JSON 主体（容忍代码围栏与前后杂讯）。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end >= start:
        return stripped[start : end + 1]
    return stripped


def compact_warnings(warnings: list[str]) -> list[str]:
    """功能：warning 归一（去空白 + 按首次出现去重）。"""
    deduped: list[str] = []
    for warning in warnings:
        text = str(warning).strip()
        if text and text not in deduped:
            deduped.append(text)
    return deduped


async def complete_skill_text(
    provider: BaseProvider,
    prompt: str,
    model_input: str,
    *,
    max_tokens: int,
    label: str,
    session_id: str | None = None,
) -> str:
    """功能：组装 system+user 消息并经 accounted_complete 统一入口调用 provider 非流式补全
    （入统一预算闸门/成本台账，杜绝游离计费；显式携带输出预算，由 provider 采纳并按其
    模型目录钳制）。预算闸门拒绝归一为 LLMError，由调用方的降级/兜底链接管。
    session_id 显式传入时按真实操作 id 归集成本（缺省回退 label 兼容既有行为）。"""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": model_input},
    ]
    try:
        result: LLMResponse = await accounted_complete(
            provider,
            messages,
            label=label,
            session_id=session_id or label,
            agent_id=label,
            max_tokens=max_tokens,
        )
    except BudgetExhaustedError as exc:
        raise LLMError(f"技能 LLM 调用被预算闸门拒绝（{label}）：{exc}") from exc
    return result.content or ""


def available_tool_names(available_tools: list[dict[str, Any]]) -> set[str]:
    """功能：现有 Tool registry 名称集合。"""
    names: set[str] = set()
    for tool in available_tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def sanitize_allowed_actions(
    steps: list[dict[str, Any]],
    available_tools: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """功能：清洗 allowed_actions——call_tool 仅保留 registry 已配置工具；
    非工具动作仅保留四类合法动作（与蒸馏/改写提示词动作词表一致）。
    返回 (清洗后步骤, 缺失工具名, 未定义动作名)。
    只引现有 registry——模型在 tool_mentions 里提及不等于该工具可调用。"""
    names = available_tool_names(available_tools)
    missing_names: list[str] = []
    unknown_actions: list[str] = []
    normalized_steps: list[dict[str, Any]] = []
    for step in steps:
        next_step = dict(step)
        actions: list[str] = []
        for action in next_step.get("allowed_actions", []):
            action_text = str(action).strip()
            if action_text.startswith("call_tool:"):
                tool_name = action_text.replace("call_tool:", "", 1).strip()
                if tool_name in names:
                    actions.append(action_text)
                elif tool_name and tool_name not in missing_names:
                    missing_names.append(tool_name)
                continue
            if action_text in ALLOWED_NON_TOOL_ACTIONS:
                actions.append(action_text)
                continue
            if action_text and action_text not in unknown_actions:
                unknown_actions.append(action_text)
        next_step["allowed_actions"] = actions
        normalized_steps.append(next_step)
    return normalized_steps, missing_names, unknown_actions


def normalize_tool_suggestions(
    value: Any, available_tools: list[dict[str, Any]]
) -> list[ToolSuggestion]:
    """功能：模型 tool_mentions → 工具建议（匹配现有 registry 或标 incomplete，不发明新品）。"""
    suggestions: list[ToolSuggestion] = []
    seen: set[str] = set()
    if not isinstance(value, list):
        return suggestions
    for item in value:
        if not isinstance(item, dict):
            continue
        suggestion = _tool_mention_to_resolution(item, available_tools)
        if suggestion is None or suggestion.name in seen:
            continue
        suggestions.append(suggestion)
        seen.add(suggestion.name)
    return suggestions


def tool_resolution_warnings(suggestions: list[ToolSuggestion]) -> list[str]:
    """功能：incomplete 级工具建议 → 提示 warning。"""
    warnings: list[str] = []
    for suggestion in suggestions:
        if suggestion.resolution_status != "incomplete":
            continue
        label = suggestion.display_name or suggestion.name
        reason = suggestion.missing_reason or "缺少完整接口信息"
        warnings.append(f"模型提到了可能的工具「{label}」，但当前不能新增：{reason}。")
    return warnings


def _tool_mention_to_resolution(
    item: dict[str, Any], available_tools: list[dict[str, Any]]
) -> ToolSuggestion | None:
    """功能：单条 tool_mention → ToolSuggestion（名称或接口地址匹配 registry → existing）。"""
    name = _text(item.get("name")) or _text(item.get("inferred_name"))
    display_name = _text(item.get("display_name")) or _text(item.get("label"))
    description = _text(item.get("description")) or _text(item.get("purpose"))
    url = _text(item.get("url"))
    method = _tool_method(item.get("method"), "POST")
    input_schema = item.get("input_schema")
    output_schema = item.get("output_schema")
    source_excerpt = _text(item.get("source_excerpt")) or None
    reason = (
        _text(item.get("reason"))
        or _text(item.get("purpose"))
        or "模型从技能文档中抽取到该工具提及。"
    )

    matched_tool = _match_available_tool(name, url, available_tools)
    if matched_tool is not None:
        matched_name = _text(matched_tool.get("name")) or name
        return ToolSuggestion(
            name=matched_name,
            display_name=_text(matched_tool.get("display_name")) or display_name or matched_name,
            description=_text(matched_tool.get("description")) or description,
            method=_tool_method(matched_tool.get("method"), method),
            url=_text(matched_tool.get("url")) or url,
            input_schema=matched_tool.get("input_schema")
            if isinstance(matched_tool.get("input_schema"), dict)
            else {},
            output_schema=matched_tool.get("output_schema")
            if isinstance(matched_tool.get("output_schema"), dict)
            else {},
            source_excerpt=source_excerpt,
            reason="已匹配到现有工具配置。",
            resolution_status="existing",
            matched_tool_name=matched_name,
        )

    if not name and not display_name and not url:
        return None
    missing_reasons = _tool_mention_missing_reasons(url, input_schema, output_schema)
    return ToolSuggestion(
        name=name or _tool_name_from_url(url) or display_name or "incomplete_tool",
        display_name=display_name or name or _tool_name_from_url(url) or "未完整配置的工具",
        description=description,
        method=method,
        url=url,
        input_schema=input_schema if isinstance(input_schema, dict) else {},
        output_schema=output_schema if isinstance(output_schema, dict) else {},
        source_excerpt=source_excerpt,
        reason=reason,
        resolution_status="incomplete",
        missing_reason="；".join(missing_reasons),
    )


def _tool_mention_missing_reasons(url: str, input_schema: Any, output_schema: Any) -> list[str]:
    """功能：未匹配工具缺项原因。"""
    reasons: list[str] = []
    if not url:
        reasons.append("缺少可访问接口地址或路径")
    if not isinstance(input_schema, dict) or not input_schema:
        reasons.append("缺少输入参数结构")
    if not isinstance(output_schema, dict) or not output_schema:
        reasons.append("缺少返回结果结构")
    return reasons


def _match_available_tool(
    name: str, url: str, available_tools: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """功能：按名称或接口地址匹配现有 registry 工具。"""
    name_text = name.strip()
    for tool in available_tools:
        if not isinstance(tool, dict):
            continue
        tool_name = _text(tool.get("name"))
        if name_text and tool_name and name_text == tool_name:
            return tool
        tool_url = _text(tool.get("url"))
        if (
            tool_url
            and url
            and url.strip().strip("`'\"<>，。；;,") == tool_url.strip().strip("`'\"<>，。；;,")
        ):
            return tool
    return None


def _tool_name_from_url(url: str) -> str:
    """功能：从接口地址提取工具名候选。"""
    text = url.strip("/").replace("-", "_").replace("/", ".")
    text = re.sub(r"[^A-Za-z0-9_.]+", "_", text).strip("._")
    return text or "tool_candidate"


def _tool_method(value: Any, fallback: str = "POST") -> str:
    """功能：HTTP 方法规范化。"""
    method = str(value or fallback or "POST").upper()
    return method if method in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "POST"


def _text(value: Any, fallback: str = "") -> str:
    """功能：字符串归一（去空白，空则回退 fallback）。"""
    text = str(value).strip() if value is not None else ""
    return text or fallback or ""
