# 功能：run_agent_turn 记忆持久化健壮性测试（tool_results=None 不得炸记忆整链）
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from bandi.core.agent_runner import _persist_memory


class _FakeCtx:
    def __init__(
        self, tool_results, terminal_reason=None, final_message="完成", raw_input="用户问题"
    ):
        self.tool_results = tool_results
        self.final_message = final_message
        self.current_content = None
        self.raw_input = raw_input
        self.turn_index = 3
        self.terminal_reason = terminal_reason


class TestPersistMemoryNoneToolResults:
    def test_tool_results_none_does_not_crash(self) -> None:
        """rework 分支置 tool_results=None 后终止的轮次，记忆持久化不得 TypeError。"""
        ctx = _FakeCtx(tool_results=None)
        manager = MagicMock()
        asyncio.run(_persist_memory(ctx, manager, session_id="sess-1"))
        manager.long_term.prune_partitioned.assert_called_once(), "轮末分区 prune 必须照常执行"
        assert manager.long_term.prune_partitioned.call_args.kwargs == {
            "max_items": 5000,
            "per_partition_max": 300,
        }, "分区 prune 必须按 (max_items, per_partition_max) 生产参数调用"

    def test_plain_input_skips_long_term(self) -> None:
        """无事实/偏好的用户输入不写 long_term（回复侧永不直写，其余链路不炸）。"""
        ctx = _FakeCtx(tool_results=None)
        manager = MagicMock()
        asyncio.run(_persist_memory(ctx, manager, session_id="sess-2"))
        assert not manager.long_term.store.called, "无事实输入不得写 long_term"

    def test_user_fact_writes_long_term(self) -> None:
        """用户陈述的可记事实必须写 long_term（生产写入主链路，内容源只取用户输入）。

        文本须具备入库价值特征（结论=过程结论）才会直写；无冲突检查
        必须返回 False（否则进暂存等批准）。
        """
        user_fact = "结论：以后我的回复内容都统一用暂存方案，决定把后台写入改造为待批准队列。" * 5
        ctx = _FakeCtx(
            tool_results=None,
            terminal_reason=SimpleNamespace(name="COMPLETED"),
            raw_input=user_fact,
        )
        manager = MagicMock()
        manager.long_term.check_conflict.return_value = False
        asyncio.run(_persist_memory(ctx, manager, session_id="sess-3"))
        assert manager.long_term.store.called, "用户事实必须写入 long_term"
        item = manager.long_term.store.call_args.args[0]
        assert "回复内容" in item.content
        assert item.metadata["user_input"] == user_fact[:500], "item 必须携带原始输入"

    def test_hierarchy_snapshot_still_persisted_with_none_tool_results(self) -> None:
        """回归：tool_results=None 时 hierarchy 快照仍须写入（若启用）。"""
        ctx = _FakeCtx(tool_results=None, terminal_reason=SimpleNamespace(name="COMPLETED"))
        manager = MagicMock()
        hierarchy = MagicMock()
        asyncio.run(
            _persist_memory(
                ctx,
                manager,
                session_id="sess-5",
                hierarchy=hierarchy,
                project_id="p1",
                employee_id="e1",
            )
        )
        assert hierarchy.save_memory.called, "tool_results=None 时 hierarchy 快照仍须写入"
