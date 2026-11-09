import codecs
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from bandi.core.message import LLMResponse, TokenUsage, ToolCall
from bandi.llm.streaming import StreamEvent, StreamEventType

logger = logging.getLogger(__name__)

_REASONING_DELTA_KEYS = ("reasoning_content", "thinking", "reasoning")


def _payload_digest(payload: dict[str, Any]) -> str:
    """功能：请求体摘要（HTTP 错误诊断）——角色序列 + 逐条长度 + 参数键。

    不打全文（prompt 可达十万字符）；另扫描非法控制字符（\\x00-\\x1f 除
    \\n\\t\\r）——上游 400 常见诱因是隐藏字符混入正文。
    """
    parts: list[str] = []
    keys = [k for k in payload.keys() if k not in ("messages", "tools")]
    parts.append(f"params={keys}")
    tools = payload.get("tools") or []
    parts.append(f"tools={len(tools)}")
    messages = payload.get("messages") or []
    segs: list[str] = []
    suspects: list[str] = []
    for idx, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "?")
        content = m.get("content")
        clen = len(content) if isinstance(content, str) else -1
        tcalls = m.get("tool_calls")
        seg = f"{role}:{clen}"
        if tcalls:
            seg += f"+tc{len(tcalls)}"
        if m.get("tool_call_id"):
            seg += f"/tcid={str(m['tool_call_id'])[:24]}"
        segs.append(f"{idx}:{seg}")
        if isinstance(content, str):
            for ch in content:
                o = ord(ch)
                if (o < 0x20 and ch not in "\n\t\r") or o == 0x7F:
                    ctx_start = max(0, content.find(ch) - 20)
                    suspects.append(
                        f"msg[{idx}] role={role} has ctrl U+{o:04X} near ...{content[ctx_start : ctx_start + 60]!r}"
                    )
                    break
    parts.append("msgs=" + " ".join(segs))
    if suspects:
        parts.append("SUSPECT_CHARS: " + " | ".join(suspects[:5]))
    return " | ".join(parts)


def _log_http_error(
    where: str, url: str, payload: dict[str, Any], status_code: int, body: str
) -> None:
    """功能：HTTP 4xx/5xx 落日志——状态码 + 响应体 + 请求体摘要（诊断抓包）。"""
    logger.error(
        "LLM HTTP %s at %s: status=%d | resp_body=%s | req_digest=%s",
        where,
        url,
        status_code,
        (body or "")[:2000],
        _payload_digest(payload),
    )


def _extract_reasoning(
    payload: dict[str, Any],
) -> str:  # 功能：从响应体提取模型推理文本（OpenAI 兼容多字段兼容）
    for key in _REASONING_DELTA_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _usage_from_raw(usage_raw: dict[str, Any]) -> TokenUsage:
    # 功能：usage dict → TokenUsage（含前缀缓存命中）
    # 字段口径：OpenAI = prompt_tokens_details.cached_tokens（prompt_tokens 已含命中）；
    # DeepSeek = usage 顶层 prompt_cache_hit_tokens（prompt_tokens 亦为总量）
    # 命中数恒 0（不报错、静默），命中率不可观测（的阻塞点之一）。
    details = usage_raw.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens")
    if cached is None:
        cached = usage_raw.get("prompt_cache_hit_tokens")
    prompt = int(usage_raw.get("prompt_tokens") or 0)
    if not prompt:
        # 少数网关只回 hit/miss 两字段——总量需自行相加
        miss = usage_raw.get("prompt_cache_miss_tokens")
        if miss is not None:
            prompt = int(miss) + int(cached or 0)
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        cache_read_tokens=int(cached or 0),
        # OpenAI 系 prompt_tokens 即总量（命中部分已含在内）⇒ 直接作命中率分母
        input_tokens_total=prompt,
    )


async def openai_complete(  # 功能：调用 OpenAI 兼容的 /chat/completions，返回解析后的 LLMResponse
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    timeout: int = 60,
    max_tokens: int | None = None,
    max_tokens_field: str = "max_tokens",
    thinking_payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> LLMResponse:
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update({str(k): str(v) for k, v in extra_headers.items()})
    body: dict = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
    # max_tokens 必须进入请求 body——字段名由 provider 决定（o 系列/GPT-5 用
    # max_completion_tokens，其余 max_tokens）
    if max_tokens is not None:
        body[max_tokens_field] = max_tokens
    # thinking 参数由 provider 按官方语义翻译（通用兼容端点为空 dict 不发送）
    if thinking_payload:
        body.update(thinking_payload)
    resp = await client.post(url, json=body, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        _log_http_error("complete", url, body, resp.status_code, resp.text)
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices")
    if not choices:
        raise RuntimeError(f"Empty choices from API: {data}")
    choice = choices[0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    tool_calls = _parse_tool_calls(msg)
    usage_raw = data.get("usage", {})
    usage = _usage_from_raw(usage_raw)
    finish_reason = choice.get("finish_reason", "stop")
    refusal = msg.get("refusal") is not None or finish_reason == "content_filter"
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=finish_reason,
        refusal=refusal,
        reasoning=_extract_reasoning(msg),
    )


def _parse_tool_calls(data: dict[str, Any]) -> list[ToolCall]:
    raw = data.get("tool_calls") or []
    calls = []
    for idx, tc in enumerate(raw):
        fn = tc.get("function", {})
        args_raw = fn.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except json.JSONDecodeError:
            # 返回可行动错误文本——禁止静默空 dict 让写工具按默认参数执行
            from bandi.core.message import repair_tool_arguments

            args = repair_tool_arguments(args_raw if isinstance(args_raw, str) else "")
            if args is None:
                args = {
                    "_parse_error": (
                        "工具参数 JSON 解析失败，请重新生成合法的 JSON arguments，"
                        "确保引号正确转义且括号闭合。"
                    )
                }
                logger.warning(
                    "JSON parse failed for tool %s arguments, returning actionable error",
                    fn.get("name", "?"),
                )
        tool_name = fn.get("name") or tc.get("type", "")
        if not tool_name:
            continue
        calls.append(
            ToolCall(
                id=tc.get("id") or f"call_{idx}_{uuid.uuid4().hex[:8]}",
                name=tool_name,
                arguments=args,
            )
        )
    return calls


async def sse_stream(  # 功能：SSE 流式解析器，按行读取 data: 事件，产出 StreamEvent 序列
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    thinking_payload: dict[str, Any] | None = None,
) -> AsyncGenerator[StreamEvent, None]:
    # thinking 参数由 provider 按官方语义翻译后 merge（空 dict 不发送）
    if thinking_payload:
        payload.update(thinking_payload)
    async with client.stream("POST", url, json=payload, headers=headers) as resp:
        if resp.status_code >= 400:
            err_body = (await resp.aread()).decode("utf-8", "replace")
            _log_http_error("stream", url, payload, resp.status_code, err_body)
        resp.raise_for_status()
        buffer = ""
        tool_call_acc: dict[str, dict] = {}
        call_ids_by_index: dict[int, str] = {}
        # 增量解码：多字节字符（中文/emoji）跨字节块不得撕裂为 U+FFFD
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        finish_reason: str | None = None
        usage: TokenUsage | None = None

        def _final_events(reason: str) -> list[StreamEvent]:
            # 功能：流收尾事件——TOOL_CALL_END 补齐 + DONE（finish/usage 归集）。
            # reason 由调用方给定：[DONE] ⇒ finish_reason or "stop"（规范完成）；
            # 自然关闭 ⇒ finish_reason or "incomplete"（**不得**把无理由关闭伪装成 stop）。
            events: list[StreamEvent] = []
            for acc in tool_call_acc.values():
                events.append(
                    StreamEvent(
                        type=StreamEventType.TOOL_CALL_END,
                        tool_call_id=acc["id"],
                        final_tool_call_id=acc.get("_final_id") or "",
                    )
                )
            events.append(StreamEvent(type=StreamEventType.DONE, finish_reason=reason, usage=usage))
            return events

        done_received = False

        async def _handle_line(line: str) -> AsyncGenerator[StreamEvent, None]:
            # 功能：单行 SSE 处理（循环行与**末行冲刷**共用——末行可能无尾换行
            # `[DONE]` 时置 done_received 并产出收尾事件，由外层据 DONE 事件终止。
            nonlocal finish_reason, usage, done_received
            line = line.strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                return
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                done_received = True
                for ev in _final_events(finish_reason or "stop"):
                    yield ev
                return
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                return

            # chunk（位于 finish 之后），必须在 choices 过滤之前提取
            usage_data = data.get("usage")
            if usage_data:
                usage = _usage_from_raw(usage_data)

            choices = data.get("choices") or []
            if not choices:
                return
            delta = choices[0].get("delta", {})
            finish = choices[0].get("finish_reason")

            if delta.get("content"):
                yield StreamEvent(
                    type=StreamEventType.CONTENT_DELTA, content_delta=delta["content"]
                )

            reasoning_delta = _extract_reasoning(delta)
            if reasoning_delta:
                yield StreamEvent(
                    type=StreamEventType.THINKING_DELTA, thinking_delta=reasoning_delta
                )

            raw_tool_calls = delta.get("tool_calls") or []
            for tc in raw_tool_calls:
                idx = tc.get("index", 0)
                tcid = tc.get("id")
                fn = tc.get("function", {})

                # 首 chunk 无 id 时按 index 建条目照发 START，真实 id
                # 后到只更新条目的最终 id，绝不新建第二条目/重发 START
                if idx not in call_ids_by_index:
                    stream_key = tcid or f"idx_{idx}"
                    call_ids_by_index[idx] = stream_key
                    tool_call_acc[stream_key] = {
                        "id": stream_key,
                        "name": "",
                        "arguments": "",
                        "_final_id": tcid or "",
                    }
                    yield StreamEvent(
                        type=StreamEventType.TOOL_CALL_START,
                        tool_call_id=stream_key,
                        tool_name=fn.get("name", ""),
                    )
                else:
                    stream_key = call_ids_by_index[idx]
                    if tcid and tcid != stream_key:
                        tool_call_acc[stream_key]["_final_id"] = tcid

                if fn.get("name"):
                    tool_call_acc[stream_key]["name"] = fn["name"]

                if fn.get("arguments"):
                    acc = tool_call_acc[stream_key]
                    acc["arguments"] += fn["arguments"]
                    yield StreamEvent(
                        type=StreamEventType.TOOL_CALL_DELTA,
                        tool_call_id=stream_key,
                        arguments_delta=fn["arguments"],
                    )

            if finish:
                # 不在此提前返回：官方形态 usage 位于 finish 之后的独立
                # chunk——继续读至 [DONE] 或流自然关闭后统一收尾
                finish_reason = finish

        async for chunk in resp.aiter_bytes():
            buffer += decoder.decode(chunk)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                async for event in _handle_line(line):
                    yield event
                    if event.type is StreamEventType.DONE:
                        return
        # 流自然关闭（部分端点无 [DONE] 标记）
        buffer += decoder.decode(b"", final=True)
        if buffer.strip():
            async for event in _handle_line(buffer):
                yield event
                if event.type is StreamEventType.DONE:
                    return
        # 无条件收尾：finish_reason 缺失（部分端点连 finish 都不发就断流）时标
        # "incomplete"——**不得**让「零事件关闭」伪装成正常 stop（消费端 states.py 据
        if not done_received:
            for ev in _final_events(finish_reason or "incomplete"):
                yield ev
