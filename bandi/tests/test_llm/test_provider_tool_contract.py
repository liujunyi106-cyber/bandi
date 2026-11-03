# 功能：Provider 适配器工具调用转换测试（anthropic / google 多轮工具调用契约）
from __future__ import annotations

import pytest

from bandi.llm.adapters.anthropic import _convert_to_anthropic
from bandi.llm.adapters.google import _convert_to_google


class TestAnthropicToolCallConversion:
    def test_assistant_tool_calls_become_tool_use_blocks(self) -> None:
        """OpenAI 格式 assistant 消息的 tool_calls 必须转为 anthropic tool_use content block。"""
        messages = [
            {"role": "user", "content": "read the file"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"file_path": "x.py"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "file content",
            },
        ]
        system, converted = _convert_to_anthropic(messages)
        assert system == [], "无 system 消息时返回空 block 数组"
        assistant = converted[1]
        assert assistant["role"] == "assistant"
        assert isinstance(assistant["content"], list), "assistant 消息必须转为 content block 列表"
        block = assistant["content"][0]
        assert block["type"] == "tool_use", "tool_calls 必须转为 tool_use block"
        assert block["id"] == "call_1"
        assert block["name"] == "read"
        assert block["input"] == {"file_path": "x.py"}
        tool_result = converted[2]
        assert tool_result["content"][0]["type"] == "tool_result"
        assert tool_result["content"][0]["tool_use_id"] == "call_1"

    def test_assistant_without_tool_calls_plain_content(self) -> None:
        messages = [{"role": "assistant", "content": "plain reply"}]
        system, converted = _convert_to_anthropic(messages)
        assert converted[0]["content"] == "plain reply"

    def test_assistant_text_and_tool_calls_combined(self) -> None:
        """既有文本又有 tool_calls 的 assistant 消息须同时保留 text + tool_use。"""
        messages = [
            {
                "role": "assistant",
                "content": "calling tool now",
                "tool_calls": [
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "grep", "arguments": '{"pattern": "x"}'},
                    }
                ],
            }
        ]
        system, converted = _convert_to_anthropic(messages)
        blocks = converted[0]["content"]
        types = [b["type"] for b in blocks]
        assert "text" in types, "文本必须保留"
        assert "tool_use" in types, "tool_use 必须存在"
        assert any(b.get("name") == "grep" for b in blocks if b["type"] == "tool_use")


class TestGoogleToolCallConversion:
    def test_assistant_tool_calls_become_function_call_parts(self) -> None:
        """OpenAI 格式 assistant tool_calls 必须转为 google functionCall parts。"""
        messages = [
            {"role": "user", "content": "run the search"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "g1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": '{"query": "x"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "g1",
                "content": "results",
            },
        ]
        system, converted = _convert_to_google(messages)
        assistant = converted[1]
        assert isinstance(assistant.get("parts"), list)
        fc = [p for p in assistant["parts"] if "functionCall" in p]
        assert fc, "assistant tool_calls 必须转为 functionCall part"
        assert fc[0]["functionCall"]["name"] == "web_search"
        assert fc[0]["functionCall"]["args"] == {"query": "x"}

    def test_tool_result_role_has_function_response(self) -> None:
        """tool 消息必须转为 google functionResponse part（带 tool_call_id 配对）。"""
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "g2",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "g2", "content": "the file"},
        ]
        system, converted = _convert_to_google(messages)
        tool_msg = converted[2]
        assert tool_msg["role"] == "user"
        fr = [p for p in tool_msg.get("parts", []) if "functionResponse" in p]
        assert fr, "tool 消息必须转为 functionResponse part"
        assert fr[0]["functionResponse"]["name"] == "read"


class TestGoogleStreamFunctionCall:
    @pytest.mark.asyncio
    async def test_stream_emits_tool_call_events(self) -> None:
        """google 流式响应中的 functionCall part 必须产出 TOOL_CALL 事件（不得静默丢失）。"""

        from bandi.llm.adapters.google import google_stream
        from bandi.llm.streaming import StreamEventType

        chunks = [
            b'data: {"candidates":[{"content":{"parts":[{"functionCall":{"name":"read",'
            b'"args":{"file_path":"x.py"}}}]},"finishReason":"STOP"}]}\n\n',
            b"data: [DONE]\n\n",
        ]

        class _FakeStream:
            async def __aiter__(self):
                for c in chunks:
                    yield c

        class _FakeResponse:
            def raise_for_status(self) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def aiter_bytes(self):
                return _FakeStream()

        class _FakeClient:
            def stream(self, *args, **kwargs):
                return _FakeResponse()

        events = [e async for e in google_stream(_FakeClient(), "k", "gemini-x", [], timeout=5)]
        types = [e.type for e in events]
        assert StreamEventType.TOOL_CALL_START in types, f"必须发 TOOL_CALL_START: {types}"
        assert StreamEventType.TOOL_CALL_END in types, f"必须发 TOOL_CALL_END: {types}"
        start = next(e for e in events if e.type == StreamEventType.TOOL_CALL_START)
        assert start.tool_name == "read"


class TestGoogleToolCallIdConsistency:
    """流式/非流式 tool_call_id 必须（fc_{name} + 重名序号）"""

    def test_extract_tool_calls_ids_unique_per_name(self) -> None:
        from bandi.llm.adapters.google import _extract_tool_calls

        parts = [
            {"functionCall": {"name": "read", "args": {"f": "a"}}},
            {"functionCall": {"name": "read", "args": {"f": "b"}}},
            {"functionCall": {"name": "write", "args": {"f": "c"}}},
        ]
        calls = _extract_tool_calls(parts)
        ids = [c.id for c in calls]
        assert ids[0].startswith("fc_read"), f"id 必须与流式一致（fc_{{name}} 前缀）: {ids}"
        assert len(set(ids)) == 3, f"同名工具多次调用 id 必须唯一: {ids}"

    @pytest.mark.asyncio
    async def test_stream_tool_call_ids_unique_per_name(self) -> None:
        from bandi.llm.adapters.google import google_stream
        from bandi.llm.streaming import StreamEventType

        def _chunk(name: str, arg: str, finish: bool = False) -> bytes:
            finish_part = ',"finishReason":"STOP"' if finish else ""
            return (
                b'data: {"candidates":[{"content":{"parts":[{"functionCall":{"name":"'
                + name.encode()
                + b'","args":{"f":"'
                + arg.encode()
                + b'"}}}]}'
                + finish_part.encode()
                + b"}]}\n\n"
            )

        # 中间 chunk 不带 finishReason（真实 Google 流式最后才给）
        # 最后一个 functionCall chunk 带 STOP 结束
        chunks = [_chunk("read", "a"), _chunk("read", "b", finish=True), b"data: [DONE]\n\n"]

        class _FakeStream:
            async def __aiter__(self):
                for c in chunks:
                    yield c

        class _FakeResponse:
            def raise_for_status(self) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def aiter_bytes(self):
                return _FakeStream()

        class _FakeClient:
            def stream(self, *args, **kwargs):
                return _FakeResponse()

        events = [e async for e in google_stream(_FakeClient(), "k", "gemini-x", [], timeout=5)]
        starts = [e.tool_call_id for e in events if e.type == StreamEventType.TOOL_CALL_START]
        assert len(starts) == 2, f"两次调用必须产生两个 TOOL_CALL_START: {starts}"
        assert len(set(starts)) == 2, f"同名工具流式 id 必须唯一: {starts}"
        assert starts[0].startswith("fc_read")
