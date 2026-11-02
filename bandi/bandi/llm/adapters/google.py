from __future__ import annotations

import codecs
import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from bandi.core.message import LLMResponse, TokenUsage
from bandi.llm.streaming import StreamEvent, StreamEventType

# - 整数 `thinkingBudget` 已弃用（官方「不再建议」）→ 改 `thinkingConfig.thinkingLevel`
# - 取值：minimal / low / medium / high（默认 medium）；3.x **无法真正关闭**
# ⇒ 空档（UI「关闭」）下发 `minimal`（官方：most queries 等同不思考）
_GEMINI_LEVEL = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


def _thinking_config(thinking_mode: str, model: str) -> dict:
    """功能：thinking_mode → generationConfig.thinkingConfig（仅 Gemini 3.x 支持 thinkingLevel）。

    空档语义：3.x 不可关闭 ⇒ minimal（等价关）；非 3.x 不支持该字段 ⇒ 不下发（防 400）。
    显式档位由模型目录把关（不会出现在不支持思考的模型上），照表下发。
    """
    mode = (thinking_mode or "").strip().lower()
    if not mode:
        return {"thinkingLevel": "minimal"} if "gemini-3" in (model or "").lower() else {}
    return {"thinkingLevel": _GEMINI_LEVEL.get(mode, "medium")}


def _google_url(base_url: str, model: str, action: str) -> str:
    # 功能：Gemini 端点——档案 base_url 优先（可指向网关/区域端点）。
    base = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
    if base.endswith("/v1beta") or base.endswith("/v1"):
        prefix = base
    else:
        prefix = f"{base}/v1beta"
    return f"{prefix}/models/{model}:{action}"


async def google_complete(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    timeout: int = 60,
    max_tokens: int | None = None,
    thinking_mode: str = "",
    base_url: str = "",
) -> LLMResponse:
    url = _google_url(base_url, model, "generateContent")
    system, contents = _convert_to_google(messages)
    body: dict = {"contents": contents}
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    if tools:
        body["tools"] = _convert_tools(tools)
    if max_tokens is not None:
        # maxOutputTokens 进入 generationConfig
        body.setdefault("generationConfig", {})["maxOutputTokens"] = max_tokens
    # -thinkingLevel 枚举（原整数 thinkingBudget 已弃用）；空档 → minimal
    _thinking = _thinking_config(thinking_mode, model)
    if _thinking:
        body.setdefault("generationConfig", {})["thinkingConfig"] = _thinking
    resp = await client.post(
        url,
        json=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return _parse_google_response(data)


def _convert_to_google(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    contents: list[dict] = []
    id_to_name: dict[str, str] = {}
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            system_parts.append(content if isinstance(content, str) else str(content))
            continue
        google_role = "model" if role == "assistant" else "user"
        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            fn_name = id_to_name.get(tool_call_id, "")
            parts = [
                {
                    "functionResponse": {
                        "name": fn_name or tool_call_id,
                        "response": {"result": content},
                    }
                }
            ]
            contents.append({"role": google_role, "parts": parts})
            continue
        parts = [{"text": content}] if content else []
        tool_calls = msg.get("tool_calls")
        if role == "assistant" and tool_calls:
            for tc in tool_calls:
                fn = tc.get("function", tc)
                fn_name = fn.get("name", "")
                id_to_name[tc.get("id", "")] = fn_name
                arguments = fn.get("arguments", "{}")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                parts.append(
                    {
                        "functionCall": {
                            "name": fn_name,
                            "args": arguments if isinstance(arguments, dict) else {},
                        }
                    }
                )
        contents.append({"role": google_role, "parts": parts})
    return "\n".join(system_parts), contents


def _convert_tools(tools: list[dict]) -> list[dict]:
    result = []
    for t in tools:
        fn = t.get("function", t)
        result.append(
            {
                "functionDeclarations": [
                    {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    }
                ]
            }
        )
    return result


def _usage_from_raw(usage_raw: dict[str, Any]) -> TokenUsage:
    # 功能：usageMetadata → TokenUsage（含前缀缓存命中 cachedContentTokenCount）
    # 口径：Gemini 的 promptTokenCount 为总输入（cachedContentTokenCount 是其子集
    # 与 OpenAI 同侧）⇒ 直接作命中率分母。
    prompt = int(usage_raw.get("promptTokenCount") or 0)
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=int(usage_raw.get("candidatesTokenCount") or 0),
        cache_read_tokens=int(usage_raw.get("cachedContentTokenCount") or 0),
        input_tokens_total=prompt,
    )


def _parse_google_response(data: dict) -> LLMResponse:
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"Empty candidates from Google API: {data}")
    candidate = candidates[0]
    content_block = candidate.get("content", {})
    parts = content_block.get("parts", [])
    text_parts: list[str] = []
    thought_parts: list[str] = []
    for part in parts:
        if "text" in part:
            if part.get("thought"):
                thought_parts.append(part["text"])
            else:
                text_parts.append(part["text"])
        elif "functionCall" in part:
            pass  # Tool calls handled separately
    content = "\n".join(text_parts)
    reasoning = "\n".join(thought_parts)
    finish_reason = candidate.get("finishReason", "STOP").lower()
    if finish_reason == "stop":
        finish_reason = "stop"

    usage_raw = data.get("usageMetadata", {})
    usage = _usage_from_raw(usage_raw)

    tool_calls = _extract_tool_calls(parts)
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=finish_reason,
        reasoning=reasoning,
    )


# 关联按 id 索引会错乱）；提升为模块级后同进程内跨响应亦不复用
_TOOL_ID_SEEN: dict[str, int] = {}


def _next_tool_id(seen: dict[str, int], name: str) -> str:
    """tool_call_id 同构生成——fc_{name}，同名工具重复调用追加序号。
    seen 必须为进程级共享 dict（跨响应累计）。"""
    count = seen.get(name, 0)
    seen[name] = count + 1
    return f"fc_{name}" if count == 0 else f"fc_{name}_{count + 1}"


def _extract_tool_calls(parts: list[dict]) -> list:
    from bandi.core.message import ToolCall

    calls = []
    for part in parts:
        fc = part.get("functionCall")
        if fc:
            calls.append(
                ToolCall(
                    id=_next_tool_id(_TOOL_ID_SEEN, fc.get("name", "")),
                    name=fc.get("name", ""),
                    arguments=fc.get("args", {}),
                )
            )
    return calls


async def google_stream(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    timeout: int = 60,
    max_tokens: int | None = None,
    thinking_mode: str = "",
    base_url: str = "",
) -> AsyncGenerator[StreamEvent, None]:
    url = f"{_google_url(base_url, model, 'streamGenerateContent')}?alt=sse"
    system, contents = _convert_to_google(messages)
    body: dict = {"contents": contents}
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    if tools:
        body["tools"] = _convert_tools(tools)
    if max_tokens is not None:
        # 流式请求同样携带 maxOutputTokens
        body.setdefault("generationConfig", {})["maxOutputTokens"] = max_tokens
    # -与 complete 同源单点（thinkingLevel 枚举；空档 → minimal）
    _thinking = _thinking_config(thinking_mode, model)
    if _thinking:
        body.setdefault("generationConfig", {})["thinkingConfig"] = _thinking

    async with client.stream(
        "POST",
        url,
        json=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        timeout=timeout,
    ) as resp:
        resp.raise_for_status()
        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")()
        usage = None
        # 会把已知非零值清零——对齐 Anthropic delta 合并策略）
        usage_raw_acc: dict = {}
        stream_seen = _TOOL_ID_SEEN
        async for chunk in resp.aiter_bytes():
            buffer += decoder.decode(chunk)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    yield StreamEvent(type=StreamEventType.DONE, usage=usage)
                    return
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                meta = data.get("usageMetadata")
                if meta:
                    usage_raw_acc.update({k: v for k, v in meta.items() if v is not None})
                    usage = _usage_from_raw(usage_raw_acc)
                candidates = data.get("candidates", [])
                if not candidates:
                    continue
                candidate = candidates[0]
                content_block = candidate.get("content", {})
                parts = content_block.get("parts", [])
                for part in parts:
                    if "text" in part:
                        if part.get("thought"):
                            yield StreamEvent(
                                type=StreamEventType.THINKING_DELTA,
                                thinking_delta=part["text"],
                            )
                        else:
                            yield StreamEvent(
                                type=StreamEventType.CONTENT_DELTA, content_delta=part["text"]
                            )
                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        tool_id = _next_tool_id(stream_seen, fc.get("name", ""))
                        yield StreamEvent(
                            type=StreamEventType.TOOL_CALL_START,
                            tool_call_id=tool_id,
                            tool_name=fc.get("name", ""),
                        )
                        arguments = fc.get("args", {})
                        yield StreamEvent(
                            type=StreamEventType.TOOL_CALL_DELTA,
                            tool_call_id=tool_id,
                            arguments_delta=(
                                arguments if isinstance(arguments, str) else json.dumps(arguments)
                            ),
                        )
                        yield StreamEvent(
                            type=StreamEventType.TOOL_CALL_END,
                            tool_call_id=tool_id,
                        )
                finish_reason = candidate.get("finishReason", "")
                if finish_reason:
                    finish = finish_reason.lower()
                    yield StreamEvent(
                        type=StreamEventType.DONE,
                        finish_reason="stop" if finish == "stop" else finish,
                        usage=usage,
                    )
                    return

        # 流自然关闭（部分后端不发 finishReason）：统一发最终 DONE 携带
        # 已累积的 usage——缺失 DONE 会让 states 的 finish_reason/usage 断链
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop", usage=usage)
