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

from bandi.memory.ingest_review import is_conversational_prose, review_ingest

# L1 中实际入库的 19 条助手原话（节选，代表五类污染）
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
