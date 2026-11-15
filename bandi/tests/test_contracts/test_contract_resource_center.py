# 功能：契约——资源中心（技能 / SOP / 知识文档 / MCP 连接器）的列表、详情、保存、启停、卸载与上传。
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

HEADERS = {"Authorization": "Bearer test"}


@pytest.fixture
def app_mod(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    module = importlib.import_module("bandi.api.app")
    monkeypatch.setattr(module, "_API_KEY", "test")
    import bandi.knowledge.store as knowledge_store
    import bandi.llm.models_dev as models_dev

    monkeypatch.setattr(models_dev, "trigger_models_dev_sync", lambda: None)
    module._knowledge_store = None
    module._company_dispatcher = None
    knowledge_store._SHARED_STORE = None
    yield module
    module._knowledge_store = None
    module._company_dispatcher = None
    knowledge_store._SHARED_STORE = None


def _client(app_mod: Any) -> TestClient:
    return TestClient(app_mod.app, headers=HEADERS)


def _write_skill(
    home: Path, name: str, *, enabled: bool = True, description: str = "技能说明"
) -> Path:
    skill_dir = home / ".bandi" / "skills" / name
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    enabled_line = "true" if enabled else "false"
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"enabled: {enabled_line}\n"
        "---\n\n"
        f"# {name}\n\n正文内容。\n",
        encoding="utf-8",
    )
    return path


def _write_card(home: Path, skill_id: str, *, enabled: bool = True) -> Path:
    path = home / ".bandi" / "skills" / f"{skill_id}.card.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "skill_id": skill_id,
                "name": f"{skill_id} 名称",
                "version": "1.0.0",
                "description": "SOP 说明",
                "enabled": enabled,
                "goal": ["目标一"],
                "nodes": [
                    {"node_id": "start", "name": "开始", "instruction": "第一步"},
                    {"node_id": "end", "name": "结束", "instruction": "最后一步"},
                ],
                "edges": [{"source_node_id": "start", "next_node_id": "end"}],
                "start_node_id": "start",
                "terminal_node_ids": ["end"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def test_contract_resource_doc_list_does_not_create_source_dirs(
    app_mod: Any, tmp_path: Path
) -> None:
    """旧知识文档面已下线：list 返回 400，且不创建源文件目录/不报 500。"""
    store = app_mod._get_knowledge_store()
    store.ensure_base("shared")
    store.ingest_document(base_id="shared", filename="legacy.md", text="# Legacy\n\n内容")
    docs_dir = tmp_path / ".bandi" / "knowledge" / "docs"
    assert not docs_dir.exists()
    client = _client(app_mod)
    resp = client.get("/api/resources?kind=doc")
    assert resp.status_code == 400, resp.text
    assert not docs_dir.exists()


def test_contract_resource_open_folder(
    app_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """打开文件夹只接受已解析资源目录，并把真实路径交给系统文件管理器。"""
    _write_skill(tmp_path, "skill-folder")
    opened: list[str] = []
    monkeypatch.setattr(app_mod, "_open_local_folder", lambda path: opened.append(path))
    client = _client(app_mod)
    resp = client.post("/api/resources/skill/skill-folder/open-folder")
    assert resp.status_code == 200, resp.text
    assert opened and opened[0].endswith("skill-folder")
    assert resp.json()["opened"] == opened[0]


def test_contract_resource_lists_and_toggles(app_mod: Any, tmp_path: Path) -> None:
    """列表返回禁用项；启停写回文件且装载器不再返回禁用技能。"""
    from bandi.skills.loader import SkillLoader

    _write_skill(tmp_path, "skill-on", enabled=True)
    skill_off = _write_skill(tmp_path, "skill-off", enabled=False, description="禁用技能")
    card_off = _write_card(tmp_path, "sop-off", enabled=False)
    client = _client(app_mod)

    skills = client.get("/api/resources?kind=skill")
    assert skills.status_code == 200, skills.text
    items = {item["id"]: item for item in skills.json()["items"]}
    assert items["skill-on"]["enabled"] is True
    assert items["skill-off"]["enabled"] is False

    sops = client.get("/api/resources?kind=sop")
    assert sops.status_code == 200, sops.text
    sop_items = {item["id"]: item for item in sops.json()["items"]}
    assert sop_items["sop-off"]["enabled"] is False

    loader = SkillLoader(str(tmp_path / ".bandi" / "skills"))
    loaded_names = {skill.name for skill in loader.load_all()}
    assert "skill-on" in loaded_names
    assert "skill-off" not in loaded_names
    assert "sop-off" not in loaded_names

    resp = client.patch("/api/resources/skill/skill-off", json={"enabled": True})
    assert resp.status_code == 200, resp.text
    assert "enabled: true" in skill_off.read_text(encoding="utf-8")
    assert "skill-off" in {skill.name for skill in loader.load_all()}

    resp = client.patch("/api/resources/sop/sop-off", json={"enabled": True})
    assert resp.status_code == 200, resp.text
    assert json.loads(card_off.read_text(encoding="utf-8"))["enabled"] is True

    assert client.patch("/api/resources/doc/whatever", json={"enabled": True}).status_code == 400


def test_contract_resource_detail_save_and_uninstall_archive(app_mod: Any, tmp_path: Path) -> None:
    """保存写回文件；卸载移入归档目录并解除员工绑定，不删除本地内容。"""
    from bandi.company.agent_profile import AgentProfileManager
    from bandi.skills.loader import SkillLoader

    skill_path = _write_skill(tmp_path, "skill-save", description="旧说明")
    manager = AgentProfileManager(
        profiles_dir=str(tmp_path / ".bandi" / "agents" / "profiles"),
        skills_dir=str(tmp_path / ".bandi" / "skills"),
    )
    manager.create_profile("emp-1", name="员工一")
    manager.bind_resource("emp-1", "general_skill", "skill-save")
    client = _client(app_mod)

    detail = client.get("/api/resources/skill/skill-save")
    assert detail.status_code == 200, detail.text
    assert "旧说明" in detail.json()["content"]

    new_content = detail.json()["content"].replace("旧说明", "新说明")
    saved = client.put("/api/resources/skill/skill-save", json={"content": new_content})
    assert saved.status_code == 200, saved.text
    assert "新说明" in skill_path.read_text(encoding="utf-8")
    loaded = SkillLoader(str(tmp_path / ".bandi" / "skills")).load(str(skill_path))
    assert loaded is not None and loaded.description == "新说明"

    removed = client.delete("/api/resources/skill/skill-save")
    assert removed.status_code == 200, removed.text
    assert not skill_path.parent.exists()
    archived = list((tmp_path / ".bandi" / "_uninstalled" / "skills").glob("skill-save-*"))
    assert archived, "卸载后技能目录应移入归档"
    assert (archived[0] / "SKILL.md").read_text(encoding="utf-8") == new_content
    profile = manager.get_profile("emp-1")
    assert profile is not None
    assert all(item.resource_id != "skill-save" for item in profile.resources)


def test_contract_knowledge_document_endpoints_offline(app_mod: Any, tmp_path: Path) -> None:
    """旧 /api/resources?kind=doc 系列下线——列表/详情/上传/删除一律 400，索引数据不动。"""
    store = app_mod._get_knowledge_store()
    store.ensure_base("shared")
    result = store.ingest_document(
        base_id="shared", filename="品牌指南.md", text="# 品牌指南\n\n保持克制。"
    )
    client = _client(app_mod)
    assert client.get("/api/resources?kind=doc").status_code == 400
    assert client.get(f"/api/resources/doc/{result.document_id}").status_code == 400
    assert client.delete(f"/api/resources/doc/{result.document_id}").status_code == 400
    upload = client.post(
        "/api/resources/upload?kind=doc",
        files={"file": ("品牌指南.md", "# 重复\n", "text/markdown")},
    )
    assert upload.status_code == 400
    assert store.get_document(result.document_id) is not None


def test_contract_resource_create_sop(app_mod: Any, tmp_path: Path) -> None:
    """新建 SOP：合法卡落盘、重复 409、路径穿越 skill_id 400。"""
    client = _client(app_mod)
    card = {
        "skill_id": "new-sop",
        "name": "新建 SOP",
        "version": "1.0.0",
        "description": "从空白开始",
        "nodes": [
            {"node_id": "start", "name": "开始", "type": "start"},
            {"node_id": "end", "name": "结束", "type": "terminal"},
        ],
        "edges": [{"source_node_id": "start", "next_node_id": "end"}],
        "start_node_id": "start",
        "terminal_node_ids": ["end"],
    }
    resp = client.post("/api/resources/sop", json={"content": json.dumps(card, ensure_ascii=False)})
    assert resp.status_code == 200, resp.text
    item = resp.json()["item"]
    assert item["id"] == "new-sop"
    assert (tmp_path / ".bandi" / "skills" / "new-sop.card.json").is_file()
    listed = client.get("/api/resources?kind=sop").json()["items"]
    assert [entry["id"] for entry in listed] == ["new-sop"]

    duplicate = client.post(
        "/api/resources/sop", json={"content": json.dumps(card, ensure_ascii=False)}
    )
    assert duplicate.status_code == 409, duplicate.text

    traversal = dict(card, skill_id="../escape")
    bad = client.post(
        "/api/resources/sop", json={"content": json.dumps(traversal, ensure_ascii=False)}
    )
    assert bad.status_code == 400, bad.text
    assert not (tmp_path / ".bandi" / "escape.card.json").exists()


def test_contract_mcp_config_json_roundtrip(app_mod: Any, tmp_path: Path) -> None:
    """MCP 配置以 mcp.json 为准；非法 JSON fail-closed；保存后运行配置可见。"""
    client = _client(app_mod)
    resp = client.put(
        "/api/mcp/config",
        json={
            "mcpServers": {
                "demo": {
                    "command": "npx",
                    "args": ["-y", "demo-mcp"],
                    "env": {"TOKEN": "x"},
                    "enabled": True,
                }
            }
        },
    )
    assert resp.status_code == 200, resp.text
    mcp_file = tmp_path / ".bandi" / "mcp.json"
    assert mcp_file.is_file()
    payload = json.loads(mcp_file.read_text(encoding="utf-8"))
    assert payload["mcpServers"]["demo"]["command"] == "npx"

    from bandi.core.config import BandiConfig

    servers = BandiConfig.load().tools.mcp_servers
    assert [server.name for server in servers] == ["demo"]
    assert servers[0].env == {"TOKEN": "x"}

    config = client.get("/api/mcp/config")
    assert config.status_code == 200, config.text
    assert "demo" in config.json()["content"]["mcpServers"]

    patched = client.patch("/api/mcp/servers/demo", json={"enabled": False})
    assert patched.status_code == 200, patched.text
    assert (
        json.loads(mcp_file.read_text(encoding="utf-8"))["mcpServers"]["demo"]["enabled"] is False
    )

    invalid = client.put("/api/mcp/config", json={"mcpServers": {"bad": {"args": []}}})
    assert invalid.status_code == 400
    assert (
        json.loads(mcp_file.read_text(encoding="utf-8"))["mcpServers"]["demo"]["enabled"] is False
    )
