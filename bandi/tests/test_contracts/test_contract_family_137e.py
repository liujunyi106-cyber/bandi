from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import bandi.api.app  # noqa: F401 — 确保模块加载（sys.modules 取真身）
from bandi.api.app import app
from bandi.company.attachments import AttachmentRecord

# 功能：契约——公司通道对话附件（单聊/群聊：upload_id → 文本注记拼入消息内容）
# 钉行为（不钉实现）：


app_module = sys.modules["bandi.api.app"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bandi.company.attachments import AttachmentStore

    store = AttachmentStore(base_dir=str(tmp_path / "uploads"))
    monkeypatch.setattr(app_module, "_attachment_store", store)


def _save(data: bytes, filename: str) -> AttachmentRecord:
    store = app_module._get_attachment_store()
    return store.save(data, filename=filename, project_id="p1")


class TestAttachmentNotes:
    def test_text_attachment_note(self) -> None:
        record = _save("# 标题\n内容".encode(), "notes.md")
        result = app_module._with_attachment_notes(
            "看下这个", [{"upload_id": record.upload_id, "name": "notes.md"}], "p1"
        )
        assert result.startswith("看下这个")
        assert "[附件 notes.md]" in result
        assert "# 标题" in result

    def test_pure_attachment_passes_content_check(self) -> None:
        record = _save("正文".encode(), "a.txt")
        result = app_module._with_attachment_notes(
            "", [{"upload_id": record.upload_id, "name": "a.txt"}], "p1"
        )
        assert result.strip(), "纯附件消息也必须产出非空内容（WS handler 据此放行）"

    def test_non_text_placeholder_without_binary(self) -> None:
        record = _save(b"\x00\x01\xff\xfe", "bin.dat")
        result = app_module._with_attachment_notes(
            "带个文件", [{"upload_id": record.upload_id, "name": "bin.dat"}], "p1"
        )
        assert "非文本内容" in result
        assert "\x00" not in result, "不得回传二进制内容"

    def test_invalid_upload_ids_ignored(self) -> None:
        result = app_module._with_attachment_notes(
            "你好", [{"upload_id": "ghost"}, {"bad": 1}, "junk"], "p1"
        )
        assert result == "你好"

    def test_no_attachments_unchanged(self) -> None:
        assert app_module._with_attachment_notes("hi", None, "p1") == "hi"
        assert app_module._with_attachment_notes("hi", [], "p1") == "hi"


# ==================== 137e_employee_knowledge_api ====================
# 功能：契约——员工个体知识库（私有桶：knowledge_bases.employee_id 归属 + 端点）
# 钉行为（不钉实现）：


# bandi.api.__init__ 执行 from .app import app 后包属性遮蔽子模块——须经 sys.modules 取模块真身
r1x137e_app_module = sys.modules["bandi.api.app"]
r1x137e_client = TestClient(app)
r1x137e_client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def r1x137e__isolate() -> None:
    with patch("bandi.api.app._API_KEY", "test"):
        yield


@pytest.fixture()
def knowledge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真实 tmp KnowledgeStore（员工 A/员工 B/全局三 base + 一篇 A 文档）。"""
    from bandi.knowledge.store import KnowledgeStore

    store = KnowledgeStore(db_path=str(tmp_path / "knowledge.db"))
    store.ensure_base("kb:dev", name="开发知识", employee_id="dev")
    store.ensure_base("kb:ops", name="运维知识", employee_id="ops")
    store.ensure_base("kb:global", name="全局知识")
    store.ingest_document("kb:dev", "a.md", "# 标题\n内容", title="A")
    monkeypatch.setattr(r1x137e_app_module, "_knowledge_store", store)
    yield store
    store.close()


class TestListBases:
    def test_filter_by_employee(self, knowledge) -> None:
        bases = knowledge.list_bases(employee_id="dev")
        assert [b.id for b in bases] == ["kb:dev"], "仅员工归属 base（他人员/全局不混入）"

    def test_all_bases_without_filter(self, knowledge) -> None:
        assert len(knowledge.list_bases()) == 3


class TestEmployeeKnowledgeApi:
    def test_lists_bases_with_document_count(self, knowledge) -> None:
        resp = r1x137e_client.get("/api/company/employees/dev/knowledge")
        assert resp.status_code == 200
        body = resp.json()
        assert body["employee_id"] == "dev"
        assert len(body["bases"]) == 1
        assert body["bases"][0]["base_id"] == "kb:dev"
        assert body["bases"][0]["name"] == "开发知识"
        assert body["bases"][0]["document_count"] == 1

    def test_unknown_employee_empty_list(self, knowledge) -> None:
        resp = r1x137e_client.get("/api/company/employees/ghost/knowledge")
        assert resp.status_code == 200
        assert resp.json()["bases"] == []


# ==================== 137e_employee_profile_api ====================
# 功能：契约——员工能力档案只读端点（右栏员工卡知识库/技能绑定数据源）
# 钉行为（不钉实现）：


# bandi.api.__init__ 执行 from .app import app 后包属性遮蔽子模块——须经 sys.modules 取模块真身
r2x137e_app_module = sys.modules["bandi.api.app"]
r2x137e_client = TestClient(app)
r2x137e_client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def r2x137e__isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 默认档案目录 "~/.bandi/..." 经 Path.expanduser 解析（读 HOME/USERPROFILE 环境变量）——
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with patch("bandi.api.app._API_KEY", "test"):
        yield


def _write_profile(home: Path, employee_id: str, resources: list[dict]) -> Path:
    """写入真实档案文件（经 AgentProfile 模型 dump——读回走真实 Schema 校验路径）。"""
    from bandi.company.agent_profile import AgentProfile

    profile = AgentProfile(
        profile_id=employee_id,
        employee_id=employee_id,
        name="客服",
        status="active",
        resources=resources,
        created_at="T00:00:00+00:00",
        updated_at="T00:00:00+00:00",
    )
    path = home / ".bandi" / "agents" / "profiles" / f"{employee_id}.profile.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile.model_dump_json(), encoding="utf-8")
    return path


class TestEmployeeProfile:
    def test_profile_resources_roundtrip(self, tmp_path: Path) -> None:
        _write_profile(
            tmp_path,
            "api-team_customer_service",
            [
                {
                    "resource_type": "knowledge_base",
                    "resource_id": "kb-main",
                    "status": "active",
                    "created_at": "T00:00:00+00:00",
                    "updated_at": "T00:00:00+00:00",
                }
            ],
        )
        resp = r2x137e_client.get("/api/company/employees/api-team_customer_service/profile")
        assert resp.status_code == 200
        profile = resp.json()["profile"]
        assert profile["employee_id"] == "api-team_customer_service"
        assert profile["resources"][0]["resource_type"] == "knowledge_base"
        assert profile["resources"][0]["resource_id"] == "kb-main"

    def test_missing_profile_404(self) -> None:
        resp = r2x137e_client.get("/api/company/employees/no-such-employee/profile")
        assert resp.status_code == 404

    def test_employee_create_bootstraps_profile(self) -> None:
        employee_id = f"profile-auto-{uuid4().hex[:8]}"
        created = r2x137e_client.post(
            "/api/company/employees",
            json={"employee_id": employee_id, "name": "自动档案员工", "role_id": ""},
        )
        assert created.status_code == 200
        profile = r2x137e_client.get(f"/api/company/employees/{employee_id}/profile")
        assert profile.status_code == 200
        assert profile.json()["profile"]["employee_id"] == employee_id

    def test_traversal_id_rejected(self) -> None:
        # 反斜杠穿越解码后应被 path_guard 拒绝（400）；若被路由层归一（404）同样 fail-closed
        resp = r2x137e_client.get("/api/company/employees/..%5C..%5Csecret/profile")
        assert resp.status_code in (400, 404), "越界 id 必须 fail-closed（不得 200/500）"
        assert resp.status_code != 200

    def test_default_dir_follows_home(self, tmp_path: Path) -> None:
        # 隔离性反证：档案写 tmp home 可读；不在 tmp 的真实 home 档案不可见
        _write_profile(tmp_path, "tmp-emp", [])
        assert r2x137e_client.get("/api/company/employees/tmp-emp/profile").status_code == 200, (
            "tmp home 档案必须可读（隔离生效）"
        )
