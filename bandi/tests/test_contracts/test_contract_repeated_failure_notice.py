"""契约：反复失败调用的改路提示。

现场：参数校验类错误下模型不停换参数形状重调同一个工具，烧光 max_iterations，
该轮就没有最终回复（表现为「空回复」）。提示按「本轮同一工具连续失败次数」
插入，不终止——何时收手仍由模型判断。
"""

from __future__ import annotations

from bandi.agent.loop import (
    _REPEATED_FAILURE_THRESHOLD,
    REPEATED_FAILURE_TEXT,
    AgentLoop,
)
from bandi.core.message import MessageRole


class _Ctx:
    def __init__(self, results: list) -> None:
        self.tool_results = results
        self.meta: dict = {}
        self.messages: list = []
        self.turn_index = 1


class _R:
    def __init__(self, name: str, ok: bool) -> None:
        self.tool_name = name
        self.success = ok


def _loop() -> AgentLoop:
    return AgentLoop(max_iterations=3)


class TestRepeatedFailureNotice:
    def test_injected_after_repeated_same_tool_failure(self) -> None:
        ctx = _Ctx([_R("ontology_assert", False)] * _REPEATED_FAILURE_THRESHOLD)
        _loop()._inject_repeated_failure_notice(ctx)
        assert any(
            m.role == MessageRole.SYSTEM and "ontology_assert" in (m.content or "")
            for m in ctx.messages
        )

    def test_not_injected_below_threshold(self) -> None:
        ctx = _Ctx([_R("ontology_assert", False)] * (_REPEATED_FAILURE_THRESHOLD - 1))
        _loop()._inject_repeated_failure_notice(ctx)
        assert not ctx.messages

    def test_not_injected_when_one_succeeded(self) -> None:
        """中间有一次成功就不算连续失败——路径是通的，不必叫停。"""
        ctx = _Ctx([_R("ontology_assert", False), _R("ontology_assert", True), _R("x", False)])
        _loop()._inject_repeated_failure_notice(ctx)
        assert not ctx.messages

    def test_not_injected_for_different_tools(self) -> None:
        ctx = _Ctx([_R("a", False), _R("b", False), _R("c", False)])
        _loop()._inject_repeated_failure_notice(ctx)
        assert not ctx.messages

    def test_no_duplicate_notice(self) -> None:
        """同一提示不重复插入——否则消息面会被提示刷屏。"""
        ctx = _Ctx([_R("ontology_assert", False)] * (_REPEATED_FAILURE_THRESHOLD + 2))
        loop = _loop()
        loop._inject_repeated_failure_notice(ctx)
        loop._inject_repeated_failure_notice(ctx)
        notices = [m for m in ctx.messages if m.role == MessageRole.SYSTEM]
        assert len(notices) == 1

    def test_notice_never_terminates(self) -> None:
        """关键语义：只提示不终止——终止权归既有循环/暂停判定。"""
        ctx = _Ctx([_R("ontology_assert", False)] * 6)
        _loop()._inject_repeated_failure_notice(ctx)
        assert "terminal_reason" not in ctx.meta


class TestNoticeTemplate:
    def test_template_names_tool_and_count(self) -> None:
        text = REPEATED_FAILURE_TEXT.format(tool="ontology_assert", times=3)
        assert "ontology_assert" in text and "3" in text
