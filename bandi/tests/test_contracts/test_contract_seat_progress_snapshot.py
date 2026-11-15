"""功能：契约——员工过程事件快照不被逐 token 增量挤满，工具事件始终能进快照。"""

from __future__ import annotations

import asyncio
from typing import Any

from bandi.company.seat_progress import EmployeeProgressHandler


def _handler() -> tuple[EmployeeProgressHandler, list[dict[str, Any]]]:
    frames: list[dict[str, Any]] = []
    handler = EmployeeProgressHandler(
        frames.append,
        project_id="p1",
        thread_id="t1",
        role_id="r1",
        work_item_id="w1",
    )
    return handler, frames


def test_thinking_chunks_do_not_fill_the_snapshot() -> None:
    """契约：连续 thinking_delta 合并为一条——再长的思考也挤不掉后续工具事件。"""
    handler, _frames = _handler()

    async def run() -> None:
        for index in range(500):
            await handler.on_thinking("s1", f"t{index}")
        await handler.on_tool_call_start("s1", "list_dir", {"path": "."}, "c1")
        await handler.on_tool_result("s1", "list_dir", True, "a.py", 5, "c1")

    asyncio.run(run())
    snapshot = handler.snapshot()
    kinds = [e["type"] for e in snapshot]
    assert "tool_call_start" in kinds, f"工具事件必须进快照，实际 {kinds[:8]}…"
    assert "tool_result" in kinds, f"工具结果必须进快照，实际 {kinds[:8]}…"

    thinking = [e for e in snapshot if e["type"] == "thinking_delta"]
    assert len(thinking) == 1, f"连续思考应合并成一条，实际 {len(thinking)} 条"
    assert thinking[0]["data"]["text"].startswith("t0t1"), thinking[0]["data"]["text"][:20]


def test_separated_thinking_segments_stay_separate() -> None:
    """契约：被工具隔开的思考段各自成条（段与段之间不合并）。"""
    handler, _frames = _handler()

    async def run() -> None:
        await handler.on_thinking("s1", "先看")
        await handler.on_tool_call_start("s1", "read", {"path": "a"}, "c1")
        await handler.on_tool_result("s1", "read", True, "x", 3, "c1")
        await handler.on_thinking("s1", "再看")

    asyncio.run(run())
    snapshot = handler.snapshot()
    kinds = [e["type"] for e in snapshot]
    assert kinds.count("thinking_delta") == 2, kinds
    assert kinds == [
        "thinking_delta",
        "tool_call_start",
        "tool_result",
        "thinking_delta",
    ], kinds


def test_snapshot_still_bounded_for_non_mergeable_events() -> None:
    """契约：不可合并的事件仍受上限约束（防病态事件流无限增长）。"""
    handler, _frames = _handler()

    async def run() -> None:
        for index in range(EmployeeProgressHandler._MAX_EVENTS + 50):
            await handler.on_note("s1", f"note-{index}")

    asyncio.run(run())
    snapshot = handler.snapshot()
    assert len(snapshot) <= EmployeeProgressHandler._MAX_EVENTS, len(snapshot)
