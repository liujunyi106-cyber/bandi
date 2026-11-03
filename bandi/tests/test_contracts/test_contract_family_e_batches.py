"""契约：ApprovalStore 授权维度扩展 + pending 多槽 + docstring 一致。

生产调用路径：
1. tools.approval.ApprovalStore.grant/is_granted——授权键绑定 user/role/project/policy_version
2. tools.approval.set_pending/get_pending/pop_pending——并行审批多槽
3. tools.approval.apply_decision——决策落库（API 决策端点）
4. tools.approval.get_approval_store——进程级单例（持久化恢复）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.states import _append_tool_results_to_messages, on_executing
from bandi.agent.steer import SessionSteerRegistry
from bandi.agent.transitions import build_default_machine
from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.recruiter import Recruiter
from bandi.company.seat_executor import EngineSeatExecutor, SeatResult
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.core import agent_runner as ar
from bandi.core.config import AgentConfig, BandiConfig
from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage, ToolCall, ToolResult
from bandi.memory.compaction import repair_tool_pairs
from bandi.memory.session.store import SessionStore
from bandi.observability.tracer import OTelEventBusBridge
from bandi.tools.approval import ApprovalStore, PendingApproval, params_digest
from bandi.tools.permissions_pipeline import (
    ACTIVE_LAYERS,
    RESERVED_LAYERS,
    TOTAL_LAYERS,
)


def test_grant_dimension_binding_and_legacy_signature(tmp_path: Path) -> None:
    """授权键绑定 user/role 维度（不同维度不得共享）；不带维度的旧签名行为不变。"""
    store = ApprovalStore(tmp_path / "a.json")
    store.grant(
        "s1",
        "bash",
        scope="session",
        params_digest=params_digest({"cmd": "git status"}),
        user_id="alice",
        role="dev",
    )
    assert store.is_granted(
        "s1",
        "bash",
        params_digest=params_digest({"cmd": "git status"}),
        user_id="alice",
        role="dev",
    )
    assert not store.is_granted(
        "s1", "bash", params_digest=params_digest({"cmd": "git status"}), user_id="bob", role="dev"
    ), "不同 user 不得共享授权"
    assert not store.is_granted(
        "s1",
        "bash",
        params_digest=params_digest({"cmd": "git status"}),
        user_id="alice",
        role="admin",
    ), "不同 role 不得共享授权"
    store.grant("s1", "bash", scope="session", params_digest=params_digest({"x": 1}))
    assert store.is_granted("s1", "bash", params_digest=params_digest({"x": 1})), "旧签名行为不变"


def test_pending_multi_slot_and_capacity_limit(tmp_path: Path) -> None:
    """pending 多槽并存（取队首/按 id 精确弹出）；列表必须有容量上限。"""
    store = ApprovalStore(tmp_path / "a.json")
    store.set_pending(
        "s9",
        PendingApproval(tool_call_id="c1", tool_name="bash", params_digest="", reason="r1"),
    )
    store.set_pending(
        "s9",
        PendingApproval(tool_call_id="c2", tool_name="write", params_digest="", reason="r2"),
    )
    first = store.get_pending("s9")
    assert first is not None and first.tool_call_id == "c1", "多槽并存，取队首"
    popped = store.pop_pending("s9", tool_call_id="c2")
    assert popped is not None and popped.tool_name == "write", "按 tool_call_id 精确弹出"
    remaining = store.pop_pending("s9")
    assert remaining is not None and remaining.tool_call_id == "c1", "另一条 pending 仍在"

    for i in range(6):
        store.set_pending(
            "s9",
            PendingApproval(tool_call_id=f"c{i}", tool_name="bash", params_digest="", reason="r"),
        )
    pending = store.list_pending("s9")
    assert len(pending) <= 5, f"pending 必须有容量上限（实际 {len(pending)}）"


def test_dimensions_persisted_across_recreation(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    ApprovalStore(path).grant(
        "s1",
        "bash",
        scope="session",
        params_digest=params_digest({"cmd": "ls"}),
        user_id="alice",
    )
    restored = ApprovalStore(path)
    assert restored.is_granted(
        "s1", "bash", params_digest=params_digest({"cmd": "ls"}), user_id="alice"
    ), "带维度授权必须跨进程持久化"


def test_module_docstring_matches_pending_recovery() -> None:
    """docstring 声称「重启丢 pending」与 _load 恢复 pending 矛盾——已修复一致。"""
    import inspect

    from bandi.tools import approval as mod

    doc = inspect.getdoc(mod)
    assert doc is not None and "重启丢 pending" not in doc, "docstring 不得再声称丢 pending"


def test_legacy_pending_single_dict_format_compatible(tmp_path: Path) -> None:
    """前旧格式（pending 单 dict）升级后必须兼容恢复（不静默丢失）。"""
    import json
    import time

    path = tmp_path / "a.json"
    path.write_text(
        json.dumps(
            {
                "policies": {},
                "reviewers": {},
                "session_grants": {},
                "once_grants": {},
                "pending": {
                    "s-old": {
                        "tool_call_id": "c-old-1",
                        "tool_name": "bash",
                        "params_digest": "d1",
                        "reason": "legacy",
                        "detail": "",
                        "created_at": time.time(),
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = ApprovalStore(path)
    pending = store.get_pending("s-old")
    assert pending is not None and pending.tool_call_id == "c-old-1", (
        "旧单 dict 格式必须兼容恢复（pending 不得静默丢失）"
    )


# ==================== e1_truncation_family ====================


# 超过 2000 行（同时远小于 50KB，钉的是「行数」这一个变量）
_LONG_LINES = "\n".join(f"line {i}" for i in range(2001))


def _run(tool_name: str, output: str) -> str:
    ctx = ExecutionContext(messages=[], provider=None, config=AgentConfig(), bus=EventBus())
    ctx.current_tool_calls = [ToolCall(id="c1", name=tool_name, arguments={})]
    ctx.tool_results = [
        ToolResult(tool_call_id="c1", tool_name=tool_name, success=True, output=output)
    ]
    _append_tool_results_to_messages(ctx)
    tool_msgs = [m for m in ctx.messages if m.role == MessageRole.TOOL]
    assert tool_msgs, "工具结果未回填进消息"
    return str(tool_msgs[-1].content)


def test_truncation_applies_to_listed_tools() -> None:
    """名单内高风险工具超长输出必须截断（保留前 2000 行 + 截断标记 + 取回出路）；
    短输出不得被截断（避免无谓噪声）。"""

    content = _run("subagent_spawn", _LONG_LINES)
    assert "line 2000" not in content, "超出 2000 行的内容仍在"
    assert "line 1999" in content, "应保留前 2000 行"
    assert "输出已截断" in content, "应带截断标记"
    assert "重新调用 subagent_spawn" in content, "须给出取回出路"

    bash_content = _run("bash", _LONG_LINES)
    assert "line 2000" not in bash_content
    assert "输出已截断" in bash_content

    short_content = _run("subagent_spawn", "done")
    assert "done" in short_content
    assert "截断" not in short_content


def test_truncation_skips_unlisted_tools() -> None:
    """名单外工具（含已移出名单的 read）不由本层截断。"""

    content = _run("ontology_query", _LONG_LINES)
    assert _LONG_LINES in content, "名单外工具被本层误截断"
    assert "截断" not in content

    ws = Path.home() / ".bandi" / "workspace"
    candidates = list(ws.glob("*OJO*.md")) if ws.exists() else []
    if candidates:
        long_doc = "\n".join(f"第 {i} 行" * 3 for i in range(1, 700))
        read_content = _run("read", long_doc)
        assert "输出已截断" not in read_content, (
            f"read 不应再被本层截断（内容 {len(read_content)} 字符）"
        )


# ==================== e2_loop_progress ====================
# 功能：契约——循环检测必须要求「无进展」（并发工程复核 /）
#
# `bash: git status`/`ls` 这类被 registry 判为 write 的工具，即便每轮输出
# 都在变，同指纹达窗口即 LOOP_DETECTED（合法轮询被误杀）；
# 本契约钉：写类同样要求「无进展」才判循环；只读接近窗口时留下可见告警。


class R2xe2Bus:
    async def emit(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def subscribe(self, *args: Any, **kwargs: Any) -> None:
        return None


class R2xe2Executor:
    """按脚本返回结果的可编程执行器（registry 缺省 → 走显式名单判定）。"""

    registry = None

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = outputs
        self._index = 0

    async def execute(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        output = self._outputs[min(self._index, len(self._outputs) - 1)]
        self._index += 1
        return [
            ToolResult(tool_call_id=tc.id, tool_name=tc.name, success=True, output=output)
            for tc in tool_calls
        ]


def _fingerprint(tc: ToolCall) -> str:
    keys = [(tc.name, json.dumps(tc.arguments, sort_keys=True))]
    return hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()


def r2xe2_ctx(*, hashes: list[str], executor: R2xe2Executor, window: int = 3) -> ExecutionContext:
    ctx = ExecutionContext(messages=[], provider=object(), config=AgentConfig(), bus=R2xe2Bus())
    ctx.current_tool_calls = [ToolCall(id="c1", name="bash", arguments={"cmd": "ls"})]
    ctx.tool_executor = executor
    ctx.tool_sequence_hashes = list(hashes)
    ctx.loop_detection_window = window
    ctx.meta.clear()
    return ctx


class TestWriteLoopRequiresProgress:
    """写类工具结果有变化 → 不得判循环；无进展 → 必须检出。"""

    @pytest.mark.asyncio
    async def test_progress_gate_for_write_tools(self) -> None:
        """**核心契约**：同调用指纹但输出每轮都变（有进展）→ 不判循环；
        同指纹同输出（无进展）→ 判循环；无历史结果基线时保持原行为（判循环）。"""
        tc = ToolCall(id="c1", name="bash", arguments={"cmd": "ls"})
        fp = _fingerprint(tc)
        executor = R2xe2Executor(["第一轮输出", "第二轮输出"])

        ctx1 = r2xe2_ctx(hashes=[fp], executor=executor, window=3)
        await on_executing(ctx1)
        assert ctx1.terminal_reason is None

        ctx2 = r2xe2_ctx(hashes=[fp, fp], executor=executor, window=3)
        ctx2.meta["_loop_results_hash"] = ctx1.meta["_loop_results_hash"]
        await on_executing(ctx2)

        assert ctx2.terminal_reason is None, "输出有变化仍被判循环（合法轮询被误杀）"

        identical_executor = R2xe2Executor(["同样的输出"])
        ctx3 = r2xe2_ctx(hashes=[fp], executor=identical_executor, window=3)
        await on_executing(ctx3)

        ctx4 = r2xe2_ctx(hashes=[fp, fp], executor=identical_executor, window=3)
        ctx4.meta["_loop_results_hash"] = ctx3.meta["_loop_results_hash"]
        await on_executing(ctx4)

        assert ctx4.terminal_reason == TerminalReason.LOOP_DETECTED, "无进展的写类循环漏检"

        ctx5 = r2xe2_ctx(hashes=[fp, fp], executor=R2xe2Executor(["ok"]), window=3)
        await on_executing(ctx5)

        assert ctx5.terminal_reason == TerminalReason.LOOP_DETECTED


class TestReadonlyNearMissWarning:
    """只读接近窗口时必须留下可见信号（near-miss 可观测）。"""

    @pytest.mark.asyncio
    async def test_warning_emitted_before_termination(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tc = ToolCall(id="c1", name="read", arguments={"path": "a.txt"})
        fp = _fingerprint(tc)
        executor = R2xe2Executor(["同样的只读输出"])

        # 第 1 轮：建立 sig 基线（streak=1）
        ctx1 = r2xe2_ctx(hashes=[fp], executor=executor, window=3)
        ctx1.current_tool_calls = [tc]
        await on_executing(ctx1)
        assert ctx1.terminal_reason is None

        # 第 2 轮：同调用同结果（streak=2 = window-1）→ 应留告警
        ctx2 = r2xe2_ctx(hashes=[fp, fp], executor=executor, window=3)
        ctx2.current_tool_calls = [tc]
        ctx2.meta["_readonly_loop_sig"] = ctx1.meta["_readonly_loop_sig"]
        ctx2.meta["_readonly_loop_streak"] = 1

        with caplog.at_level(logging.WARNING, logger="bandi.agent.states"):
            await on_executing(ctx2)

        assert any("空转" in r.getMessage() for r in caplog.records), (
            f"只读 near-miss 无告警：{[r.getMessage() for r in caplog.records]}"
        )
        assert ctx2.terminal_reason is None, "near-miss 阶段不得直接终止"


class TestWindowAllSameProgress:
    """🟢-B：窗口语义——相邻轮有变化不足以清零，窗口内结果签名全同才判循环。"""

    @pytest.mark.asyncio
    async def test_adjacent_change_does_not_fake_progress(self) -> None:
        """``r1,r2,r1,r1,r1``：第 4 轮相邻相同（旧实现误判循环）但窗口内仍有
        r2 → 不得终止；r2 滑出窗口后（第 5 轮）才 LOOP_DETECTED。"""
        executor = R2xe2Executor(["r1", "r2", "r1", "r1", "r1"])
        ctx = r2xe2_ctx(hashes=[], executor=executor, window=3)

        for round_no in range(1, 5):
            await on_executing(ctx)
            assert ctx.terminal_reason is None, (
                f"第 {round_no} 轮：窗口内仍有结果变化（r2），不得判循环"
            )

        await on_executing(ctx)
        assert ctx.terminal_reason == TerminalReason.LOOP_DETECTED, (
            "窗口滑到全同（r1×3）必须检出循环"
        )
        assert len(ctx.meta.get("_loop_results_hashes") or []) <= 3, "结果窗口必须有界"


# ==================== e2_readonly_loop ====================
# 功能：契约——只读工具循环检测（并发工程 批次5 / ）
#
# 代价是只读工具空转（同工具同参数反复调用）只能靠 max_turns=15 兜底。
# 本契约钉：同工具同参数的只读批 + **结果完全一致**（无进展）连续达窗口 →
# LOOP_DETECTED；任一输出变化 / 批次不同 / 掺入写类工具即清零（不误伤）。


def r3xe2_tc(cid: str, name: str = "read", args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args or {"path": "a.txt"})


class R3xe2Executor:
    """可切换输出的执行桩——用于验证「结果一致 = 无进展」判定。"""

    def __init__(self) -> None:
        self.output = "same"

    async def execute(self, calls: list[ToolCall]) -> list[ToolResult]:
        return [
            ToolResult(
                tool_call_id=tc.id,
                tool_name=tc.name,
                output=self.output,
                success=True,
                duration_ms=1,
            )
            for tc in calls
        ]


def r3xe2_ctx(window: int = 3) -> tuple[SimpleNamespace, R3xe2Executor]:
    from bandi.agent.steer import SessionSteerRegistry

    executor = R3xe2Executor()

    class _Stub(SimpleNamespace):
        pass

    ctx = SimpleNamespace(
        terminal_reason=None,
        turn_index=1,
        current_tool_calls=[r3xe2_tc("c0")],
        tool_executor=executor,
        meta={"tracer": None, "task_ledger": None, "steer_registry": SessionSteerRegistry()},
        session_id="s",
        messages=[],
        tool_results=None,
        cot_trace=[],
        loop_detection_window=window,
        tool_sequence_hashes=[],
    )
    return ctx, executor


async def _turn(ctx: SimpleNamespace, calls: list[ToolCall]) -> None:
    from bandi.agent import states as states_mod

    ctx.current_tool_calls = calls
    ctx.tool_results = None
    await states_mod.on_executing(ctx)


@pytest.mark.asyncio
async def test_readonly_loop_detection_and_window_boundary() -> None:
    """不足窗口轮数不得提前判循环；同调用 + 同输出连续达窗口 → LOOP_DETECTED。"""
    ctx, _ = r3xe2_ctx(window=3)
    for _ in range(2):
        await _turn(ctx, [r3xe2_tc("c0")])

    assert ctx.terminal_reason is None, "未达窗口不得误杀"

    await _turn(ctx, [r3xe2_tc("c0")])

    assert ctx.terminal_reason == TerminalReason.LOOP_DETECTED, "只读空转必须被检出"


@pytest.mark.asyncio
async def test_readonly_repeat_with_changing_output_not_flagged() -> None:
    """**破坏者视角**：输出每次在变（有进展）→ 永不判循环（合法重复读取）。"""
    ctx, executor = r3xe2_ctx(window=3)
    for i in range(6):
        executor.output = f"content-v{i}"
        await _turn(ctx, [r3xe2_tc("c0")])

    assert ctx.terminal_reason is None, "输出变化属有进展，不得误判循环"


@pytest.mark.asyncio
async def test_different_calls_reset_streak() -> None:
    """批次不同（参数变化）→ 计数清零，即使达窗口轮数也不判。"""
    ctx, _ = r3xe2_ctx(window=3)
    await _turn(ctx, [r3xe2_tc("c0", args={"path": "a.txt"})])
    await _turn(ctx, [r3xe2_tc("c1", args={"path": "b.txt"})])
    await _turn(ctx, [r3xe2_tc("c2", args={"path": "c.txt"})])
    await _turn(ctx, [r3xe2_tc("c3", args={"path": "d.txt"})])

    assert ctx.terminal_reason is None, "不同调用的序列不得累计为循环"


@pytest.mark.asyncio
async def test_state_changing_tool_batch_clears_readonly_streak() -> None:
    """掺入写类工具（bash）→ 只读计数清零；写类路径自身按原语义判定。"""
    ctx, _ = r3xe2_ctx(window=3)
    await _turn(ctx, [r3xe2_tc("c0", "read")])
    await _turn(ctx, [r3xe2_tc("c1", "bash")])  # 写类批：清 read streak（本轮不判——窗口未满）
    await _turn(ctx, [r3xe2_tc("c2", "read")])
    await _turn(ctx, [r3xe2_tc("c3", "read")])

    assert ctx.terminal_reason is None, "写类批应清零只读 streak（本轮 read 仅 2 连）"


@pytest.mark.asyncio
async def test_injected_input_resets_readonly_streak() -> None:
    """E2b：注入新输入 = 新方向——消费注入必须清零只读循环计数，不得更快误杀。"""
    from bandi.agent.base import ExecutionContext
    from bandi.agent.loop import AgentLoop
    from bandi.agent.steer import SessionSteerRegistry
    from bandi.core.config import AgentConfig

    class R3xe2Bus:
        async def emit(self, *args: Any, **kwargs: Any) -> None:
            return None

    ctx = ExecutionContext(messages=[], provider=object(), config=AgentConfig(), bus=R3xe2Bus())
    ctx.session_id = "s"
    ctx.meta = {"tracer": None, "task_ledger": None, "steer_registry": SessionSteerRegistry()}
    ctx.loop_detection_window = 3
    ctx.tool_executor = R3xe2Executor()

    await _turn(ctx, [r3xe2_tc("c0")])
    await _turn(ctx, [r3xe2_tc("c0")])
    assert int(ctx.meta.get("_readonly_loop_streak") or 0) == 2, "前置：streak 将满"

    ctx.inject_input("转向：换方向")
    loop = AgentLoop(max_iterations=5)
    assert await loop._consume_injected_input(ctx, 0) is True, "注入应被原子消费"
    assert ctx.meta.get("_readonly_loop_streak") is None, "注入消费必须清零只读 streak"
    assert ctx.meta.get("_readonly_loop_sig") is None

    await _turn(ctx, [r3xe2_tc("c0")])
    assert ctx.terminal_reason is None, "注入后重新计数，不得立刻 LOOP_DETECTED"


# ── 只读判定改用 registry is_read_only 元数据（范围不再按 6 名单错位）──


class _MetaTool:
    def __init__(self, is_read_only: bool) -> None:
        self.is_read_only = is_read_only


class _MetaRegistry:
    def __init__(self, read_only: dict[str, bool]) -> None:
        self._read_only = read_only

    def get(self, name: str) -> _MetaTool | None:
        if name not in self._read_only:
            return None
        return _MetaTool(self._read_only[name])


class _RegistryExecutor(R3xe2Executor):
    def __init__(self, read_only: dict[str, bool]) -> None:
        super().__init__()
        self.registry = _MetaRegistry(read_only)


def _ctx_meta(read_only: dict[str, bool], name: str, window: int = 3) -> SimpleNamespace:
    from bandi.agent.steer import SessionSteerRegistry

    return SimpleNamespace(
        terminal_reason=None,
        turn_index=1,
        current_tool_calls=[r3xe2_tc("c0", name)],
        tool_executor=_RegistryExecutor(read_only),
        meta={"tracer": None, "task_ledger": None, "steer_registry": SessionSteerRegistry()},
        session_id="s",
        messages=[],
        tool_results=None,
        cot_trace=[],
        loop_detection_window=window,
        tool_sequence_hashes=[],
    )


@pytest.mark.asyncio
async def test_registry_readonly_metadata_labels_and_detection() -> None:
    """registry 声明非只读的工具同输出重复 → 不计只读 streak / 不用「只读」文案；
    声明只读 → 只读语义终止 + streak 累计；未知工具 → 保守仍检测但无「只读」文案。"""
    ctx = _ctx_meta({"send_dm": False}, "send_dm")
    for _ in range(3):
        await _turn(ctx, [r3xe2_tc("c0", "send_dm")])

    assert ctx.meta.get("_readonly_loop_streak") is None, "非只读工具不得计入只读 streak"
    assert "只读" not in (ctx.final_message or ""), (
        f"非只读工具不得使用「只读」文案（实际 {ctx.final_message!r}）"
    )

    readonly_ctx = _ctx_meta({"read": True}, "read")
    for _ in range(3):
        await _turn(readonly_ctx, [r3xe2_tc("c0", "read")])

    assert readonly_ctx.terminal_reason == TerminalReason.LOOP_DETECTED
    assert "只读" in (readonly_ctx.final_message or "")
    assert int(readonly_ctx.meta.get("_readonly_loop_streak") or 0) == 3

    unknown_ctx = _ctx_meta({}, "mystery_tool")
    for _ in range(3):
        await _turn(unknown_ctx, [r3xe2_tc("c0", "mystery_tool")])

    assert unknown_ctx.terminal_reason == TerminalReason.LOOP_DETECTED, "未知工具仍须保持检测能力"
    assert "只读" not in (unknown_ctx.final_message or "")


def test_loop_detection_window_falls_back_to_config() -> None:
    """窗口值可由 ctx 显式设置（最高优先），并回落 config 属性（AgentConfig
    暂无该字段时回落类默认 5，字段化后自动生效）。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig

    class _NullBus:
        async def emit(self, *args: Any, **kwargs: Any) -> None:
            return None

    ctx = ExecutionContext(messages=[], provider=object(), config=AgentConfig(), bus=_NullBus())
    assert ctx.loop_detection_window == 5

    configured = ExecutionContext(
        messages=[],
        provider=object(),
        config=SimpleNamespace(loop_detection_window=2),
        bus=_NullBus(),
    )
    assert configured.loop_detection_window == 2, "config 上的窗口值必须被接线消费"

    configured.loop_detection_window = 4
    assert configured.loop_detection_window == 4, "ctx 显式设置优先于 config 回落"


@pytest.mark.asyncio
async def test_on_init_clears_cross_request_tool_hashes() -> None:
    """（收尾）：新请求边界清空 tool_sequence_hashes——防跨请求累计误判循环。"""
    from bandi.agent.base import ExecutionContext
    from bandi.agent.states import on_init
    from bandi.core.message import Message, MessageRole

    class _NullBus:
        async def emit(self, *args: Any, **kwargs: Any) -> None:
            return None

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="新请求")],
        provider=object(),
        config=object(),
        bus=_NullBus(),
    )
    ctx.tool_sequence_hashes = ["fp-1", "fp-2"]

    await on_init(ctx)

    assert ctx.tool_sequence_hashes == [], "新请求边界必须清空跨请求工具指纹"


def test_prompt_hash_key_order_instability_is_miss_direction() -> None:
    """契约注明：轮指纹（_prompt_hash）对 dict 键序不稳定——键序变化时 hash
    变化，方向为「漏检同轮重放」（判为新轮而非误判同轮），非 false-positive。"""
    from bandi.agent.states import _prompt_hash

    ordered = {"role": "user", "content": "x", "meta": {"a": 1, "b": 2}}
    reordered = {"role": "user", "content": "x", "meta": {"b": 2, "a": 1}}

    assert _prompt_hash([ordered]) != _prompt_hash([reordered]), (
        "键序变化 → 指纹变化（漏检方向，已知取舍）"
    )
    assert _prompt_hash([ordered]) == _prompt_hash([dict(ordered)]), "同键序指纹稳定"


def test_readonly_tool_kind_guards_registry_exception() -> None:
    """契约（五审 🟢）：registry.get 抛异常按未知处理（fail-closed）
    不得让恢复分区/循环检测崩溃。"""
    from bandi.agent.states import readonly_tool_kind

    class _BadRegistry:
        def get(self, name: str) -> Any:
            raise RuntimeError("boom")

    assert readonly_tool_kind("x", _BadRegistry()) is None


# ==================== e2_trace_bridge ====================


class RecordingSpan:
    def __init__(self, name: str, events: list[str]) -> None:
        self._name = name
        self._events = events
        self._attrs: dict[str, str] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self._attrs[key] = str(value)

    def end(self) -> None:
        pass

    def __enter__(self) -> RecordingSpan:
        self._events.append(f"start:{self._name}")
        return self

    def __exit__(self, *args: object) -> None:
        self._events.append(f"end:{self._name}")


class RecordingTracer:
    def __init__(self) -> None:
        self.events: list[str] = []

    def start_as_current_span(self, name: str, attributes: dict[str, Any] | None = None) -> Any:
        return RecordingSpan(name, self.events)


class _Recorder:
    def __init__(self) -> None:
        self.tracer = RecordingTracer()
        self.bridge = OTelEventBusBridge(tracer=self.tracer)


def _mk_recorder() -> tuple[OTelEventBusBridge, list[str]]:
    rec = _Recorder()
    return rec.bridge, rec.tracer.events


def test_event_bridge_pairs_spans_in_order_without_leak() -> None:
    """同 turn 两次 LLM 请求各自按栈序配对；并行工具按 call_id 配对；并发不泄漏 span。"""
    bridge, events = _mk_recorder()

    async def same_turn_requests() -> None:
        await bridge.handle_event(
            Event(type=EventType.LLM_REQUEST, data={"session_id": "s1", "turn": 3})
        )
        await bridge.handle_event(
            Event(type=EventType.LLM_RESPONSE, data={"session_id": "s1", "turn": 3})
        )
        await bridge.handle_event(
            Event(type=EventType.LLM_REQUEST, data={"session_id": "s1", "turn": 3})
        )
        await bridge.handle_event(
            Event(type=EventType.LLM_RESPONSE, data={"session_id": "s1", "turn": 3})
        )

    asyncio.run(same_turn_requests())
    assert events == [
        "start:llm.request",
        "end:llm.request",
        "start:llm.request",
        "end:llm.request",
    ], f"同 turn 两次 LLM 必须各自配对（实际 {events}）"

    tool_bridge, tool_events = _mk_recorder()

    async def parallel_tool_calls() -> None:
        await tool_bridge.handle_event(
            Event(
                type=EventType.TOOL_INVOKED,
                data={"session_id": "s1", "turn": 1, "tool_call_id": "c1"},
            )
        )
        await tool_bridge.handle_event(
            Event(
                type=EventType.TOOL_INVOKED,
                data={"session_id": "s1", "turn": 1, "tool_call_id": "c2"},
            )
        )
        await tool_bridge.handle_event(
            Event(
                type=EventType.TOOL_COMPLETED,
                data={"session_id": "s1", "turn": 1, "tool_call_id": "c2"},
            )
        )
        await tool_bridge.handle_event(
            Event(
                type=EventType.TOOL_COMPLETED,
                data={"session_id": "s1", "turn": 1, "tool_call_id": "c1"},
            )
        )

    asyncio.run(parallel_tool_calls())
    assert tool_events.count("start:tool.invoke") == 2
    assert tool_events.count("end:tool.invoke") == 2, "两个工具 span 必须都关闭"

    race_bridge, race_events = _mk_recorder()

    async def concurrent_events() -> None:
        await asyncio.gather(
            *[
                race_bridge.handle_event(
                    Event(
                        type=EventType.LLM_REQUEST,
                        data={"session_id": "s1", "turn": 1, "tool_call_id": f"c{i}"},
                    )
                )
                for i in range(8)
            ],
            *[
                race_bridge.handle_event(
                    Event(
                        type=EventType.LLM_RESPONSE,
                        data={"session_id": "s1", "turn": 1, "tool_call_id": f"c{i}"},
                    )
                )
                for i in range(8)
            ],
        )

    asyncio.run(concurrent_events())
    assert race_events.count("start:llm.request") == 8
    assert race_events.count("end:llm.request") == 8
    assert not race_bridge._stacks, f"并发后不得有 span 泄漏（实际 {race_bridge._stacks}）"


# ==================== e3_pairing ====================
# 功能：契约——工具结果与调用按 tool_call_id 配对（并发工程 批次5 / ）
#
# 结果回填按**位置 zip**（非 id）配对，数量错配仅 warning + 丢多余——
# 乱序返回时会把结果挂到错误的调用上（张冠李戴），缺项时后续调用丢消息。
# 本契约钉：乱序按 id 正确配对 / 缺项补 stub / 多余丢弃 / 空 id 走位置兜底。


def r5xe3_tc(cid: str, name: str = "read") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments={})


def _tr(cid: str, output: str, name: str = "read", success: bool = True) -> ToolResult:
    return ToolResult(
        tool_call_id=cid, tool_name=name, output=output, success=success, duration_ms=1
    )


def r5xe3_ctx(calls: list[ToolCall], results: list[ToolResult]) -> SimpleNamespace:
    return SimpleNamespace(
        messages=[],
        current_tool_calls=calls,
        tool_results=results,
        turn_index=1,
        meta={},
        pii_detector=None,
        output_sanitizer=None,
    )


def _tool_msgs(ctx: SimpleNamespace) -> list:
    return [m for m in ctx.messages if m.role == MessageRole.TOOL]


def test_results_pair_by_id_with_position_fallback() -> None:
    """乱序返回按 id 归位（消息顺序按调用序）；未知 id 结果丢弃；
    历史桩数据空 id 仍按位置兜底配对。"""
    from bandi.agent.states import _append_tool_results_to_messages

    ctx = r5xe3_ctx(
        calls=[r5xe3_tc("a"), r5xe3_tc("b")],
        results=[_tr("b", "output-B"), _tr("a", "output-A")],
    )
    _append_tool_results_to_messages(ctx)

    msgs = _tool_msgs(ctx)
    assert [m.tool_call_id for m in msgs] == ["a", "b"], "消息顺序按调用序"
    assert "output-A" in (msgs[0].content or "")
    assert "output-B" in (msgs[1].content or "")

    extra_ctx = r5xe3_ctx(calls=[r5xe3_tc("a")], results=[_tr("a", "ok"), _tr("ghost", "邪恶结果")])
    _append_tool_results_to_messages(extra_ctx)

    extra_msgs = _tool_msgs(extra_ctx)
    assert [m.tool_call_id for m in extra_msgs] == ["a"]
    assert "邪恶结果" not in (extra_msgs[0].content or ""), "未配对结果不得串入已配对消息"

    legacy_ctx = r5xe3_ctx(calls=[r5xe3_tc("a")], results=[_tr("", "legacy-output")])
    _append_tool_results_to_messages(legacy_ctx)

    legacy_msgs = _tool_msgs(legacy_ctx)
    assert len(legacy_msgs) == 1 and "legacy-output" in (legacy_msgs[0].content or "")


def test_missing_and_empty_results_inject_stubs() -> None:
    """缺项与空结果列表都逐调用补中断 stub（interrupted + tool_error），不静默丢消息。"""
    from bandi.agent.states import _append_tool_results_to_messages

    ctx = r5xe3_ctx(calls=[r5xe3_tc("a"), r5xe3_tc("b")], results=[_tr("a", "ok")])
    _append_tool_results_to_messages(ctx)

    msgs = _tool_msgs(ctx)
    assert [m.tool_call_id for m in msgs] == ["a", "b"], "缺项必须补 stub 占位"
    assert "interrupted" in (msgs[1].content or "").lower(), "stub 须标记中断语义"
    assert (msgs[1].meta or {}).get("tool_error") is True

    empty_ctx = r5xe3_ctx(calls=[r5xe3_tc("a"), r5xe3_tc("b")], results=[])
    _append_tool_results_to_messages(empty_ctx)

    empty_msgs = _tool_msgs(empty_ctx)
    assert [m.tool_call_id for m in empty_msgs] == ["a", "b"], "空结果必须逐调用补 stub"
    assert all("interrupted" in (m.content or "").lower() for m in empty_msgs)


def test_result_append_is_idempotent() -> None:
    """已 append 过的 id 不重复写入；缺项/空结果 stub 分支同样幂等。"""
    from bandi.agent.states import _append_tool_results_to_messages

    ctx = r5xe3_ctx(calls=[r5xe3_tc("a")], results=[_tr("a", "ok")])
    _append_tool_results_to_messages(ctx)
    _append_tool_results_to_messages(ctx)

    assert len(_tool_msgs(ctx)) == 1, "同 id 不得重复 append"

    stub_ctx = r5xe3_ctx(calls=[r5xe3_tc("a"), r5xe3_tc("b")], results=[_tr("a", "ok")])
    _append_tool_results_to_messages(stub_ctx)
    first = [(m.tool_call_id, m.content) for m in _tool_msgs(stub_ctx)]
    assert [m.tool_call_id for m in _tool_msgs(stub_ctx)] == ["a", "b"]

    _append_tool_results_to_messages(stub_ctx)

    assert [(m.tool_call_id, m.content) for m in _tool_msgs(stub_ctx)] == first, (
        "stub 分支必须先过 existing_ids 幂等守卫"
    )

    empty_ctx = r5xe3_ctx(calls=[r5xe3_tc("a")], results=[])
    _append_tool_results_to_messages(empty_ctx)
    _append_tool_results_to_messages(empty_ctx)

    assert [m.tool_call_id for m in _tool_msgs(empty_ctx)] == ["a"]


def test_duplicate_nonempty_id_does_not_cross_pair() -> None:
    """破坏者视角：同一非空 id 出现两条结果 → 第二条丢弃；位置兜底只配空 id
    不得把重复结果挂到缺项的其它调用上（张冠李戴）。"""
    from bandi.agent.states import _append_tool_results_to_messages

    ctx = r5xe3_ctx(
        calls=[r5xe3_tc("a"), r5xe3_tc("b")],
        results=[_tr("a", "out1"), _tr("a", "out2")],
    )
    _append_tool_results_to_messages(ctx)

    msgs = _tool_msgs(ctx)
    assert [m.tool_call_id for m in msgs] == ["a", "b"], "b 仍须补 stub 占位"
    assert "out1" in (msgs[0].content or "")
    assert "out2" not in (msgs[0].content or ""), "重复结果不得进入已配对消息"
    assert "out2" not in (msgs[1].content or ""), "重复结果不得串给缺项调用"
    assert "interrupted" in (msgs[1].content or "").lower(), "b 只能是缺项 stub 语义"


# ── 同 id 命中历史轮（非本轮）→ 丢弃新结果并告警（行为不变）──


def test_historical_duplicate_id_warns_but_behavior_unchanged(caplog) -> None:
    """同 id 命中历史轮 TOOL 消息 → 静默丢弃改为告警（含 id/工具名）
    历史结果保持唯一（新结果仍不重复入上下文）。"""
    import logging

    from bandi.agent.states import _append_tool_results_to_messages
    from bandi.core.message import Message

    ctx = r5xe3_ctx(calls=[r5xe3_tc("a")], results=[_tr("a", "new-output")])
    ctx.turn_index = 3
    ctx.messages.append(
        Message(role=MessageRole.TOOL, content="old-output", tool_call_id="a", turn_index=1)
    )

    with caplog.at_level(logging.WARNING):
        _append_tool_results_to_messages(ctx)

    msgs = _tool_msgs(ctx)
    assert len(msgs) == 1 and "old-output" in (msgs[0].content or "")
    records = [r.getMessage() for r in caplog.records]
    assert any("a" in m and "read" in m for m in records), (
        f"命中历史同 id 必须告警（含 id/工具名）: {records}"
    )


def test_same_turn_and_next_turn_duplicate_do_not_warn(caplog) -> None:
    """同轮幂等（on_executing 后 on_thinking 二次回填）不告警；
    turn 递增后的二次回填同属同一工具轮幂等——不得误报「历史轮冲突」。"""
    import logging

    from bandi.agent.states import _append_tool_results_to_messages
    from bandi.core.message import Message

    ctx = r5xe3_ctx(calls=[r5xe3_tc("a")], results=[_tr("a", "ok")])
    ctx.turn_index = 1
    ctx.messages.append(
        Message(role=MessageRole.TOOL, content="ok", tool_call_id="a", turn_index=1)
    )

    with caplog.at_level(logging.WARNING):
        _append_tool_results_to_messages(ctx)

    assert len(_tool_msgs(ctx)) == 1
    assert not any(
        "历史" in r.getMessage() or "duplicate" in r.getMessage() for r in caplog.records
    )

    caplog.clear()
    next_ctx = r5xe3_ctx(calls=[r5xe3_tc("a")], results=[_tr("a", "ok")])
    next_ctx.messages.append(
        Message(role=MessageRole.TOOL, content="ok", tool_call_id="a", turn_index=1)
    )
    next_ctx.turn_index = 2

    with caplog.at_level(logging.WARNING):
        _append_tool_results_to_messages(next_ctx)

    assert len(_tool_msgs(next_ctx)) == 1
    assert not any("already used in history" in r.getMessage() for r in caplog.records)


# ==================== e3_session_lease ====================


_INSTANCE_A = "inst-a"
_INSTANCE_B = "inst-b"


def _mk_store(tmp_path: Path, sid: str) -> tuple[SessionStore, str]:
    store = SessionStore(str(tmp_path / "sess.db"))
    created = store.create_session(mode="execute", session_id=sid)
    return store, created or sid


def test_session_lease_mutual_exclusion_and_release(tmp_path: Path) -> None:
    """他实例有效租约必须拒绝；释放后他实例可认领；租约跨 store 实例持久化可见。"""
    store, sid = _mk_store(tmp_path, "s-lease1")
    ok = ar._acquire_session_slot(sid, store, instance_id=_INSTANCE_A)
    assert ok
    assert not ar._acquire_session_slot(sid, store, instance_id=_INSTANCE_B), (
        "他实例有效租约必须拒绝（跨进程互斥）"
    )
    ar._release_session_slot(sid, store, instance_id=_INSTANCE_A)

    release_store, release_sid = _mk_store(tmp_path, "s-lease3")
    assert ar._acquire_session_slot(release_sid, release_store, instance_id=_INSTANCE_A)
    ar._release_session_slot(release_sid, release_store, instance_id=_INSTANCE_A)
    assert ar._acquire_session_slot(release_sid, release_store, instance_id=_INSTANCE_B), (
        "释放后他实例可认领"
    )
    ar._release_session_slot(release_sid, release_store, instance_id=_INSTANCE_B)

    persist_store, persist_sid = _mk_store(tmp_path, "s-lease4")
    assert ar._acquire_session_slot(persist_sid, persist_store, instance_id=_INSTANCE_A)
    store2 = SessionStore(str(tmp_path / "sess.db"))
    assert not ar._acquire_session_slot(persist_sid, store2, instance_id=_INSTANCE_B), (
        "租约必须跨进程持久化可见"
    )
    ar._release_session_slot(persist_sid, store2, instance_id=_INSTANCE_A)


def test_expired_lease_reclaim_and_cas_rejects_stale(tmp_path: Path) -> None:
    """崩溃遗留的过期租约可被认领；陈旧快照 CAS 覆盖必须被拒绝。"""
    store, sid = _mk_store(tmp_path, "s-lease2")
    # 手工写入过期租约（模拟崩溃进程遗留）
    session = store.get_session(sid)
    assert session is not None
    metadata = dict(json_loads(session.get("metadata") or "{}"))
    metadata["execution_claim"] = {
        "instance_id": "dead-instance",
        "claimed_at": (datetime.now(UTC) - timedelta(seconds=600)).isoformat(),
        "lease_expires_at": (datetime.now(UTC) - timedelta(seconds=300)).isoformat(),
    }
    store.update_session(sid, metadata=json_dumps(metadata))

    assert ar._acquire_session_slot(sid, store, instance_id=_INSTANCE_B), "过期租约可认领"
    ar._release_session_slot(sid, store, instance_id=_INSTANCE_B)

    cas_store, cas_sid = _mk_store(tmp_path, "s-lease5")
    stale_snapshot = dict(ar._session_lease_meta(cas_store, cas_sid) or {})

    assert ar._acquire_session_slot(cas_sid, cas_store, instance_id=_INSTANCE_A), "A 认领成功"
    stale_snapshot["execution_claim"] = {
        "instance_id": _INSTANCE_B,
        "claimed_at": "T00:00:00+00:00",
        "lease_expires_at": "T00:10:00+00:00",
    }
    assert not cas_store.update_session_if_metadata(cas_sid, stale_snapshot, stale_snapshot), (
        "CAS 必须拒绝陈旧快照覆盖（A 已认领）"
    )
    ar._release_session_slot(cas_sid, cas_store, instance_id=_INSTANCE_A)


def test_release_after_store_close_is_silent_noop(tmp_path: Path, caplog) -> None:
    """幂等丢弃语义：store 已关闭后调用 _release_session_slot 不得抛异常、不打警告
    （生产路径 agent_runner 外层 finally 双释放时 store 可能已 close）。"""
    import logging

    store, sid = _mk_store(tmp_path, "s-lease-close")
    assert ar._acquire_session_slot(sid, store, instance_id=_INSTANCE_A)
    store.close()
    with caplog.at_level(logging.WARNING, logger="bandi.core.agent_runner"):
        ar._release_session_slot(sid, store, instance_id=_INSTANCE_A)
    assert "session lease release failed" not in caplog.text, (
        f"store 关闭后的幂等释放不得打警告，实际: {caplog.text[:300]}"
    )


@pytest.mark.asyncio
async def test_run_agent_turn_owned_store_releases_lease_silently(
    tmp_path: Any, monkeypatch: Any, caplog: Any
) -> None:
    """
    run_agent_turn（store_owned=True 生产路径）结束后：不得打 release failed 警告
    且租约必须真正清除——新 store 实例（模拟另一进程）必须能立即认领同一 session。
    """
    import logging
    from pathlib import Path
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="lease-e2e 回复",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=4, completion_tokens=2),
            finish_reason="stop",
        )
    )

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        cfg.agent.max_iterations = 1
        cfg.agent.stream_to_ui = False
        with caplog.at_level(logging.WARNING, logger="bandi.core.agent_runner"):
            with patch("bandi.core.agent_runner.build_provider", return_value=provider):
                result = await run_agent_turn(
                    user_input="lease e2e 输入",
                    session_id=sid,
                    bandi_config=cfg,
                )
        assert result.terminal_reason is not None, "run_agent_turn 必须正常结束"

        # 新 store 实例 = 模拟另一进程读同一 db——租约必须已被清除，立即认领成功
        store2 = SessionStore(str(tmp_path / "sess.db"))
        try:
            assert ar._acquire_session_slot(sid, store2, instance_id=_INSTANCE_B), (
                "run 结束后租约必须已清除（他进程可立即认领）"
            )
            ar._release_session_slot(sid, store2, instance_id=_INSTANCE_B)
        finally:
            store2.close()

    assert "session lease release failed" not in caplog.text, (
        f"store_owned=True 的 run_agent_turn 不得打 release failed 警告，实际: {caplog.text[:400]}"
    )


def json_dumps(obj: dict) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def json_loads(text: str) -> dict:
    import json

    return json.loads(text)


# ==================== e4_repair_pairs ====================


def _mk_msgs() -> list[Message]:
    user = Message(role=MessageRole.USER, content="任务")
    assistant_with_calls = Message(
        role=MessageRole.ASSISTANT,
        content="开始执行",
        tool_calls=[ToolCall(id="tc1", name="bash", arguments={"cmd": "ls"})],
    )
    tool_result = Message(role=MessageRole.TOOL, content="<result>", tool_call_id="tc1")
    final = Message(role=MessageRole.ASSISTANT, content="完成")
    return [user, assistant_with_calls, tool_result, final]


def test_repair_tool_pairs_boundaries_and_noop() -> None:
    """切割点落在 assistant 与结果之间时前移边界纳入配对；keep_messages 不得含孤儿
    TOOL；配对完整时 repair 为 no-op。"""
    msgs = _mk_msgs()
    new_idx = repair_tool_pairs(msgs, idx=2)
    assert new_idx == 1, f"必须前移到 assistant 纳入配对（实际 {new_idx}）"
    kept = msgs[new_idx:]
    assert any(m.role == MessageRole.TOOL and m.tool_call_id == "tc1" for m in kept)
    assert any(tc.id == "tc1" for m in kept for tc in m.tool_calls or []), "配对完整"

    compact_msgs = _mk_msgs()
    non_system = [m for m in compact_msgs if m.role != MessageRole.SYSTEM]
    split_idx = repair_tool_pairs(non_system, 2)
    keep_messages = non_system[split_idx:]
    assert split_idx == 1
    assert any(m.role == MessageRole.TOOL and m.tool_call_id == "tc1" for m in keep_messages), (
        "keep_messages 不得含孤儿 TOOL（split 边界必须 repair）"
    )

    noop_msgs = _mk_msgs()
    assert repair_tool_pairs(noop_msgs, idx=0) == 0, "全保留无影响"
    assert repair_tool_pairs(noop_msgs, idx=1) == 1, "head 即 assistant 且结果在保留区，无需移动"


# ==================== e4_rework_parallel ====================
# 功能：契约——rework 阶段并行化（并发工程 批次5 / ）
#
# 而 rework 阶段纯串行 await，与 todo 设计不一致（多角色积压时吞吐受限）。
# 注：seat 模型为「每角色 1 席」，同一角色的卡天然串行；并行收益在多角色场景。
# 本契约钉：跨角色 rework 批并行执行（峰值 > 1）/ 单点失败不取消整批 /
# 每 tick 预算仍受 max_concurrent 约束（语义保真）。


_ROLES = ("r0", "r1", "r2", "r3")


def _dispatcher(
    tmp_path: Any, max_concurrent: int = 3, max_items_per_tick: int = 64
) -> CompanyDispatcher:
    org = OrgEngine()
    recruiter = Recruiter()
    for role in _ROLES:
        org.register_role(role, role.upper(), f"{role} executes tasks")
        recruiter.register_seat(role)
    return CompanyDispatcher(
        store=WorkItemStore(str(tmp_path / "dispatch.db")),
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=EngineSeatExecutor(),
        config=DispatcherConfig(
            max_concurrent=max_concurrent, max_items_per_tick=max_items_per_tick
        ),
    )


def _rework_item(dispatcher: CompanyDispatcher, role: str, index: int) -> str:
    item = create_work_item(
        title=f"rework-{role}-{index}",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role=role,
    )
    item.phase = Phase.READY_FOR_REWORK
    dispatcher.store.save(item)
    return item.id


@pytest.mark.asyncio
async def test_rework_parallelism_and_concurrency_gate(tmp_path: Any, monkeypatch: Any) -> None:
    """3 个不同角色 rework 卡同 tick 执行并发峰值 ≥ 2；max_concurrent=2 时并发峰值 ≤ 2。"""
    dispatcher = _dispatcher(tmp_path)
    for i, role in enumerate(_ROLES[:3]):
        _rework_item(dispatcher, role, i)

    active = {"n": 0, "peak": 0}

    async def fake_run(work_item_id: str) -> None:
        active["n"] += 1
        active["peak"] = max(active["peak"], active["n"])
        await asyncio.sleep(0.05)
        item = dispatcher.store.get(work_item_id)
        if item is not None:
            item.phase = Phase.RUNNING
            dispatcher.store.save(item)
        active["n"] -= 1

    monkeypatch.setattr(dispatcher, "run_work_item", fake_run)
    report = await dispatcher.tick()

    assert active["peak"] >= 2, f"跨角色 rework 未并行（峰值 {active['peak']}）"
    assert len(report.processed) == 3, f"3 卡都应落账（实际 {report.processed}）"
    assert not report.has_errors(), f"正常批次不得有错误：{report.errors}"

    gate_dir = tmp_path / "gate"
    gate_dir.mkdir()
    gate_dispatcher = _dispatcher(gate_dir, max_concurrent=2)
    for i, role in enumerate(_ROLES):
        _rework_item(gate_dispatcher, role, i)

    gate_active = {"n": 0, "peak": 0}
    ran: list[str] = []

    async def gate_fake_run(work_item_id: str) -> None:
        gate_active["n"] += 1
        gate_active["peak"] = max(gate_active["peak"], gate_active["n"])
        ran.append(work_item_id)
        item = gate_dispatcher.store.get(work_item_id)
        if item is not None:
            item.phase = Phase.RUNNING
            gate_dispatcher.store.save(item)
        await asyncio.sleep(0.02)
        gate_active["n"] -= 1

    monkeypatch.setattr(gate_dispatcher, "run_work_item", gate_fake_run)
    await gate_dispatcher.tick()

    assert len(set(ran)) >= 2, "至少两卡执行（跨角色并行基线）"
    assert gate_active["peak"] <= 2, f"并发峰值超闸门：{gate_active['peak']} > 2"


@pytest.mark.asyncio
async def test_rework_failure_isolation_and_accounting(tmp_path: Any, monkeypatch: Any) -> None:
    """单卡抛错不取消整批且计入 report.errors；SeatResult(success=False) 不计 processed。"""
    dispatcher = _dispatcher(tmp_path)
    doomed = _rework_item(dispatcher, "r0", 0)
    _rework_item(dispatcher, "r1", 1)
    _rework_item(dispatcher, "r2", 2)

    async def fake_run(work_item_id: str) -> None:
        item = dispatcher.store.get(work_item_id)
        if item is not None:
            item.phase = Phase.RUNNING
            dispatcher.store.save(item)
        if work_item_id == doomed:
            raise RuntimeError("boom")
        await asyncio.sleep(0.01)

    monkeypatch.setattr(dispatcher, "run_work_item", fake_run)
    report = await dispatcher.tick()

    assert len(report.processed) == 2, "失败卡不得阻断其余两卡"
    assert any(doomed in e for e in report.errors), f"失败必须计入 errors：{report.errors}"

    result_dir = tmp_path / "seat-result"
    result_dir.mkdir()
    result_dispatcher = _dispatcher(result_dir)
    _rework_item(result_dispatcher, "r0", 0)
    _rework_item(result_dispatcher, "r1", 1)

    async def result_fake_run(work_item_id: str) -> SeatResult:
        item = result_dispatcher.store.get(work_item_id)
        if item is not None:
            item.phase = Phase.RUNNING
            result_dispatcher.store.save(item)
        if item is not None and item.assignee_role == "r0":
            return SeatResult(success=False, output="claim lost")
        return SeatResult(success=True, output="ok")

    monkeypatch.setattr(result_dispatcher, "run_work_item", result_fake_run)
    result_report = await result_dispatcher.tick()

    assert len(result_report.processed) == 1, (
        f"失败结果被虚报为 processed：{result_report.processed}"
    )
    assert any("claim lost" in e for e in result_report.errors), (
        f"失败结果未计入 errors：{result_report.errors}"
    )


@pytest.mark.asyncio
async def test_rework_seat_accounting_in_report(tmp_path: Any, monkeypatch: Any) -> None:
    """rework 认领落 report.claimed；成功执行落 report.released；失败项同样释放座位。"""
    dispatcher = _dispatcher(tmp_path)
    _rework_item(dispatcher, "r0", 0)
    _rework_item(dispatcher, "r1", 1)

    async def fake_run(work_item_id: str) -> SeatResult:
        item = dispatcher.store.get(work_item_id)
        if item is not None:
            item.phase = Phase.RUNNING
            dispatcher.store.save(item)
        return SeatResult(success=True, output="ok")

    monkeypatch.setattr(dispatcher, "run_work_item", fake_run)
    report = await dispatcher.tick()

    assert len(report.processed) == 2, f"两卡都应落账：{report.processed}"
    assert sorted(report.released) == ["r0", "r1"], (
        f"rework 座位释放未落账：released={report.released}"
    )
    assert sorted(report.claimed) == ["r0", "r1"], f"rework 认领未落账：claimed={report.claimed}"

    fail_dir = tmp_path / "fail-release"
    fail_dir.mkdir()
    fail_dispatcher = _dispatcher(fail_dir)
    _rework_item(fail_dispatcher, "r0", 0)
    _rework_item(fail_dispatcher, "r1", 1)

    async def fail_fake_run(work_item_id: str) -> SeatResult:
        item = fail_dispatcher.store.get(work_item_id)
        if item is not None:
            item.phase = Phase.RUNNING
            fail_dispatcher.store.save(item)
        if item is not None and item.assignee_role == "r0":
            raise RuntimeError("boom")
        return SeatResult(success=True, output="ok")

    monkeypatch.setattr(fail_dispatcher, "run_work_item", fail_fake_run)
    fail_report = await fail_dispatcher.tick()

    assert sorted(fail_report.released) == ["r0", "r1"], (
        f"失败项的座位未释放落账：released={fail_report.released}"
    )
    assert any("boom" in e for e in fail_report.errors), f"失败未计入 errors：{fail_report.errors}"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rework_honors_max_items_per_tick(tmp_path: Any, monkeypatch: Any) -> None:
    """rework 每 tick 挑选数与 todo 同口径：min(max_concurrent, max_items_per_tick) 硬顶。"""
    dispatcher = _dispatcher(tmp_path, max_concurrent=3, max_items_per_tick=1)
    for i, role in enumerate(_ROLES[:3]):
        _rework_item(dispatcher, role, i)

    picked_sizes: list[int] = []
    original = dispatcher._run_picked

    async def spy(picked: Any) -> Any:
        picked_sizes.append(len(picked))
        return await original(picked)

    monkeypatch.setattr(dispatcher, "_run_picked", spy)
    await dispatcher.tick()

    assert picked_sizes, "本 tick 应至少执行一批"
    assert picked_sizes[0] == 1, (
        f"rework 阶段挑选数未受 max_items_per_tick 硬顶：{picked_sizes[0]}（应 1）"
    )


@pytest.mark.asyncio
async def test_rework_error_truncated_in_report(tmp_path: Any, monkeypatch: Any) -> None:
    """F2a 契约：SeatResult(False).output 超长（12 万字符）→ report.errors 条目必须
    截断到 2000 字符 + 省略号（todo/rework 同口径），前缀保留可读。"""
    dispatcher = _dispatcher(tmp_path)
    rework_id = _rework_item(dispatcher, "r0", 0)
    todo_item = create_work_item(
        title="todo-long-error",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="r1",
    )
    dispatcher.store.save(todo_item)
    long_output = "x" * 120_000

    async def fake_run(work_item_id: str) -> SeatResult:
        item = dispatcher.store.get(work_item_id)
        if item is not None:
            item.phase = Phase.RUNNING
            dispatcher.store.save(item)
        return SeatResult(success=False, output=long_output)

    monkeypatch.setattr(dispatcher, "run_work_item", fake_run)
    report = await dispatcher.tick()

    rework_errors = [e for e in report.errors if e.startswith(f"rework {rework_id}:")]
    todo_errors = [e for e in report.errors if e.startswith(f"todo {todo_item.id}:")]
    assert rework_errors and todo_errors, f"失败必须入 errors：{report.errors}"
    for entry in rework_errors + todo_errors:
        assert len(entry) <= 2001, f"错误条目未截断：{len(entry)}"
        assert entry.endswith("…"), f"截断标记缺失：{entry[-20:]!r}"


@pytest.mark.asyncio
async def test_rework_execution_records_timeline(tmp_path: Any, monkeypatch: Any) -> None:
    """F2b 契约：rework 执行必须落 report.timeline（字段与 todo 一致：
    work_item_id/role_id/duration_ms/ok），成败均记。"""
    dispatcher = _dispatcher(tmp_path)
    ok_id = _rework_item(dispatcher, "r0", 0)
    fail_id = _rework_item(dispatcher, "r1", 1)

    async def fake_run(work_item_id: str) -> SeatResult:
        item = dispatcher.store.get(work_item_id)
        if item is not None:
            item.phase = Phase.RUNNING
            dispatcher.store.save(item)
        return SeatResult(
            success=work_item_id != fail_id, output="ok" if work_item_id != fail_id else "boom"
        )

    monkeypatch.setattr(dispatcher, "run_work_item", fake_run)
    report = await dispatcher.tick()

    by_id = {entry[0]: entry for entry in report.timeline}
    assert ok_id in by_id, f"rework 执行未进时间线：{report.timeline}"
    assert fail_id in by_id, f"失败 rework 未进时间线：{report.timeline}"
    assert by_id[ok_id][1] == "r0" and by_id[ok_id][3] is True
    assert by_id[fail_id][1] == "r1" and by_id[fail_id][3] is False
    for _wid, _role, duration_ms, _ok in by_id.values():
        assert duration_ms >= 0.0


# ==================== e5_layer_declaration ====================
# 功能：契约——权限管线的「声明 = 实际」层数（并发工程 批次5 / ）
#
# 模块 docstring 宣称 7 层管线「Deny→Ask→Mode→Shell→UI→External→Sensitive」
# 但 Layer 5(UI) 与 Layer 7(External) 是**预留层、不参与拦截**，实际生效只有 5 层；
# 且 docstring 的层顺序与代码执行顺序不符（代码是 …Shell→Sensitive→(External)）。
# 本契约把「声明与实际」钉在一起，防止继续静默漂移。


def test_layer_counts_and_disjointness() -> None:
    """管线共 7 层（5 生效 + 2 预留）——总数不得静默变化，预留层不得被算作生效层。"""
    assert TOTAL_LAYERS == 7
    assert len(ACTIVE_LAYERS) == 5
    assert len(RESERVED_LAYERS) == 2
    assert set(ACTIVE_LAYERS).isdisjoint(RESERVED_LAYERS), "预留层与生效层重叠"


def test_reserved_and_active_layer_membership() -> None:
    """预留层必须正是 UI 与外部服务两层；生效层覆盖实际拦截链。"""
    assert set(RESERVED_LAYERS) == {"ui", "external"}
    assert set(ACTIVE_LAYERS) == {"deny", "ask", "mode", "shell", "sensitive"}


# ==================== e6_inject_state ====================
# 功能：契约——实时注入不得改写状态机状态（并发工程复核 🟡-4）
#
# `agent/base.py::inject_input` 是**外部任务**入口（API `/steer` 等），却会写
# `self.current_state = "thinking"`；而主循环在 `loop.py:728/735` 直接用
# `ctx.current_state` 取 handler，且 `thinking` 的迁移表**无 default 兜底**
# （`base.py:699-700` 未匹配即返回 "terminated"）。
# 工具执行期（最长窗口）插话会命中该竞态 → 本轮提前终止、工具结果白跑
# 注入文本靠 兜底回队。
# 本契约钉：注入只置标志**不改状态**；状态只由 loop 消费点在有序位置设置。


def r10xe6_ctx(state: str = "executing") -> ExecutionContext:
    ctx = ExecutionContext(messages=[], provider=object(), config=object(), bus=object())
    ctx.current_state = state
    return ctx


class TestInjectDoesNotMutateState:
    """注入是「标志写入」，不是「状态迁移」。"""

    @pytest.mark.parametrize(
        "state", ["init", "thinking", "parsing", "executing", "observing", "reflecting"]
    )
    def test_state_is_unchanged_for_every_state(self, state: str) -> None:
        """参数化覆盖全部状态（防只修单路径）。"""
        ctx = r10xe6_ctx(state)
        ctx.inject_input("改成按 B 方案做")

        assert ctx.current_state == state, f"注入改写了状态：{state} → {ctx.current_state}"

    def test_injected_text_recorded_and_concurrent_merge(self) -> None:
        """不改状态的前提下注入文本仍必须入标志位；并发 push 不覆盖、未消费则合并。"""
        ctx = r10xe6_ctx("executing")
        ctx.inject_input("先别删，改成备份")

        assert ctx.take_injected_input() == "先别删，改成备份"

        concurrent_ctx = r10xe6_ctx("executing")
        threads = [
            threading.Thread(target=concurrent_ctx.inject_input, args=(f"指令{i}",))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        text = concurrent_ctx.take_injected_input() or ""
        assert all(f"指令{i}" in text for i in range(8)), f"并发注入被覆盖：{text!r}"


class TestInjectInputBlankGuard:
    """🟢-G：空/纯空白注入是 no-op（生产入口校验的兜底幂等）。"""

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t ", "　"])
    def test_blank_injection_changes_nothing(self, blank: str) -> None:
        ctx = r10xe6_ctx("executing")
        ctx.inject_input(blank)

        assert ctx.injected_input is None
        assert ctx.injected_text is None
        assert ctx._injected_consumed is False
        assert ctx.current_state == "executing"
        assert ctx.take_injected_input() is None

    def test_surrounding_whitespace_trimmed(self) -> None:
        ctx = r10xe6_ctx("executing")
        ctx.inject_input("  先看日志  ")
        assert ctx.take_injected_input() == "先看日志"


class TestStateMachineOutcome:
    """破坏者视角：注入后状态机不得被推向 terminated。"""

    def test_injection_state_machine_outcomes(self) -> None:
        """执行期插入 → 状态机仍走 executing → observing；thinking 各迁移全部带条件
        （无兜底，风险前提）；executing 有无条件迁移（保持状态即可安全前进）。"""
        ctx = r10xe6_ctx("executing")
        ctx.inject_input("等一下，先看下日志")

        next_state = build_default_machine().next(ctx)

        assert next_state == "observing", (
            f"执行期注入把本轮推向 {next_state!r}（期望 observing）——工具结果将白跑"
        )

        sm = build_default_machine()
        thinking = sm._transitions["thinking"]

        assert thinking, "thinking 迁移表为空"
        assert all(t.condition is not None for t in thinking), (
            "thinking 新增了无条件迁移——风险前提已变，需复核外部注入写状态是否仍有害"
        )

        executing = build_default_machine()._transitions["executing"]

        assert any(t.condition is None for t in executing), "executing 缺少无条件迁移"


# ==================== e6_steer_convergence ====================
# 功能：契约——steering 双通道收敛为单一入口（并发工程 批次5 / ）
#
# （API push → on_executing 批边界 drain 成 USER 消息）语义重叠；同一文本若
# 同时经两条通道投递会产生**两条** USER 消息。
# 本契约钉：push 对活跃会话优先走实时注入且不入队（批通道 drain 必空）；
# 无活跃 ctx（会话未运行/已结束）才回退队列（兼容既有语义）。


class R11xe6Bus:
    async def emit(self, *args: Any, **kwargs: Any) -> None:
        return None


def _register_active(session_id: str, ctx: ExecutionContext) -> None:
    import bandi.core.agent_runner as runner_mod

    runner_mod._active_contexts[session_id] = ctx


def _unregister_active(session_id: str) -> None:
    import bandi.core.agent_runner as runner_mod

    runner_mod._active_contexts.pop(session_id, None)


def test_steer_push_routing_and_validation() -> None:
    """活跃会话 push 走实时注入不入队；无活跃/已终止回退队列；queue_only 直接入队；
    FIFO 与空内容/空 id 校验保持既有语义。"""
    sid = "sess-e6-active"
    ctx = ExecutionContext(messages=[], provider=object(), config=object(), bus=R11xe6Bus())
    registry = SessionSteerRegistry()
    _register_active(sid, ctx)
    try:
        registry.push(sid, "转向：先看配置")
    finally:
        _unregister_active(sid)

    assert ctx.injected_input == "转向：先看配置", "活跃会话必须走实时注入"
    assert registry.drain(sid) == [], "同一文本不得再入队（防批通道二次投递）"

    idle_registry = SessionSteerRegistry()
    idle_registry.push("sess-e6-idle", "排队转向")

    assert idle_registry.drain("sess-e6-idle") == ["排队转向"]

    terminated_sid = "sess-e6-terminated"
    terminated_ctx = ExecutionContext(
        messages=[], provider=object(), config=object(), bus=R11xe6Bus()
    )
    terminated_ctx.terminal_reason = TerminalReason.COMPLETED
    terminated_registry = SessionSteerRegistry()
    _register_active(terminated_sid, terminated_ctx)
    try:
        terminated_registry.push(terminated_sid, "迟到的转向")
    finally:
        _unregister_active(terminated_sid)

    assert terminated_ctx.injected_input is None, "已终止 ctx 不得吞下注入"
    assert terminated_registry.drain(terminated_sid) == ["迟到的转向"], "已终止会话必须回退队列"

    queue_only_sid = "sess-e6-queue-only"
    queue_only_ctx = ExecutionContext(
        messages=[], provider=object(), config=object(), bus=R11xe6Bus()
    )
    queue_only_registry = SessionSteerRegistry()
    _register_active(queue_only_sid, queue_only_ctx)
    try:
        queue_only_registry.push(queue_only_sid, "只入队转向", queue_only=True)
    finally:
        _unregister_active(queue_only_sid)

    assert queue_only_ctx.injected_input is None, "queue_only 不得写活跃 ctx"
    assert queue_only_registry.drain(queue_only_sid) == ["只入队转向"], "queue_only 必须直接入队"

    fifo_registry = SessionSteerRegistry()
    fifo_registry.push("s1", "a")
    fifo_registry.push("s1", "b")
    assert fifo_registry.drain("s1") == ["a", "b"]

    with pytest.raises(ValueError):
        fifo_registry.push("s1", "   ")
    with pytest.raises(ValueError):
        fifo_registry.push(None, "x")


def test_injection_merge_and_take_window() -> None:
    """多次注入在实时通道内合并为一条（`\\n` 连接）；原子 take 后新 push
    不得被旧「读后 clear」窗口吞掉，取走后不得双读。"""
    sid = "sess-e6-multi"
    ctx = ExecutionContext(messages=[], provider=object(), config=object(), bus=R11xe6Bus())
    registry = SessionSteerRegistry()
    _register_active(sid, ctx)
    try:
        registry.push(sid, "第一条")
        registry.push(sid, "第二条")
    finally:
        _unregister_active(sid)

    assert ctx.injected_input == "第一条\n第二条"
    assert registry.drain(sid) == []

    take_ctx = ExecutionContext(messages=[], provider=object(), config=object(), bus=R11xe6Bus())
    take_ctx.inject_input("第一条")
    assert take_ctx.take_injected_input() == "第一条"

    take_ctx.inject_input("第二条")
    assert take_ctx.take_injected_input() == "第二条", "take 后的新 push 不得丢失"

    assert take_ctx.take_injected_input() is None, "取走后不得双读"


def test_concurrent_inject_take_never_loses_text() -> None:
    """E6a：多线程并发 push N 条 + 主线程循环 take——文本零丢失、不双读。

    （无锁「读-改-写」在并发拼接/覆盖时会整条丢失；take 同锁互斥。）"""
    import sys
    import threading
    import time

    ctx = ExecutionContext(messages=[], provider=object(), config=object(), bus=R11xe6Bus())
    n = 320
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def _push(i: int) -> None:
        try:
            barrier.wait(timeout=15)
            ctx.inject_input(f"m{i:03d}")
        except BaseException as exc:  # noqa: BLE001 — 线程异常须回传主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=_push, args=(i,)) for i in range(n)]
    seen: set[str] = set()
    old_interval = sys.getswitchinterval()
    # 极限切换间隔放大竞态窗口（无锁实现必丢；有锁实现不受影响）
    sys.setswitchinterval(1e-6)
    try:
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            text = ctx.take_injected_input()
            if text:
                seen.update(part for part in text.split("\n") if part)
            time.sleep(0.0002)
        for t in threads:
            t.join(timeout=5)
        while True:
            text = ctx.take_injected_input()
            if not text:
                break
            seen.update(part for part in text.split("\n") if part)
    finally:
        sys.setswitchinterval(old_interval)

    assert not errors, f"push 线程异常: {errors[:3]}"
    expected = {f"m{i:03d}" for i in range(n)}
    lost = sorted(expected - seen)
    assert not lost, f"并发注入丢文本 {len(lost)} 条: {lost[:8]}"
    assert len(seen) == n, f"只读消费不得重复/多出文本: 期望 {n} 实收 {len(seen)}"


# ── abort 竞态丢注入 → loop 退出兜底回退 steer 队列 ──


class _AbortStub:
    """最小 abort 控制器桩：should_abort 为真即触发 loop 中断。"""

    def __init__(self) -> None:
        self.should_abort = False


class _LoopStubProvider:
    """最小 provider：立即返回纯文本完成（驱动正常路径收敛）。"""

    supports_streaming = False

    @property
    def model_name(self) -> str:
        return "stub"

    @property
    def protocol(self) -> str:
        return "openai"

    async def complete(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any):
        from bandi.core.message import LLMResponse

        return LLMResponse(content="完成")


def _loop_ctx(session_id: str, registry: SessionSteerRegistry):
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.core.message import Message, MessageRole

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="任务")],
        provider=_LoopStubProvider(),
        config=AgentConfig(),
        bus=R11xe6Bus(),
    )
    ctx.session_id = session_id
    ctx.meta["steer_registry"] = registry
    return ctx


@pytest.mark.asyncio
async def test_loop_exit_requeues_unconsumed_injection(monkeypatch: Any) -> None:
    """abort / 异常 / 超时退出必须回退未消费注入（不得留死 ctx）；
    正常路径注入恰好消费为一条 USER 消息，退出时不得再回队（不双投）。"""
    from bandi.agent.loop import AgentLoop

    sid = "sess-e6-abort-race"
    registry = SessionSteerRegistry()
    ctx = _loop_ctx(sid, registry)
    abort = _AbortStub()
    loop = AgentLoop(max_iterations=3, abort_controller=abort)
    monkeypatch.setattr(loop, "_wire_safety", lambda c: None)
    ctx.inject_input("转向：先看配置")
    abort.should_abort = True

    reason = await loop.run(ctx)

    assert reason == TerminalReason.USER_INTERRUPT
    assert ctx.injected_input is None, "文本必须被取走（不得留死 ctx）"
    assert registry.drain(sid) == ["转向：先看配置"], "abort 退出必须回退未消费注入"

    exc_sid = "sess-e6-exc-race"
    exc_registry = SessionSteerRegistry()
    exc_ctx = _loop_ctx(exc_sid, exc_registry)

    async def boom(ctx: Any, max_turns: int, turns_taken: int):
        raise RuntimeError("boom")

    exc_loop = AgentLoop(max_iterations=3)
    monkeypatch.setattr(exc_loop, "_wire_safety", lambda c: None)
    monkeypatch.setattr(exc_loop, "_run_loop", boom)
    exc_ctx.inject_input("异常退出转向")

    with pytest.raises(RuntimeError):
        await exc_loop.run(exc_ctx)

    assert exc_registry.drain(exc_sid) == ["异常退出转向"], "异常退出必须回退未消费注入"
    assert exc_ctx.terminal_reason is None, "回退不得篡改终态语义（无终态保持无终态）"

    timeout_sid = "sess-e6-timeout-race"
    timeout_registry = SessionSteerRegistry()
    timeout_ctx = _loop_ctx(timeout_sid, timeout_registry)

    async def hang(ctx: Any, max_turns: int, turns_taken: int):
        await asyncio.sleep(30)

    timeout_loop = AgentLoop(max_iterations=3)
    monkeypatch.setattr(timeout_loop, "_wire_safety", lambda c: None)
    monkeypatch.setattr(timeout_loop, "_run_loop", hang)
    timeout_ctx.inject_input("超时退出转向")

    timeout_reason = await timeout_loop.run(timeout_ctx, session_timeout_sec=0.05)

    assert timeout_reason == TerminalReason.TIMEOUT
    assert timeout_registry.drain(timeout_sid) == ["超时退出转向"], "超时退出必须回退未消费注入"

    normal_sid = "sess-e6-normal"
    normal_registry = SessionSteerRegistry()
    normal_ctx = _loop_ctx(normal_sid, normal_registry)
    normal_loop = AgentLoop(max_iterations=3)
    monkeypatch.setattr(normal_loop, "_wire_safety", lambda c: None)
    normal_ctx.inject_input("正常转向")

    normal_reason = await normal_loop.run(normal_ctx)

    assert normal_reason == TerminalReason.COMPLETED
    assert normal_registry.drain(normal_sid) == [], "正常消费后不得再回队（防双投）"
    injected_msgs = [
        m for m in normal_ctx.messages if m.role == MessageRole.USER and m.content == "正常转向"
    ]
    assert len(injected_msgs) == 1, "注入文本恰好消费为一条 USER 消息"


# ── 🟡 放大：注入 USER 消息必须落库（崩溃恢复不丢转向） ──────────────────


def _persist_ctx(session_id: str, store: Any) -> Any:
    """最小注入消费 ctx：真实 store 走 ctx.meta['session_store']（生产同源）。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig

    ctx = ExecutionContext(messages=[], provider=object(), config=AgentConfig(), bus=R11xe6Bus())
    ctx.session_id = session_id
    ctx.meta["session_store"] = store
    return ctx


@pytest.mark.asyncio
async def test_injected_message_persistence_and_recovery(tmp_path: Any) -> None:
    """注入消费必须落库（get_branch 可读回）；崩溃重开库走恢复链可见；
    无 session_store 时静默跳过不炸主链。"""
    from bandi.agent.loop import AgentLoop
    from bandi.core.agent_runner import _session_restore
    from bandi.memory.session.store import SessionStore

    store = SessionStore(tmp_path / "sessions-a.db")
    sid = store.create_session(mode="execute")
    ctx = _persist_ctx(sid, store)
    loop = AgentLoop(max_iterations=3)
    ctx.inject_input("转向：先看配置")

    assert await loop._consume_injected_input(ctx, 0) is True

    users = [
        e
        for e in store.get_branch(sid)
        if e.get("entry_type") == "message"
        and e.get("role") == "user"
        and e.get("content") == "转向：先看配置"
    ]
    assert len(users) == 1, f"注入 USER 消息必须落库（实际 {store.get_branch(sid)}）"
    store.close()

    db = tmp_path / "sessions-b.db"
    crash_store = SessionStore(db)
    crash_sid = crash_store.create_session(mode="execute")
    crash_ctx = _persist_ctx(crash_sid, crash_store)
    crash_loop = AgentLoop(max_iterations=3)
    crash_ctx.inject_input("崩溃前转向：改用 B 方案")
    assert await crash_loop._consume_injected_input(crash_ctx, 0) is True
    crash_store.close()

    reopened = SessionStore(db)
    restored = ExecutionContext(
        messages=[], provider=object(), config=AgentConfig(), bus=R11xe6Bus()
    )
    _session_restore(crash_sid, reopened, restored)

    assert any(m.content == "崩溃前转向：改用 B 方案" for m in restored.messages), (
        "崩溃恢复重建的 messages 必须包含转向（否则恢复丢转向）"
    )
    reopened.close()

    no_store_ctx = _persist_ctx("sess-e6-no-store", None)
    no_store_ctx.meta.pop("session_store", None)
    no_store_loop = AgentLoop(max_iterations=3)
    no_store_ctx.inject_input("无 store 转向")

    assert await no_store_loop._consume_injected_input(no_store_ctx, 0) is True
    assert any(
        m.role == MessageRole.USER and m.content == "无 store 转向" for m in no_store_ctx.messages
    )


@pytest.mark.asyncio
async def test_injected_message_persist_idempotent(tmp_path: Any) -> None:
    """幂等：同 session+turn+content 重复消费不双写（崩溃重放不堆叠）。"""
    from bandi.agent.loop import AgentLoop
    from bandi.memory.session.store import SessionStore

    store = SessionStore(tmp_path / "sessions.db")
    sid = store.create_session(mode="execute")
    ctx = _persist_ctx(sid, store)
    loop = AgentLoop(max_iterations=3)

    ctx.inject_input("重复转向")
    assert await loop._consume_injected_input(ctx, 0) is True
    ctx.inject_input("重复转向")
    assert await loop._consume_injected_input(ctx, 0) is True

    users = [
        e
        for e in store.get_branch(sid)
        if e.get("entry_type") == "message"
        and e.get("role") == "user"
        and e.get("content") == "重复转向"
    ]
    assert len(users) == 1, f"同内容同轮不得双写（实际 {len(users)} 条）"
    store.close()


@pytest.mark.asyncio
async def test_requeued_injection_no_episodic_importance_bonus(monkeypatch: Any) -> None:
    """回队后清 injected_text——未消费轮的 episodic importance 不得 +0.1。"""
    from bandi.agent.loop import AgentLoop
    from bandi.core.agent_runner import _episode_importance

    sid = "sess-e6-requeue-importance"
    registry = SessionSteerRegistry()
    ctx = _loop_ctx(sid, registry)
    abort = _AbortStub()
    loop = AgentLoop(max_iterations=3, abort_controller=abort)
    monkeypatch.setattr(loop, "_wire_safety", lambda c: None)
    ctx.inject_input("未消费转向")
    abort.should_abort = True

    await loop.run(ctx)

    assert registry.drain(sid) == ["未消费转向"], "前提：文本必须回队"
    assert ctx.injected_text is None, "回队后必须清注入痕迹"
    assert _episode_importance(ctx) == 0.4, "未消费轮 episodic 不得带转向加分"


class _FailingRegistry:
    """push 必失败的 registry 桩（观测路径）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    def push(self, session_id: str | None, content: str, *, queue_only: bool = False) -> None:
        self.calls.append((str(session_id), content, queue_only))
        raise RuntimeError("steer registry down")

    def drain(self, session_id: str | None) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_requeue_push_failure_logged_and_counted(monkeypatch: Any, caplog: Any) -> None:
    """回队 push 失败必须 error 日志 + 计数 + 主流程不炸。"""
    import logging

    from bandi.agent.base import TerminalReason
    from bandi.agent.loop import AgentLoop

    sid = "sess-e6-push-fail"
    registry = _FailingRegistry()
    ctx = _loop_ctx(sid, registry)
    abort = _AbortStub()
    loop = AgentLoop(max_iterations=3, abort_controller=abort)
    monkeypatch.setattr(loop, "_wire_safety", lambda c: None)
    ctx.inject_input("回退失败的中文转向内容")
    abort.should_abort = True

    with caplog.at_level(logging.ERROR, logger="bandi.agent.loop"):
        reason = await loop.run(ctx)

    assert reason == TerminalReason.USER_INTERRUPT, "回退失败不得改变退出结果"
    assert ctx.meta.get("injection_requeue_failures") == 1, "失败必须计数（可观测）"
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("sess-e6-push-fail" in m or "回退失败的中文转向内容" in m for m in errors), (
        f"必须输出含 session/预览的 error 日志（实际 {errors}）"
    )
    assert registry.calls and registry.calls[0][2] is True, "回退必须走 queue_only 通道"
