# 功能：契约——上下文投影。
# 钉行为：六投影渲染（工具结果/流程节点/等待输入/任务台账/相关记忆/知识命中）/
# 空源跳过与异常降级/有界（行帽+总帽）/ 变更指纹（帧状态+任务数）/ 投影消息
# 幂等 upsert（最后 SYSTEM 之后）/ states 集成（压缩后插入 + on_init 变更刷新 +
# 真实帧 store E2E）。
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from bandi.memory.context_projection import (
    MAX_PROJECTION_CHARS,
    PROJECTION_PREFIX,
    ProjectionSources,
    build_context_projection,
    projection_fingerprint,
    projection_message_index,
    upsert_projection_message,
)

# ──────────────────────────────────────────────────────────────────────
# A. 渲染族
# ──────────────────────────────────────────────────────────────────────


def _stack(*, active: Any = None, pending: list[Any] | None = None, revision: int = 1) -> Any:
    return SimpleNamespace(active=active, pending=pending or [], revision=revision)


def _frame(skill: str, node: str = "n1", *, awaiting: list[str] | None = None) -> Any:
    return SimpleNamespace(
        skill_id=skill, node_id=node, status="active", awaiting_fields=awaiting or []
    )


class TestRendering:
    def test_all_six_sections(self) -> None:
        sources = ProjectionSources(
            tool_results=lambda: ["bash: 输出摘要"],
            frames=lambda: _stack(active=_frame("发货技能", "确认订单", awaiting=["地址"])),
            tasks=lambda: [{"content": "查询库存", "status": "in_progress"}],
            memories=lambda: ["用户偏好加急发货"],
            knowledge=lambda: ["[k1] 发货政策：次日达"],
        )
        text = build_context_projection(sources)
        assert text.startswith(PROJECTION_PREFIX)
        for title in ("工具结果", "流程节点", "等待输入", "任务台账", "相关记忆", "知识命中"):
            assert f"## {title}" in text, f"缺节: {title}"
        assert "发货技能" in text and "地址" in text and "查询库存" in text

    def test_empty_sources_returns_empty(self) -> None:
        assert build_context_projection(ProjectionSources()) == ""

    def test_missing_sources_skipped(self) -> None:
        text = build_context_projection(ProjectionSources(frames=lambda: None))
        assert text == "", "源返回 None 视为空"

    def test_source_exception_degrades_to_empty(self) -> None:
        def boom() -> list[str]:
            raise RuntimeError("boom")

        text = build_context_projection(ProjectionSources(tool_results=boom, tasks=boom))
        assert text == "", "投影源异常必须降级为空（不阻断主链）"

    def test_total_cap_enforced(self) -> None:
        big = "长" * 5000
        sources = ProjectionSources(
            tool_results=lambda: [big] * 4,
            frames=lambda: _stack(
                active=_frame("技" * 300, "n"),
                pending=[
                    SimpleNamespace(skill_id="p" * 300, node_id="x", awaiting_fields=[])
                    for _ in range(3)
                ],
            ),
            tasks=lambda: [{"content": big}] * 5,
            memories=lambda: [big] * 3,
            knowledge=lambda: [big] * 3,
        )
        text = build_context_projection(sources)
        assert len(text) <= MAX_PROJECTION_CHARS + 1, "总帽必须生效"
        assert text.endswith("…")

    def test_line_cap(self) -> None:
        sources = ProjectionSources(tool_results=lambda: [f"line{i}" for i in range(10)])
        text = build_context_projection(sources)
        assert text.count("- line") == 4, "工具结果行帽=4"

    def test_frames_projection_pending(self) -> None:
        stack = _stack(
            active=_frame("S1", "a"),
            pending=[
                SimpleNamespace(skill_id=f"P{i}", node_id="x", awaiting_fields=[]) for i in range(5)
            ],
        )
        text = build_context_projection(ProjectionSources(frames=lambda: stack))
        assert "（active）" in text and "（suspended）" in text
        assert text.count("（suspended）") == 3, "挂起帧行帽=3"


# ──────────────────────────────────────────────────────────────────────
# B. 指纹族
# ──────────────────────────────────────────────────────────────────────


class TestFingerprint:
    def test_stable_and_sensitive(self) -> None:
        make = lambda node: ProjectionSources(  # noqa: E731
            frames=lambda: _stack(active=_frame("S", node), revision=2),
            tasks=lambda: [{"content": "t1"}],
        )
        assert projection_fingerprint(make("a")) == projection_fingerprint(make("a"))
        assert projection_fingerprint(make("a")) != projection_fingerprint(make("b")), (
            "节点变化必须改变指纹"
        )

    def test_task_count_sensitive(self) -> None:
        one = ProjectionSources(tasks=lambda: [{"content": "t"}])
        two = ProjectionSources(tasks=lambda: [{"content": "t"}, {"content": "u"}])
        assert projection_fingerprint(one) != projection_fingerprint(two)


# ──────────────────────────────────────────────────────────────────────
# C. 消息操作族
# ──────────────────────────────────────────────────────────────────────


class TestMessageUpsert:
    def _messages(self) -> list[Any]:
        from bandi.core.message import Message, MessageRole

        return [
            Message(role=MessageRole.SYSTEM, content="system"),
            Message(role=MessageRole.SYSTEM, content="[上下文摘要] 旧摘要"),
            Message(role=MessageRole.USER, content="hi"),
        ]

    def test_insert_after_last_system(self) -> None:
        messages = self._messages()
        assert upsert_projection_message(messages, f"{PROJECTION_PREFIX}\n## X") is True
        assert projection_message_index(messages) == 2, "必须插在最后 SYSTEM 之后"
        assert messages[3].content == "hi"

    def test_upsert_idempotent(self) -> None:
        messages = self._messages()
        upsert_projection_message(messages, f"{PROJECTION_PREFIX}\nA")
        count = len(messages)
        upsert_projection_message(messages, f"{PROJECTION_PREFIX}\nB")
        assert len(messages) == count, "替换不得新增条数"
        assert projection_message_index(messages) >= 0
        assert messages[projection_message_index(messages)].content.endswith("B")

    def test_empty_text_noop(self) -> None:
        messages = self._messages()
        assert upsert_projection_message(messages, "") is False
        assert len(messages) == 3


# ──────────────────────────────────────────────────────────────────────
# D. states 集成族
# ──────────────────────────────────────────────────────────────────────


def _mk_ctx(raw_input: str = "任务") -> Any:
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import Message, MessageRole
    from bandi.llm.mock import MockProvider

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.SYSTEM, content="sys")],
        provider=MockProvider(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.raw_input = raw_input
    ctx.meta["session_id"] = "s-proj"
    ctx.meta["task_ledger"] = SimpleNamespace(items=[{"content": "查库存", "status": "pending"}])
    return ctx


def _fake_compact(monkeypatch: pytest.MonkeyPatch) -> None:
    from bandi.agent import states as states_mod

    stats = SimpleNamespace(
        stage="micro",
        input_chars=10,
        output_chars=5,
        input_messages=3,
        output_messages=2,
        micro_deduplicated=0,
        micro_trimmed=0,
        micro_merged=0,
        progressive_chunks=0,
    )
    outcome = SimpleNamespace(success=True, stats=stats, messages=[])

    async def fake(ctx: Any) -> Any:
        return outcome

    monkeypatch.setattr(
        states_mod,
        "get_query_deps",
        lambda ctx: SimpleNamespace(microcompact=fake, autocompact=fake),
    )


class TestStatesIntegration:
    def test_compaction_inserts_projection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bandi.agent.states import on_compressing

        _fake_compact(monkeypatch)
        ctx = _mk_ctx()
        ctx.compress.turns_since_maintenance = 99
        ctx.compress.last_active_ts = 0.0
        asyncio.run(on_compressing(ctx))

        index = projection_message_index(ctx.messages)
        assert index >= 0, "压缩后必须插入投影段"
        assert "查库存" in ctx.messages[index].content, "任务台账必须进投影"
        assert ctx.meta.get("projection_fingerprint"), "指纹必须记录"

    def test_refresh_on_change(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bandi.agent import states as states_mod
        from bandi.agent.states import _refresh_projection_if_changed

        ctx = _mk_ctx()
        frame_holder = {"node": "a"}
        monkeypatch.setattr(
            states_mod,
            "_projection_sources_for",
            lambda ctx: ProjectionSources(
                frames=lambda: _stack(active=_frame("S", frame_holder["node"]), revision=1),
                tasks=lambda: [{"content": "t"}],
            ),
        )
        upsert_projection_message(ctx.messages, f"{PROJECTION_PREFIX}\n旧投影")
        old_fp = projection_fingerprint(
            ProjectionSources(
                frames=lambda: _stack(active=_frame("S", "a"), revision=1),
                tasks=lambda: [{"content": "t"}],
            )
        )
        ctx.meta["projection_fingerprint"] = old_fp

        frame_holder["node"] = "b"
        _refresh_projection_if_changed(ctx)

        index = projection_message_index(ctx.messages)
        assert "b" in ctx.messages[index].content, "帧节点变化必须刷新投影"
        assert (
            len([m for m in ctx.messages if str(m.content).startswith(PROJECTION_PREFIX)]) == 1
        ), "投影消息必须恒一条"

    def test_refresh_skips_when_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bandi.agent import states as states_mod
        from bandi.agent.states import _refresh_projection_if_changed

        ctx = _mk_ctx()
        sources = ProjectionSources(
            frames=lambda: _stack(active=_frame("S", "a"), revision=1),
            tasks=lambda: [{"content": "t"}],
        )
        monkeypatch.setattr(states_mod, "_projection_sources_for", lambda ctx: sources)
        upsert_projection_message(ctx.messages, f"{PROJECTION_PREFIX}\nSTABLE")
        ctx.meta["projection_fingerprint"] = projection_fingerprint(sources)

        before = [m.content for m in ctx.messages]
        _refresh_projection_if_changed(ctx)
        after = [m.content for m in ctx.messages]
        assert before == after, "指纹不变不得重建/重写"

    def test_refresh_no_projection_no_create(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bandi.agent.states import _refresh_projection_if_changed

        ctx = _mk_ctx()
        before = len(ctx.messages)
        _refresh_projection_if_changed(ctx)
        assert len(ctx.messages) == before, "无投影段时不得生成（落点 A：压缩后才投影）"

    def test_real_frame_store_e2e(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        import bandi.skills.frame_store as fs_mod
        from bandi.skills.frame_store import save_frames
        from bandi.skills.progress import frame_scope_for_session
        from bandi.skills.runtime import SkillFrame, SkillFrameStack, SkillFrameStatus

        scope = frame_scope_for_session("s-proj")
        stack = SkillFrameStack(
            scope_id=scope,
            active=SkillFrame(
                task_id="t1",
                skill_id="发货技能",
                node_id="确认订单",
                status=SkillFrameStatus.ACTIVE,
                created_at="T10:00:00",
                updated_at="T10:00:00",
            ),
            revision=1,
        )
        save_frames(stack, frames_dir=str(tmp_path / "frames"))
        real_load = fs_mod.load_frames
        monkeypatch.setattr(
            fs_mod,
            "load_frames",
            lambda scope_id: real_load(scope_id, frames_dir=str(tmp_path / "frames")),
        )

        from bandi.agent.states import _projection_sources_for

        ctx = _mk_ctx()
        text = build_context_projection(_projection_sources_for(ctx))
        assert "发货技能" in text and "确认订单" in text, "真实帧 store 必须进投影"
        assert "查库存" in text
