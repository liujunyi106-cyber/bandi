# 功能：评测裁判降级契约——无真实 judge 不得假绿（「评测假绿」收口）
# 标记，并将 degraded 用例从通过率与加权分中剔除（报告显式计数）。
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bandi.eval.judge import BenchmarkRunner, TestCase


class _Agent:
    async def __call__(self, user_input: str) -> object:
        return SimpleNamespace(execution_trace="mode=COMPLETED", final_message="done")


def _case(case_id: str, text: str) -> TestCase:
    return TestCase(
        id=case_id, name=case_id, input=text, expected_tools=[], expected_output_hints=[]
    )


class TestMockJudgeNoFakeGreen:
    @pytest.mark.asyncio
    async def test_mock_judge_does_not_fake_pass(self) -> None:
        """无真实 judge：普通用例必须 fail-closed + degraded，不得计为通过。"""
        runner = BenchmarkRunner()
        report = await runner.run_suite(_Agent(), test_cases=[_case("T-1", "hello")])
        jr = report.results[0]
        assert jr.task_success is False, "mock judge 不得伪造 task_success（评测假绿）"
        assert jr.degraded is True, "必须显式标记降级（供报告剔除）"
        assert report.passed == 0
        assert report.failed == 0, "degraded 不是失败，是未评"
        assert report.degraded == 1
        assert report.weighted_score == 0.0, "无有效评分时加权分不得虚高"

    @pytest.mark.asyncio
    async def test_degraded_excluded_from_weighted_score(self, monkeypatch) -> None:
        """混合场景：degraded 用例从加权分/通过率剔除，有效用例评分不受稀释。"""
        runner = BenchmarkRunner()

        async def fake_run_judge(self: object, user_input: str, **kwargs: object) -> dict:
            if user_input == "bad":
                return {"task_success": False, "degraded": True, "brief_reason": "mock"}
            return {
                "task_success": True,
                "brief_reason": "ok",
                "tool_accuracy": {"tool_selection_correct": True, "param_errors": []},
                "response_accuracy": {"matched": 1, "partial": 0, "missing": 0, "score": 1.0},
            }

        monkeypatch.setattr(runner, "run_judge", fake_run_judge.__get__(runner))
        report = await runner.run_suite(_Agent(), test_cases=[_case("A", "ok"), _case("B", "bad")])
        assert report.degraded == 1
        assert report.passed == 1
        assert report.weighted_score == 1.0, "有效用例全绿时加权分应为 1.0（不被 degraded 稀释）"
