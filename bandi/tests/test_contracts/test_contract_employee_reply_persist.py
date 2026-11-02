# 功能：契约——员工产出回写**会话历史**（缺口：员工回复只写线程 ⇒ 刷新/切走/新开会话即消失）。
#
# ① 前端只在 live `thread_message` 帧里把员工回复塞进**内存**（ChatView.appendEmployeeReply）；
# ② 后端产出只回写**线程**（company_mode.py `manager.add_message(..., message_id=item.id)`）；
# ③ 会话历史（session_entries）里没有员工回复 ⇒ 刷新即丢。
# 修法（写时归属）：派单时记「发起会话 id」→ 跑完双写该会话历史；幂等键与线程回写同源（work item id）。
#
# 钉行为：
# ① 派单把「发起会话 id」写进卡（否则回写无处可归）；
# ② 跑完带 origin_session_id 的卡 → 该会话历史出现员工回复（读回断言最终产物）；
# ③ 幂等：同卡重放不重复插入；
# ④ 破坏者：会话不存在 / 秘书会话 / 空内容 → 静默跳过且不抛（不 FK 崩、不污染秘书会话）；
# ⑤ 无 origin_session_id 的卡（群聊广播/CLI 派单）→ 行为不变（不写会话历史）。
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from bandi.api.company_chat import handle_chat_message
from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.recruiter import Recruiter
from bandi.company.seat_executor import SeatResult
from bandi.company.session_writeback import write_employee_reply_to_session
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.memory.session.store import SessionStore


class _StubSeatExecutor:
    """功能：固定产出的座位执行器（不调 LLM——断言的是回写链路而非模型）。"""

    async def prepare_seat(self, runtime: Any, work_item: Any) -> None:
        pass

    async def run_turn(self, runtime: Any, work_item: Any) -> SeatResult:
        return SeatResult(success=True, output="主图已出：main-v2.png")

    async def checkpoint(self, runtime: Any) -> dict[str, Any]:
        return {}

    async def interrupt(self, runtime: Any) -> None:
        pass

    async def shutdown(self) -> None:
        pass


def _session_store(tmp_path: Path) -> SessionStore:
    return SessionStore(str(tmp_path / "bandi.db"))


def _dispatcher(tmp_path: Path, session_store: SessionStore) -> CompanyDispatcher:
    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    disp = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=_StubSeatExecutor(),
        config=DispatcherConfig(max_concurrent=2),
    )
    # 生产装配同口径：API 装配点把 session_writer 指向写回函数（app.py 装配）
    disp.session_writer = (
        lambda session_id, sender_role, sender_name, content, work_item_id, process_events=None: (
            write_employee_reply_to_session(
                session_store,
                session_id,
                sender_role=sender_role,
                sender_name=sender_name,
                content=content,
                work_item_id=work_item_id,
            )
        )
    )
    return disp


def _run_card(disp: CompanyDispatcher, metadata: dict[str, Any]) -> str:
    item = create_work_item(
        title="回写契约任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        metadata=metadata,
    )
    disp.store.save(item)
    assert disp.claim("worker")
    result = asyncio.run(disp.run_work_item(item.id))
    assert result.success, result.output
    return item.id


def _entries(session_store: SessionStore, session_id: str) -> list[dict[str, Any]]:
    return [e for e in session_store.get_branch(session_id) if e.get("entry_type") == "message"]


class TestDispatchRecordsOriginSession:
    """① 派单记「发起会话 id」——否则回写无处可归。"""

    def test_mention_dispatch_carries_origin_session(self) -> None:
        org = OrgEngine()
        org.register_role("worker", "Worker", "Executes tasks")
        created = handle_chat_message(
            content="@worker 做张主图",
            sender_role="user",
            communication=None,
            store=WorkItemStore(":memory:"),
            org_engine=org,
            send_dm=False,
            session_id="sess-origin-1",
        )
        assert created, "命中角色应建卡"
        assert created[0].metadata.get("origin_session_id") == "sess-origin-1"

    def test_dispatch_without_session_leaves_key_absent(self) -> None:
        """⑤ 群聊广播/CLI 派单没有会话 → 不写该键（行为不变）。"""
        org = OrgEngine()
        org.register_role("worker", "Worker", "Executes tasks")
        created = handle_chat_message(
            content="@worker 做张主图",
            sender_role="user",
            communication=None,
            store=WorkItemStore(":memory:"),
            org_engine=org,
            send_dm=False,
        )
        assert created and "origin_session_id" not in created[0].metadata


class TestReplyLandsInSessionHistory:
    """② 生产链路：跑完卡 → 会话历史读回员工回复。"""

    def test_reply_appears_in_history(self, tmp_path: Path) -> None:
        sessions = _session_store(tmp_path)
        sessions.create_session("sess-a", title="团队群聊会话")
        disp = _dispatcher(tmp_path, sessions)

        _run_card(disp, {"origin_session_id": "sess-a"})

        entries = _entries(sessions, "sess-a")
        assert len(entries) == 1, f"员工回复必须落会话历史，实际 {len(entries)} 条"
        assert entries[0]["content"] == "主图已出：main-v2.png"
        assert entries[0]["role"] == "assistant", "以 assistant 条目承载（前端消息流按此渲染）"
        meta = json.loads(entries[0].get("metadata") or "{}")
        assert meta.get("employee_reply") is True, "必须带员工回复标记（前端据此渲染头像/名字）"
        assert meta.get("sender_role") == "worker"
        assert meta.get("sender_name") == "Worker", "显示名随条目快照（侧栏/分享页等界面免解析）"

    def test_card_without_origin_session_leaves_history_empty(self, tmp_path: Path) -> None:
        sessions = _session_store(tmp_path)
        sessions.create_session("sess-b", title="团队群聊会话")
        disp = _dispatcher(tmp_path, sessions)

        _run_card(disp, {})

        assert _entries(sessions, "sess-b") == [], "无归属会话的卡不得写别人的历史"


class TestIdempotencyAndBreakers:
    """③ 幂等 + ④ 破坏者。"""

    def test_replay_same_work_item_is_idempotent(self, tmp_path: Path) -> None:
        sessions = _session_store(tmp_path)
        sessions.create_session("sess-c")
        store = sessions
        first = store.append_employee_reply(
            "sess-c", content="回复", sender_role="worker", work_item_id="wi-1"
        )
        second = store.append_employee_reply(
            "sess-c", content="回复", sender_role="worker", work_item_id="wi-1"
        )
        assert first, "首次应写入并返回条目 id"
        assert second == first, "同卡重放落回同一条目（幂等：恰一条、位置稳定）"
        assert len(_entries(store, "sess-c")) == 1

    def test_unknown_session_is_skipped_without_crash(self, tmp_path: Path) -> None:
        sessions = _session_store(tmp_path)
        assert (
            write_employee_reply_to_session(
                sessions,
                "sess-missing",
                sender_role="worker",
                content="回复",
                work_item_id="wi-2",
            )
            is None
        )

    def test_secretary_session_is_skipped(self, tmp_path: Path) -> None:
        """项目内秘书会话不注入员工回复（与前端 live 口径一致：ChatView 对 origin=secretary 早退）。"""
        sessions = _session_store(tmp_path)
        sessions.create_session("sess-sec", origin="secretary")
        assert (
            write_employee_reply_to_session(
                sessions,
                "sess-sec",
                sender_role="worker",
                content="回复",
                work_item_id="wi-3",
            )
            is None
        )
        assert _entries(sessions, "sess-sec") == []

    def test_blank_content_is_skipped(self, tmp_path: Path) -> None:
        sessions = _session_store(tmp_path)
        sessions.create_session("sess-d")
        assert (
            write_employee_reply_to_session(
                sessions, "sess-d", sender_role="worker", content="   ", work_item_id="wi-4"
            )
            is None
        )
        assert _entries(sessions, "sess-d") == []
