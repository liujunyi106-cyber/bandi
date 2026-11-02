# 功能：公平性评估（识别公平性问题并生成报告）
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

GENDER_TERMS: dict[str, list[str] | dict[str, str]] = {
    "male_biased": [
        "chairman",
        "fireman",
        "policeman",
        "businessman",
        "salesman",
        "mankind",
        "manpower",
        "man-made",
    ],
    "female_biased": [
        "actress",
        "waitress",
        "stewardess",
        "housewife",
        "maiden name",
        "female doctor",
    ],
    "neutral_alternative": {
        "chairman": "chairperson",
        "fireman": "firefighter",
        "policeman": "police officer",
        "businessman": "business executive",
        "salesman": "salesperson",
        "mankind": "humankind",
        "manpower": "workforce",
        "man-made": "synthetic",
        "actress": "actor",
        "waitress": "server",
        "stewardess": "flight attendant",
        "housewife": "homemaker",
    },
}

REGION_TERMS: dict[str, list[str]] = {
    "developed_centric": [
        "first world",
        "western world",
        "developed countries",
        "global north",
    ],
    "developing_stereotypes": [
        "third world",
        "underdeveloped",
        "backward",
        "exotic",
        "primitive",
    ],
}

VOCAB_PATTERNS: list[dict[str, Any]] = [
    {"pattern": r"\b(stupid|dumb|idiot|crazy|lunatic)\b", "label": "ableist", "severity": "medium"},
    {
        "pattern": r"\b(whiny|dramatic|hysterical|emotional)\b",
        "label": "gender_stereotype",
        "severity": "medium",
    },
    {
        "pattern": r"\b(ghetto|trashy|redneck|white trash)\b",
        "label": "classist",
        "severity": "high",
    },
]


@dataclass
class FairnessIssue:
    dimension: str
    issue_type: str
    term: str
    severity: str
    suggestion: str | None = None


@dataclass
class FairnessReport:
    text: str = ""
    issues: list[FairnessIssue] = field(default_factory=list)
    score: float = 1.0

    @property
    def has_issues(self) -> bool:
        return len(self.issues) > 0

    @property
    def summary(self) -> str:
        if not self.issues:
            return "No fairness issues detected."
        grouped: dict[str, list[str]] = {}
        for issue in self.issues:
            grouped.setdefault(issue.dimension, []).append(
                f"[{issue.severity}] {issue.term} ({issue.issue_type})"
            )
        parts = [f"Fairness score: {self.score:.2f}"]
        for dim, items in grouped.items():
            parts.append(f"  {dim}: {'; '.join(items)}")
        return "\n".join(parts)


class FairnessEvaluator:
    def evaluate(self, text: str) -> FairnessReport:
        if not text:
            return FairnessReport(text=text)
        issues: list[FairnessIssue] = []
        text_lower = text.lower()

        self._check_biased_terms("gender", GENDER_TERMS, text_lower, issues)
        self._check_biased_terms("region", REGION_TERMS, text_lower, issues)
        self._check_vocabulary(text_lower, issues)

        score = max(0.0, 1.0 - len(issues) * 0.15)
        return FairnessReport(text=text, issues=issues, score=round(score, 2))

    def _check_biased_terms(
        self,
        dimension: str,
        terms: dict[str, list[str] | dict[str, str]],
        text_lower: str,
        issues: list[FairnessIssue],
    ) -> None:
        neutral_map: dict[str, str] = terms.get("neutral_alternative", {})
        if isinstance(neutral_map, list):
            neutral_map = {}
        severity_map = {
            "male_biased": ("low", "male_biased"),
            "female_biased": ("low", "female_biased"),
            "developed_centric": ("medium", "developed_centric"),
            "developing_stereotypes": ("high", "stereotyping"),
        }
        for key, entries in terms.items():
            if not isinstance(entries, list):
                continue
            severity, issue_type = severity_map.get(key, ("medium", key))
            for term in entries:
                # "man" 命中 "manager"、"woman" 命中 "human" 等误判
                if re.search(rf"\b{re.escape(term)}\b", text_lower):
                    suggestion = (
                        neutral_map.get(term)
                        if dimension == "gender"
                        else (
                            f"consider '{term}' phrasing"
                            if key == "developed_centric"
                            else "use neutral geographic terms"
                        )
                    )
                    issues.append(
                        FairnessIssue(
                            dimension=dimension,
                            issue_type=issue_type,
                            term=term,
                            severity=severity,
                            suggestion=suggestion,
                        )
                    )

    def _check_vocabulary(self, text_lower: str, issues: list[FairnessIssue]) -> None:
        for pattern_def in VOCAB_PATTERNS:
            matches = re.findall(pattern_def["pattern"], text_lower)
            for match in matches:
                issues.append(
                    FairnessIssue(
                        dimension="vocabulary",
                        issue_type=pattern_def["label"],
                        term=match,
                        severity=pattern_def["severity"],
                    )
                )

    def evaluate_messages(self, messages: list[dict]) -> list[FairnessReport]:
        return [self.evaluate(m.get("content", "")) for m in messages if isinstance(m, dict)]
