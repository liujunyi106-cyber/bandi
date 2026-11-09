# 功能：消息模型与序列化（Message/ToolCall/LLMResponse + dict 转换）
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageRole(Enum):  # 功能：消息角色枚举（system/user/assistant/tool）
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:  # 功能：工具调用请求（模型返回的 id/name/arguments）
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:  # 功能：工具执行结果（成败/输出/错误/持续时间/终止标记）
    tool_call_id: str
    tool_name: str
    success: bool
    output: str
    error: str | None = None
    duration_ms: float = 0.0
    should_abort: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.error not in (None, "")


@dataclass
class TokenUsage:  # 功能：Token 用量（入/出/缓存命中/总计）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 前缀缓存命中 token（Anthropic cache_read_input_tokens / OpenAI
    # prompt_tokens_details.cached_tokens / Gemini cachedContentTokenCount）——
    # 成本拆解与缓存有效性观测的数据源
    cache_read_tokens: int = 0
    # 前缀缓存**写入** token（Anthropic cache_creation_input_tokens，账单按 1.）；
    # OpenAI / DeepSeek / Gemini 无此概念，恒 0
    cache_creation_tokens: int = 0
    # 本次请求的**总输入 token**（provider 归一后）——命中率的分母。
    # 口径差异：OpenAI / DeepSeek / Gemini 的 prompt_tokens 已含命中部分；
    # Anthropic 的 input_tokens **不含**已缓存部分（命中/写入单独列）。
    # 为 0 时命中率回落以 prompt_tokens 为分母。
    input_tokens_total: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_rate(self) -> float:
        """功能：缓存命中率 = 命中 ÷ 总输入，钳制 [0,1]（防跨协议口径混用越界）。"""
        total = self.input_tokens_total or self.prompt_tokens
        if total <= 0:
            return 0.0
        return min(1.0, max(0.0, self.cache_read_tokens / total))


@dataclass
class LLMResponse:  # 功能：LLM 返回封装（内容/工具调用/用量/终止原因/拒绝标记/置信度/推理轨迹）
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage | None = None
    finish_reason: str = "stop"
    refusal: bool = False
    confidence: float = 1.0
    reasoning: str = ""

    def __post_init__(self) -> None:
        if self.refusal and self.tool_calls:
            self.tool_calls = []


@dataclass
class Message:  # 功能：Agent 循环中的统一消息单元（角色/内容/工具调用/转轮索引）
    role: MessageRole | str
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    turn_index: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


def message_from_dict(d: dict[str, Any]) -> Message:  # 功能：dict → Message（从组装器输出还原）
    role_str = d.get("role", "user")
    try:
        role = MessageRole(role_str)
    except ValueError:
        role = role_str
    tool_calls_raw = d.get("tool_calls")
    tool_calls = None
    if isinstance(tool_calls_raw, list) and tool_calls_raw:
        tool_calls = [
            ToolCall(
                id=tc.get("id", ""),
                name=(tc.get("function") or tc).get("name", ""),
                arguments=json.loads((tc.get("function") or tc).get("arguments", "{}"))
                if isinstance((tc.get("function") or tc).get("arguments", "{}"), str)
                else (tc.get("function") or tc).get("arguments", {}),
            )
            for tc in tool_calls_raw
        ]
    # 还原 turn_index/meta（checkpoint 快照恢复后轮次归组语义不失真）
    return Message(
        role=role,
        content=d.get("content"),
        tool_call_id=d.get("tool_call_id"),
        name=d.get("name"),
        tool_calls=tool_calls,
        turn_index=int(d.get("turn_index", 0) or 0),
        meta=dict(d.get("meta", {}) or {}),
    )


def message_to_snapshot(msg: Message) -> dict[str, Any]:
    """功能：压缩投影用的中性序列化（与 message_from_dict 读写对称）。

    只保留可持久化字段；meta 原样带走（json.dumps 侧 default=str 兜底），
    供压缩落库后重启按 active slice 精确回灌。
    """
    role = msg.role.value if isinstance(msg.role, MessageRole) else msg.role
    return {
        "role": role,
        "content": msg.content,
        "tool_call_id": msg.tool_call_id,
        "name": msg.name,
        "tool_calls": [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in (msg.tool_calls or [])
        ],
        "turn_index": msg.turn_index,
        "meta": dict(msg.meta or {}),
    }


def message_to_dict(
    msg: Message, protocol: str = "openai"
) -> dict[str, Any]:  # 功能：Message → Provider API dict（按协议格式输出）
    if protocol == "anthropic":
        return _to_anthropic_dict(msg)
    if protocol == "google":
        return _to_google_dict(msg)
    return _to_openai_dict(msg)


def repair_tool_arguments(raw: str) -> dict | None:
    """功能：修复 LLM 输出的损坏工具参数 JSON。

    修复顺序：① 去 BOM/首尾空白 → ② 剥代码围栏 → ③ 尾逗号清理 → ④ 截断补闭合。
    全部失败返回 None（调用方转为可行动错误，禁止静默空 dict 继续执行写操作）。
    """
    import re

    # 空 arguments 非法 JSON，交调用方转可行动错误
    if not raw:
        return None
    text = raw.strip().lstrip("\ufeff")
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尾逗号修复：{...}, 或 [...], 尾部逗号
    fixed = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    # 截断修复：补闭合括号（截断在对象/数组中间时无法恢复，尝试后放弃）
    for opener, closer in (("{", "}"), ("[", "]")):
        if text.count(opener) > text.count(closer):
            text += closer * (text.count(opener) - text.count(closer))
            text = re.sub(r",\s*([}\]])", r"\1", text)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                break
    return None


def _normalize_arguments(arguments: Any) -> dict[str, Any]:
    """深度复审修复：arguments 归一——外部写入 str（JSON 字符串）时解析为
    dict，否则 _to_openai_dict 双重序列化（json.dumps(str) → 转义字符串）。
    对齐 message_from_dict 的 json.loads 行为。"""
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return arguments if isinstance(arguments, dict) else {}


def message_from_entry(entry: dict[str, Any]) -> Message:
    """
    写读对称（context_loader 感知层与 _session_restore 恢复路径共用）。"""
    role_raw = entry.get("role", "user")
    try:
        role = MessageRole(role_raw) if isinstance(role_raw, str) else role_raw
    except ValueError:
        role = role_raw
    tool_calls: list[ToolCall] | None = None
    meta_dict: dict[str, Any] = {}
    raw_meta = entry.get("metadata")
    if raw_meta:
        try:
            parsed = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
            meta_dict = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            meta_dict = {}
        calls = meta_dict.get("tool_calls")
        if calls:
            tool_calls = [
                ToolCall(
                    id=str(tc.get("id", "")),
                    name=tc.get("name", ""),
                    arguments=_normalize_arguments(tc.get("arguments", {})),
                )
                for tc in calls
                if isinstance(tc, dict)
            ]
    return Message(
        role=role,
        content=entry.get("content", ""),
        tool_call_id=entry.get("tool_call_id"),
        name=str(meta_dict.get("name") or "") or None,
        tool_calls=tool_calls,
        turn_index=entry.get("turn_index") or 0,
        meta=meta_dict,
    )


def _to_openai_dict(msg: Message) -> dict[str, Any]:
    d: dict[str, Any] = {"role": msg.role.value if isinstance(msg.role, MessageRole) else msg.role}
    if msg.content is not None:
        d["content"] = msg.content
    if msg.tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in msg.tool_calls
        ]
    if msg.tool_call_id:
        d["tool_call_id"] = msg.tool_call_id
    # 功能：thinking 模式（DeepSeek 等）要求带工具调用的 assistant 消息回传
    # reasoning_content——缺失时上游返回 400 "must be passed back"。
    role_value = msg.role.value if isinstance(msg.role, MessageRole) else str(msg.role)
    if role_value == "assistant":
        reasoning_content = str((msg.meta or {}).get("reasoning_content") or "").strip()
        if reasoning_content:
            d["reasoning_content"] = reasoning_content
    return d


def _to_anthropic_dict(msg: Message) -> dict[str, Any]:
    d: dict[str, Any] = {"role": msg.role.value if isinstance(msg.role, MessageRole) else msg.role}
    content: list[dict[str, Any]] = []
    if msg.content:
        content.append({"type": "text", "text": msg.content})
    if msg.tool_calls:
        for tc in msg.tool_calls:
            content.append(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
            )
    if msg.tool_call_id:
        d["tool_call_id"] = msg.tool_call_id
    d["content"] = content
    return d


def _to_google_dict(msg: Message) -> dict[str, Any]:
    d: dict[str, Any] = {"role": msg.role.value if isinstance(msg.role, MessageRole) else msg.role}
    parts: list[dict[str, Any]] = []
    if msg.content:
        parts.append({"text": msg.content})
    if msg.tool_calls:
        for tc in msg.tool_calls:
            parts.append({"functionCall": {"name": tc.name, "args": tc.arguments}})
    d["parts"] = parts
    return d
