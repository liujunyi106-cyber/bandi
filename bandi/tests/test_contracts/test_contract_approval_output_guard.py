# 功能：契约——审批等待不得污染员工产出与卡状态（三件套修复）。
#
# 员工执行撞审批门（headless）→ 工具返回 APPROVAL_REQUIRED:{问题卡 JSON} →
# ① states 终态把 JSON 裸传 final_message（借道产出通道）；
# ② 公司回写链（线程 add_message / 会话 session_writer / 卡 _last_output）
# 无形态护栏（普通聊天的 _should_persist_assistant 只管 chat 链路）；
# ③ success=False → _task_status=failed（审批等待被误标失败）。
# 附：该 JSON 前后端均无解析方（审批卡走独立 ApprovalStore/交互帧通道）⇒ 无意义遗留。
#
# 钉行为：
# ① 终态文案化：AWAITING_APPROVAL 的 final_message 为人类可读文案
# 不含问题卡 JSON/字段（状态语义仍 AWAITING_APPROVAL）；
# ② 回写形态护栏：APPROVAL_REQUIRED 前缀或 AWAITING_APPROVAL 下的 `{` 形态
# 不得进线程 / 会话历史 / _last_output（破坏者：直插 JSON 断言零污染）；
# 合法业务 JSON 产出不受影响（不得一刀切封 `{`）；
# ③ 卡状态第三态：awaiting_approval（不落 failed）；gate 对审批等待卡
# 转 NEEDS_ATTENTION 等人工——不进 REWORK 空转循环、不误 PASS 推进。
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.gate_harness import GateHarness
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.recruiter import Recruiter
from bandi.company.seat_executor import SeatResult
from bandi.company.session_writeback import write_employee_reply_to_session
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.memory.session.store import SessionStore

# 生产形态的审批问题卡 JSON（build_approval_question 全字段）。
APPROVAL_QUESTION_JSON = json.dumps(
    {
        "id": "approval-question:call_1",
        "prompt": "Allow the agent to run `bash`?",
        "choices": ["Allow once", "Allow for session", "Deny"],
        "allow_custom": False,
        "multi_select": False,
        "ui_variant": "permission",
        "permission_kind": "tool_run",
        "detail": "requires user approval",
        "primary_choice": "Allow once",
        "secondary_choice": "Deny",
    },
    ensure_ascii=False,
)

APPROVAL_REQUIRED_OUTPUT = f"APPROVAL_REQUIRED:{APPROVAL_QUESTION_JSON}"


class _StubSeatExecutor:
    """功能：返回固定 SeatResult 的座位执行器（断言回写链路而非模型）。"""

    def __init__(self, result: SeatResult) -> None:
        self._result = result

    async def prepare_seat(self, runtime: Any, work_item: Any) -> None:
        pass

    async def run_turn(self, runtime: Any, work_item: Any) -> SeatResult:
        return self._result

    async def checkpoint(self, runtime: Any) -> dict[str, Any]:
        return {}

    async def interrupt(self, runtime: Any) -> None:
        pass

    async def shutdown(self) -> None:
        pass


class _SpyThreads:
    """功能：线程回写观察器（形态对齐 ThreadManager.add_message 关键字签名）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def add_message(
        self,
        thread_id: str,
        sender: str,
        content: str,
        mentions: list[str] | None = None,
        message_id: str | None = None,
        work_item_id: str | None = None,
        handoff: Any | None = None,
        process_events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls.append(
            {
                "thread_id": thread_id,
                "sender": sender,
                "content": content,
                "message_id": message_id,
            }
        )


def _dispatcher(
    tmp_path: Path, session_store: SessionStore, result: SeatResult, *, thread_id: str = ""
) -> tuple[CompanyDispatcher, _SpyThreads]:
    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    threads = _SpyThreads()
    disp = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=_StubSeatExecutor(result),
        config=DispatcherConfig(max_concurrent=2),
        thread_manager=threads,
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
    return disp, threads


def _run_card(
    disp: CompanyDispatcher, metadata: dict[str, Any], *, expect_success: bool | None = None
) -> tuple[Any, SeatResult]:
    item = create_work_item(
        title="审批污染契约任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        metadata=metadata,
    )
    disp.store.save(item)
    assert disp.claim("worker")
    result = asyncio.run(disp.run_work_item(item.id))
    if expect_success is not None:
        assert result.success is expect_success, result.output
    return disp.store.get(item.id), result


def _employee_entries(session_store: SessionStore, session_id: str) -> list[dict[str, Any]]:
    return [
        entry
        for entry in session_store.get_branch(session_id)
        if entry.get("entry_type") == "message" and entry.get("role") == "assistant"
    ]


def _contains_approval_payload(text: str | None) -> bool:
    blob = str(text or "")
    return (
        "APPROVAL_REQUIRED" in blob
        or "approval-question" in blob
        or '"choices"' in blob
        or "Allow the agent to run" in blob
    )


class TestTerminalMessageCopy:
    """① 终态文案化：状态仍 AWAITING_APPROVAL，但 final_message 不再借道问题卡 JSON。"""

    def test_final_message_is_readable_copy_with_tool_name(self) -> None:
        from types import SimpleNamespace

        from bandi.agent import states as states_mod
        from bandi.agent.base import TerminalReason
        from bandi.core.message import Message, MessageRole, ToolCall, ToolResult

        class _Executor:
            async def execute(self, calls: list) -> list:
                return [
                    ToolResult(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        success=False,
                        output="",
                        error=APPROVAL_REQUIRED_OUTPUT,
                    )
                    for tc in calls
                ]

        ctx = SimpleNamespace(
            terminal_reason=None,
            turn_index=1,
            current_tool_calls=[ToolCall(id="c1", name="bash", arguments={})],
            tool_executor=_Executor(),
            meta={"tracer": None, "task_ledger": None},
            session_id="s",
            messages=[Message(role=MessageRole.USER, content="q")],
            tool_results=None,
            cot_trace=[],
            loop_detection_window=5,
            tool_sequence_hashes=[],
        )

        asyncio.run(states_mod.on_executing(ctx))

        assert ctx.terminal_reason == TerminalReason.AWAITING_APPROVAL, (
            f"必须以审批暂停终止（实际 {ctx.terminal_reason}）"
        )
        message = str(ctx.final_message or "")
        assert message.strip(), "终态必须有可读文案（不得为空）"
        assert "bash" in message, "文案应含待审批工具名（用户可读性）"
        assert not _contains_approval_payload(message), (
            f"终态文案不得携带问题卡 JSON/字段（实际 {message!r}）"
        )
        assert not message.lstrip().startswith("{"), "终态文案不得以 JSON 对象开头"


class TestDeliverableGuard:
    """② 回写形态护栏纯函数——只挡审批协议形态，不误伤合法产出。"""

    def _guard(self, output: str, terminal_reason: str = "") -> bool:
        from bandi.company.company_mode import _is_deliverable_output

        return _is_deliverable_output(
            SeatResult(success=False, output=output, metadata={"terminal_reason": terminal_reason})
        )

    def test_approval_protocol_prefix_is_never_deliverable(self) -> None:
        assert self._guard(APPROVAL_REQUIRED_OUTPUT, "AWAITING_APPROVAL") is False
        # 前缀在任何终态都拦住（异常路径兜底）
        assert self._guard(APPROVAL_REQUIRED_OUTPUT, "COMPLETED") is False

    def test_approval_json_without_prefix_is_blocked(self) -> None:
        """破坏者：去掉前缀的裸问题卡 JSON 在审批终态同样拦住。"""
        assert self._guard(APPROVAL_QUESTION_JSON, "AWAITING_APPROVAL") is False

    def test_normal_and_legit_json_outputs_pass(self) -> None:
        assert self._guard("市场调研报告已完成，见附件。", "COMPLETED") is True
        assert self._guard('{"report": "ok"}', "COMPLETED") is True
        # 审批等待下的人读文案是合格产出（会正常回写）
        assert self._guard("等待人工决策：请审批工具 `bash` 后继续。", "AWAITING_APPROVAL") is True
        # 空文本不属污染（回写侧空内容自有 skip 语义）
        assert self._guard("", "COMPLETED") is True

    def test_original_output_payload_is_blocked(self) -> None:
        """违规路径的 _original_output 为问题卡 JSON 时同样拦住（双文本判定）。"""
        from bandi.company.company_mode import _is_deliverable_output

        result = SeatResult(
            success=False,
            output="输出契约校验失败: {...}",
            metadata={
                "terminal_reason": "AWAITING_APPROVAL",
                "_original_output": APPROVAL_QUESTION_JSON,
            },
        )
        assert _is_deliverable_output(result) is False


class TestCompanyWritebackNoPollution:
    """②+③ 生产链路：线程 / 会话历史 / _last_output 零 JSON；卡状态第三态。"""

    def test_approval_text_card_writes_copy_and_awaiting_status(self, tmp_path: Path) -> None:
        """①后真实形态：output=文案 → 回写可见文案；状态 awaiting_approval；无 JSON。"""
        sessions = SessionStore(str(tmp_path / "bandi.db"))
        sessions.create_session("sess-a", title="团队群聊会话")
        copy_text = "等待人工决策：请审批工具 `bash` 后继续。"
        result = SeatResult(
            success=False,
            output=copy_text,
            metadata={"terminal_reason": "AWAITING_APPROVAL", "total_tokens": 0, "turn_index": 1},
        )
        disp, threads = _dispatcher(tmp_path, sessions, result)

        item, _ = _run_card(
            disp, {"origin_session_id": "sess-a", "thread_id": "th-1"}, expect_success=False
        )

        assert item.runtime_metadata.get("_task_status") == "awaiting_approval", (
            f"审批等待必须第三态而非 failed（实际 {item.runtime_metadata.get('_task_status')}）"
        )
        entries = _employee_entries(sessions, "sess-a")
        assert len(entries) == 1 and entries[0]["content"] == copy_text
        assert threads.calls and threads.calls[0]["content"] == copy_text
        assert not _contains_approval_payload(item.runtime_metadata.get("_last_output")), (
            "_last_output 不得被审批协议形态污染"
        )

    def test_breaker_raw_json_card_leaves_zero_pollution(self, tmp_path: Path) -> None:
        """破坏者：直插 APPROVAL_REQUIRED JSON（模拟绕过①的旧路径）→ 三处回写零污染。"""
        sessions = SessionStore(str(tmp_path / "bandi.db"))
        sessions.create_session("sess-b", title="团队群聊会话")
        result = SeatResult(
            success=False,
            output=APPROVAL_REQUIRED_OUTPUT,
            metadata={"terminal_reason": "AWAITING_APPROVAL", "total_tokens": 0, "turn_index": 1},
        )
        disp, threads = _dispatcher(tmp_path, sessions, result)

        item, _ = _run_card(
            disp, {"origin_session_id": "sess-b", "thread_id": "th-2"}, expect_success=False
        )

        assert _employee_entries(sessions, "sess-b") == [], "审批 JSON 不得写进会话历史"
        assert threads.calls == [], "审批 JSON 不得写进讨论线程"
        assert not _contains_approval_payload(item.runtime_metadata.get("_last_output")), (
            "_last_output 不得落审批 JSON"
        )
        assert item.runtime_metadata.get("_task_status") == "awaiting_approval"

    def test_normal_card_regression(self, tmp_path: Path) -> None:
        """回归：正常产出回写与 completed 状态不受护栏影响。"""
        sessions = SessionStore(str(tmp_path / "bandi.db"))
        sessions.create_session("sess-c", title="团队群聊会话")
        result = SeatResult(
            success=True,
            output="主图已出：main-v2.png",
            metadata={"terminal_reason": "COMPLETED", "total_tokens": 3, "turn_index": 1},
        )
        disp, threads = _dispatcher(tmp_path, sessions, result)

        item, _ = _run_card(
            disp, {"origin_session_id": "sess-c", "thread_id": "th-3"}, expect_success=True
        )

        assert item.runtime_metadata.get("_task_status") == "completed"
        entries = _employee_entries(sessions, "sess-c")
        assert len(entries) == 1 and entries[0]["content"] == "主图已出：main-v2.png"
        assert threads.calls and threads.calls[0]["content"] == "主图已出：main-v2.png"


class TestGateChainForAwaitingApproval:
    """③ gate 连锁：审批等待卡转 NEEDS_ATTENTION 等人工——不 REWORK 空转、不误 PASS。"""

    def test_awaiting_card_lands_needs_attention_not_rework(self, tmp_path: Path) -> None:
        sessions = SessionStore(str(tmp_path / "bandi.db"))
        sessions.create_session("sess-d")
        result = SeatResult(
            success=False,
            output="等待人工决策：请审批工具 `bash` 后继续。",
            metadata={"terminal_reason": "AWAITING_APPROVAL", "total_tokens": 0, "turn_index": 1},
        )
        disp, _ = _dispatcher(tmp_path, sessions, result)

        item, _ = _run_card(disp, {"origin_session_id": "sess-d"}, expect_success=False)
        assert item.phase == Phase.RUNNING, f"执行完停在 RUNNING（实际 {item.phase.value}）"

        asyncio.run(disp.tick())

        item = disp.store.get(item.id)
        assert item.phase == Phase.NEEDS_ATTENTION, (
            f"审批等待卡应转 NEEDS_ATTENTION 等人工（实际 {item.phase.value}）"
        )
        assert (
            "approval" in str(item.metadata.get("last_transition_reason", "")).lower()
            or "approval" in str(item.runtime_metadata.get("last_transition_reason", "")).lower()
        ), "转态原因需可追溯（approval pending）"
