# 功能：团队模板 CLI 契约——list / instantiate / create+update-skills+update-sop /
# export-import-delete 链路（真实落盘 org.json + 模板库）。
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from bandi.cli.team import team_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模板库/组织落 tmp（不污染用户 ~/.bandi）+ 重置模块级单例。

    HOME/USERPROFILE env 同步隔离：profiles 等默认路径经 os.path.expanduser("~")
    解析（不读 Path.home patch），不隔离 env 会写真实用户目录。
    """
    import bandi.cli.team as mt
    from bandi.company.org_engine import OrgEngine
    from bandi.company.recruiter import Recruiter
    from bandi.company.team_template_store import TeamTemplateStore

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    mt._store = TeamTemplateStore(templates_dir=str(tmp_path / "teams"))
    mt._engine = OrgEngine()
    mt._recruiter = Recruiter()
    mt._org_path = str(tmp_path / "org.json")


class TestTeamCli:
    def test_list_shows_builtin(self, runner: CliRunner) -> None:
        result = runner.invoke(team_app, ["list"])
        assert result.exit_code == 0, result.output
        assert "selection-team" in result.output
        assert "startup-studio" in result.output

    def test_instantiate_selection_team(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(team_app, ["instantiate", "selection-team"])
        assert result.exit_code == 0, result.output
        assert "selection-team" in result.output

        import json

        import bandi.cli.team as mt

        lead = mt._engine.get_role("selection-team_selection_lead")
        assert lead is not None, "实例化必须注册前缀角色"
        assert lead.team_id == "selection-team"
        assert mt._engine.get_employee("selection-team_selection_lead") is not None
        assert Path(mt._org_path).is_file(), "实例化必须落盘 org.json"

        payload = json.loads(Path(mt._org_path).read_text(encoding="utf-8"))
        persisted_seats = set(payload.get("seats", {}))
        assert "selection-team_selection_lead" in persisted_seats, (
            f"席位必须随实例化落盘（CLI 真实链路），实际 {persisted_seats}"
        )

    def test_create_and_update_skills_sop(self, runner: CliRunner) -> None:
        result = runner.invoke(team_app, ["create", "my-team", "--from", "selection-team"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(
            team_app,
            ["update-skills", "my-team", "--role", "selection_lead", "--skills", "选品技能,谈判"],
        )
        assert result.exit_code == 0, result.output

        result = runner.invoke(team_app, ["update-sop", "my-team", "--sop", "选品SOP"])
        assert result.exit_code == 0, result.output

        import bandi.cli.team as mt

        tpl = mt._store.get_template("my-team")
        assert tpl is not None
        lead = next(r for r in tpl.roles if r.id == "selection_lead")
        assert lead.skill_refs == ["选品技能", "谈判"]
        assert tpl.sop_refs == ["选品SOP"]

    def test_export_import_delete_chain(self, runner: CliRunner, tmp_path: Path) -> None:
        out = tmp_path / "share" / "sel.json"
        result = runner.invoke(team_app, ["export", "selection-team", "--out", str(out)])
        assert result.exit_code == 0, result.output
        assert out.is_file()

        result = runner.invoke(team_app, ["import", str(out)])
        assert result.exit_code == 0, result.output

        import bandi.cli.team as mt

        assert mt._store.template_path("selection-team").is_file()

        result = runner.invoke(team_app, ["delete", "selection-team"])
        assert result.exit_code == 0, result.output
        assert not mt._store.template_path("selection-team").is_file()

    def test_unknown_template_fails(self, runner: CliRunner) -> None:
        result = runner.invoke(team_app, ["instantiate", "ghost-team"])
        assert result.exit_code == 1
        result2 = runner.invoke(
            team_app, ["update-skills", "ghost", "--role", "x", "--skills", "y"]
        )
        assert result2.exit_code == 1

    def test_instantiate_invalid_team_id_fails(self, runner: CliRunner) -> None:
        """破坏者：--team-id 非法（空白/冒号）必须受控失败（exit 1），不裸崩栈。"""
        result = runner.invoke(team_app, ["instantiate", "selection-team", "--team-id", "a b"])
        assert result.exit_code == 1
        assert "team_id" in result.output

    def test_create_blank_name_rejected(self, runner: CliRunner) -> None:
        """破坏者：--name 空白不得写出坏模板（save 入口 re-validate，exit 1 零落盘）。"""
        result = runner.invoke(
            team_app, ["create", "spaced", "--from", "selection-team", "--name", "   "]
        )
        assert result.exit_code == 1

        import bandi.cli.team as mt

        assert not mt._store.template_path("spaced").is_file()

    def test_export_failure_fails_gracefully(self, runner: CliRunner, tmp_path: Path) -> None:
        """破坏者：export 目标为目录（写失败）→ 受控失败，不裸抛 SaveError。"""
        target_dir = tmp_path / "as-dir"
        target_dir.mkdir()
        result = runner.invoke(team_app, ["export", "selection-team", "--out", str(target_dir)])
        assert result.exit_code == 1
