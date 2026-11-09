# 功能：流式双缓冲区 + 增量解析器（Harness 对齐）
from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from enum import Enum, auto

from bandi.core.exceptions import LLMError
from bandi.core.message import LLMResponse, TokenUsage

logger = logging.getLogger(__name__)


class StreamEventType(Enum):
    CONTENT_DELTA = auto()
    THINKING_DELTA = auto()
    TOOL_CALL_START = auto()
    TOOL_CALL_DELTA = auto()
    TOOL_CALL_END = auto()
    DONE = auto()
    ERROR = auto()


class StreamEvent:
    type: StreamEventType
    content_delta: str | None = None
    thinking_delta: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    error_message: str | None = None
    final_tool_call_id: str | None = None

    def __init__(
        self,
        type: StreamEventType,
        content_delta: str | None = None,
        thinking_delta: str | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        arguments_delta: str | None = None,
        usage: TokenUsage | None = None,
        finish_reason: str | None = None,
        error_message: str | None = None,
        final_tool_call_id: str | None = None,
    ):
        self.type = type
        self.content_delta = content_delta
        self.thinking_delta = thinking_delta
        self.tool_call_id = tool_call_id
        self.tool_name = tool_name
        self.arguments_delta = arguments_delta
        self.usage = usage
        self.finish_reason = finish_reason
        self.error_message = error_message
        self.final_tool_call_id = final_tool_call_id


class StreamingBuffer:
    """功能：流式双缓冲区——文本缓冲区 + 工具调用缓冲区，支持增量解析"""

    def __init__(self) -> None:
        self._text_buffer: str = ""
        self._tool_call_buffer: dict[str, dict[str, str]] = {}

    @property
    def text(self) -> str:
        return self._text_buffer

    @property
    def tool_calls(self) -> dict[str, dict[str, str]]:
        return self._tool_call_buffer

    def append_text(self, delta: str) -> None:
        self._text_buffer += delta

    def append_thinking(self, delta: str) -> None:
        # Thinking 部分也累积到文本缓冲区（作为 reasoning）
        if not hasattr(self, "_thinking_buffer"):
            self._thinking_buffer: str = ""
        self._thinking_buffer += delta

    @property
    def thinking(self) -> str:
        return getattr(self, "_thinking_buffer", "")

    def start_tool_call(self, tool_call_id: str, tool_name: str) -> None:
        self._tool_call_buffer[tool_call_id] = {
            "id": tool_call_id,
            "name": tool_name,
            "arguments": "",
            "_final_id": "",
        }

    def append_tool_call(self, tool_call_id: str, arguments_delta: str) -> None:
        # auto-create：DELTA 先于 START 到达时自动建条目（对齐 AgentLoop
        # 主链路的 setdefault 防御语义，避免乱序流丢参数）
        if tool_call_id not in self._tool_call_buffer:
            self._tool_call_buffer[tool_call_id] = {
                "id": tool_call_id,
                "name": "",
                "arguments": "",
                "_final_id": "",
            }
        self._tool_call_buffer[tool_call_id]["arguments"] += arguments_delta

    def end_tool_call(self, tool_call_id: str, final_id: str = "") -> None:
        # 功能：流结束回填真实工具调用 id（首块缺 id 时的占位符在最终产物中替换）。
        entry = self._tool_call_buffer.get(tool_call_id)
        if entry is not None and final_id:
            entry["_final_id"] = final_id

    def get_completed_tool_calls(self) -> list[dict[str, str]]:
        """功能：返回所有已完成的工具调用（含累积参数）"""
        return list(self._tool_call_buffer.values())

    def clear(self) -> None:
        self._text_buffer = ""
        self._tool_call_buffer = {}
        if hasattr(self, "_thinking_buffer"):
            delattr(self, "_thinking_buffer")


async def merge_stream_events(
    stream: AsyncGenerator[StreamEvent, None],
) -> LLMResponse:
    """功能：合并流式事件为 LLMResponse（使用双缓冲区）。

    终态语义：流结束但**从未收到 DONE** ⇒ `finish_reason="incomplete"`
    （半截断线，消费端按失败处置）——**不得**伪造 "stop" 把零事件/断流伪装成正常完成。
    此前初值 "stop" 且无 DONE 兜底，与 `agent/states.py` 内联消费的 `seen_done → incomplete`
    语义不一致：同一条「无 DONE」流，两条消费路径给出不同结论。
    """
    buffer = StreamingBuffer()
    finish_reason: str | None = None
    usage: TokenUsage | None = None

    async for event in stream:
        if event.type == StreamEventType.CONTENT_DELTA and event.content_delta:
            buffer.append_text(event.content_delta)
        elif event.type == StreamEventType.THINKING_DELTA and event.thinking_delta:
            buffer.append_thinking(event.thinking_delta)
        elif event.type == StreamEventType.TOOL_CALL_START and event.tool_call_id:
            buffer.start_tool_call(event.tool_call_id, event.tool_name or "")
        elif event.type == StreamEventType.TOOL_CALL_DELTA and event.tool_call_id:
            buffer.append_tool_call(event.tool_call_id, event.arguments_delta or "")
        elif event.type == StreamEventType.TOOL_CALL_END and event.tool_call_id:
            buffer.end_tool_call(event.tool_call_id, event.final_tool_call_id or "")
        elif event.type == StreamEventType.DONE:
            if event.finish_reason:
                finish_reason = event.finish_reason
            if event.usage:
                usage = event.usage
        elif event.type == StreamEventType.ERROR:
            raise LLMError(event.error_message or "Stream error")

    from bandi.core.message import ToolCall, repair_tool_arguments

    parsed_calls = []
    for tc_info in buffer.get_completed_tool_calls():
        args_raw = tc_info.get("arguments", "")
        try:
            args = json.loads(args_raw)
        except (json.JSONDecodeError, TypeError):
            # 禁止静默降级 args={}（空参数执行写类工具是危险行为）
            repaired = repair_tool_arguments(args_raw)
            if repaired is None:
                args = {
                    "_parse_error": (
                        "流式参数 JSON 解析失败，请重新生成合法的 JSON arguments，"
                        "确保引号正确转义且括号闭合。"
                    )
                }
                logger.warning(
                    "JSON parse failed for tool %s arguments, returning actionable error",
                    tc_info.get("name", "?"),
                )
            else:
                args = repaired
        parsed_calls.append(
            ToolCall(
                id=tc_info.get("_final_id") or tc_info["id"],
                name=tc_info["name"],
                arguments=args,
            )
        )

    return LLMResponse(
        content=buffer.text,
        tool_calls=parsed_calls,
        usage=usage or TokenUsage(prompt_tokens=0, completion_tokens=0),
        finish_reason=finish_reason or "incomplete",
        reasoning=buffer.thinking,
    )
