# 功能：company/org API 面契约——员工 CRUD / 组织配置 / WorkItem / Talent / 群聊
# 背景：后端 company 能力全现成（OrgEngine/TalentMarket/WorkItemStore/CommunicationManager）
# 契约钉「行为结果/最终产物」：写→读回一致、org.json 持久化、非法入参 4xx。
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """company 单例隔离：Path.home→tmp + 重置全局 dispatcher（每测试重建装配）。"""
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._company_dispatcher", None),
        patch("bandi.api.app._custom_runtime_runner", None),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


def test_org_teams_lists_configured_teams() -> None:
    """契约：已配置团队清单——org 角色 team_id 聚合（选择团队弹窗数据源）；
    未归属团队的角色不进清单。"""
    client.post(
        "/api/company/roles",
        json={"id": "lead-x", "name": "组长X", "team_id": "team-x", "is_final_decider": True},
    )
    client.post(
        "/api/company/roles",
        json={"id": "worker-x", "name": "组员X", "reports_to": "lead-x", "team_id": "team-x"},
    )
    resp = client.get("/api/company/org-teams")
    assert resp.status_code == 200
    teams = resp.json()["teams"]
    match = [item for item in teams if item["team_id"] == "team-x"]
    assert match, f"已配置团队必须进清单，实际 {teams}"
    assert match[0]["role_count"] == 2
    assert {role["role_id"] for role in match[0]["roles"]} == {"lead-x", "worker-x"}


def test_create_session_endpoint_roundtrip() -> None:
    """契约：显式创建空会话——落库可读回（团队群聊条目点击即建）；重复 id 409；
    幽灵项目 400（防建出悬空会话）。"""
    resp = client.post(
        "/api/sessions",
        json={"session_id": "sess-gc-1", "title": "项目总群聊", "project_id": "company"},
    )
    assert resp.status_code == 200, resp.text
    session = resp.json()["session"]
    assert session["id"] == "sess-gc-1"
    assert session["title"] == "项目总群聊"
    detail = client.get("/api/sessions/sess-gc-1")
    assert detail.status_code == 200

    dup = client.post("/api/sessions", json={"session_id": "sess-gc-1"})
    assert dup.status_code == 409

    ghost = client.post("/api/sessions", json={"session_id": "sess-gc-2", "project_id": "ghost-p"})
    assert ghost.status_code == 400, "幽灵项目必须拒绝（不建悬空会话）"


def _dispatcher() -> Any:
    from bandi.api.app import _get_company_dispatcher

    return _get_company_dispatcher()


from typing import Any  # noqa: E402


class TestOrgEmployees:
    def test_list_empty_and_create(self) -> None:
        resp = client.get("/api/company/employees")
        assert resp.status_code == 200
        assert resp.json()["employees"] == []

        resp = client.post(
            "/api/company/employees",
            json={
                "employee_id": "worker-1",
                "name": "一号员工",
                "role_id": "worker",
                "description": "测试员工",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["employee_id"] == "worker-1"
        assert data["status"] == "active"

        resp = client.get("/api/company/employees")
        assert [e["employee_id"] for e in resp.json()["employees"]] == ["worker-1"]

    def test_get_and_retire(self) -> None:
        dispatcher = _dispatcher()
        from bandi.company.org_engine import OrgEmployee

        dispatcher.org_engine.register_employee(
            OrgEmployee(employee_id="e1", name="甲", role_id="worker")
        )
        resp = client.get("/api/company/employees/e1")
        assert resp.status_code == 200
        assert resp.json()["name"] == "甲"

        resp = client.delete("/api/company/employees/e1")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["purged"] is True
        assert payload["tombstone"]["employee_id"] == "e1"
        assert client.get("/api/company/employees/e1").status_code == 404

    def test_get_missing_404_and_bad_create_400(self) -> None:
        resp = client.get("/api/company/employees/nope")
        assert resp.status_code == 404

        resp = client.post("/api/company/employees", json={"name": "缺 id"})
        assert resp.status_code == 400


class TestOrgRolesChart:
    def test_roles_and_chart(self) -> None:
        resp = client.get("/api/company/roles")
        assert resp.status_code == 200
        # builtin roles 已注册（worker 为 dispatcher 装配最小集）
        role_ids = {r["id"] for r in resp.json()["roles"]}
        assert "worker" in role_ids

        resp = client.get("/api/company/org-chart")
        assert resp.status_code == 200
        assert isinstance(resp.json()["chart"], list)

    def test_create_role_persisted(self) -> None:
        resp = client.post(
            "/api/company/roles",
            json={"id": "ops", "name": "运维", "reports_to": "worker"},
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == "ops"

        # org.json 落盘（API 装配持久化路径）
        org_file = Path.home() / ".bandi" / "company" / "org.json"
        assert org_file.exists(), "角色创建必须持久化 org.json"
        assert "ops" in org_file.read_text(encoding="utf-8")


class TestWorkItems:
    def test_list_and_get(self) -> None:
        _dispatcher()  # 装配冒烟：TalentMarket 懒挂不炸
        from bandi.company.store import WorkItem  # noqa: F401  仅验证装配可导入

        resp = client.get("/api/company/work-items")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)
        assert "items" in resp.json()


class TestWorkItemTimeSerialization:
    """看板时间必须带时区偏移：naive 按本地口径解释，aware 按其偏移换算到本地。"""

    @staticmethod
    def _payload(value: object) -> dict[str, object]:
        from bandi.api.app import _work_item_to_dict
        from bandi.company.phase import Phase
        from bandi.company.work_item import WorkItem, WorkItemTurnType

        item = WorkItem(
            id="wi-time",
            title="时间口径",
            description="",
            phase=Phase.QUEUED,
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="worker",
            parent_id=None,
            children_ids=[],
            links={},
            metadata={},
            runtime_metadata={},
            created_at=value,
            updated_at=value,
        )
        return _work_item_to_dict(item)

    def test_time_carries_utc_offset(self) -> None:
        import re
        from datetime import UTC, datetime

        payload = self._payload(datetime(2026, 10, 27, 15, 35, tzinfo=UTC))
        assert re.search(r"[+-]\d{2}:\d{2}$", str(payload["created_at"]))
        assert re.search(r"[+-]\d{2}:\d{2}$", str(payload["updated_at"]))

    def test_naive_and_aware_same_instant_serialize_equal(self) -> None:
        from datetime import datetime

        naive_local = datetime(2026, 10, 27, 23, 35, 12)
        aware_same = naive_local.astimezone()
        assert (
            self._payload(naive_local)["updated_at"] == self._payload(aware_same)["updated_at"]
        ), "naive（本地口径）与同一时刻的 aware 值必须序列化成同一时间串"


class TestTalentMarket:
    def test_catalog_and_templates(self) -> None:
        resp = client.get("/api/company/talent/catalog")
        assert resp.status_code == 200
        assert isinstance(resp.json()["catalog"], list)
        assert resp.json()["catalog"], "人才市场内置分类目录非空"

        resp = client.get("/api/company/talent/templates")
        assert resp.status_code == 200
        assert isinstance(resp.json()["templates"], list)


class TestInbox:
    def test_inbox_read(self) -> None:
        resp = client.get("/api/company/inbox", params={"role_id": "manager"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestEmployeeUpdate:
    def test_put_updates_fields(self) -> None:
        client.post(
            "/api/company/employees",
            json={"employee_id": "w2", "name": "乙", "role_id": "worker"},
        )
        resp = client.put(
            "/api/company/employees/w2",
            json={"name": "乙二号", "description": "更新后描述", "status": "paused"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "乙二号"
        assert data["status"] == "paused"

        resp = client.get("/api/company/employees/w2")
        assert resp.json()["description"] == "更新后描述"

    def test_put_missing_404(self) -> None:
        resp = client.put("/api/company/employees/none", json={"name": "x"})
        assert resp.status_code == 404


class TestWorkItemsFilter:
    def test_filter_by_phase(self) -> None:
        resp = client.get("/api/company/work-items", params={"phase": "queued"})
        assert resp.status_code == 200
        assert isinstance(resp.json()["items"], list)

    def test_filter_invalid_phase_400(self) -> None:
        resp = client.get("/api/company/work-items", params={"phase": "BOGUS"})
        assert resp.status_code == 400

    def test_filter_invalid_column_400(self) -> None:
        resp = client.get("/api/company/work-items", params={"column": "BOGUS"})
        assert resp.status_code == 400

    def test_combined_filters_are_intersection(self) -> None:
        """phase+column+role 同时传时取交集，非优先级丢弃。"""
        resp = client.get(
            "/api/company/work-items",
            params={"phase": "queued", "column": "todo", "role": "worker"},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        for it in items:
            assert it["phase"] == "queued"
            assert it["column"] == "todo"
            assert it["assignee_role"] == "worker"

    def test_column_counts_reflect_filters(self) -> None:
        """column_counts 属过滤集而非全库（过滤后计数与条目列一致）。"""
        resp = client.get("/api/company/work-items", params={"column": "done"})
        assert resp.status_code == 200
        body = resp.json()
        items = body["items"]
        counts = body["column_counts"]
        assert counts["todo"] == 0
        assert counts["in_progress"] == 0
        assert counts["in_review"] == 0
        total = sum(counts.values())
        assert total == len(items)
        for it in items:
            assert it["column"] == "done"


class TestTalentHire:
    def test_template_detail_and_hire(self) -> None:
        listing = client.get("/api/company/talent/templates").json()["templates"]
        assert listing, "内置人才模板非空"
        name = listing[0]["name"]

        resp = client.get(f"/api/company/talent/templates/{name}")
        assert resp.status_code == 200
        assert resp.json()["name"] == name

        resp = client.post(
            "/api/company/talent/hire",
            json={"template_name": name, "employee_id": "hired-1", "role_id": "worker"},
        )
        assert resp.status_code == 200
        assert resp.json()["employee_id"] == "hired-1"
        assert resp.json()["role_id"] == "worker"


class TestApiDispatcherPersistCallable:
    def test_org_change_auto_persists_to_org_json(self, tmp_path: Path) -> None:
        """API _get_company_dispatcher 装配后，org 变更必须自动落盘 org.json
        ——register_* 触发 _persist_callable（与 CLI cli/company.py 装配一致）
        不得依赖端点各自显式 save_org（易漏）。"""
        import json

        from bandi.company.org_engine import OrgEmployee

        disp = _dispatcher()
        disp.org_engine.register_employee(
            OrgEmployee(
                employee_id="auto-persist-1",
                name="Auto Persist",
                role_id="worker",
            )
        )
        org_json = tmp_path / ".bandi" / "company" / "org.json"
        assert org_json.exists(), "装配后的 org 变更必须自动写 org.json（AR-14）"
        payload = json.loads(org_json.read_text(encoding="utf-8"))
        employee_ids = [e["employee_id"] for e in payload.get("employees", [])]
        assert "auto-persist-1" in employee_ids, f"org.json 必须含新员工: {payload}"
