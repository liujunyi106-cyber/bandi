# 功能：契约——员工链路产物登记（右栏「产物」数据源）
# 背景：产物登记此前只挂在用户 chat 回合的 ProcessRecorder 上，
# 员工执行链路（EmployeeProgressHandler）从不登记 ⇒ session_artifacts 恒空、
# 右栏产物永不出现。员工写类工具成功须登记到其执行会话（role-session），
# 项目聚合面（project_artifacts 前缀扫描）据此可见。
from __future__ import annotations

import asyncio
from pathlib import Path

from bandi.company.seat_progress import EmployeeProgressHandler
from bandi.memory.session.store import SessionStore


def _run(coro):
    return asyncio.run(coro)


def test_employee_write_registers_artifact(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "bandi.db"))
    sid = "role-session::p1::staff::t::worker::worker"
    store.create_session(sid)
    handler = EmployeeProgressHandler(
        lambda _frame: None, artifact_store=store, work_item_id="wi-42"
    )

    _run(handler.on_tool_call_start(sid, "write", {"file_path": "deliverables/a.md"}, "tc1"))
    _run(handler.on_tool_result(sid, "write", True, "ok", 5, "tc1"))

    arts = store.list_artifacts(sid)
    assert len(arts) == 1, f"员工写文件成功必须登记产物，实际 {len(arts)}"
    assert arts[0]["path"] == "deliverables/a.md"
    assert arts[0]["tool_name"] == "write"
    assert arts[0]["work_item_id"] == "wi-42", "产物必须关联工作项（回复下方交付物卡片精确匹配）"


def test_breaker_no_artifact_on_failure_or_non_write(tmp_path: Path) -> None:
    """破坏者：工具失败 / 非写工具 / 无 store 三种情况不得登记（也不得抛）。"""
    store = SessionStore(str(tmp_path / "bandi.db"))
    sid = "role-session::p1::staff::t::worker::worker"
    store.create_session(sid)
    handler = EmployeeProgressHandler(lambda _frame: None, artifact_store=store)

    _run(handler.on_tool_call_start(sid, "write", {"file_path": "x.md"}, "tc1"))
    _run(handler.on_tool_result(sid, "write", False, "boom", 5, "tc1"))
    _run(handler.on_tool_call_start(sid, "bash", {"command": "ls"}, "tc2"))
    _run(handler.on_tool_result(sid, "bash", True, "ok", 5, "tc2"))
    assert store.list_artifacts(sid) == [], "失败/非写工具不得登记产物"

    bare = EmployeeProgressHandler(lambda _frame: None)
    _run(bare.on_tool_call_start(sid, "write", {"file_path": "y.md"}, "tc9"))
    _run(bare.on_tool_result(sid, "write", True, "ok", 5, "tc9"))
    assert store.list_artifacts(sid) == [], "无 store 时静默跳过（不注册也不抛）"
