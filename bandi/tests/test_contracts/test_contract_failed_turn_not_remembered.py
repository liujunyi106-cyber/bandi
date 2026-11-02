"""契约：失败回合不得进入经历层（污染长期记忆的防线）。

背景（L1 真机）：MODEL_ERROR 回合的 "all LLM candidates failed (capability +
config chain): Client error…" 被当作「回答」写入 episodic，随后被 consolidation
当作高价值情节晋升进长期记忆，污染召回。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bandi.agent.base import TerminalReason
from bandi.core.agent_runner import _EPISODIC_ELIGIBLE_TERMINALS, _persist_memory
from bandi.memory.ingest_review import review_episodic_ingest, review_ingest

# L1 真机取样的两条污染样本（消息中心 episodic_consolidation 条目原文）
POLLUTED_MODEL_ERROR = (
    "你好呀，你能帮我做什么？ → 回答: all LLM candidates failed "
    "(capability + config chain): Client error '400 Bad Request' for url "
    "'https://api.deepseek.com/chat/completions'"
)
POLLUTED_STREAM_BROKEN = "帮我查下文档 → 回答: 流中断，请重试"


def test_failed_turn_answer_rejected() -> None:
    verdict = review_episodic_ingest(POLLUTED_MODEL_ERROR)
    assert verdict.decision == "reject"
    assert verdict.reason == "failed_turn"


def test_interrupted_answer_rejected() -> None:
    verdict = review_episodic_ingest(POLLUTED_STREAM_BROKEN)
    assert verdict.decision == "reject"
    assert verdict.reason == "failed_turn"


def test_long_term_ingest_rejects_failed_turn() -> None:
    """契约：长期记忆直写路径（agent_runner `turn:` 项）同样拒失败文本。

    L1 真机实锤：long_term.db 里存在 id=`turn:sess-...:1`、content 为
    "all LLM candidates failed…" 的条目——该路径走 review_ingest 而非
    review_episodic_ingest，两处必须都拦。
    """
    verdict = review_ingest(
        POLLUTED_MODEL_ERROR.rsplit("→ 回答:", 1)[-1],
        memory_type="business_fact",
        source="agent_turn",
        user_input="你好呀，你能帮我做什么？",
    )
    assert verdict.decision == "reject"
    assert verdict.reason == "failed_turn"


def test_long_term_ingest_keeps_real_business_fact() -> None:
    """反向契约：失败检查不得误伤真实业务文本。

    只断言「拦截理由不是 failed_turn」——最终 accept/reject 由既有打分规则决定
    （该样本因条件数不足被判 no_signal，属正常门槛，不在本次改动范围）。
    """
    verdict = review_ingest(
        "慢查询根因是缺少复合索引，已给出 DDL 与执行计划。",
        memory_type="business_fact",
        source="agent_turn",
        user_input="帮我优化一下慢查询",
    )
    assert verdict.reason != "failed_turn"


def test_normal_business_answer_still_accepted() -> None:
    """防过度拦截：正常业务结论必须仍能记录。"""
    verdict = review_episodic_ingest(
        "帮我查一下慢查询怎么优化 → 回答: 根因是缺少复合索引，已给出 DDL 与执行计划。"
    )
    assert verdict.decision == "accept"


@pytest.mark.parametrize(
    "terminal",
    ["COMPLETED", "VERIFIED", "REWORK", "ESCALATED"],
)
def test_eligible_terminals_kept(terminal: str) -> None:
    assert terminal in _EPISODIC_ELIGIBLE_TERMINALS


def test_approval_wait_terminal_not_episodic() -> None:
    """契约：等待人工审批的回合不是「经历」，不得写入 episodic。

    破坏者视角：审批等待被当作终态放行后，每点一次「允许」都会生成一条
    「等待人工决策：请审批工具 bash」垃圾经历，并再次被提名为长期记忆。
    """
    assert "AWAITING_APPROVAL" not in _EPISODIC_ELIGIBLE_TERMINALS


@pytest.mark.parametrize(
    "text",
    [
        "用户请求开发待办App → 回答: 等待人工决策：请审批工具 bash 后继续。",
        "[事实] 等待人工决策：请审批工具 write 后继续。",
        "等待工具审批中，批准后我会继续执行。",
        "[经历] 用户请求开发待办App，等待人工审批工具write。",
        "[经历] 用户请求继续操作并添加筛选条件，需要人工审批后继续。",
        "用户请求通过 bash 工具执行 echo 命令并查看结果，需人工审批。",
        "Approval required before running this tool.",
    ],
)
def test_approval_wait_text_rejected(text: str) -> None:
    verdict = review_episodic_ingest(text)
    assert verdict.decision == "reject"
    assert verdict.reason == "approval_wait"


def test_consolidation_skips_approval_wait_episode() -> None:
    """契约：历史里已存在的审批等待 episode 不得再被整合提名。"""
    from bandi.memory.consolidation import _is_low_value_episode

    assert _is_low_value_episode(
        "用户请求开发待办App → 回答: 等待人工决策：请审批工具 bash 后继续。"
    )
    assert not _is_low_value_episode(
        "帮我查一下慢查询怎么优化 → 回答: 根因是缺少复合索引，已给出 DDL 与执行计划。"
    )


def test_unknown_terminal_treated_as_eligible() -> None:
    """UNKNOWN = 「无终态信息」而非「失败」——必须放行。

    破坏者视角：若把 UNKNOWN 归入失败类，所有不带 terminal_reason 的回合
    （既有测试夹具与部分生产路径）都会被静默禁止写经历层。
    """
    assert "UNKNOWN" in _EPISODIC_ELIGIBLE_TERMINALS


@pytest.mark.parametrize(
    "terminal",
    [
        "MODEL_ERROR",
        "MAX_TURNS",
        "TIMEOUT",
        "SAFETY_BLOCKED",
        "USER_INTERRUPT",
        "LOOP_DETECTED",
        "PROMPT_TOO_LONG",
        "TOKEN_BUDGET_EXHAUSTED",
        "NO_REPLY",
        "TOOL_ERROR",
    ],
)
def test_failed_terminals_excluded(terminal: str) -> None:
    assert terminal not in _EPISODIC_ELIGIBLE_TERMINALS


def _ctx(terminal_reason, final_message: str):
    """功能：构造 _persist_memory 所需的最小 ctx（真实字段名，缺字段会炸）。"""
    return SimpleNamespace(
        raw_input="你好呀，你能帮我做什么？",
        turn_index=1,
        terminal_reason=terminal_reason,
        final_message=final_message,
        current_content=None,
        tool_results=None,
        messages=[],
    )


def test_model_error_turn_never_recorded_episodically() -> None:
    """行为契约（接线级）：MODEL_ERROR 回合不得调用 episodic.record。

    钉生产写入路径而非常量——断开白名单接线时本用例必须变红。
    样本刻意不含失败文案（避免与文本负面项耦合），使本用例只验终态闸门。
    """
    manager = MagicMock()
    ctx = _ctx(TerminalReason.MODEL_ERROR, "已经完成了前三项，第四项的结果如下。")
    asyncio.run(_persist_memory(ctx, manager, session_id="sess-model-error"))
    assert not manager.episodic.record.called, "失败终态不得写入经历层"


def test_max_turns_half_baked_reply_never_recorded() -> None:
    """行为契约：MAX_TURNS 的半成品回复（文本正常、无失败标记）同样不得入记忆。"""
    manager = MagicMock()
    ctx = _ctx(TerminalReason.MAX_TURNS, "已完成前三项，第四项还在处理中，稍后继续。")
    asyncio.run(_persist_memory(ctx, manager, session_id="sess-max-turns"))
    assert not manager.episodic.record.called, "未完成回合的半成品回复不得写入经历层"


def test_completed_turn_still_recorded_episodically() -> None:
    """反向契约：正常完成回合仍必须写入（防过度拦截）。"""
    manager = MagicMock()
    ctx = _ctx(TerminalReason.COMPLETED, "已按要求完成检索，结论如下：优先走缓存层。")
    asyncio.run(_persist_memory(ctx, manager, session_id="sess-completed"))
    assert manager.episodic.record.called, "正常完成回合必须写入经历层"
