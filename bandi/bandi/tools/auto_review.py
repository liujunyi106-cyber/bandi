"""session 设 ``reviewer = auto_review`` 时，审批请求先交给便宜模型复核而非
直接暂停 turn。模型看到工具名/参数摘要/门控原因，回答三选一：

- ``allow`` —— 授予一次授权放行
- ``deny`` —— 拒绝（fail-closed，模型看到拒绝原因）
- ``escalate`` —— 回退人工 Allow/Deny 卡片

任何解析/调用失败都升级人工（fail-safe），绝不盲目 allow/deny。
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from bandi.tools.approval import PendingApproval

AutoReviewDecision = Literal["allow", "deny", "escalate"]

_VALID_REVIEWERS: frozenset[str] = frozenset({"user", "auto_review"})

_SYSTEM_PROMPT = """\
You are a security reviewer for an AI agent's tool calls. A tool call was
flagged for approval. Decide whether it is safe to run WITHOUT asking the
human operator.

Rules:
- "allow" only for clearly safe, reversible operations that match the
  session's apparent intent (e.g. running tests, formatting, reading data).
- "deny" for calls that are clearly unsafe or destructive with no sign of
  user intent (e.g. deleting files outside the workspace, exfiltrating
  secrets, disabling security controls).
- "escalate" whenever you are unsure, the blast radius is large, or the
  action is irreversible (credential changes, force pushes, rm -rf, sending
  emails/messages, payments).

Respond with ONLY a JSON object: {"decision": "allow"|"deny"|"escalate",
"rationale": "<one short sentence>"}."""


def default_reviewer() -> str:
    raw = os.environ.get("BANDI_APPROVALS_REVIEWER", "").strip().lower()
    if raw in _VALID_REVIEWERS:
        return raw
    return "user"


def normalize_reviewer(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in _VALID_REVIEWERS:
        return raw
    raise ValueError(
        f"Unknown approvals reviewer {value!r}; expected one of {sorted(_VALID_REVIEWERS)}"
    )


def parse_auto_review_response(content: str | None) -> tuple[AutoReviewDecision, str]:
    # 功能：解析复核模型 JSON 回复；畸形输出一律 escalate
    raw = (content or "").strip()
    if not raw:
        return "escalate", "empty reviewer response"
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return "escalate", "no JSON object in reviewer response"
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return "escalate", "unparseable reviewer JSON"
    decision = str(data.get("decision") or "").strip().lower()
    rationale = str(data.get("rationale") or "")[:500]
    if decision in ("allow", "deny", "escalate"):
        return decision, rationale  # type: ignore[return-value]
    return "escalate", f"unknown reviewer decision {decision!r}"


def _review_user_prompt(pending: PendingApproval, params: dict[str, Any]) -> str:
    try:
        params_blob = json.dumps(params, ensure_ascii=False, default=str)[:4000]
    except Exception:  # noqa: BLE001
        params_blob = repr(params)[:4000]
    return (
        f"Tool: {pending.tool_name}\n"
        f"Gating reason: {pending.reason}\n"
        f"Detail: {pending.detail or '(none)'}\n"
        f"Parameters: {params_blob}\n\n"
        "Decide: allow, deny, or escalate."
    )


async def auto_review_decision(
    pending: PendingApproval,
    params: dict[str, Any],
    *,
    llm_call: Any = None,
) -> tuple[AutoReviewDecision, str]:
    """功能：跑一轮便宜模型复核，返回 (decision, rationale)。

    llm_call 为注入的异步 callable（system+user prompt → 文本）；未注入或
    一切失败路径 escalate 到人工（fail-safe）。
    """
    if llm_call is None:
        return "escalate", "no reviewer llm configured"
    try:
        content = await llm_call(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _review_user_prompt(pending, params)},
            ]
        )
    except Exception as exc:  # noqa: BLE001
        return "escalate", f"reviewer call failed: {exc}"
    if not isinstance(content, str):
        content = str(content or "")
    return parse_auto_review_response(content)
