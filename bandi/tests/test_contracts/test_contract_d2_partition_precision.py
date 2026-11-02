# 功能：契约——恢复分区精确到「已执行」粒度（并发工程复核 🟡-9）
#
# 原 `partition_recovery_tool_calls` 只看「派发记录」：一批 N 个副作用工具
# 执行前整批 id 已落盘（屏障），崩在第 1 个 → 整批 N 个都判「结果未知」
# 与注释声称的「精确未启动可重放」矛盾——**已经执行完**（结果已在上下文）的调用
# 也被要求「不要盲目重试」，任务被无谓放弃。
# 本契约钉：已有结果 → 既不重放也不标未知；fail-closed 判定（派发记录缺失→未知
# 未派发→可重放、只读→可重放）逐条不得回退。
# 追加（🟡-B）：已执行判定只认**当前轮** TOOL 消息——Google 适配器每轮 `fc_{name}`
# 计数重置会跨轮复用同 id，全历史收集会让本轮调用被静默跳过。
from __future__ import annotations

from typing import Any

from bandi.agent.loop import (
    executed_tool_call_ids,
    partition_recovery_tool_calls,
)
from bandi.core.message import Message, MessageRole, ToolCall


def _call(cid: str, name: str = "bash") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments={})


class _Ctx:
    """最小上下文替身：提供分区依赖的两个集合与当前轮次。"""

    def __init__(
        self,
        results: list[Any] | None = None,
        messages: list[Any] | None = None,
        turn_index: int = 0,
    ):
        self.tool_results = results or []
        self.messages = messages or []
        self.turn_index = turn_index


class _Result:
    def __init__(self, call_id: str) -> None:
        self.tool_call_id = call_id


class TestPartitionPrecision:
    """🟡-9：已执行项不得被归入 unknown。"""

    def test_partially_executed_batch_marks_only_unfinished(self) -> None:
        """**核心契约**：3 个副作用调用全派发、前 2 个已有结果 → 只有第 3 个判未知。"""
        calls = [_call("c1"), _call("c2"), _call("c3")]

        replayable, unknown = partition_recovery_tool_calls(
            calls,
            dispatched_ids={"c1", "c2", "c3"},
            executed_ids={"c1", "c2"},
        )

        assert [c.id for c in unknown] == ["c3"], f"未知集合不精确：{[c.id for c in unknown]}"
        assert replayable == [], "已完成项不得进入重放集合"

    def test_fully_executed_batch_yields_empty_partitions(self) -> None:
        """整批已执行 → 两个集合都空（不再打扰模型、不再重放）。"""
        calls = [_call("c1"), _call("c2")]

        replayable, unknown = partition_recovery_tool_calls(
            calls, dispatched_ids={"c1", "c2"}, executed_ids={"c1", "c2"}
        )

        assert replayable == [] and unknown == []

    def test_executed_readonly_also_excluded(self) -> None:
        """只读项若已有结果，同样不再重放（避免重复 IO）。"""
        calls = [_call("r1", "read")]

        replayable, unknown = partition_recovery_tool_calls(
            calls, dispatched_ids={"r1"}, executed_ids={"r1"}
        )

        assert replayable == [] and unknown == []


class TestPartitionFailClosed:
    """回归：fail-closed 判定逐条不得回退。"""

    def test_dispatched_without_result_is_unknown(self) -> None:
        """派发过但无结果 → 未知（核心安全语义）。"""
        _, unknown = partition_recovery_tool_calls(
            [_call("c1")], dispatched_ids={"c1"}, executed_ids=set()
        )
        assert [c.id for c in unknown] == ["c1"]

    def test_missing_dispatch_record_is_unknown(self) -> None:
        """派发记录缺失（旧快照）→ 保守判未知，不得当成「未启动」重放。"""
        _, unknown = partition_recovery_tool_calls(
            [_call("c1")], dispatched_ids=None, executed_ids=set()
        )
        assert [c.id for c in unknown] == ["c1"]

    def test_undispatched_is_replayable(self) -> None:
        """明确未启动（派发记录存在且不含 id）→ 可安全重放。"""
        replayable, unknown = partition_recovery_tool_calls(
            [_call("c9")], dispatched_ids={"c1"}, executed_ids=set()
        )
        assert [c.id for c in replayable] == ["c9"] and unknown == []

    def test_empty_call_id_is_unknown(self) -> None:
        """空 id 无法证明未启动 → 未知。"""
        _, unknown = partition_recovery_tool_calls(
            [_call("")], dispatched_ids=set(), executed_ids=set()
        )
        assert len(unknown) == 1

    def test_executed_ids_none_keeps_legacy_behavior(self) -> None:
        """不传 executed_ids → 与改造前逐条一致（旧调用方零影响）。"""
        calls = [_call("c1"), _call("c2")]

        without = partition_recovery_tool_calls(calls, dispatched_ids={"c1"})
        with_empty = partition_recovery_tool_calls(calls, dispatched_ids={"c1"}, executed_ids=set())

        assert [c.id for c in without[0]] == [c.id for c in with_empty[0]]
        assert [c.id for c in without[1]] == [c.id for c in with_empty[1]]


class TestExecutedIdCollection:
    """`executed_tool_call_ids` 必须同时看本批结果与历史 TOOL 消息。"""

    def test_collects_from_results(self) -> None:
        ctx = _Ctx(results=[_Result("c1")])
        assert executed_tool_call_ids(ctx) == {"c1"}

    def test_collects_from_tool_messages(self) -> None:
        ctx = _Ctx(
            messages=[
                Message(role=MessageRole.USER, content="hi"),
                Message(role=MessageRole.TOOL, content="ok", tool_call_id="c2"),
            ]
        )
        assert executed_tool_call_ids(ctx) == {"c2"}

    def test_collects_union_of_both_sources(self) -> None:
        """破坏者：只查一处会漏 —— 两块来源必须并集。"""
        ctx = _Ctx(
            results=[_Result("c1")],
            messages=[Message(role=MessageRole.TOOL, content="ok", tool_call_id="c2")],
        )
        assert executed_tool_call_ids(ctx) == {"c1", "c2"}

    def test_ignores_blank_and_assistant_messages(self) -> None:
        ctx = _Ctx(
            results=[_Result("")],
            messages=[Message(role=MessageRole.ASSISTANT, content="x")],
        )
        assert executed_tool_call_ids(ctx) == set()


class TestMissingTurnIndexSkipped:
    """🟢-3（复审）：turn_index 缺失/None 的 TOOL 消息不得兜底为 0。

    旧实现 `int(getattr(msg, "turn_index", 0) or 0)` 把缺失轮次折叠到首轮——当
    `ctx.turn_index == 0` 时，任意无轮次的历史 TOOL 结果都会被误判为本轮已执行
    （既不重放也不注入 unknown，静默跳过）。修复：缺失/None 直接跳过。
    """

    def test_none_turn_index_tool_message_is_skipped(self) -> None:
        ctx = _Ctx(
            messages=[
                Message(
                    role=MessageRole.TOOL,
                    content="历史结果（无轮次）",
                    tool_call_id="fc_none",
                    turn_index=None,
                )
            ],
            turn_index=0,
        )

        assert executed_tool_call_ids(ctx) == set(), "缺失轮次的 TOOL 结果不得算作本轮已执行"

    def test_missing_turn_index_attribute_is_skipped(self) -> None:
        """老形态消息（无 turn_index 字段）同样跳过（fail-closed）。"""

        class _LegacyToolMsg:
            role = MessageRole.TOOL
            tool_call_id = "fc_legacy"

        ctx = _Ctx(messages=[_LegacyToolMsg()], turn_index=0)

        assert executed_tool_call_ids(ctx) == set()

    def test_explicit_zero_turn_index_still_counts_at_turn_zero(self) -> None:
        """显式轮次 0（当前轮恰为 0）仍正常计入——修复只排除缺失/None。"""
        ctx = _Ctx(
            messages=[
                Message(role=MessageRole.TOOL, content="本轮结果", tool_call_id="fc0", turn_index=0)
            ],
            turn_index=0,
        )

        assert executed_tool_call_ids(ctx) == {"fc0"}

    def test_missing_turn_index_partition_goes_unknown(self) -> None:
        """破坏者：无轮次结果不算已执行 → 已派发的同 id 必须落 unknown（不得静默跳过）。"""
        ctx = _Ctx(
            messages=[
                Message(
                    role=MessageRole.TOOL,
                    content="历史结果",
                    tool_call_id="fc_write",
                    turn_index=None,
                )
            ],
            turn_index=0,
        )

        replayable, unknown = partition_recovery_tool_calls(
            [_call("fc_write", "write")],
            dispatched_ids={"fc_write"},
            executed_ids=executed_tool_call_ids(ctx),
        )

        assert replayable == []
        assert [c.id for c in unknown] == ["fc_write"], "不得静默跳过（两集合皆空）"


class TestCrossTurnIdPartition:
    """🟡-B：跨轮同 id（Google 适配器每轮 `fc_{name}` 计数重置）不得静默跳过。

    旧行为：`executed_tool_call_ids` 收集全历史 TOOL 消息 id——轮 3 已执行的
    `fc_write` 被当成轮 5 pending `fc_write` 的「已有结果」，该调用既不重放也不
    注入 unknown（静默跳过，比统一判未知更差）。修复：只认当前轮 TOOL 消息。
    """

    def test_historic_turn_tool_message_is_not_executed(self) -> None:
        """轮 3 的 TOOL fc_write 不得算作轮 5 pending fc_write 的已有结果。"""
        ctx = _Ctx(
            messages=[
                Message(
                    role=MessageRole.TOOL,
                    content="旧轮结果",
                    tool_call_id="fc_write",
                    turn_index=3,
                )
            ],
            turn_index=5,
        )

        assert executed_tool_call_ids(ctx) == set(), "旧轮同 id 结果不得算作本轮已执行"

    def test_cross_turn_same_id_pending_goes_unknown_not_silent(self) -> None:
        """破坏者：跨轮同 id pending 必须落 unknown（不得两集合皆空被静默跳过）。"""
        ctx = _Ctx(
            messages=[
                Message(
                    role=MessageRole.TOOL,
                    content="旧轮结果",
                    tool_call_id="fc_write",
                    turn_index=3,
                )
            ],
            turn_index=5,
        )

        replayable, unknown = partition_recovery_tool_calls(
            [_call("fc_write", "write")],
            dispatched_ids={"fc_write"},
            executed_ids=executed_tool_call_ids(ctx),
        )

        assert replayable == [], "跨轮同 id 结果未知，不得重放"
        assert [c.id for c in unknown] == ["fc_write"], "不得静默跳过（两集合皆空）"

    def test_same_turn_tool_message_still_counts_as_executed(self) -> None:
        """同轮结果仍精确判「已执行」——修复不得丢掉同轮精确性。"""
        ctx = _Ctx(
            messages=[
                Message(
                    role=MessageRole.TOOL,
                    content="本轮结果",
                    tool_call_id="fc_write",
                    turn_index=5,
                )
            ],
            turn_index=5,
        )

        assert executed_tool_call_ids(ctx) == {"fc_write"}
        replayable, unknown = partition_recovery_tool_calls(
            [_call("fc_write", "write")],
            dispatched_ids={"fc_write"},
            executed_ids=executed_tool_call_ids(ctx),
        )
        assert replayable == [] and unknown == []

    def test_mixed_turns_only_current_turn_counted(self) -> None:
        """混合历史：旧轮 fc_read + 本轮 fc_write → 只收本轮 id。"""
        ctx = _Ctx(
            messages=[
                Message(
                    role=MessageRole.TOOL,
                    content="旧轮只读结果",
                    tool_call_id="fc_read",
                    turn_index=3,
                ),
                Message(
                    role=MessageRole.TOOL,
                    content="本轮写结果",
                    tool_call_id="fc_write",
                    turn_index=5,
                ),
            ],
            turn_index=5,
        )

        assert executed_tool_call_ids(ctx) == {"fc_write"}
