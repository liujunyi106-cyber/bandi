# 功能：团队模板 API 契约——/api/company/teams 列表/详情/实例化/导入导出。
# 钉行为不钉实现：列表含内置 / 详情含角色集 / 实例化落组织（前缀角色可查回）/
# 导出→导入跨库往返 / 重复导入 409 / 未知模板 404。
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """company 单例隔离：Path.home→tmp + HOME/USERPROFILE env 同步（expanduser 口径）
    + 重置 dispatcher/团队库（每测试重建装配）。"""
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Path.home patch 不覆盖 os.path.expanduser("~")（走 HOME/USERPROFILE env）——
    # 模板库/profiles 默认路径经 expanduser 解析，不隔离 env 会写真实用户目录
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
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


class TestTeamApi:
    def test_list_and_detail(self) -> None:
        resp = client.get("/api/company/teams")
        assert resp.status_code == 200
        ids = {t["template_id"] for t in resp.json()["teams"]}
        assert "selection-team" in ids
        assert "startup-studio" in ids

        resp = client.get("/api/company/teams/selection-team")
        assert resp.status_code == 200
        team = resp.json()["team"]
        assert team["roles"], "详情必须含角色集"

        resp = client.get("/api/company/teams/ghost-team")
        assert resp.status_code == 404

    def test_instantiate_registers_org(self) -> None:
        resp = client.post("/api/company/teams/selection-team/instantiate", json={})
        assert resp.status_code == 200
        report = resp.json()["report"]
        assert report["team_id"] == "selection-team"
        assert report["created_role_ids"], "实例化必须创建角色"

        resp = client.get("/api/company/roles")
        assert resp.status_code == 200
        role_ids = {r["id"] for r in resp.json()["roles"]}
        assert "selection-team_selection_lead" in role_ids, "实例化角色必须可经 API 查回"

    def test_instantiate_persists_seats_to_org_json(self, tmp_path: Path) -> None:
        """席位必须随实例化落盘 org.json（否则重启后团队卡不可 claim）。"""
        resp = client.post("/api/company/teams/selection-team/instantiate", json={})
        assert resp.status_code == 200, resp.text

        org_json = tmp_path / ".bandi" / "company" / "org.json"
        assert org_json.is_file()
        payload = json.loads(org_json.read_text(encoding="utf-8"))
        persisted_seats = set(payload.get("seats", {}))
        assert "selection-team_selection_lead" in persisted_seats, (
            f"API 实例化必须落盘席位，实际 {persisted_seats}"
        )

    def test_instantiate_invalid_team_id_400(self) -> None:
        """破坏者：body.team_id 非法（空白）→ 400（不得 500 裸崩）。"""
        resp = client.post("/api/company/teams/selection-team/instantiate", json={"team_id": "a b"})
        assert resp.status_code == 400

    def test_export_import_roundtrip(self, tmp_path: Path) -> None:
        out = tmp_path / "share" / "sel.json"
        resp = client.post("/api/company/teams/selection-team/export", json={"out_path": str(out)})
        assert resp.status_code == 200, resp.text
        assert out.is_file()

        resp = client.post("/api/company/teams/import", json={"path": str(out)})
        assert resp.status_code == 200, resp.text

        resp2 = client.post("/api/company/teams/import", json={"path": str(out)})
        assert resp2.status_code == 409, "重复导入无 overwrite 必须拒绝"

    def test_unknown_template_instantiate_404(self) -> None:
        resp = client.post("/api/company/teams/ghost/instantiate", json={})
        assert resp.status_code == 404
