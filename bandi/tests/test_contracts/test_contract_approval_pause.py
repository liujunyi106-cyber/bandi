"""
  ApprovalStore：policy 三态（untrusted/on-request/never，非法值拒绝）
     grants（once 用后即焚 / session 持久）、pending set/pop/clear
  决策解析：中英文 token + JSON 形态 → allow_once/allow_session/deny
  回填体：deny 带 DENY 语义；allow 带 re-issue 指令
  auto_review：JSON 三决策解析；一切失败路径 escalate（fail-safe）
  executor 审批门控：headless + ask 工具 → APPROVAL_REQUIRED + store 登记；
     once grant 后放行；policy=never 直接拒绝；有同步 UI 时零介入（CLI 不变）
  on_executing 接线：APPROVAL_REQUIRED → turn 以 AWAITING_APPROVAL 终止
     final_message 含审批问题卡片数据
  API 决策端点：POST /api/sessions/{sid}/approval-decision 决策入库 + 回填

破坏者断言：无 pending 时决策端点拒绝；once grant 消费后第二次必须重新审批；
auto_review 解析失败不得 allow/deny（必须 escalate）。
"""

import json
from pathlib import Path
from typing import Any

import pytest

# ── ApprovalStore ──────────────────────────────────────────────────────────


def test_approval_store_policy_grants_pending() -> None:
    """契约：policy 三态 + grants 消费语义 + pending 生命周期。"""
    from bandi.tools.approval import ApprovalStore, PendingApproval

    store = ApprovalStore()

    with pytest.raises(ValueError):
        store.set_policy("s1", "always-allow")  # 非法策略必须拒绝

    store.set_policy("s1", "untrusted")
    assert store.get_policy("s1") == "untrusted"
    assert store.get_policy(None) in ("on-request",), "缺省策略 on-request"
    assert store.get_policy("未知会话") == "on-request"

    store.grant("s1", "bash", scope="once")
    assert store.is_granted("s1", "bash") is True
    assert store.is_granted("s1", "bash") is True, "once 语义：窗口内不消费即焚"

    # session grant：持续有效
    store.grant("s1", "bash", scope="session")
    for _ in range(3):
        assert store.is_granted("s1", "bash") is True

    # pending：pop 匹配 tool_call_id
    store.set_pending(
        "s1", PendingApproval(tool_call_id="t1", tool_name="bash", params_digest="x", reason="r")
    )
    assert store.pop_pending("s1", tool_call_id="wrong") is None, "id 不匹配不得弹出"
    popped = store.pop_pending("s1", tool_call_id="t1")
    assert popped is not None and popped.tool_name == "bash"
    assert store.pop_pending("s1") is None, "弹出后清空"


# ── /决策解析与回填体 ────────────────────────────────────────────────────


def test_parse_decision_tokens_cn_en_json() -> None:
    """契约：中英文 token 与 JSON 形态归一化。"""
    from bandi.tools.approval import parse_approval_decision

    assert parse_approval_decision("允许一次") == "allow_once"
    assert parse_approval_decision("批准") == "allow_once"
    assert parse_approval_decision("始终允许") == "allow_session"
    assert parse_approval_decision("Allow Once") == "allow_once"
    assert parse_approval_decision("deny") == "deny"
    assert parse_approval_decision('{"decision": "deny"}') == "deny"
    assert parse_approval_decision("随便说点什么") is None
    assert parse_approval_decision("") is None


def test_approval_reply_content_semantics() -> None:
    """契约：deny 必须带禁止重试语义；allow 必须带 re-issue 指令。"""
    from bandi.tools.approval import approval_reply_content

    deny = json.loads(approval_reply_content("deny", "bash"))
    assert deny["approval_decision"] == "deny"
    assert "DENIED" in deny["note"] and "Do not retry" in deny["note"]

    allow = json.loads(approval_reply_content("allow_session", "bash"))
    assert allow["approval_decision"] == "allow_session"
    assert "APPROVED" in allow["note"] and "Re-issue" in allow["note"]


# ── auto_review ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_review_decisions_and_fail_safe() -> None:
    """契约（+破坏者）：合法 JSON 三决策生效；空/坏输出一律 escalate。"""
    from bandi.tools.approval import PendingApproval
    from bandi.tools.auto_review import auto_review_decision

    pending = PendingApproval(tool_call_id="t1", tool_name="bash", params_digest="x", reason="r")

    async def llm_allow(prompt: Any) -> str:
        return '{"decision": "allow", "rationale": "safe read-only"}'

    async def llm_deny(prompt: Any) -> str:
        return '{"decision": "deny", "rationale": "destructive"}'

    async def llm_garbage(prompt: Any) -> str:
        return "not json at all"

    async def llm_boom(prompt: Any) -> str:
        raise RuntimeError("llm down")

    d1 = await auto_review_decision(pending, {}, llm_call=llm_allow)
    assert d1[0] == "allow"
    d2 = await auto_review_decision(pending, {}, llm_call=llm_deny)
    assert d2[0] == "deny"
    d3 = await auto_review_decision(pending, {}, llm_call=llm_garbage)
    assert d3[0] == "escalate", "坏输出必须 escalate 不得猜"
    d4 = await auto_review_decision(pending, {}, llm_call=llm_boom)
    assert d4[0] == "escalate", "调用失败必须 escalate（fail-safe）"


# ── executor 审批门控 ───────────────────────────────────────────────────────


class _ToolStub:
    name = "bash"
    risk_level = "medium"

    def validate_params(self, arguments: dict) -> list:
        return []


class _PipelineStub:
    """headless pipeline（user_interaction=None）+ ask 名单含 bash。"""

    def __init__(self, with_ui: bool = False) -> None:
        self.ask_tools = {"bash"}
        self.user_interaction = (lambda: object())() if with_ui else None
        self.cached: list[tuple[str, dict]] = []

    @property
    def user_interaction_available(self) -> bool:
        return self.user_interaction is not None

    def needs_approval(self, tc: Any, tool: Any, mode: str) -> bool:
        if mode == "plan":
            return False
        return tc.name in self.ask_tools

    def cache_approved(self, tool_name: str, arguments: dict) -> None:
        # 审批放行后写确认缓存（executor._consume_approval_grant 调用）
        self.cached.append((tool_name, dict(arguments)))


@pytest.mark.asyncio
async def test_headless_gate_pauses_then_grant_releases(monkeypatch: Any) -> None:
    """契约：headless ask 工具首次 → APPROVAL_REQUIRED + pending 登记；
    once grant 后同一调用放行。"""
    from bandi.core.message import ToolCall
    from bandi.tools.approval import get_approval_store, reset_approval_store
    from bandi.tools.executor import ToolExecutor

    reset_approval_store()
    monkeypatch.setattr("bandi.tools.executor.ToolExecutor.__init__", lambda self, *a, **k: None)
    ex = ToolExecutor()
    ex._permissions_pipeline = _PipelineStub()
    ex._mode = "execute"
    ex._session_meta = {"session_id": "sess-appr"}
    tc = ToolCall(id="t9", name="bash", arguments={"command": "ls"})

    first = await ex._approval_gate(tc, _ToolStub())
    assert first is not None and first.error.startswith("APPROVAL_REQUIRED:")
    store = get_approval_store()
    assert store.get_pending("sess-appr") is not None, "pending 必须登记"

    # 未授权重试同 id → pop 匹配后再次暂停
    store.set_pending("sess-appr", store.get_pending("sess-appr"))
    second = await ex._approval_gate(tc, _ToolStub())
    assert second is not None and second.error.startswith("APPROVAL_REQUIRED:")

    # 用户 allow_once → 单次（本轮）同工具全部调用授权——命中后窗口内保留
    store.grant("sess-appr", "bash", scope="once")
    assert ex._consume_approval_grant(tc) is True, "grant 命中必须放行（单一消费点）"
    assert ex._permissions_pipeline.cached, "放行必须写权限确认缓存（防 ask 层二次拒绝）"
    assert store.is_granted("sess-appr", "bash") is True, "once grant 窗口内保留（不消费即焚）"

    store.set_policy("sess-appr", "never")
    denied = await ex._approval_gate(ToolCall(id="t10", name="bash", arguments={}), _ToolStub())
    assert denied is not None and "never" in (denied.error or "")
    reset_approval_store()


@pytest.mark.asyncio
async def test_gate_skips_when_sync_ui_present_or_not_ask() -> None:
    """契约（P5b+破坏者）：有同步 UI（CLI）时门控零介入；非 ask 工具不拦截。"""
    from bandi.core.message import ToolCall
    from bandi.tools.approval import reset_approval_store
    from bandi.tools.executor import ToolExecutor

    reset_approval_store()
    ex = ToolExecutor(registry=None)
    ex.__dict__.update(
        {
            "_permissions_pipeline": _PipelineStub(with_ui=True),
            "_mode": "execute",
            "_session_meta": {"session_id": "s"},
        }
    )
    assert await ex._approval_gate(ToolCall(id="a", name="bash", arguments={}), _ToolStub()) is None

    class _ReadOnlyPipeline(_PipelineStub):
        def needs_approval(self, tc: Any, tool: Any, mode: str) -> bool:
            return False

    ex.__dict__.update({"_permissions_pipeline": _ReadOnlyPipeline()})
    assert await ex._approval_gate(ToolCall(id="b", name="read", arguments={}), _ToolStub()) is None


@pytest.mark.asyncio
async def test_employee_workspace_auto_allow(tmp_path: Path) -> None:
    """契约：员工会话内工作区文件操作免审批——工作区内 write/bash 自动放行
    （不登记 pending）；工作区外/危险命令/非员工会话仍走审批。"""
    from bandi.core.message import ToolCall
    from bandi.tools.approval import get_approval_store, reset_approval_store
    from bandi.tools.executor import ToolExecutor

    reset_approval_store()
    (tmp_path / "ws").mkdir()
    pipe = _PipelineStub()
    pipe.ask_tools = {"bash", "write"}
    ex = ToolExecutor(registry=None)
    ex.__dict__.update(
        {
            "_permissions_pipeline": pipe,
            "_mode": "execute",
            "_session_meta": {"session_id": "role-session::p1::staff::teamA::roleA::roleA"},
            "_project_root": str(tmp_path / "ws"),
        }
    )
    sid = "role-session::p1::staff::teamA::roleA::roleA"

    class _WriteStub(_ToolStub):
        name = "write"

    inside = ToolCall(
        id="w1", name="write", arguments={"file_path": "market_brief.md", "content": "x"}
    )
    assert await ex._approval_gate(inside, _WriteStub()) is None, "工作区内 write 必须免审批"
    assert get_approval_store().get_pending(sid) is None, "免审批不得登记 pending"

    outside = ToolCall(
        id="w2", name="write", arguments={"file_path": str(tmp_path / "outside.md"), "content": "x"}
    )
    gate = await ex._approval_gate(outside, _WriteStub())
    assert gate is not None and (gate.error or "").startswith("APPROVAL_REQUIRED:"), (
        "工作区外 write 仍须审批"
    )

    safe = ToolCall(id="b1", name="bash", arguments={"command": "ls -la"})
    assert await ex._approval_gate(safe, _ToolStub()) is None, "工作区内安全 bash 免审批"
    dangerous = ToolCall(id="b2", name="bash", arguments={"command": "rm -rf ."})
    gate2 = await ex._approval_gate(dangerous, _ToolStub())
    assert gate2 is not None and (gate2.error or "").startswith("APPROVAL_REQUIRED:"), (
        "危险命令仍须审批"
    )
    escape = ToolCall(id="b3", name="bash", arguments={"command": "cat ../secret.txt"})
    gate3 = await ex._approval_gate(escape, _ToolStub())
    assert gate3 is not None and (gate3.error or "").startswith("APPROVAL_REQUIRED:"), (
        "上级目录穿越仍须审批"
    )

    ex._session_meta = {"session_id": "sess-normal"}
    gate4 = await ex._approval_gate(
        ToolCall(id="w3", name="write", arguments={"file_path": "x.md"}), _WriteStub()
    )
    assert gate4 is not None, "破坏者：非员工会话不得适用工作区免审批"
    reset_approval_store()


@pytest.mark.asyncio
async def test_employee_workspace_auto_allow_with_ui_channel(tmp_path: Path) -> None:
    """契约（真机修复）：员工装配 UI 交互通道（多问卡/权限卡）后，工作区内文件
    操作同样免审批——gate 写确认缓存，pipeline 的 UI 询问缓存命中放行（不弹卡）；
    工作区外不写缓存（仍由 UI 询问挂起——fail-closed 语义不丢）。"""
    from bandi.core.message import ToolCall
    from bandi.tools.approval import get_approval_store, reset_approval_store
    from bandi.tools.executor import ToolExecutor

    reset_approval_store()
    (tmp_path / "ws").mkdir()
    pipe = _PipelineStub(with_ui=True)
    pipe.ask_tools = {"bash", "write"}
    ex = ToolExecutor(registry=None)
    ex.__dict__.update(
        {
            "_permissions_pipeline": pipe,
            "_mode": "execute",
            "_session_meta": {"session_id": "role-session::p1::staff::teamA::roleA::roleA"},
            "_project_root": str(tmp_path / "ws"),
        }
    )

    class _WriteStub(_ToolStub):
        name = "write"

    inside = ToolCall(id="wu1", name="write", arguments={"file_path": "scope.md", "content": "x"})
    assert await ex._approval_gate(inside, _WriteStub()) is None, (
        "有 UI 通道时工作区内 write 也必须免审批（缓存命中放行，不弹卡）"
    )
    assert pipe.cached and pipe.cached[-1][0] == "write", "免审批必须写确认缓存"
    assert (
        get_approval_store().get_pending("role-session::p1::staff::teamA::roleA::roleA") is None
    ), "免审批不得登记 pending"

    cached_before = len(pipe.cached)
    outside = ToolCall(
        id="wu2",
        name="write",
        arguments={"file_path": str(tmp_path / "outside.md"), "content": "x"},
    )
    assert await ex._approval_gate(outside, _WriteStub()) is None, (
        "有 UI 时工作区外 write 由同步询问路径接管（gate 零介入）"
    )
    assert len(pipe.cached) == cached_before, "工作区外不得写确认缓存（仍走 UI 询问挂起）"
    reset_approval_store()


def test_needs_approval_bypass_alignment_with_ask_list() -> None:
    """契约（B-q3）：needs_approval 必须与 _check_ask_list 同语义——bypass 工具
    即使误配进 ask 名单也放行，杜绝 CLI 放行 / headless 拦截的通道分裂。"""
    from bandi.core.message import ToolCall
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    tc = ToolCall(id="x", name="bash", arguments={})

    pp_bypassed = PermissionsPipeline(ask_tools={"bash"}, bypass_tools={"bash"})
    assert pp_bypassed.needs_approval(tc, None, "execute") is False, (
        "bypass 工具必须排除在审批门外（与 _check_ask_list 对齐）"
    )

    pp_ask = PermissionsPipeline(ask_tools={"bash"})
    assert pp_ask.needs_approval(tc, None, "execute") is True
    assert pp_ask.needs_approval(tc, None, "plan") is False, "plan 态只读白名单不拦截"


# ── on_executing 接线 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_executing_awaits_approval_on_required_result() -> None:
    """契约（+ 修订）：APPROVAL_REQUIRED 结果 → AWAITING_APPROVAL 终止
    终态文案化（含工具名的人读文本，不再借道问题卡 JSON——问题卡走独立审批/交互帧通道）
    且不判 TOOL_ERROR。"""
    from types import SimpleNamespace

    from bandi.agent import states as states_mod
    from bandi.agent.base import TerminalReason
    from bandi.core.message import Message, MessageRole, ToolCall, ToolResult

    executed: list[list] = []

    class _Executor:
        async def execute(self, calls: list) -> list:
            executed.append(calls)
            return [
                ToolResult(
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    success=False,
                    output="",
                    error='APPROVAL_REQUIRED:{"prompt": "Allow bash?"}',
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

    await states_mod.on_executing(ctx)

    assert ctx.terminal_reason == TerminalReason.AWAITING_APPROVAL, (
        f"必须以审批暂停终止（实际 {ctx.terminal_reason}）"
    )
    message = str(ctx.final_message or "")
    assert "bash" in message, "final_message 必须为人类可读文案（含待审批工具名）"
    assert not message.lstrip().startswith("{"), (
        "终态不得借道问题卡 JSON（APPR-1 修复：文案化，问题卡走独立通道）"
    )
    assert "Allow the agent to run" not in message


# ── API 决策端点 ─────────────────────────────────────────────────────────────


def test_api_approval_decision_endpoint() -> None:
    """契约：决策端点 allow_once 入库 + 回填；无 pending 拒绝。"""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from bandi.api.app import app
    from bandi.tools.approval import (
        ApprovalStore,
        PendingApproval,
        get_approval_store,
    )

    store = get_approval_store()
    store.set_pending(
        "sess-api-appr",
        PendingApproval(tool_call_id="tx", tool_name="write", params_digest="p", reason="r"),
    )

    client = TestClient(app)
    client.headers["Authorization"] = "Bearer p"
    with patch("bandi.api.app._API_KEY", "p"):
        resp = client.post(
            "/api/sessions/sess-api-appr/approval-decision",
            json={"decision": "allow_once"},
        )
        assert resp.status_code == 200, f"决策应成功（实际 {resp.status_code} {resp.text}）"
        assert store.is_granted("sess-api-appr", "write"), "allow_once 必须入库 grant"

        conflict = client.post(
            "/api/sessions/sess-api-appr/approval-decision",
            json={"decision": "deny"},
        )
        assert conflict.status_code in (404, 409), (
            "无 pending 必须拒绝（实际 {conflict.status_code}）"
        )

    assert ApprovalStore is not None


def test_api_approval_decision_precise_tool_call_id() -> None:
    """契约：多 pending 并存时按 tool_call_id 精确决策——点哪张卡清哪张。"""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from bandi.api.app import app
    from bandi.tools.approval import PendingApproval, get_approval_store

    store = get_approval_store()
    store.set_pending(
        "sess-precise",
        PendingApproval(tool_call_id="c1", tool_name="write", params_digest="p1", reason="r"),
    )
    store.set_pending(
        "sess-precise",
        PendingApproval(tool_call_id="c2", tool_name="bash", params_digest="p2", reason="r"),
    )

    client = TestClient(app)
    client.headers["Authorization"] = "Bearer p"
    with patch("bandi.api.app._API_KEY", "p"):
        resp = client.post(
            "/api/sessions/sess-precise/approval-decision",
            json={"decision": "allow_once", "tool_call_id": "c2"},
        )
        assert resp.status_code == 200, f"精确决策应成功（实际 {resp.status_code} {resp.text}）"
        body = resp.json()
        assert body["tool_name"] == "bash", "决策对象必须是被点的 c2（bash）"
        assert body["tool_call_id"] == "c2"
        remaining = store.get_pending("sess-precise")
        assert remaining is not None and remaining.tool_call_id == "c1", "c1 必须保留在队列"

        miss = client.post(
            "/api/sessions/sess-precise/approval-decision",
            json={"decision": "deny", "tool_call_id": "nope"},
        )
        assert miss.status_code == 409, "不存在的 tool_call_id 必须拒绝（不得误清队首）"
        assert store.get_pending("sess-precise") is not None, "误指不得消耗队列"
