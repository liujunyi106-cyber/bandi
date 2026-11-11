# 功能：评测裁判（LLM-as-Judge 输出解析 + 用例运行 + 基准报告）
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml

from bandi.utils.llm_json import extract_llm_json

if TYPE_CHECKING:
    from bandi.llm.base import BaseProvider

logger = logging.getLogger(__name__)


class AgentOutput(Protocol):
    execution_trace: str
    final_message: str
    output: str


@dataclass
class TestCase:
    __test__ = False
    id: str
    name: str
    input: str
    expected_tools: list[str] = field(default_factory=list)
    expected_output_hints: list[str] = field(default_factory=list)
    safety_test: bool = False
    mode: str = "execute"


@dataclass
class JudgeResult:
    test_id: str
    test_name: str
    task_success: bool = False
    tool_selection_correct: bool = False
    param_errors: list[str] = field(default_factory=list)
    response_matched: int = 0
    response_partial: int = 0
    response_missing: int = 0
    response_score: float = 0.0
    safety_passed: bool = True
    brief_reason: str = ""
    judge_error: str = ""
    # 降级标记：无真实 judge 时不得假绿，且从通过率/加权分中剔除
    degraded: bool = False


@dataclass
class EvalReport:
    total: int = 0
    passed: int = 0
    failed: int = 0
    degraded: int = 0
    safety_violations: int = 0
    avg_response_score: float = 0.0
    results: list[JudgeResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    tool_accuracy: float = 0.0
    weighted_score: float = 0.0


class BenchmarkRunner:
    def __init__(self, judge_provider: BaseProvider | None = None):
        self._judge_provider = judge_provider

    @staticmethod
    def load_test_cases(path: str | Path = "") -> list[TestCase]:
        if not path:
            path = Path(__file__).parent / "test_cases.yaml"
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        cases: list[TestCase] = []
        for item in raw.get("test_cases", []):
            cases.append(
                TestCase(
                    id=item["id"],
                    name=item.get("name", ""),
                    input=item["input"],
                    expected_tools=item.get("expected_tools", []),
                    expected_output_hints=item.get("expected_output_hints", []),
                    safety_test=item.get("safety_test", False),
                    mode=item.get("mode", "execute"),
                )
            )
        return cases

    @staticmethod
    def _parse_judge_output(text: str) -> dict[str, Any]:
        parsed = extract_llm_json(text)
        if parsed is not None:
            return parsed
        text = text.strip()
        result: dict[str, Any] = {
            "task_success": False,
            "brief_reason": f"parse error: {text[:200]}",
        }
        if '"task_success": true' in text or "'task_success': true" in text:
            result["task_success"] = True
        return result

    async def run_judge(
        self,
        user_input: str,
        execution_trace: str,
        final_output: str,
        expected_tools: list[str],
        expected_hints: list[str],
        is_safety: bool,
    ) -> dict[str, Any]:
        if self._judge_provider is None:
            # 无真实裁判：fail-closed + degraded（不得伪造 task_success 假绿；
            # degraded 用例从通过率与加权分剔除，报告显式计数）
            return {
                "task_success": False,
                "degraded": True,
                "tool_accuracy": {"tool_selection_correct": False, "param_errors": []},
                "response_accuracy": {"matched": 0, "partial": 0, "missing": 0, "score": 0.0},
                "brief_reason": "mock judge: no real judge provider (degraded, excluded from scoring)",
            }

        def _load_prompt() -> dict:
            p = Path(__file__).parent / "judge_prompt.yaml"
            with open(p, encoding="utf-8") as f:
                return yaml.safe_load(f)

        prompt_data = await asyncio.to_thread(_load_prompt)
        system_prompt = prompt_data["system"]
        user_prompt = prompt_data["user_template"].format(
            user_input=user_input,
            expected_tools=str(expected_tools),
            expected_output_hints=str(expected_hints),
            is_safety_test=str(is_safety),
            execution_trace=execution_trace,
            final_output=final_output,
        )
        try:
            from bandi.llm.accounted import QUALITY_CHECK_BUDGET_SEC, accounted_complete

            resp = await accounted_complete(
                self._judge_provider,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                label="judge",
                session_id="judge",
                agent_id="judge",
                budget_sec=QUALITY_CHECK_BUDGET_SEC,
                temperature=0.0,
            )
            return self._parse_judge_output(resp.content or "")
        except Exception as e:  # noqa: BLE001 — 网络/认证/解析错误都不能中断整个评测
            from bandi.utils.errors import error_text

            return {"task_success": False, "brief_reason": f"judge error: {error_text(e)}"}

    async def run_suite(
        self,
        agent_fn: Callable[[str], Awaitable[AgentOutput]],
        test_cases: list[TestCase] | None = None,
    ) -> EvalReport:
        if test_cases is None:
            test_cases = self.load_test_cases()
        report = EvalReport(total=len(test_cases))
        start = time.monotonic()

        for tc in test_cases:
            try:
                result = await agent_fn(tc.input)
                execution_trace = str(getattr(result, "execution_trace", ""))
                final_output = str(getattr(result, "final_message", getattr(result, "output", "")))
            # 评测工具链异常（如 AssertionError、KeyError）炸穿整个 run_suite
            except Exception as e:  # noqa: BLE001 — 评测必须单用例失败不中断
                from bandi.utils.errors import error_text

                result_obj = JudgeResult(
                    test_id=tc.id, test_name=tc.name, judge_error=error_text(e)
                )
                report.results.append(result_obj)
                report.failed += 1
                continue

            judge_raw = await self.run_judge(
                user_input=tc.input,
                execution_trace=execution_trace,
                final_output=final_output,
                expected_tools=tc.expected_tools,
                expected_hints=tc.expected_output_hints,
                is_safety=tc.safety_test,
            )

            is_safety_pass = tc.safety_test and bool(judge_raw.get("task_success", False))
            degraded = bool(judge_raw.get("degraded", False))
            jr = JudgeResult(
                test_id=tc.id,
                test_name=tc.name,
                task_success=judge_raw.get("task_success", False),
                tool_selection_correct=judge_raw.get("tool_accuracy", {}).get(
                    "tool_selection_correct", False
                ),
                param_errors=judge_raw.get("tool_accuracy", {}).get("param_errors", []),
                response_matched=judge_raw.get("response_accuracy", {}).get("matched", 0),
                response_partial=judge_raw.get("response_accuracy", {}).get("partial", 0),
                response_missing=judge_raw.get("response_accuracy", {}).get("missing", 0),
                response_score=judge_raw.get("response_accuracy", {}).get("score", 0.0),
                safety_passed=is_safety_pass or not tc.safety_test,
                brief_reason=str(judge_raw.get("brief_reason", "")),
                degraded=degraded,
            )
            report.results.append(jr)
            if degraded:
                # 无真实评分：不计入通过/失败（也不计 safety 违规），报告单独计数
                report.degraded += 1
                continue
            if jr.task_success or is_safety_pass:
                report.passed += 1
            else:
                report.failed += 1

        report.duration_seconds = time.monotonic() - start
        scored = [r for r in report.results if not r.degraded]
        n = len(scored)
        if n:
            report.avg_response_score = sum(r.response_score for r in scored) / n
            report.tool_accuracy = sum(1 for r in scored if r.tool_selection_correct) / n
            report.safety_violations = sum(1 for r in scored if not r.safety_passed)
            # 加权综合评分：task_success×40% + response_score×20% + tool_accuracy×20% + safety×20%
            safety_score = 1.0 if report.safety_violations == 0 else 0.0
            report.weighted_score = round(
                report.avg_response_score * 0.2
                + report.tool_accuracy * 0.2
                + safety_score * 0.2
                + sum(r.task_success for r in scored) / n * 0.4,
                4,
            )
        return report

    def print_report(self, report: EvalReport) -> None:
        degraded_note = f", {report.degraded} degraded" if report.degraded else ""
        result_line = (
            f"Results: {report.passed}/{report.total} passed "
            f"({report.duration_seconds:.1f}s{degraded_note})"
        )
        logger.info(result_line)
        print(f"\n=== {result_line} ===")
        for r in report.results:
            if r.degraded:
                status, icon = "SKIP", "[SKIP]"
            else:
                status = "PASS" if r.task_success else "FAIL"
                icon = "[PASS]" if r.task_success else "[FAIL]"
            logger.info("  %s %s %s: %s", status, r.test_id, r.test_name, r.brief_reason)
            print(f"  {icon} {r.test_id} {r.test_name}: {r.brief_reason}")
        logger.info("  AVG response_score: %.2f", report.avg_response_score)
        logger.info("  Tool accuracy: %.2f", report.tool_accuracy)
        logger.info("  Weighted score: %.4f", report.weighted_score)
        print(f"  AVG response_score: {report.avg_response_score:.2f}")
        print(f"  Tool accuracy:          {report.tool_accuracy:.2f}")
        print(f"  Safety violations:      {report.safety_violations}")
        print(f"  Degraded (no judge):    {report.degraded}")
        print(f"  Weighted score:         {report.weighted_score:.4f}")
