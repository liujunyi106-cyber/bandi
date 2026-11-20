"""功能：`_is_reasoning_only` 必须排除「发起工具调用」的正常轮次。

真机缺陷（每轮白烧一次候选链）：

    WARN fallback deepseek/deepseek-v4.1-flash -> _ReasoningOnlyError: (空消息)
    INFO llm call: ... finish=tool_calls          ← 紧接着就成功，且是工具调用

日志里 `_ReasoningOnlyError` 出现 85 次，其后全部是正常的 `finish=tool_calls`。

根因：思考型模型**发起工具调用时 content 天然为空**（正文要等工具结果之后的
那一轮才给出），reasoning 里正是「我要调用 X」的思考。旧判定只看
`content 空 && reasoning 非空`，把这轮完全正常的工具调用判成 reasoning-only，
触发换模型重问 → 整条候选链白走一遍 → 长会话下直接吃掉预算。

本契约钉四条判据边界。
"""

from __future__ import annotations

from bandi.agent.states import _is_reasoning_only
from bandi.core.message import LLMResponse


def test_tool_call_round_is_not_reasoning_only() -> None:
    """功能：发起工具调用（正文天然为空）必须视为正常轮次。"""
    response = LLMResponse(
        content="",
        reasoning="我需要先看目录，调用 bash。",
        tool_calls=[{"id": "c1", "name": "bash", "arguments": {"cmd": "ls"}}],
    )
    assert not _is_reasoning_only(response, safety_finish=False), (
        "工具调用轮被误判为 reasoning-only：会触发无谓重问，每轮白烧一次候选链"
    )


def test_plain_reasoning_only_still_detected() -> None:
    """功能：真的只出推理不给正文（无工具调用）仍须识别（防过度放宽）。"""
    response = LLMResponse(content="", reasoning="答案是 15，但我不写出去。")
    assert _is_reasoning_only(response, safety_finish=False)


def test_content_present_is_never_reasoning_only() -> None:
    """功能：有正文即正常。"""
    assert not _is_reasoning_only(
        LLMResponse(content="15", reasoning="think"), safety_finish=False
    )


def test_empty_without_reasoning_is_not_reasoning_only() -> None:
    """功能：既无正文也无推理属另一类故障（真空响应），不归本闸门。"""
    assert not _is_reasoning_only(LLMResponse(content=""), safety_finish=False)


def test_safety_finish_never_flagged() -> None:
    """功能：安全终止（内容被过滤）不得触发重问——重问会绕过安全判定。"""
    response = LLMResponse(content="", reasoning="blocked")
    assert not _is_reasoning_only(response, safety_finish=True)
