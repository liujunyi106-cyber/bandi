# 功能：契约测试——看板列口径与草稿卡启动入口（模板卡必须真的能启动）
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from bandi.company.phase import Phase, kanban_column


def test_kanban_column_maps_draft_to_todo() -> None:
    """草稿（PAUSED）归待办：没在跑就不该占「进行中」。"""
    assert kanban_column(Phase.PAUSED) == "todo"


def test_kanban_column_keeps_failed_in_done() -> None:
    """失败/取消仍归已完成，靠徽标区分，不单独占列。"""
    assert kanban_column(Phase.FAILED) == "done"
    assert kanban_column(Phase.CANCELLED) == "done"


def test_kanban_column_keeps_runtime_states_in_progress() -> None:
    """执行态与等待态仍在进行中（回归：别把 PAUSED 挪走时误伤相邻状态）。"""
    for phase in (
        Phase.RUNNING,
        Phase.WAITING_FOR_PEER,
        Phase.WAITING_FOR_CHILDREN,
        Phase.NEEDS_ATTENTION,
    ):
        assert kanban_column(phase) == "in_progress", phase


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


def _make_card(card_id: str, phase_name: str) -> str:
    """功能：直接造一张工作项卡（不依赖模板——模板现在只建组织）。"""
    from bandi.api.app import _org_api_dispatcher
    from bandi.company.phase import Phase
    from bandi.company.work_item import WorkItem, WorkItemTurnType

    dispatcher = _org_api_dispatcher()
    metadata: dict = {}
    if phase_name == "needs_attention":
        metadata = {"source": "chat_mention"}
    dispatcher.store.save(
        WorkItem(
            id=card_id,
            title=card_id,
            description="",
            phase=Phase(phase_name),
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="r1",
            parent_id=None,
            children_ids=[],
            links={},
            metadata=metadata,
            runtime_metadata={},
        )
    )
    return card_id


def test_retry_rejects_failed_card(api: Any) -> None:
    """重试只对 NEEDS_ATTENTION 有效——failed 卡不可重试（UI 不能给这个按钮）。"""
    card_id = _make_card("kb-failed", "failed")

    retried = api.post(f"/api/company/work-items/{card_id}/retry")
    assert retried.status_code == 400
