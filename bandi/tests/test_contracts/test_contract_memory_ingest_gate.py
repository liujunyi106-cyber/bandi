"""功能：记忆入库/晋升的门槛——一次性任务指令不得进入长期记忆或审批列表。

真机缺陷：89 条待审批里全是「读 out/a.csv 把前 3 名写到 out/b.txt」这类一次性
指令。它们的问题不是「不真实」，而是**换一个任务就失效**——留在长期层只会让
审批列表被一次性操作淹没，并让召回结果变噪音。
"""

from __future__ import annotations

import pytest

from bandi.memory.ingest_review import (
    review_episodic_ingest,
    review_with_judge,
)

TASK_INSTRUCTIONS = [
    "步骤 · 端到端做一个小工具：读 out/数据源.csv（如果不存在就自己造一个），按 score 排序，把前 3 名写到 out/排行榜.txt",
    "事实 · 帮我在项目里生成一个 out/预算表.xlsx：表头「科目/一季度/二季度/合计」",
    "步骤 · 读一下项目里 fixtures/report.docx，在文末追加一行「附录：数据截至 2026-06-30」，另存为 out/报告_终版.docx",
]

LASTING_FACTS = [
    "项目代号为「青龙」，部署端口为 7421。",
    "用户偏好简洁的中文回复，不喜欢冗长铺垫。",
    "用户的座右铭是「慢慢来比较快」，已取代旧座右铭「先把事做完」。",
]


@pytest.mark.parametrize("text", TASK_INSTRUCTIONS)
def test_task_instruction_is_rejected(text: str) -> None:
    """功能：一次性任务指令必须被拒（不是值得长期记住的事实）。"""
    verdict = review_episodic_ingest(text)
    assert verdict.decision == "reject", f"应拒但放行: {text[:40]}"
    assert verdict.reason == "task_instruction"


@pytest.mark.parametrize("text", LASTING_FACTS)
def test_lasting_fact_is_accepted(text: str) -> None:
    """功能：破坏者视角——稳定的用户/项目事实不能被误拦（拦过头会让记忆全空）。"""
    assert review_episodic_ingest(text).decision == "accept"


def test_rule_stage_saves_llm_call() -> None:
    """功能：规则已拦的内容不再问模型——省一次调用，也避免模型把明显噪音判回来。"""
    calls: list[str] = []

    def spy(prompt: str) -> str:
        calls.append(prompt)
        return "ACCEPT"

    review_with_judge(TASK_INSTRUCTIONS[0], spy)
    assert calls == [], "规则拒绝后不应再调模型"


def test_llm_rejects_ambiguous_text() -> None:
    """功能：规则放行但语义模糊的内容，交给模型精判拒掉。"""
    # 规则层看不出的语义噪音（无指令动词、无路径），只能靠模型判
    verdict = review_with_judge("今天天气不错，随便聊了几句关于效率的话题", lambda _p: "REJECT")
    assert verdict.decision == "reject"
    assert verdict.reason == "llm_judged"


def test_llm_failure_keeps_rule_verdict() -> None:
    """功能：模型不可用不得丢弃候选——审批是增强项，失败退化为规则结论。"""

    def broken(_prompt: str) -> str:
        raise RuntimeError("model down")

    assert review_with_judge(LASTING_FACTS[0], broken).decision == "accept"


def test_no_llm_falls_back_to_rules() -> None:
    """功能：未注入模型时纯规则判定（无网络单测路径）。"""
    assert review_with_judge(TASK_INSTRUCTIONS[0], None).decision == "reject"
    assert review_with_judge(LASTING_FACTS[0], None).decision == "accept"


def test_promotion_path_uses_same_review() -> None:
    """功能：晋升路径与入库路径共用同一判断——否则任务指令会从晋升口漏进审批列表。"""
    import inspect

    from bandi.memory import maintenance

    source = inspect.getsource(maintenance.promote_notable_episodes)
    assert "review_episodic_ingest" in source, "晋升路径必须走完整入库判断"
