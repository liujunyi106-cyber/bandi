# 功能：工作项级审批——组织操作（reorg 等）的人工确认通道
#
# `authorize_tool_call` / `predict` / `authorize` + 三张风险名单 + denial 记忆 +
# 唯一生产链路是 `tools/approval.py` 的 `ApprovalStore` + `tools/executor.py` 的
# `_approval_gate`。
#
# 收敛动作：
# * 风险分级 → 下沉 `bandi/safety/tool_risk.py`（单一来源，本模块 re-export）；
# * 拒绝追踪 → 迁到 `bandi/safety/denial_tracker.py` **并接入生产链路**
#
# 保留：
# * `ApprovalEngine.authorize_work_item_action` —— reorg 提案的人工确认
# （唯一消费者 `reorg_manager.py:182`）；
# * `ApprovalAction` / `ApprovalDecision` —— 上者的返回结构；
# * 风险分级符号的 re-export —— 保持既有 import 路径可用。

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from bandi.core.user_interaction import UserInteraction
from bandi.safety.tool_risk import (
    RiskLevel,
    classify_tool_risk,
    predict_risk_from_arguments,
)

__all__ = [
    "ApprovalAction",
    "ApprovalDecision",
    "ApprovalEngine",
    "RiskLevel",
    "classify_tool_risk",
    "predict_risk_from_arguments",
]


class ApprovalAction(StrEnum):
    AUTO_APPROVE = "auto_approve"
    REQUIRE_CONFIRM = "require_confirm"
    DENY = "deny"


@dataclass
class ApprovalDecision:
    action: ApprovalAction
    risk_level: RiskLevel
    reason: str
    matched_rule: str | None = None


class ApprovalEngine:
    """工作项级授权引擎。

    只负责一件事：**一次组织操作的放行判定**——高风险/强制人工时走
    `UserInteraction.ask_confirmation` 真挂起；低风险自动放行；其余要求确认。
    工具调用审批**不经过这里**（见 `tools/approval.py`）。
    """

    def __init__(self, user_interaction: UserInteraction | None = None) -> None:
        self._ui = user_interaction

    async def authorize_work_item_action(
        self,
        task: Any,
        work_item_title: str,
        metadata: dict[str, Any],
        on_progress: Callable[[str], None] | None = None,
        force_human: bool = False,
    ) -> tuple[bool, ApprovalDecision]:
        """授权一次工作项级操作（company_reorg 等 gate）。

        有 UserInteraction 时按 risk 分级自动放行或要求人工确认；无 UI 时
        **fail-closed**（要求确认而不放行）。
        """
        if on_progress:
            on_progress(f"[Approval] authorizing work item action: {work_item_title}")
        risk_value = str(metadata.get("risk_level") or "medium").lower()
        if risk_value not in {level.value for level in RiskLevel}:
            risk_value = "medium"
        risk = RiskLevel(risk_value)

        if force_human or risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            if self._ui is None:
                return False, ApprovalDecision(
                    ApprovalAction.REQUIRE_CONFIRM,
                    risk,
                    "No user interaction available for human confirmation",
                )
            prompt = f"Approve work item action `{work_item_title}`?"
            if await self._ui.ask_confirmation(prompt):
                return True, ApprovalDecision(
                    ApprovalAction.AUTO_APPROVE,
                    risk,
                    f"User confirmed: {work_item_title}",
                )
            return False, ApprovalDecision(
                ApprovalAction.DENY,
                risk,
                f"User denied: {work_item_title}",
            )

        if risk == RiskLevel.LOW:
            return True, ApprovalDecision(
                ApprovalAction.AUTO_APPROVE,
                risk,
                "Low risk work item action",
            )
        return False, ApprovalDecision(
            ApprovalAction.REQUIRE_CONFIRM,
            risk,
            "Medium risk — confirm required",
        )
