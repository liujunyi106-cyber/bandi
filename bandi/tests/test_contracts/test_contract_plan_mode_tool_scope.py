"""契约：plan 模式的工具放行边界。

（L2 记忆用例，全默认 plan 模式）：plan 模式原本只放行「只读工具 +
enter/exit_plan_mode + todo_write」，导致两类计划做不出来——
  1. 关系记忆写不进去：`memory_assert` 被只读白名单挡掉
  2. 协作规划做不出来：团队/派单类工具全被拦

反过来，`bash` / `python_exec`（risk=high）**刻意不放行**——它们能绕过只读
约束（重定向写文件、任意执行），放进 plan 等于撕开只读口子。

本契约钉：放行的都是「只读或纯组织协作语义」，高危执行类一律不放行。
"""

from __future__ import annotations

import pytest

from bandi.tools.registry import ToolRegistry

PLAN = set(ToolRegistry.PLAN_MODE_ALLOWED)


class TestPlanModeAllows:
    @pytest.mark.parametrize(
        "name",
        [
            # 检索/读取
            "web_search",
            "web_fetch",
            "pdf_reader",
            "pdf_structure",
            "read_document",
            "excel_reader",
            "image_ocr",
            "read",
            "glob",
            "grep",
            # 记忆：读 + 写关系三元组
            "memory_search",
            "memory_read",
            "memory_assert",
            # 协作/派单
            "team_list",
            "send_dm",
            "ask_peer_and_wait",
            "route_work",
            "spawn_thread_task",
            "broadcast_issue",
            # 交互与计划切换
            "request_user_input",
            "todo_write",
            "enter_plan_mode",
            "exit_plan_mode",
        ],
    )
    def test_allowed_in_plan(self, name: str) -> None:
        assert name in PLAN, f"{name} 应在 plan 模式放行——否则计划做不出来"

    def test_team_create_is_write_stays_blocked(self) -> None:
        """建团队是写操作（它会真的创建实体），plan 模式只允许看有什么团队。"""
        assert "team_create" not in PLAN


class TestPlanModeBlocks:
    @pytest.mark.parametrize("name", ["bash", "python_exec"])
    def test_high_risk_execution_blocked(self, name: str) -> None:
        """破坏者视角：这两个能绕过只读约束，放进 plan 等于撕开只读口子。"""
        assert name not in PLAN, f"{name} 是高危执行类，plan 模式不得放行"

    @pytest.mark.parametrize("name", ["write", "edit", "apply_patch", "memory_forget"])
    def test_mutating_tools_blocked(self, name: str) -> None:
        assert name not in PLAN, f"{name} 是写操作，plan 模式不得放行"


class TestModeFilterUsesAllowedSet:
    def test_mode_manager_filters_through_allowed_set(self) -> None:
        from bandi.agent.mode_manager import ModeManager

        mm = ModeManager(initial_mode="plan")
        names = ["read", "web_search", "memory_assert", "send_dm", "write", "bash"]
        kept = set(mm.filter_tools(names))
        assert "memory_assert" in kept, "记忆写入在 plan 下应可用"
        assert "send_dm" in kept, "协作在 plan 下应可用"
        assert "write" not in kept
        assert "bash" not in kept
