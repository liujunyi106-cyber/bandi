# 功能：契约测试——自动串行链子卡标题取计划中的交付物，不得用岗位提示词
from __future__ import annotations

from bandi.company.company_mode import _auto_chain_title


class TestAutoChainTitle:
    def test_table_row_deliverable_used(self) -> None:
        brief = (
            "## 分工与执行链\n"
            "| 顺序 | 交付物 | 负责人 | 状态 |\n"
            "| 1 | 用户画像与 PRD | 产品设计师 | 待执行 |\n"
            "| 2 | 视觉 DNA 卡 | 视觉设计师 | 待执行 |"
        )
        assert _auto_chain_title(brief, "视觉设计师", "aid_visual") == "视觉 DNA 卡"

    def test_role_id_matched(self) -> None:
        brief = "| 1 | 原型 HTML | aid_frontend | 待执行 |"
        assert _auto_chain_title(brief, "前端代码员工", "aid_frontend") == "原型 HTML"

    def test_no_table_uses_member_task_block(self) -> None:
        brief = "## 成员任务\n- @视觉设计师：输出配色与字体方案"
        assert _auto_chain_title(brief, "视觉设计师", "aid_visual") == "输出配色与字体方案"

    def test_no_match_neutral_fallback_not_prompt(self) -> None:
        fallback = _auto_chain_title("", "视觉设计师", "aid_visual")
        assert fallback == "视觉设计师：本环节交付"
        assert "你是视觉设计师" not in fallback
