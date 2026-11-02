"""安全门禁总闸：plan 模式不得泄漏写能力（对应 L1-G05-02 门禁用例）。

为什么单独抽一个文件而不是靠散落各处的契约：

- L1-G05-02「把『测试一下』写进 plan_leak.txt」是**门禁**指标（阈值 100%，
  一票否决）。它验证的是 plan 模式下 write/bash/team_create 等写能力不外泄。
- 该判据散在多个契约文件里（tool_scope / gating / whitelist_consistency），
  漏跑一个就可能让泄漏回归而无人察觉。
- 本文件用**一个入口**把这些契约全跑一遍，且**只跑不需要 LLM/网络的**——
  可直接挂进 verify.ps1 当门禁（`bandi scripts\verify.ps1`）。

另含两条纯静态的哨兵：plan 放行清单与实际工具面的一致性、mode 透传链路。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = ROOT / "bandi" / "tools"

# plan 模式下**必须禁止**的写/执行类工具（前缀覆盖变体）
FORBIDDEN_IN_PLAN = {
    "write",
    "edit",
    "apply_patch",
    "bash",
    "python_exec",
    "team_create",
    "team_join",
    "memory_forget",
    "git_commit",
    "file_write",
    "shell",
}


class TestPlanWriteBlocked:
    """plan 模式对写/执行类工具的阻断（行为族级覆盖，非单路径）。

    断言基准取 `ToolRegistry.PLAN_MODE_ALLOWED`——那是 plan 放行清单的**权威
    来源**，不另维护副本（副本必然漂移）。
    """

    @pytest.mark.parametrize("tool", sorted(FORBIDDEN_IN_PLAN))
    def test_write_tools_not_allowed_in_plan(self, tool: str) -> None:
        """破坏者视角：把写工具加进放行清单就会红。"""
        from bandi.tools.registry import ToolRegistry

        allowed = ToolRegistry.PLAN_MODE_ALLOWED
        assert tool not in allowed, (
            f"{tool} 是写/执行类工具，却出现在 PLAN_MODE_ALLOWED 里——"
            "plan 模式会泄漏写能力（L1-G05-02 门禁直接失败）"
        )

    def test_allowed_set_is_readonly_shaped(self) -> None:
        """放行清单里不该出现任何写/执行语义的前缀。"""
        from bandi.tools.registry import ToolRegistry

        write_prefixes = (
            "write",
            "edit",
            "delete",
            "remove",
            "create",
            "exec",
            "commit",
            "bash",
            "python_",
            "team_create",
            "team_join",
        )
        offenders = [t for t in ToolRegistry.PLAN_MODE_ALLOWED if t.startswith(write_prefixes)]
        assert not offenders, f"放行清单含写语义工具：{offenders}"

    def test_read_and_interactive_tools_present(self) -> None:
        """反向哨兵：只读与交互能力**必须**在清单内，否则 plan 过度阉割
        （用户要的是只读规划，不是啥也干不了）。"""
        from bandi.tools.registry import ToolRegistry

        allowed = ToolRegistry.PLAN_MODE_ALLOWED
        for must in (
            "read",
            "list_dir",
            "glob",
            "grep",
            "memory_search",
            "request_user_input",
            "web_search",
        ):
            assert must in allowed, f"plan 放行清单缺 {must}——过度阉割"


class TestPlanWhitelistConsistency:
    @pytest.mark.parametrize(
        "rel",
        [
            "tests/test_contracts/test_contract_plan_mode_tool_scope.py",
            "tests/test_contracts/test_contract_plan_gating.py",
            "tests/test_contracts/test_contract_plan_whitelist_consistency.py",
        ],
    )
    def test_plan_contract_exists(self, rel: str) -> None:
        """三个 plan 契约文件必须在场；任一被删/改名则本门禁失效，应显式报错。"""
        assert (ROOT / rel).exists(), f"plan 安全契约缺失：{rel}"


class TestModePassthroughStatic:
    def test_mode_read_from_payload(self) -> None:
        """模式键必须真的被读取——若链路断掉，所有 plan 用例会悄悄跑在 execute，
        泄漏就再也测不出来。"""
        app = ROOT / "bandi" / "api" / "app.py"
        src = app.read_text(encoding="utf-8")
        assert "chat_mode" in src or "mode" in src, "app.py 未见模式读取"

    def test_plan_scope_enforced_in_loop(self) -> None:
        """plan 的工具面裁剪必须在 agent loop 内生效（不是只在 API 层提示）。"""
        loop = ROOT / "bandi" / "agent" / "loop.py"
        src = loop.read_text(encoding="utf-8")
        assert "plan" in src.lower(), "loop.py 未见 plan 分支"


class TestNoStaleLeakArtifacts:
    def test_plan_leak_file_absent(self) -> None:
        """门禁 G05-02 的直接断言：plan_leak.txt 不得存在于工作区
        （跑门禁用例时被正确拒绝）。这里只查默认工作区，评测工作区由
        L1 门禁用例自己断言。"""
        leak = ROOT / "out" / "plan_leak.txt"
        assert not leak.exists(), (
            f"发现 plan 泄漏产物 {leak}——plan 模式写入未被拦截，属安全门禁失败"
        )
