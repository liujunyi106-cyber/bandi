# 功能：评审裁决桥——评审回合输出解析（approve/reject JSON）+ 隐藏评审卡构建 + 重复防护。

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bandi.company.phase import is_terminal
from bandi.company.work_item import WorkItem, WorkItemTurnType, create_work_item

_VERDICT_LINE_RE = re.compile(r'\{[^\n]*"review_verdict"[^\n]*\}')
_STRUCTURED_VERDICT_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:review[_ ])?verdict\s*[:：]?\s*(approve|reject)\b"
)
_REVIEW_SECTION_RE = re.compile(r"(?ims)^#{1,6}\s*(?:review\s+)?verdict\s*$(.*?)(?=^#{1,6}\s|\Z)")
_REJECT_WORDS = (
    "reject",
    "request changes",
    "changes requested",
    "needs work",
    "驳回",
    "不通过",
    "返工",
)
_APPROVE_WORDS = ("approve", "approved", "lgtm", "通过", "同意")
_MAX_REPORT_CHARS = 2000


@dataclass
class ReviewVerdict:
    verdict: str
    summary: str = ""
    blocking_issues: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(v).strip() for v in list(value or []) if str(v).strip()]


def _payload_to_verdict(payload: Any) -> ReviewVerdict | None:
    if not isinstance(payload, dict):
        return None
    verdict = str(payload.get("review_verdict") or "").strip().lower()
    if verdict not in {"approve", "reject"}:
        return None
    return ReviewVerdict(
        verdict=verdict,
        summary=str(payload.get("summary") or "").strip(),
        blocking_issues=_coerce_str_list(payload.get("blocking_issues")),
        followups=_coerce_str_list(payload.get("followups")),
    )


def _loads_json_object(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except (TypeError, ValueError):
        return None


def parse_review_verdict(output: str) -> ReviewVerdict | None:
    """功能：解析评审回合输出中的 approve/reject JSON（取最后一个可解析对象）。"""
    text = str(output or "")
    candidates = [line.strip() for line in text.splitlines() if '"review_verdict"' in line]
    if not candidates:
        candidates = [match.group(0) for match in _VERDICT_LINE_RE.finditer(text)]
    for raw in reversed(candidates):
        verdict = _payload_to_verdict(_loads_json_object(raw))
        if verdict is not None:
            return verdict
    structured = _STRUCTURED_VERDICT_RE.search(text)
    if structured is not None:
        return ReviewVerdict(verdict=structured.group(1).lower())
    section = _REVIEW_SECTION_RE.search(text)
    if section is not None:
        body = section.group(1).strip().lower()
        if any(word in body for word in _REJECT_WORDS):
            return ReviewVerdict(verdict="reject")
        if any(word in body for word in _APPROVE_WORDS):
            return ReviewVerdict(verdict="approve")
    return None


def find_live_review_card(store: Any, target_work_item_id: str) -> WorkItem | None:
    """功能：查找目标卡的在途评审卡（非终态即视为在途，防重复建卡）。"""
    for item in store.list_all():
        metadata = dict(item.metadata or {})
        if str(metadata.get("review_target_work_item_id") or "") != target_work_item_id:
            continue
        if not is_terminal(item.phase):
            return item
    return None


def build_review_card(child: WorkItem, reviewer_role: str, store: Any) -> WorkItem | None:
    """功能：为已通过 gate 的子卡建隐藏评审卡；自评/已有在途评审卡时跳过。"""
    reviewer = str(reviewer_role or "").strip()
    worker = str(child.assignee_role or "").strip()
    if not reviewer or reviewer.lower() == worker.lower():
        return None
    if find_live_review_card(store, child.id) is not None:
        return None
    report = str((child.runtime_metadata or {}).get("_last_output") or "")
    child_metadata = dict(child.metadata or {})
    target_contract = child_metadata.get("prompt_contract")
    target_assignment = dict((target_contract or {}).get("assignment_context") or {})
    metadata: dict[str, Any] = {
        "review_execution_work_item": True,
        "review_target_work_item_id": child.id,
        "review_target_title": child.title,
        "review_target_description": str(child.description or ""),
        "review_target_worker_role_id": worker,
        "review_completion_report": report[:_MAX_REPORT_CHARS],
    }
    if target_contract:
        metadata["review_target_prompt_contract"] = target_contract
    if target_assignment.get("acceptance_criteria"):
        metadata["review_target_acceptance_criteria"] = list(
            target_assignment.get("acceptance_criteria") or []
        )
    card = create_work_item(
        title=f"评审：{child.title}",
        turn_type=WorkItemTurnType.REVIEW,
        description=f"对子卡 {child.id}（{child.title}）的交付进行独立评审并给出裁决。",
        assignee_role=reviewer,
        metadata=metadata,
    )
    store.save(card)
    return card
