from __future__ import annotations

import pytest

from bandi.company.gate_harness import GateHarness, GateVerdict, hashlib_blame


class TestGateHarness:
    def test_pass(self):
        harness = GateHarness()
        result = harness.evaluate("wi1", "do X", "done X")
        assert result.verdict == GateVerdict.PASS

    def test_failed_task(self):
        harness = GateHarness()
        result = harness.evaluate("wi1", "do X", "", task_status="failed")
        assert result.verdict == GateVerdict.REWORK

    def test_empty_output(self):
        harness = GateHarness()
        result = harness.evaluate("wi1", "do X", "")
        assert result.verdict == GateVerdict.REWORK

    def test_self_reported_blocker(self):
        harness = GateHarness()
        result = harness.evaluate(
            "wi1", "do X", "partial", self_reported_risks=["blocker: missing API key"]
        )
        assert result.verdict == GateVerdict.ESCALATE

    def test_stagnation_on_repeated_failure_output(self):
        harness = GateHarness(max_reworks=2)
        for _ in range(3):
            harness.evaluate("wi1", "requirements", "")
        result = harness.evaluate("wi1", "requirements", "")
        assert result.verdict == GateVerdict.ESCALATE

    def test_repeated_pass_never_escalates(self):
        harness = GateHarness(max_reworks=2)
        for _ in range(10):
            result = harness.evaluate("wi1", "requirements", "same successful output")
            assert result.verdict == GateVerdict.PASS

    def test_progress_resets_stagnation(self):
        harness = GateHarness(max_reworks=2)
        harness.evaluate("wi1", "requirements", "")
        harness.evaluate("wi1", "requirements", "good output")
        for _ in range(3):
            harness.evaluate("wi1", "requirements", "")
        result = harness.evaluate("wi1", "requirements", "")
        assert result.verdict == GateVerdict.ESCALATE

    def test_reset(self):
        harness = GateHarness(max_reworks=2)
        harness.evaluate("wi1", "req", "out")
        harness.reset("wi1")
        result = harness.evaluate("wi1", "req", "out")
        assert result.verdict == GateVerdict.PASS

    @pytest.mark.asyncio
    async def test_evaluate_llm_pass(self):
        harness = GateHarness()

        class _MockLLM:
            async def complete(self, messages):
                class _Resp:
                    content = "PASS: Output meets requirements"

                return _Resp()

        result = await harness.evaluate_llm("wi1", "do X", "done X", _MockLLM())
        assert result.verdict == GateVerdict.PASS

    @pytest.mark.asyncio
    async def test_evaluate_llm_escalate(self):
        harness = GateHarness()

        class _MockLLM:
            async def complete(self, messages):
                class _Resp:
                    content = "ESCALATE: Critical issue found in output"

                return _Resp()

        result = await harness.evaluate_llm("wi1", "do X", "broken", _MockLLM())
        assert result.verdict == GateVerdict.ESCALATE

    @pytest.mark.asyncio
    async def test_evaluate_llm_fallback_on_failure(self):
        harness = GateHarness()

        class _FailingLLM:
            async def complete(self, messages):
                raise RuntimeError("LLM unavailable")

        result = await harness.evaluate_llm("wi1", "do X", "done X", _FailingLLM())
        assert result.verdict == GateVerdict.ESCALATE

    @pytest.mark.asyncio
    async def test_evaluate_llm_replan(self):
        harness = GateHarness()

        class _MockLLM:
            async def complete(self, messages):
                class _Resp:
                    content = "REPLAN: Wrong approach, needs redesign"

                return _Resp()

        result = await harness.evaluate_llm("wi1", "build X", "wrong approach", _MockLLM())
        assert result.verdict == GateVerdict.REPLAN

    @pytest.mark.asyncio
    async def test_evaluate_llm_blocked(self):
        harness = GateHarness()

        class _MockLLM:
            async def complete(self, messages):
                class _Resp:
                    content = "BLOCKED: Missing API credentials"

                return _Resp()

        result = await harness.evaluate_llm("wi1", "call API", "tried but failed", _MockLLM())
        assert result.verdict == GateVerdict.BLOCKED


class TestHashlibBlame:
    def test_empty_string(self):
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert hashlib_blame("") == expected

    def test_deterministic(self):
        assert hashlib_blame("hello world") == hashlib_blame("hello world")

    def test_different_inputs_different_hashes(self):
        assert hashlib_blame("foo") != hashlib_blame("bar")
