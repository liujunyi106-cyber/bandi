from bandi.core.message import (
    LLMResponse,
    Message,
    MessageRole,
    TokenUsage,
    ToolCall,
    ToolResult,
    message_to_dict,
)


class TestToolCall:
    def test_create(self) -> None:
        tc = ToolCall(id="call_1", name="read_file", arguments={"path": "/tmp/test.txt"})
        assert tc.id == "call_1"
        assert tc.name == "read_file"
        assert tc.arguments == {"path": "/tmp/test.txt"}

    def test_empty_arguments(self) -> None:
        tc = ToolCall(id="call_2", name="noop", arguments={})
        assert tc.arguments == {}

    def test_complex_arguments(self) -> None:
        tc = ToolCall(
            id="call_3", name="create_task", arguments={"title": "test", "priority": "high"}
        )
        assert tc.arguments["title"] == "test"


class TestTokenUsage:
    def test_create(self) -> None:
        tu = TokenUsage(prompt_tokens=10, completion_tokens=5)
        assert tu.total == 15

    def test_zero_tokens(self) -> None:
        tu = TokenUsage()
        assert tu.total == 0

    def test_partial(self) -> None:
        tu = TokenUsage(prompt_tokens=100)
        assert tu.total == 100


class TestLLMResponse:
    def test_create(self) -> None:
        usage = TokenUsage(prompt_tokens=10, completion_tokens=5)
        resp = LLMResponse(content="Hello", tool_calls=[], usage=usage, finish_reason="stop")
        assert resp.content == "Hello"
        assert resp.usage is not None
        assert resp.usage.total == 15

    def test_no_content(self) -> None:
        resp = LLMResponse(content="")
        assert resp.content == ""
        assert resp.tool_calls == []
        assert resp.usage is None

    def test_refusal(self) -> None:
        resp = LLMResponse(content="", refusal=True)
        assert resp.refusal is True

    def test_with_tool_calls(self) -> None:
        tc = ToolCall(id="tc_1", name="search", arguments={"query": "python"})
        resp = LLMResponse(content="", tool_calls=[tc], finish_reason="tool_calls")
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "search"


class TestToolResult:
    def test_success(self) -> None:
        tr = ToolResult(
            tool_call_id="tc_1", tool_name="read_file", success=True, output="file content"
        )
        assert tr.is_error is False

    def test_error(self) -> None:
        tr = ToolResult(
            tool_call_id="tc_2",
            tool_name="read_file",
            success=False,
            output="",
            error="File not found",
        )
        assert tr.is_error is True

    def test_should_abort(self) -> None:
        tr = ToolResult(
            tool_call_id="tc_3", tool_name="bash", success=False, output="", should_abort=True
        )
        assert tr.should_abort is True


class TestMessage:
    def test_user_message(self) -> None:
        msg = Message(role=MessageRole.USER, content="hello")
        assert msg.role == MessageRole.USER
        assert msg.content == "hello"

    def test_system_message(self) -> None:
        msg = Message(role=MessageRole.SYSTEM, content="You are a helpful assistant.")
        assert msg.role.value == "system"

    def test_tool_message(self) -> None:
        msg = Message(role=MessageRole.TOOL, content='{"result": "ok"}', tool_call_id="tc_1")
        assert msg.tool_call_id == "tc_1"
        assert msg.role.value == "tool"

    def test_string_role(self) -> None:
        msg = Message(role="user", content="test")
        assert msg.role == "user"


class TestMessageToDict:
    def test_openai_user(self) -> None:
        msg = Message(role=MessageRole.USER, content="hello")
        d = message_to_dict(msg)
        assert d == {"role": "user", "content": "hello"}

    def test_openai_assistant_with_tool_calls(self) -> None:
        msg = Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c1", name="bash", arguments={"cmd": "ls"})],
        )
        d = message_to_dict(msg)
        assert d["role"] == "assistant"
        assert d["tool_calls"][0]["id"] == "c1"
        assert d["tool_calls"][0]["function"]["name"] == "bash"

    def test_openai_tool(self) -> None:
        msg = Message(role=MessageRole.TOOL, content="ok", tool_call_id="c1")
        d = message_to_dict(msg)
        assert d == {"role": "tool", "content": "ok", "tool_call_id": "c1"}

    def test_anthropic_protocol(self) -> None:
        msg = Message(role=MessageRole.USER, content="hi")
        d = message_to_dict(msg, protocol="anthropic")
        assert d["role"] == "user"
        assert d["content"] == [{"type": "text", "text": "hi"}]

    def test_google_protocol(self) -> None:
        msg = Message(role=MessageRole.USER, content="hi")
        d = message_to_dict(msg, protocol="google")
        assert d["role"] == "user"
        assert d["parts"] == [{"text": "hi"}]
