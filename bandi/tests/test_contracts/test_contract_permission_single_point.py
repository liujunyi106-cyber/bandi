# 功能：权限评估单点契约——deny/mode/shell 子检查每次工具调用只评估一次（6.1 收口）
# 私有子检查（deny/mode/shell），与 `PermissionMiddleware` 的 `pp.check` 重复评估；
# 拦截行为不变（破坏者：删重复检查不得让任何拒绝路径漏网）。
from __future__ import annotations

from typing import Any

import pytest

from bandi.agent.factory import build_policy_engine, build_tool_pipeline
from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.executor import ToolExecutor
from bandi.tools.permissions_pipeline import PermissionsPipeline
from bandi.tools.registry import ToolRegistry


class _BashStub(BaseTool):
    name: str = "bash"
    description: str = "stub"
    category: ToolCategory = ToolCategory.UTIL
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }
    risk_level: str = "high"

    async def execute(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        return {"ok": True, "command": params.get("command")}


def _spy_pipeline_counters(
    pp: PermissionsPipeline, monkeypatch: pytest.MonkeyPatch
) -> dict[str, int]:
    """功能：统计权限子检查的**判据计算**次数（行为断言：单点计算）。

    mode 的观测点是 `_compute_mode_verdict` 而非 `_check_mode` 包装：同一次调用里
    「参数校验前的模式闸门」与「权限流水线内的模式检查」都要问同一个问题
    （该工具在当前模式可否用），错误优先级要求两者各就各位、包装被调两次。
    判据本身只取决于 (工具名, 模式) 且与参数无关，由 `_check_mode` 记忆化后
    只算一次。deny/shell 要遍历规则集，成本随规则数放大，直接观测其包装。
    """
    counts = {"deny": 0, "mode": 0, "shell": 0}
    for attr, key in (
        ("_check_deny_list", "deny"),
        ("_compute_mode_verdict", "mode"),
        ("_check_shell_safety", "shell"),
    ):
        original = getattr(pp, attr)

        def make_spy(orig: Any, bucket: str):
            def spy(*args: Any, **kwargs: Any):
                counts[bucket] += 1
                return orig(*args, **kwargs)

            return spy

        monkeypatch.setattr(pp, attr, make_spy(original, key))
    return counts


def _executor(pp: PermissionsPipeline, mode: str = "execute") -> ToolExecutor:
    registry = ToolRegistry()
    registry.register_instance(_BashStub())
    return ToolExecutor(
        registry,
        permissions_pipeline=pp,
        policy_engine=build_policy_engine(pp, "user"),
        tool_pipeline=build_tool_pipeline(pp),
        mode=mode,
    )


class TestSingleEvaluationPoint:
    @pytest.mark.asyncio
    async def test_subchecks_evaluated_once_per_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """正常调用：deny/mode/shell 子检查各只评估一次（无重复评估点）。"""
        pp = PermissionsPipeline(bypass_tools={"bash"})
        counts = _spy_pipeline_counters(pp, monkeypatch)
        executor = _executor(pp)
        results = await executor.execute(
            [ToolCall(id="c1", name="bash", arguments={"command": "echo hello"})]
        )
        assert results and results[0].success, results
        assert counts == {"deny": 1, "mode": 1, "shell": 1}, (
            f"权限子检查必须单点评估（实际 {counts}）——重复评估会随规则数放大"
        )

    @pytest.mark.asyncio
    async def test_deny_still_blocks_after_dedupe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """破坏者：删重复检查不得打开缺口——deny 命令仍必须被拦截。"""
        pp = PermissionsPipeline(bypass_tools={"bash"}, deny_commands=["rm -rf"])
        counts = _spy_pipeline_counters(pp, monkeypatch)
        executor = _executor(pp)
        results = await executor.execute(
            [ToolCall(id="c1", name="bash", arguments={"command": "rm -rf /tmp/data"})]
        )
        assert results and results[0].success is False, "deny 命令不得放行"
        assert (
            "denied" in (results[0].error or "").lower()
            or "deny" in (results[0].error or "").lower()
        )
        assert counts["deny"] == 1

    @pytest.mark.asyncio
    async def test_mode_restriction_still_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """破坏者：plan 模式的写类限制仍必须生效（mode 检查单点后不漏）。"""
        pp = PermissionsPipeline(bypass_tools={"bash"})
        _spy_pipeline_counters(pp, monkeypatch)
        executor = _executor(pp, mode="plan")
        results = await executor.execute(
            [ToolCall(id="c1", name="bash", arguments={"command": "echo hi > out.txt"})]
        )
        assert results and results[0].success is False, "plan 模式写类命令不得放行"
