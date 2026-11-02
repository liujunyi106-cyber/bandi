# 功能：契约——TierB 收尾批（DoR 核验驱动的补缺）。
#
# 背景：前端开工前置条件（计划 §八）逐项核验发现两处缺口：
# #4 成本查询需「重启后可查」——`/api/sessions/{id}/cost-detail` 是内存实时值
# 落库台账（.2 llm_request_log）缺 API 读面；
# #6 多 Agent 并发可观测需 per-agent 时间线——report.transitions 只有相位迁移
# 无耗时数据（团队看板无法展示「谁跑了多久」）。
# 本文件钉死这两项的**最终产物**行为。
from __future__ import annotations

import sys
from pathlib import Path

import pytest


def _api_module():
    """取 bandi.api.app 模块对象（属性 bandi.api.app 是 FastAPI 实例，须经 sys.modules）。"""
    import bandi.api.app  # noqa: F401

    return sys.modules["bandi.api.app"]


def test_cost_history_endpoint_reads_persisted_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """契约（DoR-4）：/api/cost/* 落库查询——重启（新 store 实例）后历史仍可查。"""
    from fastapi.testclient import TestClient

    from bandi.observability.cost_store import (
        CostRecord,
        get_shared_cost_store,
        reset_shared_cost_stores,
    )

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    api_mod = _api_module()
    monkeypatch.setattr(api_mod, "_API_KEY", "test")

    # 写入一条历史台账后「模拟重启」：清共享实例，端点须从库文件读回
    store = get_shared_cost_store(tmp_path / ".bandi" / "data" / "cost.db")
    store.enqueue(
        CostRecord(
            session_id="s-hist",
            model="m",
            input_tokens=10,
            output_tokens=5,
            total_cost_cny=0.01,
        )
    )
    store.flush()
    reset_shared_cost_stores()

    client = TestClient(api_mod.app)
    client.headers["Authorization"] = "Bearer test"
    resp = client.get("/api/cost/sessions/s-hist")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_cost"]["input_tokens"] == 10
    assert body["session_cost"]["output_tokens"] == 5
    assert len(body["records"]) >= 1
    assert body["records"][0]["model"] == "m"
