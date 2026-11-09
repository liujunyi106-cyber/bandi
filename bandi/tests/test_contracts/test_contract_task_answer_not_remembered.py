"""契约：任务应答快照不得进长期记忆（第 ⑥ 类事实性闸门）。

L1 G20 组暴露：72 条跑完，长期记忆里灌进多条**助手应答正文**——它们
"有具体事实"故前五类闸门都拦不住，但本质是**一次查询的结果快照**：

- G20-04「去重后共 5 人，分别是张伟、李娜、王芳、刘洋、**陈敏**」
 （真实 CSV 是"陈静"——连当时的幻觉一起固化进记忆库）
- G20-08「后者第一行是：**姓名,部门,入职年份**」
- fixtures 目录清单、sales.csv 未找到的回复……

CSV 一换内容这些结论立刻过时，却已进库并反复召回。判据取**双信号**：
引用了工具产物路径 **且** 给出计数/名单/引述结论——单独任一个仍可能是合法
事实（「老板偏好 fixtures/ 下的数据」含路径但无结论）。"""

from __future__ import annotations

import pytest

from bandi.memory.ingest_review import is_conversational_prose

REJECT_CASES = [
    pytest.param(
        "老板，从 `fixtures/staff.csv` 中提取姓名列，统计结果如下：\n\n"
        "**不重复人名：5 个**\n\n分别是：张伟、李娜、王芳、刘洋、陈敏\n\n"
        "（第 2 行和第 5 行的「张伟」重复，去重后共 5 人）",
        id="g20-04-真实污染条目",
    ),
    pytest.param(
        "`fixtures/readme_不存在的.md` 不存在，已改读 `fixtures/staff.csv`。\n\n"
        "后者第一行是：**姓名,部门,入职年份**",
        id="g20-08-引述型无计数",
    ),
    pytest.param("已扫描 C:\\Users\\user\\Desktop\\报告 目录，共 12 个文件", id="无扩展名绝对路径"),
    pytest.param("处理 `out/人员.txt` 内容，合计 40 条记录", id="合计式"),
    pytest.param("`fixtures/staff.csv` 一共有 5 个人", id="一共有+人"),
]

ALLOW_CASES = [
    pytest.param("老板习惯用 fixtures/ 目录下的数据做分析", id="含路径但无结论"),
    pytest.param("老板把素材放在 C:\\Users\\user\\Desktop\\L1评测素材 里", id="绝对路径但无结论"),
    pytest.param("老板的 DeepSeek API key 已配置完成，模型可用", id="无路径的事实"),
    pytest.param("老板要求所有报告输出到 out/ 目录", id="无路径的偏好"),
    pytest.param("项目采用 pytest -n 4 并行跑测试", id="技术约定"),
    pytest.param("老板入职 3 年，熟悉 Go 与 Python", id="计数但无产物引用"),
]


class TestRejectTaskAnswerSnapshot:
    @pytest.mark.parametrize("text", REJECT_CASES)
    def test_rejected(self, text: str) -> None:
        assert is_conversational_prose(text) == "task_answer_snapshot"


class TestAllowRealFacts:
    @pytest.mark.parametrize("text", ALLOW_CASES)
    def test_allowed(self, text: str) -> None:
        assert is_conversational_prose(text) is None

    def test_explicit_remember_bypasses_prose_gate(self) -> None:
        """破坏者视角：闸门不得把「用户明确要求记住」的路也堵死——
        该路径由 agent_runner 的 explicit_remember 前置分支处理，此处只保证
        闸门本身不误杀纯事实。"""
        assert is_conversational_prose("记住：我的测试账号是 demo001，密码在 vault 里") is None


class TestNoSingleSignalRejection:
    def test_artifact_ref_alone_allowed(self) -> None:
        """破坏者视角：只有路径没有结论不得拒——否则误杀合法事实。"""
        assert is_conversational_prose("我的项目代码在 C:\\Users\\me\\Desktop\\proj") is None

    def test_result_claim_alone_allowed(self) -> None:
        """只有计数没有产物引用不得拒——日常陈述大量含计数。"""
        assert is_conversational_prose("老板一共买了 3 台显示器") is None
