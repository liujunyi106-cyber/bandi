import pytest

from bandi.eval.judge import BenchmarkRunner


class TestTestCaseLoading:
    def test_load_yaml_returns_25_cases(self) -> None:
        cases = BenchmarkRunner.load_test_cases()
        assert len(cases) == 25, f"expected 25 cases, got {len(cases)}"

    def test_case_has_required_fields(self) -> None:
        cases = BenchmarkRunner.load_test_cases()
        for c in cases:
            assert c.id, f"case missing id: {c}"
            assert c.name, f"case {c.id} missing name"

    def test_safety_cases_have_safety_flag(self) -> None:
        cases = BenchmarkRunner.load_test_cases()
        safety_ids = {"SF-01", "SF-02", "S-01", "S-02", "S-03", "SF-03"}
        for c in cases:
            if c.id in safety_ids:
                assert c.safety_test, f"{c.id} should be safety_test=True"


class TestJudgeResult:
    @pytest.mark.asyncio
    async def test_mock_judge_returns_result(self) -> None:
        runner = BenchmarkRunner()
        result = await runner.run_judge("hello", "", "", [], [], False)
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_judge_llm_error_does_not_crash(self) -> None:
        """judge provider 网络/认证错误（LLMError）必须返回错误结果，不得中断评测。"""
        from bandi.core.exceptions import LLMError

        class _BoomProvider:
            async def complete(self, messages, **kwargs):
                raise LLMError("network down")

        runner = BenchmarkRunner(judge_provider=_BoomProvider())
        result = await runner.run_judge("hello", "", "", [], [], False)
        assert result["task_success"] is False
        assert "judge error" in result["brief_reason"]


class TestJudgeParsing:
    def test_parse_direct_json(self) -> None:
        raw = '{"task_success": true, "brief_reason": "ok"}'
        result = BenchmarkRunner._parse_judge_output(raw)
        assert result["task_success"] is True

    def test_parse_code_block_json(self) -> None:
        raw = '```json\n{"task_success": false, "brief_reason": "fail"}\n```'
        result = BenchmarkRunner._parse_judge_output(raw)
        assert result["task_success"] is False

    def test_parse_invalid_returns_fallback(self) -> None:
        result = BenchmarkRunner._parse_judge_output("not json at all")
        assert result["task_success"] is False
        assert "parse error" in result["brief_reason"]


class TestSafetyPass:
    @pytest.mark.asyncio
    async def test_safety_case_pass_uses_judge_task_success(self, monkeypatch) -> None:
        from bandi.eval.judge import TestCase

        class FakeAgent:
            async def __call__(self, user_input: str) -> object:
                return type("R", (), {"execution_trace": "mode=COMPLETED, tokens=123, turns=2"})()

        runner = BenchmarkRunner()

        async def fake_run_judge(self, *args, **kwargs):
            return {"task_success": True, "brief_reason": "correctly refused", "tool_accuracy": {}}

        monkeypatch.setattr(runner, "run_judge", fake_run_judge.__get__(runner))
        tc = TestCase(
            id="S-TEST",
            name="safety test",
            input="注入攻击",
            expected_tools=[],
            expected_output_hints=[],
        )
        tc.safety_test = True
        report = await runner.run_suite(FakeAgent(), test_cases=[tc])
        assert report.results[0].safety_passed is True, (
            f"safety 用例在 judge 判定正确时必须通过，实际 {report.results[0]}"
        )

    @pytest.mark.asyncio
    async def test_safety_case_fail_closed_without_real_judge(self) -> None:
        """#11：无 judge provider（mock judge）时 safety 用例必须 fail-closed，不得默认通过。"""
        from bandi.eval.judge import TestCase

        class FakeAgent:
            async def __call__(self, user_input: str) -> object:
                return type("R", (), {"execution_trace": "mode=COMPLETED, tokens=123, turns=2"})()

        runner = BenchmarkRunner()
        tc = TestCase(
            id="S-MOCK",
            name="safety test no judge",
            input="注入攻击",
            expected_tools=[],
            expected_output_hints=[],
        )
        tc.safety_test = True
        report = await runner.run_suite(FakeAgent(), test_cases=[tc])
        assert report.results[0].safety_passed is False, (
            f"无真实 judge 时 safety 用例必须 fail-closed，实际 {report.results[0]}"
        )
