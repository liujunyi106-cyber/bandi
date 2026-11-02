"""契约：检索空转防护。

现象：模型拿到空检索结果后不停换关键词重查——「订单服务依赖支付
服务」→「服务依赖关系」——每次调用参数都不同但结果全空，烧光 max_iterations，
该轮就永远没有最终回复。

旧判据（同指纹窗口）只认「调用参数与结果同时相同」，换词即绕过。这里钉的是
按**结果是否变化**判定：检索工具换词重查拿不到新东西就是空转。

语义边界：命中只提示不终止——单次正常检索不受影响，何时收手由模型判断。
"""

from __future__ import annotations

import pytest

from bandi.agent.states import (
    _is_retrieval_tool,
    _mark_retrieval_stall,
)


class _Ctx:
    def __init__(self, names: list[str]) -> None:
        from bandi.agent.base import TerminalReason

        self.current_tool_calls = [type("C", (), {"name": n})() for n in names]
        self.meta: dict = {}
        self.tool_results: list = []
        self.terminal_reason: TerminalReason | None = None
        self.final_message: str | None = None
        self.loop_detection_window = 4


class TestRetrievalClassification:
    @pytest.mark.parametrize(
        "name", ["memory_search", "ontology_query", "web_search", "grep", "read", "tool_search"]
    )
    def test_pure_query_tools_are_retrieval(self, name: str) -> None:
        assert _is_retrieval_tool(name)

    @pytest.mark.parametrize(
        "name",
        ["memory_assert", "todo_write", "send_dm", "memory_forget", "write", "bash"],
    )
    def test_mutating_tools_are_not_retrieval(self, name: str) -> None:
        """破坏者视角：写操作每次推进状态，判定空转会误杀正常的连续写入。"""
        assert not _is_retrieval_tool(name)


class TestStallDetection:
    def test_repeated_empty_results_marks_stall(self) -> None:
        """换关键词但结果恒定 → 判定空转。"""
        ctx = _Ctx(["memory_search"])
        for _ in range(3):
            _mark_retrieval_stall(ctx, ["readonly"], ["h1", "h1", "h1"], 4)
        assert int(ctx.meta.get("_retrieval_stall_rounds", 0)) >= 1

    def test_changed_results_clears_stall(self) -> None:
        """一旦拿到新结果就不再提示——继续查是合理的。"""
        ctx = _Ctx(["memory_search"])
        _mark_retrieval_stall(ctx, ["readonly"], ["h1", "h1", "h1"], 4)
        _mark_retrieval_stall(ctx, ["readonly"], ["h1", "h1", "h2"], 4)
        assert "_retrieval_stall_rounds" not in ctx.meta

    def test_write_tool_never_marks_stall(self) -> None:
        ctx = _Ctx(["memory_assert", "memory_assert"])
        for _ in range(3):
            _mark_retrieval_stall(ctx, ["write", "write"], ["h1", "h1", "h1"], 4)
        assert "_retrieval_stall_rounds" not in ctx.meta

    def test_mixed_batch_not_marked(self) -> None:
        """检索+写入混批时不算空转——写入本身就在推进。"""
        ctx = _Ctx(["memory_search", "memory_assert"])
        for _ in range(3):
            _mark_retrieval_stall(ctx, ["readonly", "write"], ["h1", "h1", "h1"], 4)
        assert "_retrieval_stall_rounds" not in ctx.meta

    def test_single_call_not_marked(self) -> None:
        """破坏者视角：只查一次是完全合法的，不能被当空转。"""
        ctx = _Ctx(["memory_search"])
        _mark_retrieval_stall(ctx, ["readonly"], ["h1"], 4)
        assert "_retrieval_stall_rounds" not in ctx.meta

    def test_stall_does_not_terminate(self) -> None:
        """关键语义：只置标志，绝不自行终止——终止权仍归主循环与既有循环检测。"""
        ctx = _Ctx(["memory_search"])
        for _ in range(5):
            _mark_retrieval_stall(ctx, ["readonly"], ["h1", "h1", "h1"], 4)
        assert ctx.terminal_reason is None
        assert ctx.final_message is None
