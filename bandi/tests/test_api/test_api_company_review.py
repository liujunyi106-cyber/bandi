# 功能：契约——company 工作项评审/重试 API（桌面端评审闭环）。
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import importlib

    app_mod = importlib.import_module("bandi.api.app")
    monkeypatch.setattr(app_mod, "_API_KEY", "test")
    # 隔离 startup 的 models.dev 同步（防真实网络/全局 _sync_done 污染后续测试）
    import bandi.llm.models_dev as md_mod

    monkeypatch.setattr(md_mod, "trigger_models_dev_sync", lambda: None)
    app_mod._company_dispatcher = None
    yield tmp_path
    app_mod._company_dispatcher = None


def test_contract_company_review_retry_endpoints(home: Any) -> None:
    """契约（桌面端闭环·破坏者）：评审/重试必须可经 API 驱动——修复前仅 CLI
    （桌面端评审卡永久滞留）。"""
    import importlib

    from fastapi.testclient import TestClient

    app_mod = importlib.import_module("bandi.api.app")
    from bandi.company.phase import Phase
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    client = TestClient(app_mod.app, headers={"Authorization": "Bearer test"})
    dispatcher = app_mod._get_company_dispatcher()

    review_item = create_work_item(
        title="review me",
        turn_type=WorkItemTurnType.EXECUTE,
        phase=Phase.AWAITING_MANAGER_REVIEW,
    )
    dispatcher.store.save(review_item)

    resp = client.post(
        f"/api/company/work-items/{review_item.id}/review",
        json={"verdict": "approve", "feedback": ""},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["recorded"] is True
    loaded = dispatcher.store.get(review_item.id)
    assert loaded is not None
    assert loaded.metadata.get("review_verdict") == "approve", "verdict 必须落盘待 tick 消费"

    asyncio.run(dispatcher.tick())
    assert dispatcher.store.get(review_item.id).phase == Phase.APPROVED, (
        "approve 经 tick 消费后必须转 APPROVED（评审闭环）"
    )

    retry_item = create_work_item(
        title="retry me",
        turn_type=WorkItemTurnType.EXECUTE,
        phase=Phase.NEEDS_ATTENTION,
    )
    dispatcher.store.save(retry_item)
    resp2 = client.post(f"/api/company/work-items/{retry_item.id}/retry")
    assert resp2.status_code == 200, resp2.text
    assert dispatcher.store.get(retry_item.id).phase == Phase.READY, "retry 后必须重新入队"

    resp3 = client.post("/api/company/work-items/does-not-exist/retry")
    assert resp3.status_code == 404, resp3.text

    resp4 = client.post(
        f"/api/company/work-items/{review_item.id}/review", json={"verdict": "nope"}
    )
    assert resp4.status_code == 400, resp4.text
