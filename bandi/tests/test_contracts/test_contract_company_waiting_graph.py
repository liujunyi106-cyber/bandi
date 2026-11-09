"""契约族：通信等待图统一封装 + 消息幂等。

生产调用路径：
1. CommunicationManager.build_waiting_graph——从共享 store 组装等待图（heartbeat 消费）
2. CommunicationManager.send_dm(message_id)——显式 id 重复发送拒绝（幂等）
3. heartbeat 死锁检测——复用 build_waiting_graph（不再各自内存组装）

断言「最终产物」：等待图从卡 metadata 正确组装 / 同 message_id 二次发送返回
既有 id 不重复落盘。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.company.communication import CommunicationManager
from bandi.company.phase import Phase
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _mk_cm(tmp_path: Path) -> CommunicationManager:
    return CommunicationManager(
        workspace=str(tmp_path), project_id="p", session_id="s", org_engine=None
    )


def _mk_waiting_item(role: str, waiting_on: list[str]) -> Any:
    item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    item.phase = Phase.WAITING_FOR_PEER
    item.assignee_role = role
    item.metadata["peer_wait"] = {"waiting_on_agents": waiting_on}
    return item


def test_build_waiting_graph_from_items() -> None:
    """等待图从卡 metadata 的 peer_wait.waiting_on_agents 组装。"""
    cm = _mk_cm(Path("."))
    items = [
        _mk_waiting_item("role-a", ["role-b", "role-c"]),
        _mk_waiting_item("role-b", ["role-a"]),
        _mk_waiting_item("role-c", []),  # 无等待目标不建边
    ]
    graph = cm.build_waiting_graph(items)
    assert graph["role-a"] == {"role-b", "role-c"}
    assert graph["role-b"] == {"role-a"}
    assert "role-c" not in graph, "无等待目标不得出现在等待图"


def test_waiting_graph_deadlock_detection() -> None:
    """A 等 B 且 B 等 A 成环——detect_deadlocks 检出。"""
    cm = _mk_cm(Path("."))
    items = [
        _mk_waiting_item("role-a", ["role-b"]),
        _mk_waiting_item("role-b", ["role-a"]),
    ]
    graph = cm.build_waiting_graph(items)
    cycles = cm.detect_deadlocks(graph)
    # 环表示为 (起点, 终点) 对，A↔B 环返回 (A,A) 或 (B,B)
    assert any(c[0] == c[1] and c[0] in {"role-a", "role-b"} for c in cycles), "A↔B 互等必须成环"


def test_send_dm_idempotent_on_message_id(tmp_path: Path) -> None:
    """同 message_id 二次发送返回既有 id，不重复落盘。"""
    cm = _mk_cm(tmp_path)
    first = cm.send_dm("a", "b", "sub", "body", message_id="msg-1")
    assert first == "msg-1"
    second = cm.send_dm("a", "b", "sub", "body", message_id="msg-1")
    assert second == "msg-1", "同 id 必须幂等返回"
    # 收件箱只有一条
    from bandi.company.comms import list_inbox

    msgs = list_inbox(str(tmp_path), "p", "s", "b")
    assert len(msgs) == 1, "重复发送不得产生重复消息"


def test_send_dm_distinct_ids_send_twice(tmp_path: Path) -> None:
    """不同 message_id 正常发送两条。"""
    cm = _mk_cm(tmp_path)
    cm.send_dm("a", "b", "s1", "b1", message_id="m-1")
    cm.send_dm("a", "b", "s2", "b2", message_id="m-2")
    from bandi.company.comms import list_inbox

    msgs = list_inbox(str(tmp_path), "p", "s", "b")
    assert len(msgs) == 2


def test_send_dm_file_idempotent_across_restart(tmp_path: Path) -> None:
    """文件级幂等：进程重启（新 CommunicationManager 实例，_sent_messages 空）后
    同 message_id 重发不覆盖重写（收件箱仍只有一条）。"""
    cm1 = _mk_cm(tmp_path)
    cm1.send_dm("a", "b", "sub", "body", message_id="msg-x")
    # 模拟进程重启：新实例 _sent_messages 为空
    cm2 = _mk_cm(tmp_path)
    second = cm2.send_dm("a", "b", "sub", "body", message_id="msg-x")
    assert second == "msg-x", "跨进程同 id 必须幂等返回"
    from bandi.company.comms import list_inbox

    msgs = list_inbox(str(tmp_path), "p", "s", "b")
    assert len(msgs) == 1, "跨进程重发不得产生重复消息"


def test_tick_detects_deadlock_cycles(tmp_path: Path) -> None:
    """单次 tick 检出 WAITING_FOR_PEER 互等环（接入 tick，非仅 run_forever）。"""
    import asyncio

    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.org_engine import OrgEngine
    from bandi.company.store import WorkItemStore
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    store = WorkItemStore(str(tmp_path / "wi.db"))
    a = create_work_item(title="a", turn_type=WorkItemTurnType.EXECUTE, assignee_role="role-a")
    a.phase = Phase.WAITING_FOR_PEER
    a.metadata["peer_wait"] = {"waiting_on_agents": ["role-b"]}
    store.save(a)
    b = create_work_item(title="b", turn_type=WorkItemTurnType.EXECUTE, assignee_role="role-b")
    b.phase = Phase.WAITING_FOR_PEER
    b.metadata["peer_wait"] = {"waiting_on_agents": ["role-a"]}
    store.save(b)

    cm = _mk_cm(tmp_path)
    disp = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=OrgEngine(),
        communication=cm,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    report = asyncio.run(disp.tick())
    assert any("deadlock" in e for e in report.errors), (
        f"tick 必须检出死锁环（实际 errors={report.errors}）"
    )
