"""契约：助手话术不得进入长期记忆。

背景：用户只给指令（「记录恒星贸易项目」）、事实由助手查出时，助手结论会回落
进长期记忆。但 `review_ingest` 的「≥60 字即有长期价值」让**任何长回复**都通过，
L1 真机实测 72 条曾把 19 条助手原话整段入库。

入库的不是「价值低」的内容，是**根本不是事实**的东西：拒绝话术、目录清单、
执行计划、能力清单、故障说明。它们会在后续召回中冒充用户偏好与历史事实。

判定边界要严：真正的结论（「结论：恒星贸易项目已确认合同金额五十万元…」）
必须照常入库，否则会把「记录X→记住结论」这个正常流程一起砍掉。
"""

from __future__ import annotations

import pytest

from bandi.memory.ingest_review import (
    is_conversational_prose,
    is_noise_memory,
    review_episodic_ingest,
    review_ingest,
)

# L1 真机中实际入库的 19 条助手原话（节选，代表五类污染）
POLLUTED = [
    ("permission", "抱歉，我当前处于**计划模式**，只允许只读操作，无法写入文件"),
    ("permission", "老板，当前处于计划模式，无法直接执行写入操作。我先制定计划"),
    ("failure", "老板，天气查询服务暂时不可用，我无法获取今天的实时天气信息。"),
    ("failure", "老板，搜索服务暂时不可用，我帮您查不到 AI 方面的最新新闻。"),
    ("failure", "老板，我当前工作环境的沙箱限制无法直接访问您的桌面路径"),
    ("inventory", "老板，fixtures 目录下没有找到 sales.csv。当前存在的 CSV 有：\n\n- staff.csv"),
    ("inventory", "老板，fixtures 目录下的文件情况如下：\n\n**根目录（7 个文件）**\n| calc.py |"),
    ("inventory", "老板，您的桌面内容挺丰富的！我帮您整理了一下：\n\n## 主要文件夹"),
    ("inventory", "老板，**未找到**这三个文件。\n\nfixtures 目录当前只有：\n- calc.py"),
    ("plan", "老板，分析完毕。两个函数结构完全一致。计划如下：\n\n1. 提取公共函数"),
    ("plan", "## 修复计划\n\n### 问题定位\n- `test_calc.py` 测试 calc 模块"),
    ("plan", "老板，整理了执行步骤，请确认：\n\n**步骤列表：**"),
    ("capability", "老板好！我能帮你做这些：\n\n**我能做的：**\n- 联网搜索信息"),
]

# 真正的助手结论——必须照常入库
LEGIT = [
    "结论：第一轮回答：恒星贸易项目已确认合同金额五十万元，联系人王经理，"
    "下一步确认交付日期，议价空间百分之七十五。",
    "结论：第一轮回答：恒星贸易项目已完成方案评审，预算五十万元已确认，"
    "联系人王经理将在下周一确认最终排期，整体进度百分之六十五。",
]


class TestProseDetection:
    @pytest.mark.parametrize("kind,text", POLLUTED)
    def test_polluted_detected(self, kind: str, text: str) -> None:
        assert is_conversational_prose(text) is not None, f"{kind} 类话术未被识别：{text[:40]}"

    @pytest.mark.parametrize("text", LEGIT)
    def test_real_conclusions_not_flagged(self, text: str) -> None:
        """破坏者视角：闸门过宽会把「记录X→记住结论」一起砍掉。"""
        assert is_conversational_prose(text) is None

    def test_empty_rejected(self) -> None:
        assert is_conversational_prose("") is not None


class TestReviewIngestRejectsProse:
    @pytest.mark.parametrize("kind,text", POLLUTED)
    def test_prose_never_accepted(self, kind: str, text: str) -> None:
        v = review_ingest(text, memory_type="task_state", user_input="帮我看看")
        assert v.decision != "accept", f"{kind} 类话术被入库了：{text[:40]}"

    @pytest.mark.parametrize("text", LEGIT)
    def test_real_conclusion_still_accepted(self, text: str) -> None:
        v = review_ingest(text, memory_type="process_conclusion", user_input="记录恒星贸易项目")
        assert v.decision == "accept", f"真实结论被误拒：{v.reason}"

    def test_explicit_remember_bypasses_prose_gate(self) -> None:
        """用户明确说「记住」时，他就是要记这段内容——闸门不得拦。"""
        v = review_ingest(
            "老板，当前处于计划模式，无法写入文件",
            memory_type="preference",
            user_input="记住：计划模式下不能写文件",
        )
        assert v.decision == "accept"


class TestTaskResultAndExplicitRemember:
    def test_task_result_table_is_not_prose(self) -> None:
        """任务结果表格是业务事实，不该被「多行清单」形态误判为话术。"""
        text = (
            "已拆成4项任务并完成：\n\n"
            "| 任务 | 结果 |\n|------|------|\n"
            "| ① 去重后总人数 | **5人** |\n| ② 研发 | 2人 |\n"
            "| ③ 市场 | 2人 |\n| ④ 运营 | 1人 |\n| ⑤ 最早入职年份 | 2021年 |"
        )
        assert is_conversational_prose(text) is None

    def test_explicit_remember_short_ack_accepted(self) -> None:
        v = review_ingest(
            "好的，已记住",
            memory_type="preference",
            user_input="记住：我的偏好是回答简洁",
        )
        assert v.decision == "accept", f"显式记住被低价值闸门误拒：{v.reason}"


class TestGreetingTurnNotSaved:
    def test_long_answer_greeting_rejected(self) -> None:
        """功能：用户只打招呼时，即使助手回答很长，也不得写入剧情记忆。"""
        summary = (
            "用户: 你好 → 回答: 老板好！我是 Bandi 数字员工中的产品设计师，"
            "很高兴为您服务。请问有什么可以帮您？"
        )
        assert is_noise_memory(summary) == "greeting"
        assert review_episodic_ingest(summary).decision == "reject"
        v = review_ingest(
            "这是一段很长的回答内容，" * 20,
            memory_type="business_fact",
            user_input="你好",
        )
        assert v.decision == "reject", f"问候轮不得写长期记忆：{v.reason}"
        assert v.reason == "greeting"

    def test_explicit_remember_still_allowed(self) -> None:
        """功能：显式「记住」豁免优先于问候过滤。"""
        v = review_ingest(
            "好的，已记住",
            memory_type="preference",
            user_input="记住：我的偏好是回答简单",
        )
        assert v.decision == "accept"


class TestGreetingWithTaskSuffix:
    def test_greeting_with_parenthetical_suffix_rejected(self) -> None:
        """功能：任务标题重复括号「你好（你好）」仍按问候拒绝。"""
        summary = "任务: 你好（你好） → 回答: 任务 #你好 已完成，当前状态正常，等待新任务。"
        assert is_noise_memory(summary) == "greeting"
        assert review_episodic_ingest(summary).decision == "reject"
