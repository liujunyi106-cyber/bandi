"""契约（TB-R2）：拒绝缠绕的「累计拒绝 → 硬终止」生产接线。

行为不变式（多 Agent 隔离安全 —— 用户点名 "不要实现歪了"）：

  1. headless 审批门（`_approval_gate` 自动拒绝短路分支）：每次自动拒绝记一次
     拒绝（系统代拒）——死缠重试可真实累积到累计阈值（默认 10）——达阈值时
     该次拒绝结果 `should_abort=True`，由 states 既有硬终止链接管（先给模型
     一次总结机会，再撞才真终止）。
  2. 阈值之下（累计 9）不误触发。
  3. 破坏者（多 Agent 隔离）：公司角色会话 A（员工甲）触顶，不得污染同 run
     另一员工会话 B（各自 role-session 键独立）。
  4. 破坏者（工具维度）：工具 X 触顶不影响工具 Y。

构造：真实流形态 —— 先经机审记账达到连续短路（3 次），再连续调用审批门
（短路分支每次自动拒绝即 +1 记账），逐步逼近累计阈值。conftest 重置 tracker。
"""

from __future__ import annotations

from typing import Any

import pytest

from bandi.core.message import ToolCall


class _PipelineStub:
    """功能：headless 判定桩——ask 工具集合 + 无同步 UI（进入审批流）。"""

    def __init__(self) -> None:
        self.ask_tools = {"bash", "write"}

    @property
    def user_interaction_available(self) -> bool:
        return False

    def needs_approval(self, tc: Any, tool: Any, mode: str) -> bool:
        return tc.name in self.ask_tools

    def cache_approved(self, tool_name: str, arguments: dict) -> None:
        return None


class _ToolStub:
    def __init__(self) -> None:
        self.name = "bash"
        self.risk_level = "high"


def _executor(session_id: str) -> Any:
    from bandi.tools.executor import ToolExecutor

    ex = ToolExecutor(registry=None)
    ex.__dict__.update(
        {
            "_permissions_pipeline": _PipelineStub(),
            "_mode": "execute",
            "_session_meta": {"session_id": session_id},
        }
    )
    return ex


def _deny(session_id: str, tool_name: str, times: int) -> None:
    from bandi.safety.denial_tracker import get_denial_tracker

    tracker = get_denial_tracker()
    for _ in range(times):
        tracker.record_denial(session_id, tool_name)


async def _hit_gate(ex: Any, tool_name: str, call_id: str) -> Any:
    return await ex._approval_gate(ToolCall(id=call_id, name=tool_name, arguments={}), _ToolStub())


@pytest.mark.asyncio
async def test_denial_total_reaches_threshold_triggers_hard_stop() -> None:
    """契约 1：短路分支每次自动拒绝记账——累计达阈值那一次必须携带 should_abort。"""
    sid = "denial-abort-s1"
    _deny(sid, "bash", 3)
    ex = _executor(sid)
    flags = []
    for i in range(7):
        result = await _hit_gate(ex, "bash", f"d{i}")
        assert result is not None
        flags.append(result.should_abort)
    assert flags[:6] == [False] * 6, f"未达阈值不得触发硬终止：{flags}"
    assert flags[6] is True, "累计第 10 次拒绝必须触发硬终止"
    last = await _hit_gate(ex, "bash", "d-last")
    assert last is not None and "中止" in (last.error or ""), (
        f"文案须说明本轮已中止：{last.error!r}"
    )


@pytest.mark.asyncio
async def test_denial_below_threshold_does_not_hard_stop() -> None:
    """契约 2（破坏者）：累计 9（阈值之下）不得误触发硬终止。"""
    sid = "denial-abort-s2"
    _deny(sid, "bash", 3)
    ex = _executor(sid)
    result = None
    for i in range(6):
        result = await _hit_gate(ex, "bash", f"e{i}")
    assert result is not None
    assert result.should_abort is False, "累计 9 次（阈值之下）不得触发硬终止"


@pytest.mark.asyncio
async def test_denial_abort_isolated_between_agent_sessions() -> None:
    """契约 3（多 Agent 破坏者）：员工甲会话触顶不污染员工乙会话同工具判定。"""
    sess_a = "role-session::run-9::团队::员工甲"
    sess_b = "role-session::run-9::团队::员工乙"
    _deny(sess_a, "bash", 3)
    _deny(sess_b, "bash", 3)
    ex_a = _executor(sess_a)
    ex_b = _executor(sess_b)
    last_a = None
    for i in range(7):
        last_a = await _hit_gate(ex_a, "bash", f"a{i}")
    last_b = await _hit_gate(ex_b, "bash", "b0")
    assert last_a is not None and last_b is not None
    assert last_a.should_abort is True, "触顶会话应硬终止"
    assert last_b.should_abort is False, "另一员工会话不得被跨会话污染"


@pytest.mark.asyncio
async def test_denial_abort_isolated_between_tools() -> None:
    """契约 4（破坏者）：工具 X 触顶不影响工具 Y 的判定。"""
    sid = "denial-abort-s4"
    _deny(sid, "bash", 3)
    _deny(sid, "write", 3)
    ex = _executor(sid)
    last_x = None
    for i in range(7):
        last_x = await _hit_gate(ex, "bash", f"x{i}")
    last_y = await _hit_gate(ex, "write", "y0")
    assert last_x is not None and last_y is not None
    assert last_x.should_abort is True
    assert last_y.should_abort is False, "工具维度不得串扰"
