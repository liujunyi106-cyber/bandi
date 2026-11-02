# 功能：契约——桥接（帧栈 → 组织层进度只读投影；隔离防污染）
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bandi.skills.card import SkillCard
from bandi.skills.frame_store import load_frames, save_frames
from bandi.skills.progress import build_sop_progress, frame_scope_for_session
from bandi.skills.runtime import SkillRuntime


def _card() -> SkillCard:
    return SkillCard(
        **{
            "skill_id": "sk_bridge_case",
            "name": "桥接用例",
            "version": "1.0.0",
            "description": "两节点流程（供投影测试）。",
            "required_info": ["订单号"],
            "nodes": [
                {
                    "node_id": "collect",
                    "name": "收集",
                    "type": "collect_info",
                    "instruction": "收集。",
                    "expected_user_info": ["订单号"],
                },
                {
                    "node_id": "done",
                    "name": "完成",
                    "type": "response",
                    "instruction": "完成。",
                },
            ],
            "edges": [{"source_node_id": "collect", "next_node_id": "done", "priority": 0}],
            "start_node_id": "collect",
            "terminal_node_ids": ["done"],
        }
    )


def _stack(scope: str = "sess-bridge", *, slots: dict[str, Any] | None = None) -> Any:
    runtime = SkillRuntime()
    return runtime.start(
        runtime.new_stack(scope),
        _card(),
        task_id="t1",
        slots=slots if slots is not None else {"客户名": "张三", "订单号": ""},
    )


def test_projection_contains_structured_progress() -> None:
    """契约（桥接·投影）：组织层可见——技能/当前节点/状态/待收集字段。"""
    progress = build_sop_progress(_stack())
    assert progress is not None
    active = progress["active"]
    assert active["skill_id"] == "sk_bridge_case"
    assert active["node_id"] == "collect"
    assert active["status"] == "active"
    assert active["awaiting_fields"] == ["订单号"], "待收集字段名必须可见"
    assert progress["pending"] == []
    assert progress["completed_count"] == 0


def test_projection_excludes_slot_values() -> None:
    """破坏者（隔离·防泄漏）：员工执行数据（槽位**值**）不得进组织层投影——
    只传播字段名（已收集哪些项），不传播内容（如客户数据）。"""
    progress = build_sop_progress(_stack(slots={"客户名": "张三", "订单号": "A1"}))
    assert progress is not None
    blob = json.dumps(progress, ensure_ascii=False)
    assert "张三" not in blob, "槽位值不得出现在投影中（员工执行数据隔离）"
    assert "客户名" in blob, "字段名可见（组织层看'收了哪些项'）"
    assert "A1" not in json.dumps(progress, ensure_ascii=False)


def test_projection_is_read_only(tmp_path: Path) -> None:
    """破坏者（防污染）：投影是纯读——帧文件逐字不变（组织层不得写帧）。"""
    frames_dir = str(tmp_path / "frames")
    save_frames(_stack(), frames_dir)
    path = Path(frames_dir) / "sess-bridge.frames.json"
    before = path.read_bytes()

    build_sop_progress(load_frames("sess-bridge", frames_dir))

    assert path.read_bytes() == before, "投影不得改动帧文件（只读）"


def test_projection_mutation_does_not_touch_frame(tmp_path: Path) -> None:
    """破坏者（值快照隔离）：修改投影结构不得影响帧栈对象/磁盘。"""
    frames_dir = str(tmp_path / "frames")
    save_frames(_stack(), frames_dir)
    stack = load_frames("sess-bridge", frames_dir)
    progress = build_sop_progress(stack)
    assert progress is not None

    progress["active"]["node_id"] = "TAMPERED"
    progress["scope_id"] = "TAMPERED"
    reloaded = load_frames("sess-bridge", frames_dir)
    assert reloaded is not None and reloaded.active is not None
    assert reloaded.active.node_id == "collect", "投影是值快照，改它不影响帧"
    assert reloaded.scope_id == "sess-bridge"


def test_projection_none_for_missing_or_empty() -> None:
    """边界：无帧（None）或空栈 → None（组织层不制造空投影）。"""
    runtime = SkillRuntime()
    assert build_sop_progress(None) is None
    assert build_sop_progress(runtime.new_stack("empty")) is None


def test_frame_scope_for_session_derivation(tmp_path: Path) -> None:
    """契约（scope 派生统一标准）：公司链路会话 id（含 '::'）经派生后必须合法可落盘
    稳定、且不暴露会话结构——写入端/loop 注入/组织投影三方共享同一函数。"""
    company_session = "role-session::p::s::ra::ea"
    scope = frame_scope_for_session(company_session)
    assert "::" not in scope, "派生结果不得含非法字符"
    # 合法可落盘（path_guard 通过即证明）
    save_frames(SkillRuntime().new_stack(scope), str(tmp_path / "frames"))
    assert load_frames(scope, str(tmp_path / "frames")) is not None
    # 稳定性 + 区分度 + 不暴露原文
    assert frame_scope_for_session(company_session) == scope
    assert frame_scope_for_session(company_session + "x") != scope
    assert "role-session" not in scope


@pytest.mark.asyncio
async def test_seat_executor_projects_sop_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约（桥接·接线）：员工会话存在 SOP 帧时，工作项执行后
    `runtime_metadata["sop_progress"]` 必须是该会话帧的只读投影。"""
    from bandi.company.company_runtime import CompanyRuntime
    from bandi.company.seat_executor import EngineSeatExecutor
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    frames_dir = tmp_path / "frames"
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
    save_frames(_stack(frame_scope_for_session(runtime.member_session_id())), str(frames_dir))

    async def fake_run_agent(**kwargs: Any) -> tuple[str, str, int, int]:
        return "ok output", "COMPLETED", 10, 1

    monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)
    executor = EngineSeatExecutor(frames_dir=str(frames_dir))
    item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    await executor.run_turn(runtime, item)

    progress = item.runtime_metadata.get("sop_progress")
    assert progress is not None, "有帧时组织层必须获得进度投影"
    assert progress["active"]["skill_id"] == "sk_bridge_case"


@pytest.mark.asyncio
async def test_seat_executor_no_frame_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """边界：无帧不制造空投影（组织层元数据不被无意义写入）。"""
    from bandi.company.company_runtime import CompanyRuntime
    from bandi.company.seat_executor import EngineSeatExecutor
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    async def fake_run_agent(**kwargs: Any) -> tuple[str, str, int, int]:
        return "ok output", "COMPLETED", 10, 1

    monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)
    executor = EngineSeatExecutor(frames_dir=str(tmp_path / "empty_frames"))
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
    item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    await executor.run_turn(runtime, item)

    assert "sop_progress" not in item.runtime_metadata


@pytest.mark.asyncio
async def test_scope_isolation_between_employees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """破坏者（防串污染）：不同员工会话的帧互不串——A 员工投影只含 A 的帧。"""
    from bandi.company.company_runtime import CompanyRuntime
    from bandi.company.seat_executor import EngineSeatExecutor
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    frames_dir = tmp_path / "frames"
    runtime_a = CompanyRuntime(project_id="p", session_scope_id="s", role_id="ra", employee_id="ea")
    runtime_b = CompanyRuntime(project_id="p", session_scope_id="s", role_id="rb", employee_id="eb")
    save_frames(_stack(frame_scope_for_session(runtime_a.member_session_id())), str(frames_dir))

    async def fake_run_agent(**kwargs: Any) -> tuple[str, str, int, int]:
        return "ok output", "COMPLETED", 10, 1

    monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)
    executor = EngineSeatExecutor(frames_dir=str(frames_dir))

    item_b = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    await executor.run_turn(runtime_b, item_b)
    assert "sop_progress" not in item_b.runtime_metadata, "B 员工无帧不得看 A 员工的帧"

    item_a = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    await executor.run_turn(runtime_a, item_a)
    assert item_a.runtime_metadata["sop_progress"]["scope_id"] == frame_scope_for_session(
        runtime_a.member_session_id()
    )
