# 功能：契约——模板只建组织；卡完成后无审批员即终态；看板是执行投影
"""覆盖：
1. 应用模板不产生任何工作项（模板只建组织，任务由团队在群聊里自己产出）
2. 无审批员（顶层卡）的卡产出即 APPROVED，不停在待审
3. 有审批员（父卡负责人）且关闭自动通过时，卡停在 AWAITING_MANAGER_REVIEW
4. 看板：默认隐藏纯对话卡与历史模板草稿残留
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import pytest

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


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    app_mod = importlib.import_module("bandi.api.app")
    monkeypatch.setattr(app_mod, "_API_KEY", "test")
    import bandi.llm.models_dev as md_mod

    monkeypatch.setattr(md_mod, "trigger_models_dev_sync", lambda: None)
    app_mod._company_dispatcher = None
    app_mod._project_store = None

    from fastapi.testclient import TestClient

    yield TestClient(app_mod.app, headers={"Authorization": "Bearer test"})
    app_mod._company_dispatcher = None
    app_mod._project_store = None


def _store() -> Any:
    from bandi.api.app import _org_api_dispatcher

    return _org_api_dispatcher().store


def _count_cards() -> int:
    return len(_store().list_all())


def test_instantiate_creates_org_only(api: Any) -> None:
    """应用模板只建组织：5 员工 + 团队，0 张工作项。"""
    assert _count_cards() == 0
    response = api.post(
        "/api/company/teams/product-team/instantiate",
        json={"team_id": "t-org", "team_name": "产品组"},
    )
    assert response.status_code == 200, response.text
    report = response.json()["report"]
    assert len(report["created_employee_ids"]) == 5
    assert report["created_work_item_ids"] == []
    assert _count_cards() == 0


def test_template_has_no_work_item_graph(api: Any) -> None:
    """模板不再携带任务图：列表与详情都没有 work_item_templates 字段。"""
    teams = api.get("/api/company/teams").json()["teams"]
    assert teams, "内置模板不应为空"
    for template in teams:
        assert "work_item_templates" not in template
        assert "work_item_count" not in template
    detail = api.get("/api/company/teams/product-team")
    assert detail.status_code == 200, detail.text
    assert "work_item_templates" not in detail.json()


class _StubSeatExecutor:
    """功能：固定产出的座位执行器（断言相位流转而非模型）。"""

    async def prepare_seat(self, runtime: Any, work_item: Any) -> None:
        pass

    async def run_turn(self, runtime: Any, work_item: Any) -> SeatResult:
        return SeatResult(
            success=True, output="产出：页面与文案已交付", metadata={"total_tokens": 0}
        )

    async def checkpoint(self, runtime: Any) -> dict[str, Any]:
        return {}

    async def interrupt(self, runtime: Any) -> None:
        pass

    async def shutdown(self) -> None:
        pass


def _dispatcher(tmp_path: Path, *, auto_approve: bool) -> CompanyDispatcher:
    sessions = SessionStore(str(tmp_path / "bandi.db"))
    sessions.create_session("sess-plan")
    store = WorkItemStore(str(tmp_path / "company.db"))
    org = OrgEngine()
    org.register_role("worker", "Worker", "执行")
    org.register_role("lead", "Lead", "上级")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    disp = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        seat_executor=_StubSeatExecutor(),
        config=DispatcherConfig(
            max_concurrent=1,
            auto_approve_reviews=auto_approve,
            serial_chain=False,
        ),
    )
    disp.session_writer = (
        lambda session_id, sender_role, sender_name, content, work_item_id, process_events=None: (
            write_employee_reply_to_session(
                sessions,
                session_id,
                sender_role=sender_role,
                sender_name=sender_name,
                content=content,
                work_item_id=work_item_id,
            )
        )
    )
    return disp


def _run_card(disp: CompanyDispatcher, parent_id: str | None = None) -> Any:
    item = create_work_item(
        title="执行任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        parent_id=parent_id,
        metadata={"source": "agent_to_agent", "origin_session_id": "sess-plan"},
    )
    disp.store.save(item)
    assert disp.claim("worker")
    asyncio.run(disp.run_work_item(item.id))
    assert disp.store.get(item.id).phase is Phase.RUNNING
    asyncio.run(disp.tick())
    return disp.store.get(item.id)


def test_card_without_reviewer_completes_directly(tmp_path: Path) -> None:
    """没有审批员（顶层卡）：产出即终态，不经过待审。"""
    disp = _dispatcher(tmp_path, auto_approve=False)
    item = _run_card(disp)
    assert item.phase is Phase.APPROVED, f"无审批员应直接完成（实际 {item.phase.value}）"


def test_card_with_reviewer_waits_when_auto_approve_off(tmp_path: Path) -> None:
    """有审批员（父卡负责人）且关闭自动通过：停在待审等人处理。"""
    disp = _dispatcher(tmp_path, auto_approve=False)
    parent = create_work_item(
        title="父任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="lead",
        metadata={"source": "agent_to_agent"},
    )
    disp.store.save(parent)
    item = _run_card(disp, parent_id=parent.id)
    assert item.phase is Phase.AWAITING_MANAGER_REVIEW, (
        f"有审批员应停在待审（实际 {item.phase.value}）"
    )


def test_card_with_reviewer_completes_when_auto_approve_on(tmp_path: Path) -> None:
    """破坏者：自动通过开启时，有审批员也不许停在待审空转。"""
    disp = _dispatcher(tmp_path, auto_approve=True)
    parent = create_work_item(
        title="父任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="lead",
        metadata={"source": "agent_to_agent"},
    )
    disp.store.save(parent)
    item = _run_card(disp, parent_id=parent.id)
    assert item.phase is Phase.APPROVED, f"自动通过应直接完成（实际 {item.phase.value}）"


def test_board_hides_chat_cards_by_default(api: Any) -> None:
    """群聊/单聊承载卡与纯对话卡默认不上看板，只保留团队执行任务。"""
    from bandi.company.work_item import WorkItem

    store = _store()
    for card_id, source, kind in (
        ("c-query", "chat_mention", "query"),
        ("c-adjust", "chat_mention", "adjust"),
        ("c-dm", "chat_dm", ""),
        ("c-task", "auto_chain", ""),
        ("c-confirm", "agent_to_agent", "confirm"),
    ):
        metadata: dict[str, Any] = {"source": source}
        if kind:
            metadata["chat_kind"] = kind
        store.save(
            WorkItem(
                id=card_id,
                title=card_id,
                description="",
                phase=Phase.APPROVED,
                turn_type=WorkItemTurnType.EXECUTE,
                assignee_role="r1",
                parent_id=None,
                children_ids=[],
                links={},
                metadata=metadata,
                runtime_metadata={},
            )
        )

    board = api.get("/api/company/work-items").json()
    ids = {item["id"] for item in board["items"]}
    assert ids == {"c-task", "c-confirm"}
    assert board["hidden_chat_count"] == 3
    assert board["column_counts"]["done"] == 2

    full = api.get("/api/company/work-items", params={"hide_chat": False}).json()
    assert len(full["items"]) == 5
    assert full["hidden_chat_count"] == 0


def test_board_hides_legacy_template_drafts(api: Any) -> None:
    """历史模板草稿残留（paused + source=team_template）一律不投影。"""
    from bandi.company.work_item import WorkItem

    _store().save(
        WorkItem(
            id="legacy_spec",
            title="旧草稿",
            description="",
            phase=Phase.PAUSED,
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="r1",
            parent_id=None,
            children_ids=[],
            links={},
            metadata={"source": "team_template"},
            runtime_metadata={},
        )
    )
    board = api.get("/api/company/work-items", params={"hide_chat": False}).json()
    assert board["items"] == []
