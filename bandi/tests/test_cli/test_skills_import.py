# 功能：修复——技能导入 CLI 契约（`bandi skills import --team` 带属导入）。
# Loader 读回）+ 全局导入缺省 + 缺路径受控失败。
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from bandi.cli.skills import skills_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_skill(base: Path, name: str) -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 描述\n---\n\n"
        f"## Behavior\n- {name}\n\n## Instructions\n{name} 指令\n",
        encoding="utf-8",
    )
    return skill_dir


def test_import_with_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    lib = tmp_path / "lib"
    monkeypatch.setattr("bandi.skills.importer.default_skills_dir", lambda: str(lib))
    src = _write_skill(tmp_path / "src", "my-skill")

    result = runner.invoke(skills_app, ["import", str(src), "--team", "team-a"])
    assert result.exit_code == 0, result.output

    from bandi.skills.loader import SkillLoader

    skills = {s.name: s for s in SkillLoader(str(lib)).load_all()}
    assert skills["my-skill"].team_id == "team-a", "带属导入必须落盘团队归属"


def test_import_without_team_is_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    lib = tmp_path / "lib"
    monkeypatch.setattr("bandi.skills.importer.default_skills_dir", lambda: str(lib))
    src = _write_skill(tmp_path / "src", "plain-skill")

    result = runner.invoke(skills_app, ["import", str(src)])
    assert result.exit_code == 0, result.output

    from bandi.skills.loader import SkillLoader

    assert SkillLoader(str(lib)).load_all()[0].team_id == ""


def test_import_missing_path_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    lib = tmp_path / "lib"
    monkeypatch.setattr("bandi.skills.importer.default_skills_dir", lambda: str(lib))
    result = runner.invoke(skills_app, ["import", str(tmp_path / "ghost")])
    assert result.exit_code == 1
