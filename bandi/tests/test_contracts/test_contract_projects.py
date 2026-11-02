# 功能：契约——项目协作后端（ProjectStore：CRUD/原子写/路径安全 + 团队拉入校验
# + 成员动态解析）。
# 钉行为不钉实现：create→get 往返逐字（中文）/ 时间戳语义 / create 重复拒绝且原文件
# 共享引用（同团队多项目、员工不复制）/ 拉入幂等 / 未组建（NotReady）与不存在
# （NotFound）区分（破坏者）/ 成员动态解析（org 变更即反映，不落盘快照）/
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.company.org_engine import OrgEmployee, OrgEngine
from bandi.company.projects import (
    ProjectError,
    ProjectExistsError,
    ProjectLoadError,
    ProjectNotFoundError,
    ProjectRecord,
    ProjectStore,
    ProjectTeamNotFoundError,
    ProjectTeamNotReadyError,
)


def _store(tmp_path: Path, name: str = "projects") -> ProjectStore:
    return ProjectStore(projects_dir=str(tmp_path / name))


def _org_with_team(team_id: str = "sel-team", role_id: str = "sel-team_lead") -> OrgEngine:
    """功能：真实 OrgEngine + 一个归属 team_id 的角色与 1:1 员工（已组建形态）。"""
    org = OrgEngine()
    org.register_role(role_id, "选品负责人", "负责选品", None, [], {}, team_id=team_id)
    org.register_employee(OrgEmployee(employee_id=role_id, name="选品负责人", role_id=role_id))
    return org


# ── store 族（CRUD / 时间戳 / 原子性）────────────────────────────────────────


def test_s1_create_get_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create_project("p-alpha", "选品项目", root_dir="D:/work/alpha")
    assert store.project_path("p-alpha").is_file()
    loaded = store.get_project("p-alpha")
    assert loaded is not None
    assert loaded.model_dump() == created.model_dump()
    assert loaded.name == "选品项目"
    assert loaded.root_dir == "D:/work/alpha"
    assert loaded.team_ids == []


def test_s2_timestamp_semantics(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create_project("p-alpha", "选品项目")
    assert created.created_at
    assert created.updated_at
    again = store.save_project(created.model_copy(update={"name": "改名后"}))
    assert again.created_at == created.created_at
    assert again.updated_at >= created.created_at
    assert store.get_project("p-alpha").name == "改名后"


def test_s3_create_duplicate_rejected_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_project("p-alpha", "原版")
    path = tmp_path / "projects" / "p-alpha.json"
    original_text = path.read_text(encoding="utf-8")
    with pytest.raises(ProjectExistsError):
        store.create_project("p-alpha", "覆盖版")
    assert path.read_text(encoding="utf-8") == original_text
    assert store.get_project("p-alpha").name == "原版"


def test_s4_delete_three_states(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_project("p-alpha", "选品项目")
    path = store.project_path("p-alpha")
    assert store.delete_project("p-alpha") is True
    assert not path.exists()
    assert store.delete_project("p-alpha") is False
    assert store.get_project("p-alpha") is None


def test_s5_traversal_rejected_zero_residue(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ProjectError):
        store.create_project("../evil", "穿越项目")
    evil = ProjectRecord.model_construct(
        project_id="../evil",
        name="x",
        root_dir="",
        team_ids=[],
        created_at="",
        updated_at="",
    )
    with pytest.raises(ProjectError):
        store.save_project(evil)
    with pytest.raises(ProjectError):
        store.get_project("../evil")
    with pytest.raises(ProjectError):
        store.delete_project("../evil")
    assert not (tmp_path / "evil.json").exists()
    assert not (tmp_path / "projects" / "evil.json").exists()
    assert list((tmp_path / "projects").glob("*")) == []


def test_s6_bad_json_distinguishes_missing_and_list_skips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_project("p-alpha", "好项目")
    (tmp_path / "projects" / "broken.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(ProjectLoadError) as excinfo:
        store.get_project("broken")
    assert "非法" in str(excinfo.value)
    assert store.get_project("ghost") is None

    ids = {p.project_id for p in store.list_projects()}
    assert "p-alpha" in ids
    assert "broken" not in ids


def test_s7_non_utf8_maps_to_load_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_project("p-alpha", "选品项目")
    (tmp_path / "projects" / "p-alpha.json").write_bytes(b"\xff\xfe{\x00")
    with pytest.raises(ProjectLoadError):
        store.get_project("p-alpha")


def test_s8_save_revalidates_mutated_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_project("p-alpha", "选品项目")
    broken = record.model_copy(update={"name": "   "})
    with pytest.raises(ProjectError):
        store.save_project(broken)
    assert store.get_project("p-alpha").name == "选品项目"


# ── 共享引用族───────────────────────────────────────────────────


def test_s9_shared_team_across_projects(tmp_path: Path) -> None:
    store = _store(tmp_path)
    org = _org_with_team()
    store.create_project("p-alpha", "项目甲")
    store.create_project("p-beta", "项目乙")
    employees_before = {e.employee_id for e in org.list_employees(include_inactive=True)}

    first = store.add_team("p-alpha", "sel-team", org)
    second = store.add_team("p-beta", "sel-team", org)
    assert first.team_ids == ["sel-team"]
    assert second.team_ids == ["sel-team"]
    assert store.get_project("p-alpha").team_ids == ["sel-team"]
    assert store.get_project("p-beta").team_ids == ["sel-team"]
    assert {e.employee_id for e in org.list_employees(include_inactive=True)} == employees_before, (
        "共享引用不得复制员工"
    )


def test_s10_add_team_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    org = _org_with_team()
    store.create_project("p-alpha", "选品项目")
    first = store.add_team("p-alpha", "sel-team", org)
    again = store.add_team("p-alpha", "sel-team", org)
    assert again.team_ids == first.team_ids == ["sel-team"]
    assert store.get_project("p-alpha").team_ids == ["sel-team"]


# ── 校验族（NotReady vs NotFound 区分）───────────────────────────────────────


def test_s11_unknown_team_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    org = OrgEngine()
    store.create_project("p-alpha", "选品项目")
    with pytest.raises(ProjectTeamNotFoundError):
        store.add_team("p-alpha", "ghost-team", org)


def test_s12_template_exists_not_instantiated(tmp_path: Path) -> None:
    from bandi.company.team_template_store import TeamTemplateStore

    store = _store(tmp_path)
    org = OrgEngine()
    store.create_project("p-alpha", "选品项目")
    with pytest.raises(ProjectTeamNotReadyError) as excinfo:
        store.add_team(
            "p-alpha",
            "selection-team",
            org,
            template_store=TeamTemplateStore(templates_dir=str(tmp_path / "teams")),
        )
    assert "尚未组建" in str(excinfo.value)
    assert store.get_project("p-alpha").team_ids == []


def test_s13_missing_project_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    org = _org_with_team()
    with pytest.raises(ProjectNotFoundError):
        store.add_team("ghost-project", "sel-team", org)
    with pytest.raises(ProjectNotFoundError):
        store.resolve_members("ghost-project", org)


# ── 成员解析族（动态，不落盘快照）────────────────────────────────────────────


def test_s14_members_dynamic_not_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    org = _org_with_team()
    store.create_project("p-alpha", "选品项目")
    store.add_team("p-alpha", "sel-team", org)

    members = store.resolve_members("p-alpha", org)
    assert [(m.role_id, m.employee_id) for m in members] == [("sel-team_lead", "sel-team_lead")]
    assert members[0].role_name == "选品负责人"

    org.register_role("sel-team_worker", "执行", "跑流程", None, [], {}, team_id="sel-team")
    org.register_employee(
        OrgEmployee(employee_id="sel-team_worker", name="执行", role_id="sel-team_worker")
    )
    members = store.resolve_members("p-alpha", org)
    assert {m.role_id for m in members} == {"sel-team_lead", "sel-team_worker"}

    del org._roles["sel-team_worker"]  # 组织变更（模拟移除角色）
    members = store.resolve_members("p-alpha", org)
    assert {m.role_id for m in members} == {"sel-team_lead"}, "成员必须动态解析，不落盘快照"


def test_s15_multi_team_union_and_orphan_role(tmp_path: Path) -> None:
    store = _store(tmp_path)
    org = _org_with_team("sel-team", "sel-team_lead")
    org.register_role("dev-team_lead", "开发负责人", "", None, [], {}, team_id="dev-team")
    store.create_project("p-alpha", "选品项目")
    store.add_team("p-alpha", "sel-team", org)
    store.add_team("p-alpha", "dev-team", org)

    members = store.resolve_members("p-alpha", org)
    assert [m.team_id for m in members] == ["sel-team", "dev-team"]
    orphan = next(m for m in members if m.role_id == "dev-team_lead")
    assert orphan.employee_id == ""
    assert orphan.employee_name == ""


# ── DoD-12 完整生产链 ────────────────────────────────────────────────────────


def test_s16_full_chain_dod12(tmp_path: Path) -> None:
    store = _store(tmp_path, "home-a")
    org = _org_with_team("ship-team", "ship-team_ops")
    created = store.create_project("ship-p1", "发货项目", root_dir="D:/ship")
    assert created.created_at

    store.add_team("ship-p1", "ship-team", org)

    fresh = _store(tmp_path, "home-a")
    readback = fresh.get_project("ship-p1")
    assert readback is not None
    assert readback.model_dump() == store.get_project("ship-p1").model_dump()
    assert readback.team_ids == ["ship-team"]

    members = fresh.resolve_members("ship-p1", org)
    assert members and members[0].employee_name == "选品负责人"

    again = fresh.add_team("ship-p1", "ship-team", org)
    assert again.team_ids == ["ship-team"]


def test_s17_ensure_agents_md_lifecycle(tmp_path: Path) -> None:
    from bandi.company.projects import ensure_agents_md

    # ① 空目录 → 创建空 AGENTS.md，返回 True
    workdir = tmp_path / "proj-root"
    workdir.mkdir()
    assert ensure_agents_md(str(workdir)) is True
    agents_md = workdir / "AGENTS.md"
    assert agents_md.is_file()
    assert agents_md.read_text(encoding="utf-8") == "", "首次创建必须是空文件"

    # ② 已存在（有内容）→ 严格不覆盖，返回 False
    agents_md.write_text("# 项目规则\n\n已有内容", encoding="utf-8")
    assert ensure_agents_md(str(workdir)) is False
    assert agents_md.read_text(encoding="utf-8") == "# 项目规则\n\n已有内容"

    # ③ 无效目录（不存在 / 空串）→ False 且零副作用
    missing = tmp_path / "ghost"
    assert ensure_agents_md(str(missing)) is False
    assert not missing.exists(), "目录不存在时不得创建任何文件"
    assert ensure_agents_md("") is False

    # ④ 破坏者：root_dir 指向文件而非目录 → False 不裸崩
    a_file = tmp_path / "plain.txt"
    a_file.write_text("x", encoding="utf-8")
    assert ensure_agents_md(str(a_file)) is False
    assert not (tmp_path / "AGENTS.md").exists()
