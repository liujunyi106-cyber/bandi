from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from bandi.core.message import LLMResponse, TokenUsage, ToolCall
from bandi.llm.streaming import StreamEvent, StreamEventType

# - Claude 4.7 / 4.8 / Opus 5 / Sonnet 5 / Fable / Mythos 系：**仅** adaptive
# （`type:"enabled"` 直接 400；`budget_tokens` 已废弃）
# - 4.5 及更早：只支持 enabled + budget_tokens
# adaptive 系必须带 `display:"summarized"`，否则响应不含思考文本（前端过程块会空）
_ADAPTIVE_MODEL_MARKERS = (
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable",
    "claude-mythos",
)
_ANTHROPIC_EFFORT = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}
_ANTHROPIC_BUDGET = {"low": 4096, "medium": 8192, "high": 16000, "max": 32000}


def _is_adaptive_thinking_model(model: str) -> bool:
    lowered = (model or "").lower()
    return any(marker in lowered for marker in _ADAPTIVE_MODEL_MARKERS)


def _thinking_body(model: str, thinking_mode: str, max_tokens: int) -> dict:
    """功能：thinking_mode → Anthropic 请求字段（族感知；空档语义按族区分）。"""
    mode = (thinking_mode or "").strip().lower()
    if _is_adaptive_thinking_model(model):
        if not mode:
            # 5 系默认（不传）= adaptive 即「思考开」——用户选关闭必须显式 disabled
            # 否则「关闭」形同虚设（官方：仅显式 disabled 才关）
            return {"thinking": {"type": "disabled"}}
        effort = _ANTHROPIC_EFFORT.get(mode, "high")
        return {
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": effort},
        }
    # 旧系：不传 = 无思考（关闭语义天然成立）
    if not mode:
        return {}
    budget = min(_ANTHROPIC_BUDGET.get(mode, 8192), max(0, int(max_tokens) // 2))
    if budget < 1024:
        return {}  # Anthropic 约束 budget_tokens >= 1024（不满足不发送，防必然 400）
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}


def _anthropic_url(base_url: str) -> str:
    # 功能：Anthropic Messages 端点——档案 base_url 优先（可指向兼容网关）。
    base = (base_url or "https://api.anthropic.com").rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


async def anthropic_complete(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int = 4096,
    timeout: int = 60,
    thinking_mode: str = "",
    base_url: str = "",
) -> LLMResponse:
    url = _anthropic_url(base_url)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    system, anthropic_msgs = _convert_to_anthropic(messages)
    body: dict = {"model": model, "messages": anthropic_msgs, "max_tokens": max_tokens}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = _convert_tools(tools)
    # -族感知思考参数（adaptive 系走 output_config.effort）
    body.update(_thinking_body(model, thinking_mode, max_tokens))
    resp = await client.post(url, json=body, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    content = _extract_text(data.get("content", []))
    tool_calls = _extract_tool_calls(data.get("content", []))
    usage_raw = data.get("usage", {})
    usage = _usage_from_raw(usage_raw)
    stop_reason = data.get("stop_reason", "end_turn")
    finish_reason = "stop" if stop_reason == "end_turn" else stop_reason
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=finish_reason,
        reasoning=_extract_reasoning(data.get("content", [])),
    )


def _usage_from_raw(usage_raw: dict[str, Any]) -> TokenUsage:
    # 功能：usage dict → TokenUsage（含缓存命中 cache_read_input_tokens 与
    # 缓存写入 cache_creation_input_tokens）
    # ⚠️ 口径：Anthropic 的 input_tokens **不含**已缓存部分（与 OpenAI 相反）
    # 故总输入 = input + cache_read + cache_creation——命中率分母用该总量
    # 直接用 input_tokens 作分母会算出 >100% 的命中率。
    input_tokens = int(usage_raw.get("input_tokens") or 0)
    cache_read = int(usage_raw.get("cache_read_input_tokens") or 0)
    cache_creation = int(usage_raw.get("cache_creation_input_tokens") or 0)
    return TokenUsage(
        prompt_tokens=input_tokens,
        completion_tokens=int(usage_raw.get("output_tokens") or 0),
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        input_tokens_total=input_tokens + cache_read + cache_creation,
    )


def _convert_to_anthropic(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    # system 消息序列 → content-block 数组（Anthropic 协议要求 content-block 形态
    # 才能挂 cache_control 缓存断点）。缓存断点规则：仅第一个 system 块挂
    # ephemeral——组装层保证首条 system 为静态前缀（跨轮字节稳定），后续 system
    # （技能指令/压缩边界/摘要/动态上下文）内容逐轮可变，不得挂断点。
    system_blocks: list[dict] = []
    result: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            text = content if isinstance(content, str) else str(content)
            if text:
                block: dict = {"type": "text", "text": text}
                if not system_blocks:
                    block["cache_control"] = {"type": "ephemeral"}
                system_blocks.append(block)
            continue
        if role == "tool":
            result.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.get("tool_call_id", ""),
                            "content": content,
                        }
                    ],
                }
            )
            continue
        tool_calls = msg.get("tool_calls")
        if role == "assistant" and tool_calls:
            blocks: list[dict] = []
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in tool_calls:
                fn = tc.get("function", tc)
                arguments = fn.get("arguments", "{}")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": arguments if isinstance(arguments, dict) else {},
                    }
                )
            result.append({"role": "assistant", "content": blocks})
            continue
        result.append({"role": role, "content": content})
    return system_blocks, result


def _convert_tools(tools: list[dict]) -> list[dict]:
    result = []
    for t in tools:
        fn = t.get("function", t)
        result.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {}),
            }
        )
    return result


def _extract_text(content_blocks: list[dict]) -> str:
    texts: list[str] = []
    for block in content_blocks:
        if block.get("type") == "text":
            texts.append(block.get("text", ""))
    return "\n".join(texts)


def _extract_reasoning(
    content_blocks: list[dict],
) -> str:  # 功能：提取 Anthropic thinking block 文本
    parts: list[str] = []
    for block in content_blocks:
        if block.get("type") == "thinking":
            parts.append(block.get("thinking", ""))
    return "\n".join(parts)


def _extract_tool_calls(content_blocks: list[dict]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for block in content_blocks:
        if block.get("type") == "tool_use":
            tool_name = block.get("name", "")
            if not tool_name:
                continue
            args_raw = block.get("input", {})
            calls.append(
                ToolCall(
                    id=block.get("id") or f"tool_{uuid.uuid4().hex[:8]}",
                    name=tool_name,
                    arguments=args_raw if isinstance(args_raw, dict) else {},
                )
            )
    return calls


async def anthropic_stream(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int = 4096,
    timeout: int = 60,
    thinking_mode: str = "",
    base_url: str = "",
) -> AsyncGenerator[StreamEvent, None]:
    url = _anthropic_url(base_url)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    system, anthropic_msgs = _convert_to_anthropic(messages)
    body: dict = {
        "model": model,
        "messages": anthropic_msgs,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = _convert_tools(tools)
    # -族感知思考参数（与 complete 同源单点 _thinking_body）
    body.update(_thinking_body(model, thinking_mode, max_tokens))

    async with client.stream("POST", url, json=body, headers=headers, timeout=timeout) as resp:
        resp.raise_for_status()
        current_tool_name: str = ""
        current_tool_id: str = ""
        usage: TokenUsage | None = None

        async for line in resp.aiter_lines():
            line = line.strip()
            if not line:
                continue
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop", usage=usage)
                return
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            event_type = data.get("type", "")
            if event_type == "message_start":
                msg_data = data.get("message", {})
                usage_raw = msg_data.get("usage", {})
                usage = _usage_from_raw(usage_raw)
            elif event_type == "content_block_start":
                block = data.get("content_block", {})
                if block.get("type") == "tool_use":
                    current_tool_name = block.get("name", "")
                    tool_id = block.get("id", "")
                    current_tool_id = tool_id
                    yield StreamEvent(
                        type=StreamEventType.TOOL_CALL_START,
                        tool_call_id=tool_id,
                        tool_name=current_tool_name,
                    )
            elif event_type == "content_block_delta":
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    yield StreamEvent(
                        type=StreamEventType.CONTENT_DELTA,
                        content_delta=delta.get("text", ""),
                    )
                elif delta.get("type") == "thinking_delta":
                    yield StreamEvent(
                        type=StreamEventType.THINKING_DELTA,
                        thinking_delta=delta.get("thinking", ""),
                    )
                elif delta.get("type") == "input_json_delta":
                    yield StreamEvent(
                        type=StreamEventType.TOOL_CALL_DELTA,
                        tool_call_id=current_tool_id,
                        arguments_delta=delta.get("partial_json", ""),
                    )
            elif event_type == "content_block_stop":
                if current_tool_name:
                    yield StreamEvent(
                        type=StreamEventType.TOOL_CALL_END,
                        tool_call_id=current_tool_id,
                    )
                    current_tool_name = ""
                    current_tool_id = ""
            elif event_type == "message_delta":
                delta = data.get("delta", {})
                stop_reason = delta.get("stop_reason", "")
                finish = "stop" if stop_reason == "end_turn" else stop_reason
                usage_raw = data.get("usage", {})
                if usage_raw:
                    # 只带 output_tokens，input_tokens 必须保留 message_start 的值——
                    # 代理/网关常在 delta 中省略 input，无条件重建会把它覆盖为 0
                    prompt = usage_raw.get("input_tokens")
                    completion = usage_raw.get("output_tokens")
                    cache_hit = usage_raw.get("cache_read_input_tokens")
                    base_prompt = usage.prompt_tokens if usage is not None else 0
                    base_completion = usage.completion_tokens if usage is not None else 0
                    base_cache = usage.cache_read_tokens if usage is not None else 0
                    cache_creation = usage_raw.get("cache_creation_input_tokens")
                    base_creation = usage.cache_creation_tokens if usage is not None else 0
                    input_tokens = prompt if prompt is not None else base_prompt
                    cache_read = int(cache_hit) if cache_hit is not None else base_cache
                    creation = int(cache_creation) if cache_creation is not None else base_creation
                    usage = TokenUsage(
                        prompt_tokens=input_tokens,
                        completion_tokens=(
                            completion if completion is not None else base_completion
                        ),
                        cache_read_tokens=cache_read,
                        cache_creation_tokens=creation,
                        input_tokens_total=input_tokens + cache_read + creation,
                    )
                yield StreamEvent(type=StreamEventType.DONE, finish_reason=finish, usage=usage)
                return
