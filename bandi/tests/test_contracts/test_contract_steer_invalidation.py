# 功能：契约 — 需求中途修改的转向失效标记（注入消费置标记 + 投影渲染「最新指令」节）
from __future__ import annotations

from typing import Any

import pytest


def _steer_note() -> str:
    from bandi.agent.loop import _steer_invalidation_note

    return _steer_invalidation_note()


class TestSteerNoteText:
    def test_note_declares_invalidation(self) -> None:
        note = _steer_note()
        assert "作废" in note
        assert "最新" in note


class TestProjectionRendersSteerNote:
    def test_steer_note_renders_as_first_section(self) -> None:
        from bandi.memory.context_projection import (
            ProjectionSources,
            build_context_projection,
        )

        note = _steer_note()
        text = build_context_projection(ProjectionSources(steer_note=lambda: note))
        assert "## 最新指令" in text
        assert note in text

    def test_steer_note_section_precedes_other_sections(self) -> None:
        from bandi.memory.context_projection import (
            ProjectionSources,
            build_context_projection,
        )

        text = build_context_projection(
            ProjectionSources(
                steer_note=lambda: _steer_note(),
                tasks=lambda: [{"content": "旧任务"}],
            )
        )
        assert text.index("## 最新指令") < text.index("## 任务台账")

    def test_no_steer_note_no_section(self) -> None:
        from bandi.memory.context_projection import (
            ProjectionSources,
            build_context_projection,
        )

        text = build_context_projection(ProjectionSources(tasks=lambda: [{"content": "t"}]))
        assert "## 最新指令" not in text

    def test_fingerprint_changes_with_steer_note(self) -> None:
        from bandi.memory.context_projection import (
            ProjectionSources,
            projection_fingerprint,
        )

        base = projection_fingerprint(ProjectionSources(tasks=lambda: [{"content": "t"}]))
        with_note = projection_fingerprint(
            ProjectionSources(tasks=lambda: [{"content": "t"}], steer_note=lambda: "n")
        )
        assert base != with_note


class TestConsumeInjectionSetsNote:
    @pytest.mark.asyncio
    async def test_consume_injection_sets_invalidation_note(self) -> None:
        from bandi.agent.base import ExecutionContext
        from bandi.agent.loop import AgentLoop
        from bandi.agent.steer import SessionSteerRegistry
        from bandi.core.config import AgentConfig

        class _Bus:
            async def emit(self, *args: Any, **kwargs: Any) -> None:
                return None

        class _Executor:
            async def execute(self, calls: list[Any]) -> list[Any]:
                return []

        ctx = ExecutionContext(messages=[], provider=object(), config=AgentConfig(), bus=_Bus())
        ctx.session_id = "s"
        ctx.meta = {
            "tracer": None,
            "task_ledger": None,
            "steer_registry": SessionSteerRegistry(),
        }
        ctx.loop_detection_window = 3
        ctx.tool_executor = _Executor()

        ctx.inject_input("转向：改需求，以这条为准")
        loop = AgentLoop(max_iterations=5)
        assert await loop._consume_injected_input(ctx, 0) is True

        note = str(ctx.meta.get("_steer_note") or "")
        assert "作废" in note and "最新" in note
