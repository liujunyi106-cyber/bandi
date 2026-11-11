import pytest

from bandi.agent.base import ExecutionContext, RecoveryAction, TerminalReason
from bandi.agent.loop import AgentLoop
from bandi.agent.recovery_manager import RecoveryManager
from bandi.agent.transitions import build_default_machine
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall, ToolResult
from bandi.eval.fairness_evaluator import FairnessEvaluator


class RecordingBus(EventBus):
    def __init__(self):
        super().__init__()
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class MockToolExecutor:
    def __init__(self):
        self._results: list[ToolResult] | None = None

    def set_results(self, results: list[ToolResult]) -> None:
        self._results = results

    async def execute(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        if self._results is not None:
            return self._results
        return [
            ToolResult(tool_call_id=tc.id, tool_name=tc.name, success=True, output="ok")
            for tc in tool_calls
        ]


class MockProvider:
    def __init__(self):
        self.call_count = 0
        self.supports_streaming = False
        self._responses: list[LLMResponse | type[Exception]] = []

    def add_response(self, response: LLMResponse | type[Exception]) -> None:
        self._responses.append(response)

    @property
    def model_name(self) -> str:
        return "mock"

    @property
    def protocol(self) -> str:
        return "openai"

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None, **kwargs
    ) -> LLMResponse:
        self.call_count += 1
        if not self._responses:
            return LLMResponse(content="ok")
        response = self._responses.pop(0)
        if isinstance(response, type) and issubclass(response, Exception):
            raise response("mock error")
        return response

    def count_tokens(self, text: str) -> int:
        # BaseProvider 契约方法（压缩路径消费 ctx.provider.count_tokens——桩补全）
        from bandi.utils.token_counter import TokenCounter

        return TokenCounter.count(str(text))

    async def stream(self, messages: list[dict], tools: list[dict] | None = None, **kwargs):
        yield


def make_ctx(
    messages: list[Message] | None = None,
    provider: MockProvider | None = None,
) -> ExecutionContext:
    return ExecutionContext(
        messages=messages or [],
        provider=provider or MockProvider(),
        config=AgentConfig(),
        bus=RecordingBus(),
    )


@pytest.mark.asyncio
async def test_normal_flow_no_tools():
    provider = MockProvider()
    provider.add_response(LLMResponse(content="hello"))
    ctx = make_ctx(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=provider,
    )
    loop = AgentLoop()
    reason = await loop.run(ctx)
    assert reason == TerminalReason.COMPLETED
    assert ctx.current_state == "terminated"
    assert len(ctx.messages) >= 2


@pytest.mark.asyncio
async def test_perception_analysis_event_emitted():
    from bandi.perception.pipeline import Dependencies, PerceptionPipeline

    provider = MockProvider()
    provider.add_response(LLMResponse(content="hello"))
    ctx = make_ctx(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=provider,
    )
    ctx.raw_input = "hi"
    pipeline = PerceptionPipeline(
        deps=Dependencies(
            fairness_evaluator=FairnessEvaluator(),
        )
    )
    loop = AgentLoop(perception=pipeline)
    await loop.run(ctx)
    events = [e for e in ctx.bus.events if e.type == EventType.PERCEPTION_ANALYZED]
    assert len(events) == 1, "PERCEPTION_ANALYZED 事件未发出"
    data = events[0].data
    assert "fairness_score" in data
    assert "estimated_input_tokens" in data


@pytest.mark.asyncio
async def test_tool_search_expands_next_turn_tool_schema():
    """tool_search 命中的内部工具 schema 必须在下一轮真正注入工具面。"""
    from bandi.tools.base import BaseTool, ToolCategory
    from bandi.tools.registry import ToolRegistry

    class _WebSearch(BaseTool):
        name: str = "web_search"
        description: str = "search the internet"
        category: ToolCategory = ToolCategory.WEB

        async def execute(self, params, context=None):  # type: ignore[no-untyped-def]
            return "ok"

    class _SearchExecutor(MockToolExecutor):
        def __init__(self) -> None:
            super().__init__()
            self._registry = ToolRegistry()
            self._registry.register_instance(_WebSearch())

        async def execute(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
            return [
                ToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    success=True,
                    output='{"matches": ["web_search"]}',
                )
                for tc in tool_calls
            ]

    seen_tools: list[list[str]] = []
    provider = MockProvider()
    original_complete = provider.complete

    async def complete(messages, tools=None, **kwargs):
        seen_tools.append(
            [str((item.get("function") or {}).get("name") or "") for item in (tools or [])]
        )
        return await original_complete(messages, tools=tools, **kwargs)

    provider.complete = complete  # type: ignore[method-assign]
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="tool_search", arguments={"query": "web_search"})],
        )
    )
    provider.add_response(LLMResponse(content="done"))
    ctx = make_ctx(
        messages=[Message(role=MessageRole.USER, content="联网搜索")],
        provider=provider,
    )
    ctx.active_tools = [
        {
            "type": "function",
            "function": {"name": "tool_search", "description": "search tools", "parameters": {}},
        }
    ]
    ctx.meta["allowed_tool_names"] = {"tool_search", "web_search"}
    ctx.tool_executor = _SearchExecutor()
    reason = await AgentLoop().run(ctx)
    assert reason == TerminalReason.COMPLETED
    assert any("web_search" in names for names in seen_tools), "tool_search 命中后未扩面"


@pytest.mark.asyncio
async def test_normal_flow_with_tools():
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="bash", arguments={"cmd": "ls"})],
        )
    )
    provider.add_response(LLMResponse(content="done"))
    ctx = make_ctx(
        messages=[Message(role=MessageRole.USER, content="run ls")],
        provider=provider,
    )
    ctx.tool_executor = MockToolExecutor()
    loop = AgentLoop()
    reason = await loop.run(ctx)
    assert reason == TerminalReason.COMPLETED
    assert ctx.turn_index == 2


@pytest.mark.asyncio
async def test_empty_input_terminates_immediately():
    ctx = make_ctx(messages=[])
    loop = AgentLoop()
    reason = await loop.run(ctx)
    assert reason == TerminalReason.COMPLETED


@pytest.mark.asyncio
async def test_max_turns_truncation():
    provider = MockProvider()
    for _ in range(20):
        provider.add_response(
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="bash", arguments={"cmd": "ls"})],
            )
        )
    ctx = make_ctx(
        messages=[Message(role=MessageRole.USER, content="run")],
        provider=provider,
    )
    ctx.tool_executor = MockToolExecutor()
    ctx.loop_detection_window = 10
    loop = AgentLoop(max_iterations=5)
    reason = await loop.run(ctx)
    assert reason == TerminalReason.MAX_TURNS
    assert ctx.turn_index <= 5


@pytest.mark.asyncio
async def test_safety_refusal_detected():
    provider = MockProvider()
    provider.add_response(LLMResponse(content="cannot do that", refusal=True))
    ctx = make_ctx(
        messages=[Message(role=MessageRole.USER, content="do bad thing")],
        provider=provider,
    )
    loop = AgentLoop()
    reason = await loop.run(ctx)
    assert reason == TerminalReason.SAFETY_BLOCKED


@pytest.mark.asyncio
async def test_content_filter_detected():
    provider = MockProvider()
    provider.add_response(LLMResponse(content="", finish_reason="content_filter"))
    ctx = make_ctx(
        messages=[Message(role=MessageRole.USER, content="bad content")],
        provider=provider,
    )
    loop = AgentLoop()
    reason = await loop.run(ctx)
    assert reason == TerminalReason.SAFETY_BLOCKED


@pytest.mark.asyncio
async def test_loop_detection():
    provider = MockProvider()
    for _ in range(15):
        provider.add_response(
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="bash", arguments={"cmd": "ls"})],
            )
        )
    ctx = make_ctx(
        messages=[Message(role=MessageRole.USER, content="run ls")],
        provider=provider,
    )
    executor = MockToolExecutor()
    executor.set_results(
        [ToolResult(tool_call_id="call_1", tool_name="bash", success=True, output="ok")]
    )
    ctx.tool_executor = executor
    ctx.loop_detection_window = 3
    loop = AgentLoop()
    reason = await loop.run(ctx)
    assert reason == TerminalReason.LOOP_DETECTED


@pytest.mark.asyncio
async def test_model_error_on_none_response():
    ctx = make_ctx(
        messages=[Message(role=MessageRole.USER, content="hi")],
    )
    ctx.current_state = "parsing"
    loop = AgentLoop()
    loop._handlers["parsing"] = lambda c: None
    from bandi.agent.states import on_parsing

    await on_parsing(ctx)
    assert ctx.terminal_reason == TerminalReason.MODEL_ERROR


@pytest.mark.asyncio
async def test_executing_without_tools_returns_tool_error():
    ctx = make_ctx(
        messages=[Message(role=MessageRole.USER, content="hi")],
    )
    ctx.current_tool_calls = None
    from bandi.agent.states import on_executing

    await on_executing(ctx)
    assert ctx.terminal_reason == TerminalReason.TOOL_ERROR


@pytest.mark.asyncio
async def test_tool_executor_abort():
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="bash", arguments={"cmd": "rm -rf /"})],
        )
    )
    # 硬撞终止（本契约钉「abort 最终会终止」；总结机会语义见
    # test_contract_tool_failure_recovery.py）
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="call_2", name="bash", arguments={"cmd": "rm -rf /"})],
        )
    )
    ctx = make_ctx(
        messages=[Message(role=MessageRole.USER, content="danger")],
        provider=provider,
    )
    executor = MockToolExecutor()
    executor.set_results(
        [
            ToolResult(
                tool_call_id="call_1",
                tool_name="bash",
                success=False,
                output="",
                error="blocked",
                should_abort=True,
            )
        ]
    )
    ctx.tool_executor = executor
    loop = AgentLoop()
    reason = await loop.run(ctx)
    assert reason == TerminalReason.TOOL_ERROR


@pytest.mark.asyncio
async def test_state_machine_transitions():
    sm = build_default_machine()
    ctx = make_ctx(messages=[Message(role=MessageRole.USER, content="hi")])
    assert sm.next(ctx) == "thinking"
    empty_ctx = make_ctx(messages=[])
    assert sm.next(empty_ctx) == "terminated"


async def make_token_response(prompt: int, completion: int) -> LLMResponse:
    from bandi.core.message import TokenUsage

    return LLMResponse(
        content="ok", usage=TokenUsage(prompt_tokens=prompt, completion_tokens=completion)
    )


@pytest.mark.asyncio
async def test_token_budget_exceeded_terminates():
    from bandi.core.message import TokenUsage

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="bash", arguments={"cmd": "ls"})],
            usage=TokenUsage(prompt_tokens=5000, completion_tokens=1000),
        )
    )
    provider.add_response(await make_token_response(3000, 2000))
    config = AgentConfig(token_budget_limit=10000)
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="run two calls")],
        provider=provider,
        config=config,
        bus=RecordingBus(),
    )
    ctx.tool_executor = MockToolExecutor()
    loop = AgentLoop()
    reason = await loop.run(ctx)
    assert reason == TerminalReason.TOKEN_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_token_budget_disabled_when_negative():
    provider = MockProvider()
    # 阈值更新（reserve 下限 20K）：token 量降至压缩阈值以下——
    # 本测试聚焦「负预算不终止」，不应误入压缩路径（100K 会触发并需摘要 mock）
    provider.add_response(await make_token_response(5000, 5000))
    provider.add_response(await make_token_response(5000, 5000))
    ctx = make_ctx(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=provider,
    )
    ctx.config.token_budget_limit = -1
    loop = AgentLoop()
    reason = await loop.run(ctx)
    assert reason == TerminalReason.COMPLETED


@pytest.mark.asyncio
async def test_parsing_early_return_when_terminal_reason_set():
    from bandi.agent.states import on_parsing

    ctx = make_ctx(
        messages=[Message(role=MessageRole.USER, content="hi")],
    )
    ctx.terminal_reason = TerminalReason.TOKEN_BUDGET_EXHAUSTED
    ctx.current_response = LLMResponse(content="should be ignored")
    await on_parsing(ctx)
    # terminal_reason unchanged and no message appended
    assert ctx.terminal_reason == TerminalReason.TOKEN_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_recording_bus_events():
    provider = MockProvider()
    provider.add_response(LLMResponse(content="hello"))
    bus = RecordingBus()
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    loop = AgentLoop()
    await loop.run(ctx)
    event_types = [e.type for e in bus.events]
    assert EventType.TURN_STARTED in event_types
    assert EventType.TERMINATED in event_types


# ── RecoveryManager ─────────────────────────────────────────────


class FixedRecoveryManager:
    def __init__(self, action: RecoveryAction):
        self._action = action
        self.handle_count = 0
        self.on_turn_completed_count = 0
        self.errors: list[Exception] = []

    async def handle(self, error: Exception, layer: str) -> RecoveryAction:
        self.handle_count += 1
        self.errors.append(error)
        return self._action

    def on_turn_completed(self) -> None:
        self.on_turn_completed_count += 1


class TestRecoveryManager:
    @pytest.mark.asyncio
    async def test_classify_and_handle(self) -> None:
        rm = RecoveryManager(escalate_threshold=10)
        assert await rm.handle(ValueError("bad"), "test") == RecoveryAction.RETRY
        assert await rm.handle(TypeError("bad"), "test") == RecoveryAction.TERMINATE
        assert await rm.handle(TimeoutError("timeout"), "test") == RecoveryAction.RETRY
        assert (
            await rm.handle(NotImplementedError("nope"), "test") == RecoveryAction.CONTINUE_DEGRADED
        )
        assert await rm.handle(RuntimeError("unknown"), "test") == RecoveryAction.RETRY

    @pytest.mark.asyncio
    async def test_consecutive_errors_escalate(self) -> None:
        rm = RecoveryManager(escalate_threshold=10)
        for _ in range(9):
            action = await rm.handle(TimeoutError("t"), "test")
            assert action == RecoveryAction.RETRY
        action = await rm.handle(TimeoutError("t"), "test")
        assert action == RecoveryAction.ESCALATE

    @pytest.mark.asyncio
    async def test_on_turn_completed_decrements_counter(self) -> None:
        rm = RecoveryManager(escalate_threshold=10)
        for _ in range(5):
            await rm.handle(TimeoutError("t"), "test")
        rm.on_turn_completed()
        assert rm.consecutive_errors == {"test": 4}, "B5a 7.7：轮完成递减而非全清"
        action = await rm.handle(TimeoutError("t"), "test")
        assert action == RecoveryAction.RETRY

    @pytest.mark.asyncio
    async def test_degradation_tracks_layer(self) -> None:
        rm = RecoveryManager(escalate_threshold=10)
        await rm.handle(NotImplementedError("no bash"), "bash")
        assert rm.degradation.is_degraded("bash")
        assert not rm.degradation.is_degraded("http")
        rm.degradation.recover("bash")
        assert not rm.degradation.is_degraded("bash")


class TestLoopRecovery:
    @pytest.mark.asyncio
    async def test_retry_retries_handler(self) -> None:
        loop = AgentLoop()
        calls = 0

        async def _raise_once(_ctx):
            nonlocal calls
            calls += 1
            if calls < 2:
                raise ValueError("retry")

        loop._handlers["init"] = _raise_once
        provider = MockProvider()
        provider.add_response(LLMResponse(content="ok"))
        ctx = make_ctx(
            messages=[Message(role=MessageRole.USER, content="hi")],
            provider=provider,
        )
        ctx.recovery_manager = FixedRecoveryManager(RecoveryAction.RETRY)
        reason = await loop.run(ctx)
        assert calls == 2
        assert reason == TerminalReason.COMPLETED

    @pytest.mark.asyncio
    async def test_escalate_terminates(self) -> None:
        loop = AgentLoop()

        async def _raise(_ctx):
            raise ValueError("escalate")

        loop._handlers["init"] = _raise
        ctx = make_ctx(messages=[Message(role=MessageRole.USER, content="hi")])
        ctx.recovery_manager = FixedRecoveryManager(RecoveryAction.ESCALATE)
        reason = await loop.run(ctx)
        assert reason == TerminalReason.ESCALATED

    @pytest.mark.asyncio
    async def test_terminate_sets_model_error(self) -> None:
        loop = AgentLoop()

        async def _raise(_ctx):
            raise ValueError("terminate")

        loop._handlers["init"] = _raise
        ctx = make_ctx(messages=[Message(role=MessageRole.USER, content="hi")])
        ctx.recovery_manager = FixedRecoveryManager(RecoveryAction.TERMINATE)
        reason = await loop.run(ctx)
        assert reason == TerminalReason.MODEL_ERROR

    @pytest.mark.asyncio
    async def test_no_manager_sets_model_error(self) -> None:
        loop = AgentLoop()

        async def _raise(_ctx):
            raise ValueError("no manager")

        loop._handlers["init"] = _raise
        ctx = make_ctx(messages=[Message(role=MessageRole.USER, content="hi")])
        ctx.recovery_manager = None
        reason = await loop.run(ctx)
        assert reason == TerminalReason.MODEL_ERROR

    @pytest.mark.asyncio
    async def test_on_turn_completed_called_on_success(self) -> None:
        loop = AgentLoop()
        provider = MockProvider()
        provider.add_response(LLMResponse(content="ok"))
        ctx = make_ctx(
            messages=[Message(role=MessageRole.USER, content="hi")],
            provider=provider,
        )
        rm = FixedRecoveryManager(RecoveryAction.RETRY)
        ctx.recovery_manager = rm
        reason = await loop.run(ctx)
        assert reason == TerminalReason.COMPLETED
        assert rm.on_turn_completed_count >= 1
