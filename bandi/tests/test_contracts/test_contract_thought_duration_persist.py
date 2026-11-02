"""契约：思考耗时必须在**落库侧**结算。

证据（API 实读持久化的 process 节点）：
    kind=tool durationMs=3.04 / 4.89 / 5.57 ← 有（tool_result 帧带入）
    kind=thought durationMs=缺失 ← 无

后果：思考耗时只存在于浏览器内存（前端 `settleThought`），**从未回传后端** ⇒
重载/切会话后纯文本轮完全没有耗时、混合轮只剩工具耗时 ⇒ 过程块显示「已完结 · —」。

口径：opencode `ReasoningPart.time.{start,end}` —— 思考段是可度量的时间区间。
"""

from __future__ import annotations

from typing import Any

import pytest

from bandi.core.process_recorder import ProcessRecorder


class _StubStore:
    """只收集 append_process_entry 的调用（断言对象 = 落库产物）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, list[dict[str, Any]]]] = []

    def append_process_entry(
        self, session_id: str, turn_index: int, nodes: list[dict[str, Any]]
    ) -> None:
        # 深拷贝：断言落库那一刻的形态，后续改动不得影响
        import copy

        self.calls.append((session_id, turn_index, copy.deepcopy(nodes)))


class _StubInner:
    def __init__(self) -> None:
        self.done_called = False

    async def on_content_delta(self, *a: Any, **k: Any) -> None: ...
    async def on_thinking(self, *a: Any, **k: Any) -> None: ...
    async def on_tool_call_start(self, *a: Any, **k: Any) -> None: ...
    async def on_tool_result(self, *a: Any, **k: Any) -> None: ...
    async def on_skill_injected(self, *a: Any, **k: Any) -> None: ...
    async def on_note(self, *a: Any, **k: Any) -> None: ...
    async def on_memory_written(self, *a: Any, **k: Any) -> None: ...
    async def on_task_ledger(self, *a: Any, **k: Any) -> None: ...
    async def on_error(self, *a: Any, **k: Any) -> None: ...

    async def on_done(self, *a: Any, **k: Any) -> None:
        self.done_called = True


def _recorder() -> tuple[ProcessRecorder, _StubStore, _StubInner]:
    store, inner = _StubStore(), _StubInner()
    return ProcessRecorder(inner, store, "s1"), store, inner  # type: ignore[arg-type]


def _patch_clock(monkeypatch: pytest.MonkeyPatch, ticks: list[float]) -> None:
    """单调钟桩：按序返回，耗尽后停在最后一个值（不得抛 StopIteration）。"""
    state = {"i": 0}

    def _now() -> float:
        idx = min(state["i"], len(ticks) - 1)
        state["i"] += 1
        return ticks[idx]

    monkeypatch.setattr("bandi.core.process_recorder.time.monotonic", _now)


class TestThoughtDurationSettledAtPersist:
    @pytest.mark.asyncio
    async def test_pure_text_turn_persists_thought_duration(self, monkeypatch) -> None:
        """破坏者视角：把结算删掉（退回只改 status），本组必红。"""
        _patch_clock(monkeypatch, [1000.0, 1004.25])
        rec, store, _ = _recorder()
        await rec.on_thinking("s1", "先想一下")
        await rec.on_done("s1", "VERIFIED", 10, 1)
        assert store.calls, "纯文本轮必须落库（有过程节点）"
        _, _, nodes = store.calls[0]
        thought = [n for n in nodes if n["kind"] == "thought"]
        assert thought, "应存在思考节点"
        assert thought[0]["status"] == "success"
        assert thought[0].get("durationMs") == 4250, (
            f"思考耗时须按起止结算（期望 4250ms），实际 {thought[0].get('durationMs')!r}"
        )

    @pytest.mark.asyncio
    async def test_thought_before_tool_also_settled(self, monkeypatch) -> None:
        """思考→工具转换点（`_converge_running_thoughts`）也必须结算耗时。"""
        _patch_clock(monkeypatch, [200.0, 201.5])
        rec, store, _ = _recorder()
        await rec.on_thinking("s1", "看一下目录")
        await rec.on_tool_call_start("s1", "list_dir", {}, "tc-1")
        await rec.on_done("s1", "VERIFIED", 10, 1)
        _, _, nodes = store.calls[0]
        thought = [n for n in nodes if n["kind"] == "thought"][0]
        assert thought.get("durationMs") == 1500, "转换点结算须生效"

    @pytest.mark.asyncio
    async def test_internal_marker_stripped_before_persist(self, monkeypatch) -> None:
        """落库形态必须干净：内部起止标记不得进持久化。"""
        _patch_clock(monkeypatch, [1.0, 2.0])
        rec, store, _ = _recorder()
        await rec.on_thinking("s1", "x")
        await rec.on_done("s1", "VERIFIED", 1, 1)
        _, _, nodes = store.calls[0]
        for node in nodes:
            assert "_startedMono" not in node, f"内部标记泄漏到落库形态：{node}"
            assert "startedAt" not in node, "不得落库墙钟起点（只留 durationMs）"

    @pytest.mark.asyncio
    async def test_tool_duration_still_persisted(self, monkeypatch) -> None:
        """回归守卫：工具耗时路径不得因本修复而改变。"""
        _patch_clock(monkeypatch, [10.0, 11.0])
        rec, store, _ = _recorder()
        await rec.on_thinking("s1", "t")
        await rec.on_tool_call_start("s1", "glob", {}, "tc-9")
        await rec.on_tool_result("s1", "glob", True, "ok", 77, "tc-9")
        await rec.on_done("s1", "VERIFIED", 1, 1)
        _, _, nodes = store.calls[0]
        tool = [n for n in nodes if n["kind"] == "tool"][0]
        assert tool["durationMs"] == 77

    @pytest.mark.asyncio
    async def test_multiple_thought_segments_each_settled(self, monkeypatch) -> None:
        """多段思考（工具分隔）各自结算，不得只结算最后一段。"""
        _patch_clock(monkeypatch, [0.0, 1.0, 2.0, 3.5])
        rec, store, _ = _recorder()
        await rec.on_thinking("s1", "第一段")
        await rec.on_tool_call_start("s1", "read", {}, "tc-1")
        await rec.on_tool_result("s1", "read", True, "ok", 5, "tc-1")
        await rec.on_thinking("s1", "第二段")
        await rec.on_done("s1", "VERIFIED", 1, 1)
        _, _, nodes = store.calls[0]
        thoughts = [n for n in nodes if n["kind"] == "thought"]
        assert len(thoughts) == 2, f"应有两段思考，实际 {len(thoughts)}"
        assert thoughts[0].get("durationMs") == 1000, "第一段（转换点结算）"
        assert thoughts[1].get("durationMs") == 1500, "第二段（on_done 结算）"

    @pytest.mark.asyncio
    async def test_no_negative_duration_on_clock_skew(self, monkeypatch) -> None:
        """单调钟倒退（异常）不得产出负耗时。"""
        _patch_clock(monkeypatch, [5.0, 4.0])
        rec, store, _ = _recorder()
        await rec.on_thinking("s1", "x")
        await rec.on_done("s1", "VERIFIED", 1, 1)
        _, _, nodes = store.calls[0]
        thought = [n for n in nodes if n["kind"] == "thought"][0]
        assert thought["durationMs"] >= 0, "耗时必须非负"
