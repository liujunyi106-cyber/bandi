# 功能：契约——项目协作 API（api/company/projects：list/create/拉入团队 +
# /api/company/inbox?project_id= 按项目读取）。
# 钉行为不钉实现：创建→列表含动态 members / 重复创建 409 / 非法 project_id 400 不裸崩 /
# 拉入成功落盘读回 / 项目缺失 404 / 团队不存在 404 / 模板存在未组建 400（破坏者）/
# 拉入幂等 / inbox 项目不存在 404 / 真实消息读回（message_id 非空 + 中文 content
# 在 teams·inbox 两路径均 400 不 500。
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
    """company 单例隔离：Path.home→tmp + HOME/USERPROFILE env 同步 + 重置
    dispatcher/团队库/项目库（每测试重建装配）。"""
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._company_dispatcher", None),
        patch("bandi.api.app._custom_runtime_runner", None),
        patch("bandi.api.app._team_store", None),
        patch("bandi.api.app._project_store", None),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


def _instantiate_selection_team() -> None:
    resp = client.post("/api/company/teams/selection-team/instantiate", json={})
    assert resp.status_code == 200, resp.text


def _create_project(project_id: str = "p-alpha", name: str = "选品项目") -> dict:
    resp = client.post("/api/company/projects", json={"project_id": project_id, "name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["project"]


class TestProjectApi:
    def test_empty_list_then_create_and_members(self) -> None:
        resp = client.get("/api/company/projects")
        assert resp.status_code == 200
        assert resp.json()["projects"] == []

        _instantiate_selection_team()
        created = _create_project()
        assert created["project_id"] == "p-alpha"
        assert created["team_ids"] == []

        resp = client.post(
            "/api/company/projects/p-alpha/teams", json={"team_id": "selection-team"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["project"]["team_ids"] == ["selection-team"]

        resp = client.get("/api/company/projects")
        projects = resp.json()["projects"]
        assert [p["project_id"] for p in projects] == ["p-alpha"]
        members = projects[0]["members"]
        role_ids = {m["role_id"] for m in members}
        assert "selection-team_selection_lead" in role_ids, (
            f"列表必须含动态解析成员，实际 {role_ids}"
        )
        assert all(m["team_id"] == "selection-team" for m in members)

    def test_create_duplicate_409_and_invalid_400(self) -> None:
        _create_project()
        resp = client.post(
            "/api/company/projects", json={"project_id": "p-alpha", "name": "覆盖版"}
        )
        assert resp.status_code == 409

        resp = client.post("/api/company/projects", json={"project_id": "a b", "name": "非法"})
        assert resp.status_code == 400, "非法 project_id 必须 400 不裸崩"

        resp = client.post("/api/company/projects", json={"project_id": "p-x", "name": "  "})
        assert resp.status_code == 400, "空 name 必须 400"

    def test_add_team_errors_and_idempotent(self) -> None:
        _instantiate_selection_team()
        _create_project()

        resp = client.post(
            "/api/company/projects/ghost-project/teams", json={"team_id": "selection-team"}
        )
        assert resp.status_code == 404, "项目不存在 404"

        resp = client.post("/api/company/projects/p-alpha/teams", json={"team_id": "ghost-team"})
        assert resp.status_code == 404, "团队不存在 404"

        resp = client.post(
            "/api/company/projects/p-alpha/teams", json={"team_id": "startup-studio"}
        )
        assert resp.status_code == 400, "模板存在但未组建必须 400（NotReady）"

        resp = client.post(
            "/api/company/projects/p-alpha/teams", json={"team_id": "selection-team"}
        )
        assert resp.status_code == 200
        resp = client.post(
            "/api/company/projects/p-alpha/teams", json={"team_id": "selection-team"}
        )
        assert resp.status_code == 200
        assert resp.json()["project"]["team_ids"] == ["selection-team"], "拉入幂等不重复"

    def test_breaker_invalid_project_id_not_500(self) -> None:
        resp = client.post("/api/company/projects/a b/teams", json={"team_id": "selection-team"})
        assert resp.status_code == 400, "非法 project_id 在拉入路径必须 400 不 500"
        resp = client.get("/api/company/inbox", params={"role_id": "x", "project_id": "a b"})
        assert resp.status_code == 400, "非法 project_id 在 inbox 路径必须 400 不 500"


class TestTeamReadyAgainstStaleOrgView:
    """生产主链路：团队由他进程（CLI / 另一后端 / 桌面壳）实例化落盘后
    本进程读面必须能判「已组建」——否则拉入项目被误拒（团队明明在 org.json 里）。"""

    def _org_path(self) -> Path:
        return Path.home() / ".bandi" / "company" / "org.json"

    def _roles_ids(self) -> set[str]:
        resp = client.get("/api/company/roles")
        assert resp.status_code == 200, resp.text
        return {r["id"] for r in resp.json()["roles"]}

    def _instantiate_team_in_other_process(self, team_id: str) -> None:
        from bandi.company.org_engine import OrgEmployee, OrgEngine
        from bandi.company.org_persistence import save_org
        from bandi.company.recruiter import Recruiter

        engine, recruiter = OrgEngine(), Recruiter()
        engine.register_role(f"{team_id}_lead", "外部负责人", "", None, [], {}, team_id=team_id)
        engine.register_employee(
            OrgEmployee(
                employee_id=f"{team_id}_lead",
                name="外部负责人",
                role_id=f"{team_id}_lead",
                metadata={"team_id": team_id},
            )
        )
        save_org(engine, recruiter, self._org_path())

    def test_read_surface_picks_up_external_org_write(self) -> None:
        assert "ext-team_lead" not in self._roles_ids(), "启动基线：盘上尚无该团队"
        self._instantiate_team_in_other_process("ext-team")

        assert "ext-team_lead" in self._roles_ids(), (
            "他进程落盘的角色必须在读面可见（写前合并曾只在写路径生效，读面长期陈旧）"
        )

    def test_add_team_accepts_team_instantiated_elsewhere(self) -> None:
        _create_project(project_id="p-stale", name="陈旧读面项目")
        self._roles_ids()  # 建本进程装配基线
        self._instantiate_team_in_other_process("ext-team2")

        resp = client.post("/api/company/projects/p-stale/teams", json={"team_id": "ext-team2"})
        assert resp.status_code == 200, (
            f"团队已在他进程组建，拉入项目必须成功；实际 {resp.status_code} {resp.text}"
        )
        assert resp.json()["project"]["team_ids"] == ["ext-team2"]

        listing = client.get("/api/company/projects").json()["projects"]
        members = {m["role_id"] for m in listing[0]["members"]}
        assert "ext-team2_lead" in members, "拉入后动态成员解析必须含该团队角色"


class TestProjectInbox:
    def test_inbox_unknown_project_404(self) -> None:
        resp = client.get(
            "/api/company/inbox", params={"role_id": "manager", "project_id": "ghost"}
        )
        assert resp.status_code == 404

    def test_inbox_reads_project_messages(self, tmp_path: Path) -> None:
        """"""
        from bandi.company.comms import send_message

        _create_project()
        workspace = tmp_path / ".bandi" / "company"
        send_message(
            workspace=str(workspace),
            project_id="p-alpha",
            session_id="default",
            sender_role="user",
            recipient_role="selection-team_selection_lead",
            subject="项目消息",
            body="你好，项目群第一条消息",
        )
        resp = client.get(
            "/api/company/inbox",
            params={"role_id": "selection-team_selection_lead", "project_id": "p-alpha"},
        )
        assert resp.status_code == 200, resp.text
        messages = resp.json()
        assert len(messages) == 1
        assert messages[0]["message_id"], "message_id 不得为空（字段映射必须对齐 CommsMessage.id）"
        assert "你好" in messages[0]["content"], "content 必须为消息正文（CommsMessage.body）"

    def test_inbox_default_compat(self) -> None:
        """缺省 project_id 走 company 单例（旧行为兼容，不炸）。"""
        resp = client.get("/api/company/inbox", params={"role_id": "manager"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestProjectDeleteAndOpenFolder:
    """：项目「从列表中删除」+「打开文件夹」。"""

    def test_delete_removes_from_list_and_second_delete_404(self) -> None:
        _create_project()
        resp = client.delete("/api/company/projects/p-alpha")
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] is True
        resp = client.get("/api/company/projects")
        assert resp.json()["projects"] == [], "删除后列表不得再含该项目"
        resp = client.delete("/api/company/projects/p-alpha")
        assert resp.status_code == 404, "重复删除必须 404"

    def test_breaker_delete_missing_and_invalid_id(self) -> None:
        resp = client.delete("/api/company/projects/ghost")
        assert resp.status_code == 404
        resp = client.delete("/api/company/projects/a b")
        assert resp.status_code == 400, "非法 project_id 必须 400 不裸崩"

    def test_open_folder_errors(self, tmp_path: Path) -> None:
        _create_project()
        resp = client.post("/api/company/projects/p-alpha/open-folder")
        assert resp.status_code == 400, "未设置工作目录必须 400"
        resp = client.post(
            "/api/company/projects",
            json={"project_id": "p-beta", "name": "B", "root_dir": str(tmp_path / "missing")},
        )
        assert resp.status_code == 200
        resp = client.post("/api/company/projects/p-beta/open-folder")
        assert resp.status_code == 404, "目录不存在必须 404"
        resp = client.post("/api/company/projects/ghost/open-folder")
        assert resp.status_code == 404, "项目不存在必须 404"

    def test_open_folder_opens_existing_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "workdir"
        target.mkdir()
        resp = client.post(
            "/api/company/projects",
            json={"project_id": "p-gamma", "name": "G", "root_dir": str(target)},
        )
        assert resp.status_code == 200
        with patch("bandi.api.app._open_local_folder") as opener:
            resp = client.post("/api/company/projects/p-gamma/open-folder")
        assert resp.status_code == 200, resp.text
        assert resp.json()["opened"] == str(target)
        opener.assert_called_once_with(str(target))  # 必须把已校验目录交给系统打开器


class TestProjectFilePreview:
    def test_absolute_path_under_bandi_projects_is_previewable(self, tmp_path: Path) -> None:
        _create_project("p-preview")
        target = tmp_path / "BandiProjects" / "评测" / "out" / "长任务-经营分析.md"
        target.parent.mkdir(parents=True)
        target.write_text("# 经营分析", encoding="utf-8")

        resp = client.get(
            "/api/company/projects/p-preview/files/content",
            params={"path": str(target)},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "text"
        assert "经营分析" in body["content"]

    def test_absolute_path_outside_bandi_projects_is_rejected(self, tmp_path: Path) -> None:
        _create_project("p-preview-deny")
        outside = tmp_path / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        resp = client.get(
            "/api/company/projects/p-preview-deny/files/content",
            params={"path": str(outside)},
        )
        assert resp.status_code == 403
        assert "PATH_OUTSIDE_ROOT" in resp.text
