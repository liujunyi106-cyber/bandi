from bandi.core.message import Message, MessageRole, ToolCall, message_to_dict
from bandi.llm.adapters.openai_compat import _parse_tool_calls


def test_message_to_dict_simple():
    msg = Message(role=MessageRole.USER, content="hello")
    result = message_to_dict(msg)
    assert result["role"] == "user"
    assert result["content"] == "hello"


def test_message_to_dict_with_tool_calls():
    msg = Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id="call_1", name="get_weather", arguments={"city": "Beijing"})],
    )
    result = message_to_dict(msg)
    tc = result["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["function"]["name"] == "get_weather"
    assert tc["type"] == "function"


def test_message_to_dict_with_tool_call_id():
    msg = Message(role=MessageRole.TOOL, content="sunny", tool_call_id="call_1")
    result = message_to_dict(msg)
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "call_1"


def test_message_to_dict_missing_content():
    msg = Message(role=MessageRole.USER)
    result = message_to_dict(msg)
    assert "content" not in result


def test_parse_tool_calls():
    data = {
        "tool_calls": [
            {
                "id": "call_1",
                "function": {"name": "get_weather", "arguments": '{"city": "Beijing"}'},
            }
        ]
    }
    calls = _parse_tool_calls(data)
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "Beijing"}
