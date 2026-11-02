# 功能：契约 — 会话模式权威语义（显式模式唯一权威，意图路由仅作提示不得覆盖；双模式 execute/plan）
import pytest

from bandi.agent.base import ExecutionContext
from bandi.agent.loop import AgentLoop
from bandi.agent.mode_manager import ModeManager
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import Message, MessageRole

_VALID_MODES = ("execute", "plan")


class _StubProvider:
    model_name = "mock"
    protocol = "openai"

    async def complete(self, messages: list, tools: list | None = None, **kwargs):
        raise AssertionError("契约测试不应触发 LLM 调用")


class _FakeResult:
    def __init__(self, mode: str, messages: list[Message], intent_hint: str = ""):
        self.mode = mode
        self.messages = messages
        self.intent_hint = intent_hint
        self.skills_injected: list[str] = []


class _FakePerception:
    def __init__(self, intent_hint: str = ""):
        self._intent_hint = intent_hint
        self._messages = [Message(role=MessageRole.SYSTEM, content="fake system")]
        self.seen_session_mode: str | None = None

    async def process(
        self,
        raw_input: str,
        session_id: str | None = None,
        session_mode: str = "execute",
        surface_tools: list[str] | None = None,
    ):
        self.seen_session_mode = session_mode
        return _FakeResult(session_mode, self._messages, intent_hint=self._intent_hint)


def _make_ctx(mode_manager: ModeManager | None) -> ExecutionContext:
    ctx = ExecutionContext(
        messages=[],
        provider=_StubProvider(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    if mode_manager is not None:
        ctx.meta["mode_manager"] = mode_manager
        ctx.meta["mode"] = mode_manager.mode
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", list(_VALID_MODES))
async def test_intent_hint_never_overrides_explicit_mode(explicit: str) -> None:
    hint = "plan" if explicit == "execute" else "execute"
    mode_manager = ModeManager(initial_mode=explicit)
    ctx = _make_ctx(mode_manager)
    ctx.raw_input = "hello"
    loop = AgentLoop(perception=_FakePerception(intent_hint=hint))
    await loop._run_perception(ctx)
    assert mode_manager.mode == explicit, "意图推断不得覆盖显式会话模式"
    assert ctx.meta["mode"] == explicit, "ctx.meta mode 必须保持会话权威值"


@pytest.mark.asyncio
async def test_perception_receives_session_mode() -> None:
    perception = _FakePerception()
    ctx = _make_ctx(ModeManager(initial_mode="plan"))
    ctx.raw_input = "hello"
    loop = AgentLoop(perception=perception)
    await loop._run_perception(ctx)
    assert perception.seen_session_mode == "plan", (
        "感知层必须收到会话真实模式用于 system prompt 渲染"
    )


@pytest.mark.asyncio
async def test_invalid_initial_mode_falls_back_execute() -> None:
    for bogus in ("bogus", "ask"):
        mode_manager = ModeManager(initial_mode=bogus)
        assert mode_manager.mode == "execute", f"非法初始模式 {bogus} 必须兜底 execute"


@pytest.mark.asyncio
async def test_no_manager_meta_defaults_result_mode() -> None:
    ctx = _make_ctx(None)
    ctx.raw_input = "hello"
    loop = AgentLoop(perception=_FakePerception())
    await loop._run_perception(ctx)
    assert ctx.meta["mode"] == "execute", "无 mode_manager 时 meta 回退 result.mode"


@pytest.mark.asyncio
async def test_real_router_plan_keyword_does_not_switch_mode() -> None:
    from bandi.perception.pipeline import PerceptionPipeline

    mode_manager = ModeManager(initial_mode="execute")
    ctx = _make_ctx(mode_manager)
    ctx.raw_input = "帮我制定一个学习方案"
    pipeline = PerceptionPipeline()
    loop = AgentLoop(perception=pipeline)
    await loop._run_perception(ctx)
    result = ctx.meta.get("perception")
    assert mode_manager.mode == "execute", "plan 关键词命中不得切换会话模式"
    assert result is not None and result.intent_hint == "plan", "意图提示必须保留为观测字段"
    assert ctx.meta["mode"] == "execute"


def test_intent_router_returns_only_two_modes() -> None:
    from bandi.perception.intent_router import IntentRouter

    router = IntentRouter()
    for text in (
        "这个工具怎么用",
        "Python 是什么",
        "这个函数在哪里定义的",
        "what is this",
        "帮我制定一个学习方案",
        "直接执行部署",
    ):
        assert router.route(Message(role="user", content=text)) in _VALID_MODES, (
            f"route 结果必须限定双模式: {text}"
        )


def test_mode_prompts_have_two_modes_only() -> None:
    from bandi.agent.prompt_harness.prompt_loader import PromptLoader

    modes = PromptLoader().get_mode_prompts()
    assert set(modes) == set(_VALID_MODES), f"系统提示词模式段必须与双模式一致: {sorted(modes)}"


def test_ask_mode_no_longer_restricts_anywhere() -> None:
    import asyncio

    from bandi.core.message import ToolCall
    from bandi.tools.permissions_pipeline import PermissionsPipeline
    from bandi.tools.registry import ToolRegistry

    registry = ToolRegistry()
    assert registry.list_schemas(mode="ask") == registry.list_schemas(mode="execute"), (
        "ask 不再是合法模式——schema 列表不得因 ask 收缩"
    )
    tc = ToolCall(id="t1", name="ctx_inspect", arguments={})
    pipeline = PermissionsPipeline()
    v_ask = asyncio.run(pipeline.check(tc, None, mode="ask"))
    v_execute = asyncio.run(pipeline.check(tc, None, mode="execute"))
    assert v_ask.allow == v_execute.allow, "ask 模式不得再触发权限层特殊拒绝"
