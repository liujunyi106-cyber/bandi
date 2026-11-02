# 功能：契约——帧引擎接入 agent loop（loop 消费 SOP 帧状态并注入请求）。
# 钉行为：有激活帧时 loop 组装请求注入 [SOP 帧状态]（含技能/当前节点/待收集字段）；
# 无帧/损坏帧不注入且不炸主链（破坏者）；帧状态未变不重复注入；
# 帧推进后旧状态消息被替换（恒单条不膨胀）；被裁后下轮自然重注入（消息存在性检查）。
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.agent.base import ExecutionContext
from bandi.agent.loop import AgentLoop
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import Message, MessageRole
from bandi.skills.card import SkillCard
from bandi.skills.frame_store import save_frames
from bandi.skills.runtime import SkillRuntime

_FRAME_PREFIX = "[SOP 帧状态]"


class _StubProvider:
    model_name = "mock"
    protocol = "openai"

    async def complete(self, messages: list, tools: list | None = None, **kwargs):
        raise AssertionError("契约测试不应触发 LLM 调用")


class _FakePerception:
    _skill_injector = None

    async def process(
        self, raw_input: str, session_id: str | None = None, session_mode: str = "execute"
    ):
        raise AssertionError("已有 system 的会话不得重入感知层")


def _card() -> SkillCard:
    return SkillCard(
        **{
            "skill_id": "sk_frame_case",
            "name": "帧状态用例",
            "version": "1.0.0",
            "description": "三节点流程（中间节点非终态，供推进替换测试）。",
            "required_info": ["订单号"],
            "nodes": [
                {
                    "node_id": "collect_order",
                    "name": "收集",
                    "type": "collect_info",
                    "instruction": "收集订单号。",
                    "expected_user_info": ["订单号"],
                },
                {
                    "node_id": "confirm_shipment",
                    "name": "确认",
                    "type": "action",
                    "instruction": "确认发货。",
                },
                {
                    "node_id": "reply",
                    "name": "回复",
                    "type": "response",
                    "instruction": "回复用户。",
                },
            ],
            "edges": [
                {
                    "source_node_id": "collect_order",
                    "next_node_id": "confirm_shipment",
                    "priority": 0,
                },
                {"source_node_id": "confirm_shipment", "next_node_id": "reply", "priority": 0},
            ],
            "start_node_id": "collect_order",
            "terminal_node_ids": ["reply"],
        }
    )


def _write_frames(frames_dir: Path, scope: str, task_id: str = "t1") -> None:
    runtime = SkillRuntime()
    stack = runtime.start(runtime.new_stack(scope), _card(), task_id=task_id)
    save_frames(stack, str(frames_dir))


def _make_ctx(frames_dir: Path, scope: str) -> ExecutionContext:
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.SYSTEM, content="身份保护 prompt")],
        provider=_StubProvider(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.raw_input = "你好"
    ctx.meta["session_id"] = "sess-frame"
    ctx.meta["frame_scope"] = scope
    ctx.meta["frames_dir"] = str(frames_dir)
    return ctx


def _frame_messages(ctx: ExecutionContext) -> list[Message]:
    return [
        m
        for m in ctx.messages
        if m.role == MessageRole.SYSTEM and (m.content or "").startswith(_FRAME_PREFIX)
    ]


# 有激活帧 → 注入 [SOP 帧状态]（含技能/节点/待收集字段），原 system 保留
@pytest.mark.asyncio
async def test_active_frame_state_is_injected(tmp_path: Path) -> None:
    frames_dir = tmp_path / "frames"
    _write_frames(frames_dir, "sess-frame")
    ctx = _make_ctx(frames_dir, "sess-frame")

    await AgentLoop(perception=_FakePerception())._run_perception(ctx)

    frames = _frame_messages(ctx)
    assert len(frames) == 1
    content = frames[0].content
    assert "sk_frame_case" in content
    assert "collect_order" in content
    assert "订单号" in content
    assert any(m.content == "身份保护 prompt" for m in ctx.messages)


# 无帧文件 → 不注入
@pytest.mark.asyncio
async def test_no_frame_file_no_injection(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path / "frames", "sess-frame")

    await AgentLoop(perception=_FakePerception())._run_perception(ctx)

    assert _frame_messages(ctx) == []


# 帧状态未变 → 连续两轮不重复注入（幂等）
@pytest.mark.asyncio
async def test_unchanged_state_is_not_reinjected(tmp_path: Path) -> None:
    frames_dir = tmp_path / "frames"
    _write_frames(frames_dir, "sess-frame")
    ctx = _make_ctx(frames_dir, "sess-frame")
    loop = AgentLoop(perception=_FakePerception())

    await loop._run_perception(ctx)
    await loop._run_perception(ctx)

    assert len(_frame_messages(ctx)) == 1


# 帧推进后旧状态消息被替换（恒单条，内容更新为新节点）
@pytest.mark.asyncio
async def test_advanced_frame_replaces_old_state(tmp_path: Path) -> None:
    frames_dir = tmp_path / "frames"
    _write_frames(frames_dir, "sess-frame")
    ctx = _make_ctx(frames_dir, "sess-frame")
    loop = AgentLoop(perception=_FakePerception())
    await loop._run_perception(ctx)

    runtime = SkillRuntime()
    from bandi.skills.frame_store import load_frames

    stack = load_frames("sess-frame", str(frames_dir))
    assert stack is not None
    save_frames(runtime.advance(stack, _card()), str(frames_dir))

    await loop._run_perception(ctx)

    frames = _frame_messages(ctx)
    assert len(frames) == 1, "状态更新必须替换旧消息（不得堆积）"
    assert "confirm_shipment" in frames[0].content


# 破坏者：帧文件损坏 → 不注入且不抛（主链不受影响）
@pytest.mark.asyncio
async def test_corrupt_frame_file_is_tolerated(tmp_path: Path) -> None:
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    (frames_dir / "sess-frame.frames.json").write_text("{ not json", encoding="utf-8")
    ctx = _make_ctx(frames_dir, "sess-frame")

    await AgentLoop(perception=_FakePerception())._run_perception(ctx)

    assert _frame_messages(ctx) == []


# 帧状态消息被裁掉后，下轮重新注入（压缩闭环）
@pytest.mark.asyncio
async def test_reinject_after_message_dropped(tmp_path: Path) -> None:
    frames_dir = tmp_path / "frames"
    _write_frames(frames_dir, "sess-frame")
    ctx = _make_ctx(frames_dir, "sess-frame")
    loop = AgentLoop(perception=_FakePerception())
    await loop._run_perception(ctx)

    ctx.messages[:] = [m for m in ctx.messages if not (m.content or "").startswith(_FRAME_PREFIX)]
    await loop._run_perception(ctx)

    assert len(_frame_messages(ctx)) == 1
