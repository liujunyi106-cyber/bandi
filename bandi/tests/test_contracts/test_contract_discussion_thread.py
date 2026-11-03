"""功能：DiscussionThread 线程容器契约 —— 创建/追加/按 agent 过滤/持久化 hydrate。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bandi.company.discussion_thread import ThreadManager

_WORKSPACE = "."
_PROJECT = "company"
_SESSION = "test_session"


def _manager(tmp_path: Path) -> ThreadManager:
    return ThreadManager(workspace=str(tmp_path), project_id=_PROJECT, session_id=_SESSION)


def test_create_thread_records_participants_and_id(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    thread = mgr.create_thread(parent_task_id="t_1", participants=["product", "tech"])
    assert thread.thread_id.startswith("thread_")
    assert thread.parent_task_id == "t_1"
    assert thread.participants == ["product", "tech"]
    assert mgr.get_thread(thread.thread_id) is thread


def test_add_message_unknown_thread_fails_closed(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    assert mgr.add_message("thread_missing", "a", "hi") is None


def test_add_message_appends_and_tracks_agent_threads(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    thread = mgr.create_thread(participants=["product", "tech"])
    mgr.add_message(thread.thread_id, "product", "请出技术方案", mentions=["tech"])
    mgr.add_message(thread.thread_id, "tech", "方案如下", message_id="w_2")
    assert len(thread.messages) == 2
    assert thread.messages[1].sender == "tech"
    assert thread.messages[1].message_id == "w_2"
    assert [t.thread_id for t in mgr.get_threads_for_agent("product")] == [thread.thread_id]
    assert mgr.get_threads_for_agent("qa") == []


def test_persist_and_hydrate_roundtrip(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    thread = mgr.create_thread(parent_task_id="t_9", participants=["product", "tech"])
    mgr.add_message(thread.thread_id, "user", "做个竞品报告", message_id="w_1")
    mgr.add_message(
        thread.thread_id, "product", "@tech 出方案", mentions=["tech"], message_id="w_2"
    )

    restored = ThreadManager(workspace=str(tmp_path), project_id=_PROJECT, session_id=_SESSION)
    n = restored.hydrate()
    assert n == 1
    rt = restored.get_thread(thread.thread_id)
    assert rt is not None
    assert rt.parent_task_id == "t_9"
    assert rt.participants == ["product", "tech"]
    assert len(rt.messages) == 2
    assert rt.messages[1].sender == "product"
    assert rt.messages[1].content == "@tech 出方案"
    assert rt.messages[1].mentions == ["tech"]
    assert rt.messages[1].message_id == "w_2"


def test_persist_path_traversal_thread_id_rejected(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    with pytest.raises(ValueError):
        mgr.add_message("../../etc/passwd", "a", "hi")


def test_message_roundtrip_chinese_long_and_empty(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    thread = mgr.create_thread()
    mgr.add_message(thread.thread_id, "user", "测试中文长内容" * 200, message_id="")
    restored = ThreadManager(workspace=str(tmp_path), project_id=_PROJECT, session_id=_SESSION)
    restored.hydrate()
    rt = restored.get_thread(thread.thread_id)
    assert rt is not None and len(rt.messages) == 1
    assert rt.messages[0].content == "测试中文长内容" * 200


def test_run_work_item_writes_output_to_bound_thread(tmp_path: Path) -> None:
    """dispatcher.threads 挂载后：绑定了 thread_id 的卡执行完毕，产出必须写回线程。"""
    import asyncio

    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.gate_harness import GateHarness
    from bandi.company.org_engine import OrgEngine
    from bandi.company.recruiter import Recruiter
    from bandi.company.seat_executor import SeatResult
    from bandi.company.store import WorkItemStore
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    store = WorkItemStore(str(tmp_path / "dispatch.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")

    class _StubSeatExecutor:
        async def prepare_seat(self, runtime, work_item) -> None:
            pass

        async def run_turn(self, runtime, work_item) -> SeatResult:
            return SeatResult(success=True, output="产出报告已完成")

        async def checkpoint(self, runtime) -> dict:
            return {}

        async def interrupt(self, runtime) -> None:
            pass

        async def shutdown(self) -> None:
            pass

    mgr = ThreadManager(workspace=str(tmp_path), project_id=_PROJECT, session_id=_SESSION)
    thread = mgr.create_thread(parent_task_id="t_root", participants=["user", "worker"])

    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=_StubSeatExecutor(),
        config=DispatcherConfig(max_concurrent=3),
        thread_manager=mgr,
    )
    dispatcher.task_preparer = None

    item = create_work_item(
        title="产出报告",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
    )
    item.metadata["thread_id"] = thread.thread_id
    store.save(item)

    asyncio.run(dispatcher.run_work_item(item.id))

    saved = store.get(item.id)
    assert saved is not None
    assert saved.runtime_metadata.get("_task_status") == "completed"
    msgs = thread.messages
    assert len(msgs) == 1, "产出必须写回线程"
    assert msgs[0].sender == "worker"
    assert msgs[0].content == "产出报告已完成"
    assert msgs[0].message_id == item.id


def test_on_message_callback_fires_for_thread_events(tmp_path: Path) -> None:
    """ThreadManager on_message 回调：create_thread → thread_created，add_message →
    （契约：回调必须携带线程/消息数据派生字段，供生产者直接广播；回调失败不阻断线程写。）"""
    events: list[dict] = []

    def on_event(event: dict) -> None:
        events.append(event)

    mgr = ThreadManager(
        workspace=str(tmp_path),
        project_id=_PROJECT,
        session_id=_SESSION,
        on_message=on_event,
    )
    thread = mgr.create_thread(participants=["product", "tech"])
    msg = mgr.add_message(thread.thread_id, "product", "@tech 出方案", mentions=["tech"])
    assert len(events) == 2, f"create+add 各触发一次事件，实际 {events}"
    created_evt = events[0]
    assert created_evt["type"] == "thread_created"
    assert created_evt["thread_id"] == thread.thread_id
    assert created_evt["participants"] == ["product", "tech"]
    msg_evt = events[1]
    assert msg_evt["type"] == "thread_message"
    assert msg_evt["thread_id"] == thread.thread_id
    assert msg_evt["sender"] == "product"
    assert msg_evt["content"] == "@tech 出方案"
    assert msg_evt["mentions"] == ["tech"]
    assert msg_evt["message_id"] == msg.message_id
    assert msg_evt.get("participants") == ["product", "tech"], (
        "线程消息帧必须携带参与方——前端单聊视图按参与方集合精确过滤，防其他线程消息串入"
    )


def test_on_message_callback_does_not_block_unknown_thread(tmp_path: Path) -> None:
    """回调对失败路径（未知线程）不触发、不抛异常；对无效线程不破坏既有行为。"""
    events: list[str] = []

    def on_event(event: dict) -> None:
        events.append(event["type"])

    mgr = ThreadManager(
        workspace=str(tmp_path),
        project_id=_PROJECT,
        session_id=_SESSION,
        on_message=on_event,
    )
    assert mgr.add_message("thread_missing", "a", "hi") is None
    assert events == []


def test_run_work_item_thread_event_carries_output(tmp_path: Path) -> None:
    """run_work_item 产出回写线程必须触发 thread_message 事件（含最终产物，供广播）。
    （契约：报文最终产物 = 执行完成后的真实产出，非占位/调试文本。）"""
    import asyncio

    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.gate_harness import GateHarness
    from bandi.company.org_engine import OrgEngine
    from bandi.company.recruiter import Recruiter
    from bandi.company.seat_executor import SeatResult
    from bandi.company.store import WorkItemStore
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    store = WorkItemStore(str(tmp_path / "dispatch2.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")

    class _StubSeatExecutor:
        async def prepare_seat(self, runtime, work_item) -> None:
            pass

        async def run_turn(self, runtime, work_item) -> SeatResult:
            return SeatResult(success=True, output="真实产出：已完成竞品分析")

        async def checkpoint(self, runtime) -> dict:
            return {}

        async def interrupt(self, runtime) -> None:
            pass

        async def shutdown(self) -> None:
            pass

    events: list[dict] = []

    def on_event(event: dict) -> None:
        events.append(event)

    mgr = ThreadManager(
        workspace=str(tmp_path),
        project_id=_PROJECT,
        session_id=_SESSION,
        on_message=on_event,
    )
    thread = mgr.create_thread(participants=["user", "worker"])

    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=_StubSeatExecutor(),
        config=DispatcherConfig(max_concurrent=3),
        thread_manager=mgr,
    )
    dispatcher.task_preparer = None

    item = create_work_item(
        title="竞品分析",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
    )
    item.metadata["thread_id"] = thread.thread_id
    store.save(item)

    asyncio.run(dispatcher.run_work_item(item.id))

    msg_events = [e for e in events if e["type"] == "thread_message"]
    assert len(msg_events) >= 1, "产出回写必须触发 thread_message 事件"
    last = msg_events[-1]
    assert last["content"] == "真实产出：已完成竞品分析"
    assert last["sender"] == "worker"
    assert last["thread_id"] == thread.thread_id
    assert last.get("participants") == ["user", "worker"], (
        "产出回写帧同样携带参与方（单聊实时流入的过滤依据）"
    )
