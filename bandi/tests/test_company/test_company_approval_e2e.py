# 功能：链路 E2E——员工执行撞审批门（headless）时端到端行为：
# ① 审批通道挂起 + 轮次以 AWAITING_APPROVAL 终止（产出为人读文案，不含问题卡 JSON）；
# ② 产出回写（线程 / 会话历史 / _last_output）零 JSON——人读文案；
# ③ 员工卡状态第三态 awaiting_approval；gate 转 NEEDS_ATTENTION 等人工（不 REWORK 空转）。
#
# 装配真实度：唯一 mock = LLM Provider（脚本化 MockProvider，标准测试替身）；
# 其余全真实链路：EngineSeatExecutor（真实 AgentLoop / 工具执行 / 权限管道 / 审批门）
# WorkItemStore、SessionStore、session_writer 生产函数（write_employee_reply_to_session）。
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.recruiter import Recruiter
from bandi.company.seat_executor import EngineSeatExecutor
from bandi.company.session_writeback import write_employee_reply_to_session
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.core.message import LLMResponse, ToolCall
from bandi.llm.mock import MockProvider
from bandi.memory.session.store import SessionStore
from bandi.tools.approval import reset_approval_store

AWAITING_COPY_PREFIX = "等待人工决策"


def _contains_approval_payload(text: str | None) -> bool:
    blob = str(text or "")
    return (
        "APPROVAL_REQUIRED" in blob
        or "approval-question" in blob
        or '"choices"' in blob
        or "Allow the agent to run" in blob
    )


def _build_dispatcher(
    tmp_path: Path, session_store: SessionStore
) -> tuple[CompanyDispatcher, WorkItemStore]:
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
        seat_executor=EngineSeatExecutor(),
        config=DispatcherConfig(max_concurrent=1),
    )
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
    return disp, store


def test_employee_approval_end_to_end(tmp_path: Path) -> None:
    reset_approval_store()
    sessions = SessionStore(str(tmp_path / "bandi.db"))
    sessions.create_session("sess-appr", title="团队群聊会话")
    disp, store = _build_dispatcher(tmp_path, sessions)

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="tc-appr-1", name="bash", arguments={"command": "echo approval-e2e"})
            ],
            finish_reason="tool_calls",
        )
    )

    item = create_work_item(
        title="审批链端到端验证",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        metadata={"origin_session_id": "sess-appr"},
    )
    store.save(item)

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("bandi.core.agent_runner.build_provider", return_value=provider),
    ):
        assert disp.claim("worker")
        asyncio.run(disp.run_work_item(item.id))

    saved = store.get(item.id)
    # ① 轮次以审批暂停终止（文案口径）——不经 mock，真实 AgentLoop + 审批门
    assert saved is not None
    last_output = str(saved.runtime_metadata.get("_last_output") or "")
    assert last_output.startswith(AWAITING_COPY_PREFIX), (
        f"真实链路产出应为审批文案（实际 {last_output[:80]!r}）"
    )
    assert "bash" in last_output
    assert not _contains_approval_payload(last_output), "_last_output 不得含问题卡 JSON"

    # ② 会话历史（生产回写函数）出现员工文案——并在读回层零 JSON
    entries = [
        entry
        for entry in sessions.get_branch("sess-appr")
        if entry.get("entry_type") == "message" and entry.get("role") == "assistant"
    ]
    assert len(entries) == 1, f"审批等待文案应回写会话历史 1 条（实际 {len(entries)}）"
    assert entries[0]["content"].startswith(AWAITING_COPY_PREFIX)
    assert not _contains_approval_payload(entries[0]["content"])

    # ③ 卡状态第三态（不落 failed）
    assert saved.runtime_metadata.get("_task_status") == "awaiting_approval", (
        f"审批等待应为 awaiting_approval（实际 {saved.runtime_metadata.get('_task_status')}）"
    )

    with patch("pathlib.Path.home", return_value=tmp_path):
        asyncio.run(disp.tick())
    final = store.get(item.id)
    assert final.phase == Phase.NEEDS_ATTENTION, (
        f"审批等待卡应转 NEEDS_ATTENTION（实际 {final.phase.value}）"
    )
    assert "approval" in str(final.runtime_metadata.get("last_transition_reason", "")).lower()

    reset_approval_store()
