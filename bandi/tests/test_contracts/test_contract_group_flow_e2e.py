"""契约（端到端冒烟）：群聊提需求 → 建计划卡 → 派子卡挂闸门 → 切执行模式解锁。

覆盖老板点名的「跑不通」链路的前三段，全部走生产装配路径（真实
`GroupRoundStore` / `WorkItemStore` / `GroupRoundService` / `SpawnThreadTaskTool`
/ `company_chat_mode` 端点本体），不 mock 业务逻辑——只替换 LLM 与线程存储等外部依赖。

第四段（真跑出文件）需要 provider 与执行器，单独一批。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from bandi.company.group_round import GroupRoundService
from bandi.company.group_round_store import GroupRoundStore
from bandi.company.store import WorkItemStore
from bandi.company.work_item import Phase, WorkItemTurnType, create_work_item
from bandi.tools.base import ToolPermissionContext
from bandi.tools.thread_task import SpawnThreadTaskTool


class _FakeOrg:
    """最小组织引擎：只提供角色清单（spawn 的注册校验需要）。"""

    def list_roles(self):
        return [SimpleNamespace(id="dev", name="开发"), SimpleNamespace(id="pm", name="产品")]

    def get_role(self, role_id: str):
        for role in self.list_roles():
            if role.id == role_id:
                return role
        return None

    def can_spawn(self, caller_role: str, target_role: str) -> bool:
        # 功能：最小实现放开派单白名单，用例只验证模式门
        return True


class _FakeThreads:
    """最小线程管理器：记录消息，返回固定 thread_id。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    def get_thread(self, thread_id: str):
        return None

    def create_thread(self, participants):
        return SimpleNamespace(thread_id="thread-1")

    def add_message(self, thread_id: str, **kwargs):
        self.messages.append({"thread_id": thread_id, **kwargs})


@pytest.fixture()
def stores(tmp_path: Path) -> tuple[GroupRoundStore, WorkItemStore]:
    return (
        GroupRoundStore(str(tmp_path / "rounds.db")),
        WorkItemStore(str(tmp_path / "items.db")),
    )


def test_group_plan_turn_creates_plan_card(stores) -> None:
    """第一段：群聊在 plan 模式下提需求 → 建一张 PLAN 卡并绑定会话归属。"""
    rounds, items = stores
    service = GroupRoundService(rounds, items, org_engine=_FakeOrg())

    cards = service.submit_user_message(
        content="帮我做一个待办事项 App",
        sender_role="user",
        targets=["pm"],
        session_id="sess-1",
        project_id="proj-1",
        member_role_ids={"pm", "dev"},
        mode="plan",
    )

    assert len(cards) == 1, f"群聊必须恰好建一张首发言卡，实际 {len(cards)}"
    card = cards[0]
    assert card.turn_type == WorkItemTurnType.PLAN, (
        f"plan 模式必须建 PLAN 卡，实际 {card.turn_type}"
    )
    assert card.phase == Phase.QUEUED
    assert card.metadata["group_chat"] is True
    assert card.metadata["created_mode"] == "plan"
    assert card.metadata["origin_session_id"] == "sess-1", "主卡必须带会话归属（切模式据此释放）"
    assert card.metadata["round_id"], "主卡必须关联回合"


def test_spawn_from_plan_card_arms_mode_gate(stores) -> None:
    """第二段：计划模式的父卡派发子卡 → 子卡挂 mode_gate=plan 并继承会话归属。"""
    _rounds, items = stores
    parent = create_work_item(
        title="帮我做一个待办事项 App",
        turn_type=WorkItemTurnType.PLAN,
        description="帮我做一个待办事项 App",
        assignee_role="pm",
        metadata={
            "group_chat": True,
            "created_mode": "plan",
            "origin_session_id": "sess-1",
            "project_id": "proj-1",
            "round_id": "round-1",
        },
        phase=Phase.QUEUED,
    )
    items.save(parent)

    tool = SpawnThreadTaskTool(store=items, thread_manager=_FakeThreads(), org_engine=_FakeOrg())
    result = asyncio.run(
        tool.execute(
            {
                "parent_task_id": parent.id,
                "target_role": "dev",
                "task_prompt": "写待办页面的交互与样式",
            },
            ToolPermissionContext(role="pm"),
        )
    )

    assert result.get("success") is True, f"派发失败：{result}"
    child_id = result["work_item_id"]
    child = items.get(child_id)
    assert child is not None
    assert child.metadata["mode_gate"] == "plan", "计划模式下的子卡必须挂闸门"
    assert child.metadata["origin_session_id"] == "sess-1", (
        "子卡必须继承会话归属，否则切模式找不到它"
    )
    assert child.metadata["project_id"] == "proj-1"


def test_switch_to_execute_releases_gate(stores, monkeypatch) -> None:
    """第三段：切执行模式 → 该会话下所有 plan 闸门被清除（端点是真实实现）。"""
    _rounds, items = stores
    parent = create_work_item(
        title="父卡",
        turn_type=WorkItemTurnType.PLAN,
        description="父卡",
        assignee_role="pm",
        metadata={"created_mode": "plan", "origin_session_id": "sess-1"},
        phase=Phase.QUEUED,
    )
    items.save(parent)
    child = create_work_item(
        title="子卡",
        turn_type=WorkItemTurnType.EXECUTE,
        description="子卡",
        assignee_role="dev",
        metadata={
            "mode_gate": "plan",
            "created_mode": "plan",
            "origin_session_id": "sess-1",
        },
        phase=Phase.QUEUED,
    )
    items.save(child)
    # 干扰项：别的会话的闸门不得被释放
    other = create_work_item(
        title="别会话子卡",
        turn_type=WorkItemTurnType.EXECUTE,
        description="别会话子卡",
        assignee_role="dev",
        metadata={"mode_gate": "plan", "origin_session_id": "sess-2"},
        phase=Phase.QUEUED,
    )
    items.save(other)

    # 注意：`from bandi.api import app` 解析到 FastAPI 实例（包导出名），
    # 必须按模块名导入才能 monkeypatch 模块级 getter。
    import importlib

    app_module = importlib.import_module("bandi.api.app")

    monkeypatch.setattr(
        app_module,
        "_get_company_dispatcher",
        lambda: SimpleNamespace(store=items, signal_dispatcher_wake=lambda: None),
    )
    monkeypatch.setattr(
        app_module, "_get_store", lambda: SimpleNamespace(get_session=lambda _sid: None)
    )

    result = asyncio.run(app_module.company_chat_mode({"mode": "execute", "session_id": "sess-1"}))

    assert result["released"] == 1, f"应恰好释放本会话 1 张卡，实际 {result}"
    refreshed = items.get(child.id)
    assert "mode_gate" not in (refreshed.metadata or {}), "本会话子卡的闸门必须被清除"
    assert refreshed.metadata.get("created_mode") == "execute"
    untouched = items.get(other.id)
    assert untouched.metadata.get("mode_gate") == "plan", "别的会话的闸门不得被误释放"


def test_execute_child_writes_deliverable(tmp_path: Path, monkeypatch) -> None:
    """第四段：解锁后的子卡被调度拾取 → 真实 AgentLoop 执行 write → 产出文件。

    装配真实度：唯一 mock = LLM Provider（脚本化两条响应）；其余为真实
    EngineSeatExecutor / AgentLoop / 工具执行 / 权限管道。
    """
    from unittest.mock import patch

    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.gate_harness import GateHarness
    from bandi.company.org_engine import OrgEngine
    from bandi.company.recruiter import Recruiter
    from bandi.company.seat_executor import EngineSeatExecutor
    from bandi.core.message import LLMResponse, ToolCall
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore
    from bandi.tools import fs_tool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(fs_tool, "_WORKSPACE_ROOT", workspace)

    sessions = SessionStore(str(tmp_path / "bandi.db"))
    sessions.create_session("sess-1", title="团队群聊")
    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("dev", "开发", "写代码")
    recruiter = Recruiter()
    recruiter.register_seat("dev")
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=EngineSeatExecutor(),
        config=DispatcherConfig(max_concurrent=1),
    )
    dispatcher.session_writer = lambda *args, **kwargs: None

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="tc-write-1",
                    name="write",
                    arguments={
                        "path": "out/todo.html",
                        "content": "<html><body>待办清单</body></html>",
                    },
                )
            ],
            finish_reason="tool_calls",
        )
    )
    provider.add_response(LLMResponse(content="已生成 out/todo.html", finish_reason="stop"))

    item = create_work_item(
        title="写待办页面",
        turn_type=WorkItemTurnType.EXECUTE,
        description="写待办页面",
        assignee_role="dev",
        metadata={"origin_session_id": "sess-1", "group_chat": True},
    )
    store.save(item)

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("bandi.core.agent_runner.build_provider", return_value=provider),
    ):
        assert dispatcher.claim("dev"), "调度必须能认领到这张卡"
        asyncio.run(dispatcher.run_work_item(item.id))
        asyncio.run(dispatcher.tick())

    final = store.get(item.id)
    target = workspace / "out" / "todo.html"
    last_output = str((final.runtime_metadata or {}).get("_last_output") or "")
    produced = list(workspace.rglob("*"))
    assert final is not None and final.phase.value in {
        "approved",
        "awaiting_approval",
        "done",
        "needs_attention",
    }, f"卡应收敛到终态，实际 {final.phase.value}；产出={produced}"
    # write 是 medium 风险工具（permissions_override=ask）：headless 下会停在审批门。
    # 本用例钉的是「解锁后的子卡能被调度拾取并真正进入执行」，两条路径都算通过：
    #   ① 审批放行 → 文件落盘 ② 审批挂起 → 产出为「等待人工决策」文案（且提及 write）
    assert target.is_file() or "等待人工决策" in last_output, (
        f"既没落盘也没挂审批，链路断在中间：phase={final.phase.value} "
        f"last_output={last_output[:120]!r} files={produced}"
    )
    if target.is_file():
        assert "待办清单" in target.read_text(encoding="utf-8")


def test_write_after_approval_lands_file(tmp_path: Path, monkeypatch) -> None:
    """第五段：write 撞审批门 → 用户批准 → 卡恢复 → 重发调用 → 文件落盘。

    这是「长任务跨审批继续」的最小闭环：审批决策要能被下一轮执行消费（grant +
    approval_resume 前置注入），而不是永远停在「等待人工决策」。
    """
    from unittest.mock import patch

    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.gate_harness import GateHarness
    from bandi.company.org_engine import OrgEngine
    from bandi.company.recruiter import Recruiter
    from bandi.company.seat_executor import EngineSeatExecutor
    from bandi.core.message import LLMResponse, ToolCall
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore
    from bandi.tools import fs_tool
    from bandi.tools.approval import get_approval_store, reset_approval_store

    reset_approval_store()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(fs_tool, "_WORKSPACE_ROOT", workspace)

    sessions = SessionStore(str(tmp_path / "bandi.db"))
    sessions.create_session("sess-appr", title="审批续跑")
    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("dev", "开发", "写代码")
    recruiter = Recruiter()
    recruiter.register_seat("dev")
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=EngineSeatExecutor(),
        config=DispatcherConfig(max_concurrent=1),
    )
    dispatcher.session_writer = lambda *args, **kwargs: None

    write_call = ToolCall(
        id="tc-ok-1",
        name="write",
        arguments={"path": "out/approved.html", "content": "<html>已批准写入</html>"},
    )
    provider = MockProvider()
    provider.add_response(
        LLMResponse(content="", tool_calls=[write_call], finish_reason="tool_calls")
    )
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="tc-ok-2",
                    name="write",
                    arguments={"path": "out/approved.html", "content": "<html>已批准写入</html>"},
                )
            ],
            finish_reason="tool_calls",
        )
    )
    provider.add_response(LLMResponse(content="已生成 out/approved.html", finish_reason="stop"))

    item = create_work_item(
        title="写审批页面",
        turn_type=WorkItemTurnType.EXECUTE,
        description="写审批页面",
        assignee_role="dev",
        metadata={"origin_session_id": "sess-appr"},
    )
    store.save(item)

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("bandi.core.agent_runner.build_provider", return_value=provider),
    ):
        assert dispatcher.claim("dev")
        asyncio.run(dispatcher.run_work_item(item.id))
        asyncio.run(dispatcher.tick())
        hung = store.get(item.id)
        assert (hung.runtime_metadata or {}).get("_task_status") == "awaiting_approval", (
            f"前置：write 应停在审批门（实际 {hung.runtime_metadata.get('_task_status')}）"
        )

        # 授权必须挂在**执行会话**下（executor 用 session_meta.session_id 查），
        # 卡上记的 `_approval_session_id` 就是那个口径。
        approval_sid = str((hung.runtime_metadata or {}).get("_approval_session_id") or "")
        assert approval_sid, "前置：卡上必须记录审批所属会话"
        get_approval_store().grant(approval_sid, "write", scope="session")
        resumed = dispatcher.resume_after_approval(
            "sess-appr",
            decision="approve",
            tool_name="write",
            note="允许写入工作区",
        )
        assert resumed >= 1, "审批决策后必须有卡被恢复"
        asyncio.run(dispatcher.tick())

        if dispatcher.claim("dev"):
            asyncio.run(dispatcher.run_work_item(item.id))
            asyncio.run(dispatcher.tick())

    target = workspace / "out" / "approved.html"
    final = store.get(item.id)
    last_output = str((final.runtime_metadata or {}).get("_last_output") or "")
    assert target.is_file(), (
        f"批准后必须落盘；phase={final.phase.value} "
        f"last_output={last_output[:120]!r} files={list(workspace.rglob('*'))}"
    )
    assert "已批准写入" in target.read_text(encoding="utf-8")
    reset_approval_store()
