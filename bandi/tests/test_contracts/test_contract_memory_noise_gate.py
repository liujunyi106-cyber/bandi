"""契约：噪声闸门必须拒掉「任务完成汇报」，且不误伤合法长期内容。

缺陷实证（真机 2026-11-06）：长期记忆审批里出现
「[事实] 做好了老板！待办列表 App 已生成：预览路径：canvas/待办清单-app/index.html 功能：…」
——`distill.rule_distill` 取情节回答侧原文 + 晋升门不挡执行汇报所致。
本契约钉住的是**判据层**：这类文本在进入情节层 / 长期层 / 晋升通道前就被拒。
"""

from __future__ import annotations

import pytest

from bandi.memory.ingest_review import (
    is_execution_report,
    is_noise_memory,
    is_rememberable_user_input,
    review_episodic_ingest,
    review_ingest,
)

# 真机原文（取自 ~/.bandi/memory/pending/memory/*.json 的 summary / payload.content）
EXECUTION_REPORTS = [
    "做好了老板！待办列表 App 已生成： 预览路径：canvas/待办清单-app/index.html 功能： "
    "- 输入框添加待办（支持回车） - 点击圆圈标记完成/取消 - × 按钮删除",
    "老板，删除功能已补上！ 当前功能： - ✅ 输入框 + 添加按钮（支持回车） - ✅ 待办列表展示",
    "老板，待办事项 App 已完成！ 功能： - 输入框 + 添加按钮（支持回车添加） - 待办列表实时展示 "
    "预览路径： canvas/待办事项-app/index.html",
    "已创建完成！ 功能： - 输入框 + 添加按钮（支持回车添加） - 显示待办数量统计",
]


@pytest.mark.parametrize("text", EXECUTION_REPORTS)
def test_execution_report_is_detected(text: str) -> None:
    """四类真机汇报文都要被判成执行汇报。"""
    assert is_execution_report(text), f"未识别为执行汇报：{text[:40]}"


@pytest.mark.parametrize("text", EXECUTION_REPORTS)
def test_execution_report_rejected_by_both_layers(text: str) -> None:
    """情节层与长期层用同一判据：同一段文本两边都拒。"""
    assert is_noise_memory(text) == "execution_report"
    episodic = review_episodic_ingest(text)
    assert episodic.decision == "reject", f"情节层不该收：{episodic.reason}"
    assert episodic.reason == "execution_report"

    long_term = review_ingest(text, memory_type="business_fact")
    assert long_term.decision == "reject", f"长期层不该收：{long_term.reason}"


def test_execution_report_inside_episode_wrapper() -> None:
    """情节摘要形如「用户: X → 回答: Y」——回答侧是汇报也要拒。"""
    wrapped = (
        "用户: 帮我做个待办 App → 回答: 做好了老板！待办列表 App 已生成："
        "预览路径：canvas/待办清单-app/index.html"
    )
    assert is_noise_memory(wrapped) == "execution_report"


@pytest.mark.parametrize("text", ["ping", "pong", "测试", "在吗"])
def test_trivial_probe_episode_rejected(text: str) -> None:
    """连通性探针不构成经历（真机实证：ping → pong 曾被提名晋升）。"""
    assert is_noise_memory(f"用户: {text} → 回答: 老板，pong。收到消息，一切正常。") == "greeting"


LEGITIMATE = [
    "老板的偏好是报告用中文，结论不超过三条",
    "项目约定：所有交付文档放在 交付文档收集/ 目录下",
    "已有结论：缓存层统一用 Redis，不再引入本地缓存",
    "用户要求汇报格式：任务 #N 已完成 → 交付文件：<相对路径> → 关键结论（≤3 条）",
]


@pytest.mark.parametrize("text", LEGITIMATE)
def test_legitimate_content_not_mistaken_for_execution_report(text: str) -> None:
    """破坏者视角：合法偏好/约定/结论不得被完成词误杀。"""
    assert not is_execution_report(text), f"误判为执行汇报：{text}"
    assert is_noise_memory(text) is None, f"误判为噪声：{text}"


def test_explicit_remember_still_wins() -> None:
    """边界：用户明确说「记住」时不受噪声闸门约束（他就是要记这段话）。"""
    verdict = review_ingest(
        "做好了老板！已生成：预览路径：canvas/x.html",
        memory_type="business_fact",
        user_input="记住这个预览路径",
    )
    assert verdict.decision == "accept", f"显式记住应放行，实际 {verdict.decision}/{verdict.reason}"


TASK_CONTRACT_TEMPLATE = (
    "## 任务契约\n"
    "请按以下要求完成本次任务：\n"
    "- 至少 3 个步骤\n"
    "- 输出到 out/ 目录\n"
    "- 完成后回复 OK\n"
    "## 输出要求\n"
    "结论不超过 3 条。\n"
)


def test_task_contract_template_not_rememberable() -> None:
    """公司派单的任务契约模板不是用户陈述——模板里的编号/数量不得让「含数字」恒真。

    缺陷实证：员工每轮 raw_input 都是 seat_executor 拼的任务契约，`含数字 ⇒ 可记`
    使每一轮执行都被写成长期记忆，过程块因此恒显示「计入记忆 x3」。
    """
    assert not is_rememberable_user_input(TASK_CONTRACT_TEMPLATE)


def test_short_user_statement_with_number_still_rememberable() -> None:
    """破坏者视角：用户真实短陈述里的数字仍可记（端口/编号这类稳定事实）。"""
    assert is_rememberable_user_input("记住：部署端口是 7421")
    assert is_rememberable_user_input("我的工位在 3 楼")
