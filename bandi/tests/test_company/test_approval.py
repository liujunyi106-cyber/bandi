# 功能：工具风险分级 + 工作项级授权（收敛后的测试面）
#
# **零生产调用**的第二套工具审批实现（`authorize_tool_call`/`predict`/`authorize`）。
# 该实现已整体删除，风险分级下沉到 `bandi/safety/tool_risk.py`（本文件改为经
# `company.approval` 的 re-export 验证，同时守住「import 路径不破」）。
from __future__ import annotations

import pytest

from bandi.company.approval import (
    ApprovalAction,
    ApprovalEngine,
    RiskLevel,
    classify_tool_risk,
    predict_risk_from_arguments,
)


class TestToolRiskClassification:
    def test_classify_tool_risk(self):
        assert classify_tool_risk("read") == RiskLevel.LOW
        assert classify_tool_risk("bash") == RiskLevel.MEDIUM
        assert classify_tool_risk("git_commit") == RiskLevel.HIGH
        assert classify_tool_risk("apply_patch") == RiskLevel.CRITICAL
        assert classify_tool_risk("unknown_tool") == RiskLevel.MEDIUM

    def test_git_push_classified_high_not_critical(self):
        assert classify_tool_risk("git_push") == RiskLevel.HIGH

    def test_risk_upgrade_medium_to_critical(self):
        assert predict_risk_from_arguments("bash", {"force": True}) == RiskLevel.CRITICAL

    def test_risk_upgrade_high_to_critical(self):
        assert predict_risk_from_arguments("git_commit", {"force": True}) == RiskLevel.CRITICAL

    def test_risk_keeps_critical_on_low_keywords(self):
        assert predict_risk_from_arguments("apply_patch", {"read": True}) == RiskLevel.CRITICAL

    def test_risk_no_upgrade_when_equal(self):
        assert predict_risk_from_arguments("git_commit", {"delete": True}) == RiskLevel.HIGH


class TestWorkItemAuthorization:
    """`authorize_work_item_action` 是 reorg 的活路径（`reorg_manager.py:182`）。"""

    @pytest.mark.asyncio
    async def test_low_risk_auto_approves(self):
        engine = ApprovalEngine()
        approved, decision = await engine.authorize_work_item_action(
            task=None,
            work_item_title="t",
            metadata={"risk_level": "low"},
        )
        assert approved is True
        assert decision.action == ApprovalAction.AUTO_APPROVE

    @pytest.mark.asyncio
    async def test_medium_risk_requires_confirm(self):
        engine = ApprovalEngine()
        approved, decision = await engine.authorize_work_item_action(
            task=None,
            work_item_title="t",
            metadata={"risk_level": "medium"},
        )
        assert approved is False
        assert decision.action == ApprovalAction.REQUIRE_CONFIRM

    @pytest.mark.asyncio
    async def test_force_human_without_ui_fails_closed(self):
        """契约（fail-closed）：需要人工确认但没有交互通道时，必须**不放行**。"""
        engine = ApprovalEngine(user_interaction=None)
        approved, decision = await engine.authorize_work_item_action(
            task=None,
            work_item_title="t",
            metadata={"risk_level": "low"},
            force_human=True,
        )
        assert approved is False
        assert decision.action == ApprovalAction.REQUIRE_CONFIRM

    @pytest.mark.asyncio
    async def test_unknown_risk_level_falls_back_to_medium(self):
        """契约：非法 risk_level 不得被当成低风险放行。"""
        engine = ApprovalEngine()
        approved, decision = await engine.authorize_work_item_action(
            task=None,
            work_item_title="t",
            metadata={"risk_level": "bogus"},
        )
        assert approved is False
        assert decision.risk_level == RiskLevel.MEDIUM
