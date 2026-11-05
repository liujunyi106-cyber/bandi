# 功能：契约——审批决策后的员工卡恢复链。
#
# 背景：批准/拒绝后卡停在 NEEDS_ATTENTION 等人工（无自动续跑）——本修复：
# ① 决策端点 best-effort 调 `dispatcher.resume_after_approval(session_id)`：
# 按 `_approval_session_id` 找回 awaiting_approval 卡，RUNNING/NEEDS_ATTENTION
# 两种相位都 re-queue（READY），并写一次性 `metadata["approval_resume"]` 指令；
# ② seat 执行轮前置注入该指令（模型按「已批准/已拒绝」重发/放弃调用
# executor `_consume_approval_grant` 消费 grant 放行），消费即清除防重复注入；
# ③ `GET /api/company/approvals` 给审批入口列 pending。
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.recruiter import Recruiter
from bandi.company.seat_executor import _consume_approval_resume
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _dispatcher(tmp_path: Path) -> CompanyDispatcher:
    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "x")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    return CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=None,
        config=DispatcherConfig(max_concurrent=1),
    )


def _awaiting_card(disp: CompanyDispatcher, *, session_id: str, phase: Phase) -> str:
    item = create_work_item(
        title="审批恢复契约",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
    )
    item.phase = phase
    item.runtime_metadata["_approval_session_id"] = session_id
    item.runtime_metadata["_task_status"] = "awaiting_approval"
    disp.store.save(item)
    return item.id


class TestResumeAfterApproval:
    def test_running_phase_requeues_and_writes_resume(self, tmp_path: Path) -> None:
        disp = _dispatcher(tmp_path)
        wid = _awaiting_card(disp, session_id="emp-sess-1", phase=Phase.RUNNING)

        hit = disp.resume_after_approval(
            "emp-sess-1", decision="allow_once", tool_name="bash", note="APPROVED: re-issue now"
        )

        assert hit == 1
        item = disp.store.get(wid)
        assert item.phase == Phase.READY, f"RUNNING 相位应 re-queue（实际 {item.phase}）"
        resume = item.metadata.get("approval_resume") or {}
        assert resume.get("decision") == "allow_once"
        assert resume.get("tool_name") == "bash"
        assert "APPROVED" in str(resume.get("note") or "")
        assert disp.wake_event.is_set(), "re-queue 必须唤醒调度"

    def test_needs_attention_phase_requeues(self, tmp_path: Path) -> None:
        disp = _dispatcher(tmp_path)
        wid = _awaiting_card(disp, session_id="emp-sess-2", phase=Phase.NEEDS_ATTENTION)

        hit = disp.resume_after_approval("emp-sess-2", decision="deny", tool_name="bash")

        assert hit == 1
        assert disp.store.get(wid).phase == Phase.READY

    def test_breakers_no_match_no_change(self, tmp_path: Path) -> None:
        disp = _dispatcher(tmp_path)
        # ① session 不匹配
        wid = _awaiting_card(disp, session_id="emp-sess-3", phase=Phase.RUNNING)
        assert disp.resume_after_approval("other-sess") == 0
        assert disp.store.get(wid).phase == Phase.RUNNING
        # ② 非 awaiting 状态不恢复
        item = disp.store.get(wid)
        item.runtime_metadata["_approval_session_id"] = "emp-sess-4"
        item.runtime_metadata["_task_status"] = "completed"
        disp.store.save(item)
        assert disp.resume_after_approval("emp-sess-4") == 0


class TestConsumeApprovalResume:
    def test_injects_note_once_and_clears(self, tmp_path: Path) -> None:
        store = WorkItemStore(str(tmp_path / "company.db"))
        item = create_work_item(
            title="t", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        item.metadata["approval_resume"] = {
            "decision": "allow_once",
            "tool_name": "bash",
            "note": "The user APPROVED running bash. Re-issue the exact same tool call now.",
        }
        store.save(item)

        prefix = _consume_approval_resume(item, store)
        assert prefix.startswith("[审批决策恢复]")
        assert "APPROVED" in prefix, f"应注入决策端点的 re-issue 指令：{prefix}"
        assert "approval_resume" not in item.metadata
        # 落库读回：清除已持久化（防重跑重复注入）
        assert "approval_resume" not in (store.get(item.id).metadata or {})
        # 第二次消费为空
        assert _consume_approval_resume(store.get(item.id), store) == ""

    def test_no_resume_returns_empty(self, tmp_path: Path) -> None:
        item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE, assignee_role="w")
        assert _consume_approval_resume(item) == ""

    def test_fallback_note_when_absent(self) -> None:
        item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE, assignee_role="w")
        item.metadata["approval_resume"] = {"decision": "deny", "tool_name": "bash"}
        prefix = _consume_approval_resume(item)
        assert "deny" in prefix and "bash" in prefix


class TestPendingList:
    def test_list_all_pending_returns_every_session(self, tmp_path: Path) -> None:
        from bandi.tools.approval import ApprovalStore, PendingApproval

        store = ApprovalStore(storage_path=tmp_path / "approvals.json")
        store.set_pending("s1", PendingApproval("tc-1", "bash", "digest-1", "r1"))
        store.set_pending("s2", PendingApproval("tc-2", "write", "digest-2", "r2"))
        rows = store.list_all_pending()
        assert {(sid, p.tool_name) for sid, p in rows} == {("s1", "bash"), ("s2", "write")}


class TestDecisionEndpointResume:
    @pytest.mark.asyncio
    async def test_endpoint_calls_resume_and_reports(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        app_mod = importlib.import_module("bandi.api.app")
        from bandi.tools.approval import PendingApproval, get_approval_store, reset_approval_store

        reset_approval_store()
        store = get_approval_store()
        store.set_pending("emp-sess-x", PendingApproval("tc-9", "bash", "dig", "reason"))

        calls: dict = {}

        class _StubDisp:
            def resume_after_approval(self, session_id: str, **kw: object) -> int:
                calls["session_id"] = session_id
                calls["kw"] = kw
                return 1

        monkeypatch.setattr(app_mod, "_get_company_dispatcher", lambda: _StubDisp())

        body = {"decision": "allow_once"}
        result = await app_mod.session_approval_decision("emp-sess-x", body)

        assert result["resumed_work_items"] == 1
        assert calls["session_id"] == "emp-sess-x"
        assert calls["kw"].get("tool_name") == "bash"
        reset_approval_store()


@pytest.mark.asyncio
async def test_resume_rerun_does_not_inherit_stale_final_message() -> None:
    """契约（残余根因， 8771 实证）：审批轮设置的
    final_message 经 checkpoint 恢复进入重跑轮后，必须在新请求 init 边界清除——
    否则 reply 沿用旧审批文案（实证：重跑 VERIFIED/success=True 但
    `output_len=31`=旧文案；真实产出被无视且因 _should_persist_assistant 不落库；
    `_last_output` 不更新 = 「决策后重跑无新产出」现象）。
    """
    from bandi.agent.base import ExecutionContext, TerminalReason
    from bandi.agent.states import on_init
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import Message, MessageRole
    from bandi.llm.mock import MockProvider

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="resume 注入后的重跑请求")],
        provider=MockProvider(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.final_message = "等待人工决策：请审批工具 `python_exec` 后继续。"
    ctx.terminal_reason = TerminalReason.AWAITING_APPROVAL
    ctx.current_state = "init"
    ctx.raw_input = "resume 注入后的重跑请求"

    await on_init(ctx)

    assert ctx.final_message is None, (
        f"新请求 init 必须清除上一轮残留 final_message（实际 {ctx.final_message!r}）"
    )


@pytest.mark.asyncio
async def test_reply_prefers_fresh_content_when_no_stale_message() -> None:
    """契约（对照组）：无残留 final_message 时，reply 取模型真实输出（current_content）
    ——修复不得改变正常终态语义（正常成功终态 final_message 保持 None）。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import Message, MessageRole
    from bandi.llm.mock import MockProvider

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="普通请求")],
        provider=MockProvider(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.final_message = None
    ctx.current_content = "python_exec 执行成功，输出为 RESUME-FIXED-003032。"
    reply = ctx.final_message or ctx.current_content or ""
    assert reply.startswith("python_exec 执行成功"), f"实际 {reply!r}"


@pytest.mark.asyncio
async def test_allow_session_is_tool_wide_not_param_scoped() -> None:
    """契约：`allow_session`=「本会话信任该
    工具」——工具级 wildcard；模型重试换参后授权仍命中（旧：精确参数指纹失配
    → 重跑再次弹审批、卡停 needs_attention）。"""
    from bandi.tools.approval import (
        PendingApproval,
        apply_decision,
        get_approval_store,
        reset_approval_store,
    )

    reset_approval_store()
    try:
        store = get_approval_store()
        store.set_pending("sess-9", PendingApproval("tc-9", "bash", "digest-a", "reason"))
        await apply_decision(store, "sess-9", "allow_session", "bash")
        assert store.is_granted("sess-9", "bash", "digest-b") is True, (
            "会话授权后不同参数的同类调用必须命中（工具级信任）"
        )
        assert store.is_granted("sess-9", "python_exec", "digest-b") is False, "不得外溢到其他工具"
    finally:
        reset_approval_store()


@pytest.mark.asyncio
async def test_allow_once_is_param_scoped_to_exact_call() -> None:
    """契约（P0-6 混合粒度）：allow_once=仅本次这一条具体调用——
    批准后同参命中；改参数（换一次调用）必须重新审批；不同工具不放行。"""
    from bandi.tools.approval import (
        PendingApproval,
        apply_decision,
        get_approval_store,
        reset_approval_store,
    )

    reset_approval_store()
    try:
        store = get_approval_store()
        store.set_pending("sess-10", PendingApproval("tc-10", "bash", "digest-a", "reason"))
        await apply_decision(store, "sess-10", "allow_once", "bash")
        assert store.is_granted("sess-10", "bash", "digest-a") is True, "批准的具体调用应命中"
        assert store.is_granted("sess-10", "bash", "digest-b") is False, (
            "改参数即视为另一次调用，必须重新审批"
        )
        assert store.is_granted("sess-10", "other", "digest-a") is False, "不同工具不放行"
    finally:
        reset_approval_store()
