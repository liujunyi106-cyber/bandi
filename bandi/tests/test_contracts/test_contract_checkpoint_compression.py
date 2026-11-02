# + L-2a RETRY 分支落盘/事件 + 澄清消息落库；行为不变式钉住。
from typing import Any

import pytest

from bandi.core.message import Message, MessageRole

# ── fixtures / helpers ───────────────────────────────────────────────────


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _make_ctx(with_pipeline: bool = True) -> Any:
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.memory.compression_pipeline import CompressionPipeline

    config = AgentConfig(context_window=200, compression_threshold=0.5)
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="任务")],
        provider=object(),
        config=config,
        bus=_RecordingBus(),
    )
    if with_pipeline:
        pipeline = CompressionPipeline()
        pipeline.record_progressive_savings(5000, 4900)
        pipeline.record_progressive_savings(5000, 4900)
        for t in (100, 150, 300):
            pipeline._scheduler.record_tokens(t)
        ctx.compress.pipeline = pipeline
    return ctx


def test_query_state_carries_compression_context() -> None:
    """pipeline 有压缩状态时快照必须带出 token 历史 + 防抖连击。"""
    ctx = _make_ctx()
    qs = ctx.to_query_state()
    assert qs.compress_token_history == (100, 150, 300)
    assert qs.compress_low_returns == 2


def test_query_state_defaults_when_pipeline_none() -> None:
    """恢复后 pipeline 为 None 的 ctx 快照 → 空默认值不炸。"""
    ctx = _make_ctx(with_pipeline=False)
    qs = ctx.to_query_state()
    assert qs.compress_token_history == ()
    assert qs.compress_low_returns == 0


def test_restore_rebuilds_pipeline_state() -> None:
    """含压缩上下文的 state_json 恢复 → pipeline 重建且 scheduler/防抖就位
    （下轮 fast_growth 判定与防抖降级跨进程连续）。"""
    import json

    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig

    state = {
        "current_state": "thinking",
        "compress_ok": True,
        "compress_token_history": [100, 150, 300],
        "compress_low_returns": 2,
    }
    config = AgentConfig(context_window=200, compression_threshold=0.5)
    ctx = ExecutionContext(messages=[], provider=object(), config=config, bus=_RecordingBus())
    from bandi.core.agent_runner import _restore_checkpoint_state

    _restore_checkpoint_state(ctx, json.loads(json.dumps(state)))
    pipeline = ctx.compress.pipeline
    assert pipeline is not None, "RED: 恢复未重建 pipeline"
    assert tuple(pipeline.token_history()) == (100, 150, 300)
    assert pipeline.low_return_count == 2
    assert pipeline.llm_degraded() is False  # 2 < 3 未达防抖线


def test_restore_without_compression_fields_keeps_none() -> None:
    """旧格式 checkpoint（无压缩字段）恢复 → 不凭空创建 pipeline 且不炸
    （旧文件兼容——D-g4 transient 边界外的向后兼容承诺）。"""
    import json

    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig

    state = {"current_state": "thinking", "compress_ok": True}
    config = AgentConfig(context_window=200, compression_threshold=0.5)
    ctx = ExecutionContext(messages=[], provider=object(), config=config, bus=_RecordingBus())
    from bandi.core.agent_runner import _restore_checkpoint_state

    _restore_checkpoint_state(ctx, json.loads(json.dumps(state)))
    assert ctx.compress.pipeline is None
    assert ctx.compress.compress_ok is True


def test_full_roundtrip_via_store(tmp_path: Any) -> None:
    """生产 CheckpointStore save→load→restore 全链路：压缩状态无损往返。"""
    import json

    from bandi.agent.base import ExecutionContext
    from bandi.agent.checkpoint import Checkpoint, CheckpointStore
    from bandi.core.agent_runner import _restore_checkpoint_state
    from bandi.core.config import AgentConfig

    ctx = _make_ctx()
    qs = ctx.to_query_state()
    db = str(tmp_path / "cp.db")
    store = CheckpointStore(db)
    store.save(
        Checkpoint(session_id="s1", turn_index=3, state_json=json.dumps(qs.__dict__, default=tuple))
    )
    loaded = store.load(store.list_by_session("s1")[-1]["id"])
    assert loaded is not None
    config = AgentConfig(context_window=200, compression_threshold=0.5)
    ctx2 = ExecutionContext(messages=[], provider=object(), config=config, bus=_RecordingBus())
    _restore_checkpoint_state(ctx2, json.loads(loaded.state_json))
    pipeline = ctx2.compress.pipeline
    assert pipeline is not None
    assert tuple(pipeline.token_history()) == (100, 150, 300)
    assert pipeline.low_return_count == 2
    store.close()


def test_maintenance_fields_persisted_in_snapshot() -> None:
    """修复：轮数/空闲计数器必须进 QueryState 快照。

    此前 D-g4 维持 transient 语义，ctx 每轮重建后归零，「满 10 轮 / 空闲
    30 分钟」维护触发跨轮永不生效；修复后随快照持久化（wall-clock 口径）。
    """
    from bandi.agent.base import QueryState

    assert "turns_since_maintenance" in QueryState.__dataclass_fields__
    assert "last_active_ts" in QueryState.__dataclass_fields__
    state = QueryState()
    assert state.turns_since_maintenance == 0
    assert state.last_active_ts is None


def test_checkpoint_stops_storing_messages_json(tmp_path: Any) -> None:
    """反向钉住：checkpoint 不再冗余存 messages_json——消息持久化唯一来源
    是 session store；恢复链只读 state_json（生产零读取方实证）。"""
    import json

    from bandi.agent.base import ExecutionContext
    from bandi.agent.checkpoint import CheckpointStore
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig

    db = str(tmp_path / "cp.db")
    store = CheckpointStore(db)
    loop = AgentLoop(checkpoint_store=store, checkpoint_interval=1)
    config = AgentConfig(context_window=200, compression_threshold=0.5)
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.TOOL, content="工具结果", tool_call_id="c1")],
        provider=object(),
        config=config,
        bus=_RecordingBus(),
    )
    ctx.meta["session_id"] = "s1"
    ok = loop._persist_checkpoint(ctx, turns_taken=1)
    assert ok is True
    latest = store.list_by_session("s1")[-1]["id"]
    cp = store.load(latest)
    assert cp is not None
    assert json.loads(cp.messages_json or "[]") == [], (
        "RED: messages_json 应停写（死数据），实际仍写入"
    )
    store.close()


# ── L-2a 组：RETRY 分支先落盘再 continue ─────────────────────────────────


class _RetryManager:
    async def handle(self, error: Exception, layer: str) -> Any:
        from bandi.agent.base import RecoveryAction

        return RecoveryAction.RETRY


def _retry_scene(tmp_path: Any) -> tuple[Any, Any, Any]:
    """构造「thinking 抛错 + RETRY」的 AgentLoop 场景。"""
    from bandi.agent.base import ExecutionContext
    from bandi.agent.checkpoint import CheckpointStore
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig

    store = CheckpointStore(str(tmp_path / "cp.db"))
    bus = _RecordingBus()

    async def boom(ctx: Any) -> None:
        raise RuntimeError("boom")

    loop = AgentLoop(checkpoint_store=store, checkpoint_interval=1)
    loop._handlers["thinking"] = boom
    config = AgentConfig(context_window=200, compression_threshold=0.5)
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="任务")],
        provider=object(),
        config=config,
        bus=bus,
    )
    ctx.recovery_manager = _RetryManager()
    ctx.meta["session_id"] = "s1"
    ctx.current_state = "thinking"
    return loop, ctx, store


@pytest.mark.asyncio
async def test_retry_persists_checkpoint_before_continue(tmp_path: Any) -> None:
    """破坏者视角：裸 continue 会跳过保存点——RETRY 必须「先落盘再重试」
    （dsh 事务协议：副作用前确保持久化）。"""

    loop, ctx, store = _retry_scene(tmp_path)
    await loop._run_loop(ctx, max_turns=2, turns_taken=0)
    entries = store.list_by_session("s1")
    assert entries, "RED: RETRY 分支未落盘 checkpoint"
    store.close()


@pytest.mark.asyncio
async def test_retry_emits_state_changed(tmp_path: Any) -> None:
    """RETRY 分支必须发 STATE_CHANGED 事件（观测性：重试可见）。"""
    from bandi.core.events import EventType

    loop, ctx, store = _retry_scene(tmp_path)
    await loop._run_loop(ctx, max_turns=2, turns_taken=0)
    reasons = [
        e.data.get("reason")
        for e in ctx.bus.events
        if getattr(e, "type", None) == EventType.STATE_CHANGED and "reason" in e.data
    ]
    assert "retry" in reasons, f"RED: 未发 retry STATE_CHANGED，events={len(ctx.bus.events)}"
    store.close()


# ── 组：澄清消息落库 ─────────────────────────────────────────────────


class _FakeSessionStore:
    def __init__(self) -> None:
        self.saved: list[tuple[str, Message]] = []

    def append_message(self, session_id: str, msg: Message) -> None:
        self.saved.append((session_id, msg))


def _clarify_ctx(store: Any) -> Any:
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig

    config = AgentConfig(context_window=200, compression_threshold=0.5)
    ctx = ExecutionContext(messages=[], provider=object(), config=config, bus=_RecordingBus())
    ctx.clarification_count = 0
    ctx.meta["session_id"] = "s1"
    if store is not None:
        ctx.meta["session_store"] = store
    return ctx


@pytest.mark.asyncio
async def test_clarification_message_persisted_to_session(monkeypatch: Any) -> None:
    """低置信度触发的澄清提示必须同步落 session store——否则中断恢复后丢失。"""
    from bandi.agent import states as states_mod
    from bandi.core.message import LLMResponse

    monkeypatch.setattr(states_mod, "detect_confidence", lambda content: 0.4)
    fake = _FakeSessionStore()
    ctx = _clarify_ctx(fake)
    resp = LLMResponse(content="不太确定的回答", confidence=0.9)
    states_mod._apply_confidence(ctx, resp)
    assert any("[置信度提示]" in m.content for _, m in fake.saved), "RED: 澄清消息未落库"


@pytest.mark.asyncio
async def test_clarification_without_store_graceful(monkeypatch: Any) -> None:
    """meta 无 session_store（如单测/内嵌调用）时不炸、行为不变。"""
    from bandi.agent import states as states_mod
    from bandi.core.message import LLMResponse

    monkeypatch.setattr(states_mod, "detect_confidence", lambda content: 0.4)
    ctx = _clarify_ctx(None)
    resp = LLMResponse(content="不太确定的回答", confidence=0.9)
    states_mod._apply_confidence(ctx, resp)
    assert ctx.clarification_injected is True
