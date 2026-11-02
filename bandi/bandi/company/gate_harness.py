# 功能：质量门——LLM/规则 gate 评估 + 四态判定

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class GateVerdict(StrEnum):
    PASS = "pass"
    REWORK = "rework"
    REPLAN = "replan"
    BLOCKED = "blocked"
    ESCALATE = "escalate"


@dataclass
class GateResult:
    verdict: GateVerdict
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


class GateHarness:
    def __init__(self, max_reworks: int = 3) -> None:
        self.max_reworks = max_reworks
        self._rework_counts: dict[str, int] = {}
        self._blocker_fingerprints: dict[str, set[str]] = {}

    def evaluate(
        self,
        work_item_id: str,
        requirements: str,
        output: str,
        task_status: str = "completed",
        self_reported_risks: list[str] | None = None,
    ) -> GateResult:
        evidence: dict[str, Any] = {
            "work_item_id": work_item_id,
            "task_status": task_status,
            "output_length": len(output),
            "self_reported_risks": self_reported_risks or [],
        }

        if task_status == "failed":
            return GateResult(GateVerdict.REWORK, "Task failed", evidence)

        if self_reported_risks:
            for risk in self_reported_risks:
                if "blocker" in risk.lower() or "cannot" in risk.lower():
                    return GateResult(
                        GateVerdict.ESCALATE, f"Self-reported blocker: {risk}", evidence
                    )

        if not output.strip():
            self._check_stagnation(work_item_id, hashlib_blame(requirements))
            if self._rework_counts.get(work_item_id, 0) >= self.max_reworks:
                return GateResult(
                    GateVerdict.ESCALATE,
                    f"Stagnation: {self._rework_counts[work_item_id]} reworks",
                    evidence,
                )
            return GateResult(GateVerdict.REWORK, "Empty output", evidence)

        self.reset(work_item_id)
        return GateResult(GateVerdict.PASS, "All checks passed", evidence)

    async def evaluate_llm(
        self,
        work_item_id: str,
        requirements: str,
        output: str,
        llm: Any,
        task_status: str = "completed",
        self_reported_risks: list[str] | None = None,
    ) -> GateResult:
        evidence: dict[str, Any] = {
            "work_item_id": work_item_id,
            "task_status": task_status,
            "output_length": len(output),
            "self_reported_risks": self_reported_risks or [],
            "gate_method": "llm",
        }

        if task_status == "failed":
            return GateResult(GateVerdict.REWORK, "Task failed", evidence)
        if not output.strip():
            return GateResult(GateVerdict.REWORK, "Empty output", evidence)

        prompt = (
            "You are a quality gate judge. Evaluate whether the actual output meets the requirements.\n\n"
            f"## Requirements\n{requirements}\n\n"
            f"## Actual Output\n{output}\n\n"
            "## Response Format\n"
            "Respond with EXACTLY one of these verdicts followed by a colon and brief reason:\n"
            "- PASS: output fully meets requirements\n"
            "- REWORK: minor fixes needed, approach is correct\n"
            "- REPLAN: approach is wrong, needs a different plan\n"
            "- BLOCKED: external blocker, cannot proceed\n"
            "- ESCALATE: uncertain or needs human judgment\n\n"
            "Example: PASS: All requirements are met."
        )

        try:
            from bandi.llm.accounted import COMPANY_JUDGE_BUDGET_SEC, accounted_complete

            response = await accounted_complete(
                llm,
                [{"role": "user", "content": prompt}],
                label="gate_harness",
                session_id="gate",
                budget_sec=COMPANY_JUDGE_BUDGET_SEC,
            )
            response_text = response.content if hasattr(response, "content") else str(response)
            evidence["llm_response"] = response_text
            response_lower = response_text.lower()

            if (
                "pass" in response_lower
                and "replan" not in response_lower
                and "rework" not in response_lower
            ):
                return GateResult(GateVerdict.PASS, f"LLM: {response_text}", evidence)
            if "blocked" in response_lower:
                return GateResult(GateVerdict.BLOCKED, f"LLM: {response_text}", evidence)
            if "replan" in response_lower:
                return GateResult(GateVerdict.REPLAN, f"LLM: {response_text}", evidence)
            if "rework" in response_lower:
                return GateResult(GateVerdict.REWORK, f"LLM: {response_text}", evidence)
            if "escalate" in response_lower:
                return GateResult(GateVerdict.ESCALATE, f"LLM: {response_text}", evidence)
            return GateResult(GateVerdict.REWORK, f"LLM: {response_text}", evidence)
        except (RuntimeError, ValueError, AttributeError) as e:
            from bandi.utils.errors import error_text

            return GateResult(
                GateVerdict.ESCALATE, f"LLM evaluation failed: {error_text(e)}", evidence
            )

    def _check_stagnation(self, work_item_id: str, fingerprint: str) -> None:
        if work_item_id not in self._blocker_fingerprints:
            self._blocker_fingerprints[work_item_id] = set()
        fingerprints = self._blocker_fingerprints[work_item_id]
        if fingerprint in fingerprints:
            self._rework_counts[work_item_id] = self._rework_counts.get(work_item_id, 0) + 1
        else:
            # max_reworks=3 实际第 4 次才 ESCALATE（多给一次重试）
            self._rework_counts[work_item_id] = 1
        fingerprints.add(fingerprint)

    def reset(self, work_item_id: str) -> None:
        self._rework_counts.pop(work_item_id, None)
        self._blocker_fingerprints.pop(work_item_id, None)


def hashlib_blame(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()
