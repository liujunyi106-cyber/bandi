# 功能：契约——团队模板本地库（TeamTemplateStore：CRUD + 导出/导入 + 原子写）。
# 钉行为不钉实现：save→get 往返逐字、时间戳语义（save 刷新 updated_at/保留 created_at）
# export→import 跨库往返、import 已存在默认拒绝且原文件逐字不变（破坏者）
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.company.team_template import (
    TeamTemplate,
    TeamTemplateError,
    TeamTemplateLoadError,
)
from bandi.company.team_template_store import TeamTemplateStore


def _store(tmp_path: Path, name: str = "teams") -> TeamTemplateStore:
    return TeamTemplateStore(templates_dir=str(tmp_path / name))


def _template(template_id: str = "ship-team", name: str = "发货团队") -> TeamTemplate:
    return TeamTemplate(
        **{
            "template_id": template_id,
            "name": name,
            "roles": [
                {"id": "manager", "name": "经理", "can_spawn": ["worker"]},
                {"id": "worker", "name": "执行", "reports_to": "manager"},
            ],
        }
    )


def test_s1_save_get_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.save_template(_template(name="选品团队"))
    assert store.template_path("ship-team").is_file()
    loaded = store.get_template("ship-team")
    assert loaded is not None
    assert loaded.model_dump() == saved.model_dump()
    assert loaded.name == "选品团队"


def test_s2_timestamp_semantics(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.save_template(_template())
    assert saved.created_at
    assert saved.updated_at
    first_created = saved.created_at
    again = store.save_template(saved.model_copy(update={"name": "改名"}))
    assert again.created_at == first_created
    assert again.updated_at >= first_created


def test_s3_list_merges_builtin_and_local(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ids = {tpl.template_id for tpl in store.list_templates()}
    assert "startup-studio" in ids
    assert "selection-team" in ids
    store.save_template(_template(template_id="startup-studio", name="我的创业团队"))
    merged = {tpl.template_id: tpl for tpl in store.list_templates()}
    assert merged["startup-studio"].name == "我的创业团队"


def test_s4_delete_three_states(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_template(_template())
    path = store.template_path("ship-team")
    assert store.delete_template("ship-team") is True
    assert not path.exists()
    assert store.delete_template("ship-team") is False
    assert store.delete_template("selection-team") is False
    assert store.get_template("selection-team") is not None


def test_s5_export_import_roundtrip(tmp_path: Path) -> None:
    source = _store(tmp_path, "teams-src")
    tpl = source.save_template(_template(name="导出团队"))
    out = tmp_path / "share" / "ship-team.json"
    exported = source.export_template("ship-team", out)
    assert exported == out and out.is_file()

    target = _store(tmp_path, "teams-dst")
    imported = target.import_template(out)
    assert imported.model_dump(exclude={"updated_at"}) == tpl.model_dump(exclude={"updated_at"})
    assert imported.created_at == tpl.created_at
    assert target.get_template("ship-team") is not None


def test_s6_import_existing_rejected_and_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_template(_template(name="原版"))
    path = tmp_path / "teams" / "ship-team.json"
    original_text = path.read_text(encoding="utf-8")

    external = tmp_path / "incoming" / "ship-team.json"
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_text(_template(name="外来版").model_dump_json(indent=2), encoding="utf-8")
    with pytest.raises(TeamTemplateError) as excinfo:
        store.import_template(external)
    assert "已存在" in str(excinfo.value)
    assert path.read_text(encoding="utf-8") == original_text

    overwritten = store.import_template(external, overwrite=True)
    assert overwritten.name == "外来版"
    assert store.get_template("ship-team").name == "外来版"


def test_s7_import_bad_json_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    with pytest.raises(TeamTemplateLoadError):
        store.import_template(bad)
    assert store.get_template("ship-team") is None


def test_s8_get_bad_json_distinguishes_missing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_template(_template())
    path = tmp_path / "teams" / "ship-team.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(TeamTemplateLoadError) as excinfo:
        store.get_template("ship-team")
    message = str(excinfo.value)
    assert "非法" in message
    assert "不存在" not in message
    assert store.get_template("ghost-team") is None


def test_s9_traversal_rejected_zero_residue(tmp_path: Path) -> None:
    store = _store(tmp_path)
    evil = TeamTemplate.model_construct(
        template_id="../evil",
        name="x",
        roles=[],
        sop_refs=[],
        project_id="",
        description="",
        team_id="",
        created_at="",
        updated_at="",
    )
    with pytest.raises(TeamTemplateError):
        store.save_template(evil)
    with pytest.raises(TeamTemplateError):
        store.delete_template("../evil")
    assert not (tmp_path / "evil.json").exists()
    assert not (tmp_path / "teams" / ".evil.json").exists()
    assert list((tmp_path / "teams").glob("*")) == []


def test_s10_full_chain_dod12(tmp_path: Path) -> None:
    store = _store(tmp_path, "home-a")
    selection = store.get_template("selection-team")
    assert selection is not None
    saved = store.save_template(selection)
    assert saved.created_at

    fresh = _store(tmp_path, "home-a")
    readback = fresh.get_template("selection-team")
    assert readback is not None
    assert readback.model_dump() == saved.model_dump()
    assert readback.roles

    exported = fresh.export_template("selection-team", tmp_path / "out" / "selection.json")
    home_b = _store(tmp_path, "home-b")
    imported = home_b.import_template(exported)
    assert imported.model_dump(exclude={"updated_at"}) == saved.model_dump(exclude={"updated_at"})
    assert imported.created_at == saved.created_at


def test_s11_non_utf8_file_maps_to_load_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_template(_template())
    (tmp_path / "teams" / "ship-team.json").write_bytes(b"\xff\xfe{\x00")
    with pytest.raises(TeamTemplateLoadError):
        store.get_template("ship-team")


def test_s12_list_skips_broken_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_template(_template(name="好模板"))
    (tmp_path / "teams" / "broken.json").write_text("{bad", encoding="utf-8")

    templates = store.list_templates()
    ids = {t.template_id for t in templates}
    assert "ship-team" in ids
    assert "selection-team" in ids
    assert "broken" not in ids


def test_s13_save_revalidates_mutated_template(tmp_path: Path) -> None:
    store = _store(tmp_path)
    broken = _template().model_copy(update={"name": "   "})  # model_copy 不跑校验器
    with pytest.raises(TeamTemplateError):
        store.save_template(broken)
    assert not store.template_path("ship-team").is_file()


def test_s14_replace_retries_transient_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    real_replace = Path.replace
    calls = {"n": 0}

    def flaky(self: Path, target: Path) -> Path:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(5, "拒绝访问")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    saved = store.save_template(_template())
    assert saved.template_id == "ship-team"
    assert calls["n"] >= 2, "瞬态 OSError 必须重试后成功"
    assert store.template_path("ship-team").is_file()
