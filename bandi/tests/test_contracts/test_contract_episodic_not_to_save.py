"""契约：经历层入口负面清单。

缺陷实证：`review_episodic_ingest` 只拒空/单字/≤12字纯寒暄，导致：
- 自我介绍与能力清单照单全收（实测 episodic.db 14 条里 5 条同类）
- 这类情节被相似问句反复召回，`recall_count` 虚高到 8，把自己顶成高价值候选，
 进而生成噪音晋升审批

本契约钉**入口**行为：可从现状推导的内容与自我介绍不进经历层，而真实排查经历
（用户问题里带路径/报错）必须照记——判据只看「回答侧」。"""

from __future__ import annotations

import pytest

from bandi.memory.ingest_review import review_episodic_ingest


class TestEpisodicNotToSave:
    @pytest.mark.parametrize(
        ("summary", "reason"),
        [
            (
                "用户: 你好呀，你能帮我做什么？ → 回答: 老板好！我是 Bandi 助手，"
                "能帮您处理各类事务：\n\n**信息检索**\n- 搜索互联网获取最新资讯",
                "self_intro",
            ),
            ("用户: 你是谁 → 回答: 我是 Bandi 助手，我可以帮你管理 AI 员工团队", "self_intro"),
            ("用户: 配置一下 → 回答: 查了下 git log，最近三个提交都是改 README", "derivable"),
            ("用户: 看看 → 回答: 代码架构是 MVC 三层，文件路径在 src/ 下", "derivable"),
            ("用户: 排查 → 回答: 修复步骤是先加日志再定位，调试配方已记录", "derivable"),
        ],
    )
    def test_not_to_save_rejected_at_entry(self, summary: str, reason: str) -> None:
        verdict = review_episodic_ingest(summary)
        assert verdict.decision == "reject", f"应拒收，实际 {verdict.decision}"
        assert verdict.reason == reason, f"理由应为 {reason}，实际 {verdict.reason}"

    def test_real_debug_experience_still_recorded(self) -> None:
        """破坏者视角：用户问题里带路径/报错的真实排查不能被误杀。"""
        summary = (
            "用户: 帮我修个 bug 报错 500 → 回答: 已定位到 src/a.py 的空指针并修复，"
            "根因是变量未初始化，已补单测覆盖该分支"
        )
        verdict = review_episodic_ingest(summary)
        assert verdict.decision == "accept", f"真实经历应记录，实际 {verdict}"

    def test_business_fact_without_derivable_markers_recorded(self) -> None:
        summary = (
            "用户: 客户是谁 → 回答: 本季度续约客户为明远科技的林总，"
            "预算 30 万，决策人是 CTO，竞品是星海"
        )
        verdict = review_episodic_ingest(summary)
        assert verdict.decision == "accept", f"业务事实应记录，实际 {verdict}"

    def test_conclusion_signal_beats_self_intro(self) -> None:
        """含结论信号的自我介绍不算低价值——它同时带事实产出。"""
        summary = "用户: 帮我看看这个方案 → 回答: 我是 Bandi 助手。建议你先建立项目空间再派单"
        verdict = review_episodic_ingest(summary)
        assert verdict.decision == "accept", f"带建议的回复应记录，实际 {verdict}"


class TestTaskResultAndExplicitRemember:
    def test_task_result_table_is_remembered(self) -> None:
        """任务结果表格是业务事实，不能被「多行清单」形态误杀。"""
        summary = (
            "用户: 我有三件小事，请先拆成任务清单再逐项完成 → 回答: 已拆成4项任务并完成：\n\n"
            "| 任务 | 结果 |\n|------|------|\n"
            "| ① 去重后总人数 | **5人**（张伟重复1条） |\n"
            "| ② 研发 | 2人 |\n| ③ 市场 | 2人 |\n| ④ 运营 | 1人 |\n"
            "| ⑤ 最早入职年份 | 2021年 |\n\n"
            "**汇总：去重后共 5 人（研发 2、市场 2、运营 1），最早入职年份为 2021 年。**"
        )
        verdict = review_episodic_ingest(summary)
        assert verdict.decision == "accept", f"任务结果表格应记录，实际 {verdict}"

    def test_explicit_remember_short_ack_is_remembered(self) -> None:
        """用户明确说「记住」时，短确认回复也要留在经历层。"""
        summary = "用户: 记住：我的偏好是回答简洁 → 回答: 好的，已记住"
        verdict = review_episodic_ingest(summary)
        assert verdict.decision == "accept", f"显式记住应记录，实际 {verdict}"
