"""
  1. call_model ：on_thinking 主调用 / on_thinking 截断恢复循环（两处均须走 deps）
  2. autocompact ：on_compressing 正常档 / _reactive_compact_retry 响应式压缩
  3. microcompact ：on_compressing 维护档（maintenance=True，免费档零 LLM）
  4. new_id ：AgentLoop._persist_checkpoint 的 Checkpoint id

破坏者视角：stub 注入后对应路径必须命中 stub 且真实 provider 零触碰
（SentinelProvider.complete 记录并抛错——任何直连残留立即暴露）；
未注入时 get_query_deps 惰性构建生产实现并回填缓存（行为与注入前一致）。
"""

import dataclasses
import json
from typing import Any

import pytest

from bandi.agent.base import CompressionState, ExecutionContext
from bandi.core.config import AgentConfig, BandiConfig
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage


class _RecordingBus:
    """功能：契约测试用事件总线，只记录不外发。"""

    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)


class _SentinelProvider:
    """功能：破坏者哨兵 provider——complete/stream 一旦被触碰即记录并抛错
    证明生产代码绕过 deps 直连了外部效果。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def protocol(self) -> str:
        return "openai"

    @property
    def model_name(self) -> str:
        return "sentinel"

    @property
    def config(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    async def complete(self, messages: list, tools: Any = None, **kwargs: Any) -> LLMResponse:
        self.calls.append("complete")
        raise AssertionError(f"provider.complete 被直连（deps 未接线），消息数={len(messages)}")

    async def stream(self, messages: list, tools: Any = None):
        self.calls.append("stream")
        raise AssertionError("provider.stream 被直连")


def _make_ctx(provider: object, messages: list[Message]) -> ExecutionContext:
    config = AgentConfig(context_window=200, compression_threshold=0.5, stream_to_ui=False)
    ctx = ExecutionContext(
        messages=list(messages),
        provider=provider,
        config=config,
        bus=_RecordingBus(),  # type: ignore[arg-type]
    )
    ctx.total_tokens = sum(provider.count_tokens(m.content or "") for m in messages)
    return ctx


def _simple_messages() -> list[Message]:
    return [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
        Message(role=MessageRole.USER, content="请总结 day124x-j 的实施计划要点"),
    ]


def _make_deps(**overrides: Any) -> Any:
    """功能：构建 QueryDeps——未覆写的成员一律为「不应被调用」哨兵
    任何越权调用立即暴露（比静默 no-op 更强的破坏者语义）。"""
    from bandi.agent.deps import QueryDeps

    def _not_wired(name: str):
        async def _missing(ctx: object, *a: object, **k: object) -> object:
            raise AssertionError(f"QueryDeps.{name} 不应被调用")

        return _missing

    defaults: dict[str, Any] = {
        "call_model": _not_wired("call_model"),
        "autocompact": _not_wired("autocompact"),
        "microcompact": _not_wired("microcompact"),
        "new_id": lambda: "stub-fixed-id",
    }
    defaults.update(overrides)
    return QueryDeps(**defaults)


# ── /call_model —— on_thinking 主调用 + 截断恢复循环 ──────────────────


@pytest.mark.asyncio
async def test_call_model_stub_intercepts_thinking_and_truncation_recovery() -> None:
    """契约（call_model 族级 +）：stub 注入后 on_thinking 主调用与
    截断恢复循环（finish_reason=length 触发的续作重试）都必须走 stub
    provider 零触碰（破坏者：SentinelProvider 记录一切直连尝试）。"""
    from bandi.agent.states import on_thinking

    provider = _SentinelProvider()
    ctx = _make_ctx(provider, _simple_messages())
    calls: list[int] = []

    async def fake_call_model(ctx: ExecutionContext, messages: list[dict]) -> LLMResponse:
        calls.append(len(messages))
        if len(calls) == 1:
            return LLMResponse(content="回答被截断了", finish_reason="length")
        return LLMResponse(
            content="完整回答",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=3, completion_tokens=2),
            finish_reason="stop",
        )

    ctx.query_deps = _make_deps(call_model=fake_call_model)

    await on_thinking(ctx)

    assert len(calls) == 2, f"主调用+截断恢复共 2 次（实际 {len(calls)}）"
    assert provider.calls == [], f"provider 被直连：{provider.calls}"
    assert ctx.current_content == "完整回答"


@pytest.mark.asyncio
async def test_call_model_not_injected_falls_back_to_provider() -> None:
    """契约（惰性生产回退）：未注入 deps 时 on_thinking 必须照常经 provider 完成
    （行为与 DI 改造前完全一致——MockProvider 形态的全量回归即此契约的族级证明）。"""
    from bandi.agent.states import on_thinking
    from bandi.llm.mock import MockProvider

    mock_provider = MockProvider()
    ctx = _make_ctx(mock_provider, _simple_messages())

    await on_thinking(ctx)

    assert ctx.current_response is not None
    assert ctx.current_content == "Mock response"


# ── autocompact —— on_compressing 正常档 ────────────────────────────────


@pytest.mark.asyncio
async def test_autocompact_stub_intercepts_on_compressing_normal_stage() -> None:
    """契约（autocompact ）：token 达阈值进入正常压缩时必须走 deps.autocompact
    pipeline 与 provider 均不得被触碰；压缩产物与 token 重算生效。"""
    from bandi.agent.deps import CompactOutcome
    from bandi.agent.states import on_compressing
    from bandi.memory.compression_pipeline import CompressionStats

    provider = _SentinelProvider()
    # 触发条件用真实大消息而非手调 total_tokens——判定已统一为 live 实时值。
    ctx = _make_ctx(
        provider,
        [
            Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
            Message(role=MessageRole.USER, content="x" * 460),
        ],
    )
    ctx.compress.continuity_injected = True  # 屏蔽锚点注入对产物断言的干扰

    compacted = [Message(role=MessageRole.SYSTEM, content="[上下文摘要] 五节压缩产物")]
    auto_calls: list[int] = []

    async def fake_autocompact(ctx: ExecutionContext) -> CompactOutcome:
        auto_calls.append(len(ctx.messages))
        return CompactOutcome(
            messages=list(compacted), success=True, stats=CompressionStats(stage="micro")
        )

    ctx.query_deps = _make_deps(autocompact=fake_autocompact)

    await on_compressing(ctx)

    assert len(auto_calls) == 1, f"autocompact 恰调一次（实际 {len(auto_calls)}）"
    assert provider.calls == [], f"pipeline 内部直连 provider：{provider.calls}"
    assert ctx.messages == compacted, "压缩产物必须是 stub 返回的消息列表"
    assert ctx.total_tokens == sum(provider.count_tokens(m.content or "") for m in compacted), (
        "压缩成功后必须按产物重算 total_tokens"
    )


# ── microcompact —— on_compressing 维护档 ───────────────────────────────


@pytest.mark.asyncio
async def test_microcompact_stub_intercepts_maintenance_turn() -> None:
    """契约（microcompact ）：轮数维护到期（token 未达阈值）触发的免费档压缩
    必须走 deps.microcompact 且全程零 LLM 调用；成功后维护计数器重置（g 日语义）。"""
    from bandi.agent.deps import CompactOutcome
    from bandi.agent.states import on_compressing
    from bandi.memory.compression_pipeline import CompressionStats

    provider = _SentinelProvider()
    ctx = _make_ctx(provider, _simple_messages())
    ctx.total_tokens = 10  # 远低于阈值 → maintenance_mode
    ctx.compress.turns_since_maintenance = 50  # 轮满 → is_maintenance_due=True
    ctx.compress.continuity_injected = True

    trimmed = [Message(role=MessageRole.SYSTEM, content="micro 修剪产物")]
    micro_calls: list[int] = []

    async def fake_microcompact(ctx: ExecutionContext) -> CompactOutcome:
        micro_calls.append(1)
        return CompactOutcome(
            messages=list(trimmed), success=True, stats=CompressionStats(stage="micro")
        )

    ctx.query_deps = _make_deps(microcompact=fake_microcompact)

    await on_compressing(ctx)

    assert micro_calls == [1], "维护档必须走 deps.microcompact"
    assert provider.calls == [], "维护免费档禁止任何 LLM 调用"
    assert ctx.compress.turns_since_maintenance == 0, "维护成功后计数器必须重置"
    assert ctx.messages == trimmed


# ── autocompact —— reactive 响应式压缩路径 ─────────────────────────────


@pytest.mark.asyncio
async def test_reactive_compact_routes_through_deps_autocompact() -> None:
    """契约（autocompact ）：响应式压缩重试必须经 deps.autocompact 执行压缩
    （与自动压缩同源分发）。超长触发链路由 error_driven 契约覆盖，本契约钉分发事实
    ——provider 用 MockProvider 保证压缩后重试直接成功。"""
    from bandi.agent.deps import CompactOutcome
    from bandi.agent.states import _reactive_compact_retry
    from bandi.llm.mock import MockProvider
    from bandi.memory.compression_pipeline import CompressionStats

    provider = MockProvider()
    big = [
        Message(role=MessageRole.SYSTEM, content="系统提示"),
        Message(role=MessageRole.USER, content="初始任务"),
    ]
    for i in range(8):
        big.append(
            Message(
                role=MessageRole.USER,
                content=f"第{i}轮排查：logs/run_{i}.py 报 ValueError('seed')，请分析补丁方案。",
            )
        )
        big.append(Message(role=MessageRole.ASSISTANT, content=f"第{i}轮结论见 patch_{i}.diff"))
    ctx = _make_ctx(provider, big)
    ctx.compress = CompressionState()

    shrunk = [big[0], Message(role=MessageRole.SYSTEM, content="[上下文摘要] 排查历史已压缩")]
    auto_calls: list[int] = []

    async def fake_autocompact(ctx: ExecutionContext) -> CompactOutcome:
        auto_calls.append(1)
        return CompactOutcome(
            messages=list(shrunk), success=True, stats=CompressionStats(stage="progressive")
        )

    ctx.query_deps = _make_deps(autocompact=fake_autocompact)

    retry_calls: list[int] = []

    async def fake_call_model(ctx: ExecutionContext, messages: list[dict]) -> LLMResponse:
        retry_calls.append(len(messages))
        return LLMResponse(content="通过 QueryDeps 的重试", tool_calls=[])

    ctx.query_deps = _make_deps(autocompact=fake_autocompact, call_model=fake_call_model)
    result = await _reactive_compact_retry(ctx, max_attempts=2)

    assert auto_calls == [1], "reactive 压缩必须恰走一次 deps.autocompact"
    assert result is not None, "压缩缩减后重试应成功"
    assert result.content == "通过 QueryDeps 的重试"
    assert retry_calls and retry_calls[0] >= len(shrunk), (
        "压缩后的重试必须继续经 QueryDeps.call_model"
    )


# ── new_id —— checkpoint id 注入 ───────────────────────────────────────


def test_new_id_flows_into_checkpoint_id() -> None:
    """契约（new_id ）：AgentLoop._persist_checkpoint 写 checkpoint 时 id 必须
    来自 deps.new_id（可打桩确定性），不得硬编码 uuid4。"""

    class _FakeStore:
        def __init__(self) -> None:
            self.saved: list[Any] = []

        def save(self, cp: Any) -> None:
            self.saved.append(cp)

    from bandi.agent.loop import AgentLoop

    store = _FakeStore()
    agent_loop = AgentLoop(checkpoint_store=store, checkpoint_interval=1)  # type: ignore[arg-type]
    ctx = _make_ctx(_SentinelProvider(), _simple_messages())
    ctx.query_deps = _make_deps(new_id=lambda: "deterministic-id-42")
    ctx.meta["session_id"] = "sess-query-deps"

    ok = agent_loop._persist_checkpoint(ctx, turns_taken=1)

    assert ok is True
    assert store.saved, "checkpoint 必须落盘"
    assert store.saved[0].id == "deterministic-id-42", (
        f"checkpoint.id 必须来自 new_id（实际 {store.saved[0].id}）"
    )


# ── 惰性生产回退 + 快照边界 ─────────────────────────────────────────────────


def test_get_query_deps_lazy_build_and_cached() -> None:
    """契约：未注入时 get_query_deps 惰性构建生产实现并回填 ctx（二次取同实例）
    生产装配点（factory/subagent/agent_runner）无需强制改动。"""
    from bandi.agent.deps import get_query_deps

    ctx = _make_ctx(_SentinelProvider(), [])
    assert ctx.query_deps is None

    deps = get_query_deps(ctx)

    assert deps is not None
    assert ctx.query_deps is deps, "首次构建后必须回填缓存"
    assert get_query_deps(ctx) is deps, "二次获取必须复用同实例"


def test_make_default_deps_all_four_members_present() -> None:
    """契约：make_default_deps 四依赖齐全且可调用——缺任一 = 打桩面不完整。"""
    from bandi.agent.deps import make_default_deps

    deps = make_default_deps()

    assert callable(deps.call_model)
    assert callable(deps.autocompact)
    assert callable(deps.microcompact)
    assert callable(deps.new_id)


def test_query_state_snapshot_excludes_query_deps() -> None:
    """契约（transient 边界）：query_deps 含函数引用不可序列化，必须排除在"""
    ctx = _make_ctx(_SentinelProvider(), [])
    ctx.query_deps = _make_deps()

    qs = ctx.to_query_state()
    payload = json.dumps(dataclasses.asdict(qs), ensure_ascii=False)

    assert "query_deps" not in payload


@pytest.mark.asyncio
async def test_run_agent_turn_routes_llm_through_query_deps(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """
    QueryDeps.call_model 分发、checkpoint id 必须经 new_id 生成——计数包装默认实现
    断言分发点命中 + 回复与 checkpoint id 两个最终产物。"""
    from pathlib import Path
    from unittest.mock import patch

    from bandi.agent import deps as deps_mod
    from bandi.agent.checkpoint import CheckpointStore
    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="deps-e2e 回复",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=4, completion_tokens=2),
            finish_reason="stop",
        )
    )

    real_deps = deps_mod.make_default_deps()
    real_call_model = real_deps.call_model
    calls = {"call_model": 0}

    async def counting_call_model(ctx: Any, messages: list[dict]) -> LLMResponse:
        calls["call_model"] += 1
        return await real_call_model(ctx, messages)

    def fake_make_deps() -> Any:
        return deps_mod.QueryDeps(
            call_model=counting_call_model,
            autocompact=real_deps.autocompact,
            microcompact=real_deps.microcompact,
            new_id=lambda: "e2e-checkpoint-id",
        )

    monkeypatch.setattr(deps_mod, "make_default_deps", fake_make_deps)

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        cfg.agent.max_iterations = 1
        cfg.agent.stream_to_ui = False
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            result = await run_agent_turn(
                user_input="请给出 deps e2e 回复",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )
        ckpt_rows = CheckpointStore().list_by_session(sid)
        store.close()

    assert calls["call_model"] >= 1, "主链路 LLM 效果必须经 QueryDeps.call_model 分发"
    assert result.reply.startswith("deps-e2e 回复"), "最终回复必须是经 deps 链路产出的真实产物"
    assert ckpt_rows, "checkpoint 必须落盘"
    assert ckpt_rows[0]["id"] == "e2e-checkpoint-id", (
        f"checkpoint id 必须来自 new_id（实际 {ckpt_rows[0]['id']}）"
    )
