# 功能：契约——配置来源四层（user/project/local/flag）+ rules 逐层合并。
# 钉行为：local（config.local.yaml）覆盖 project；flag（BANDI_CONFIG）覆盖 local；
# loaded_sources 元数据如实上报命中来源；permissions.rules 逐层 concat 并自动
# 标注来源；env profile 现状行为保留。
from __future__ import annotations

import os
from pathlib import Path

import pytest

from bandi.core.config import BandiConfig


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / ".bandi").mkdir(parents=True)
    proj = tmp_path / "proj"
    (proj / ".bandi").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(proj)
    for key in list(os.environ):
        if key.startswith("BANDI_"):
            monkeypatch.delenv(key, raising=False)
    return home, proj


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestLayerPrecedence:
    def test_local_layer_overrides_project(self, sandbox: tuple[Path, Path]) -> None:
        home, proj = sandbox
        _write(proj / ".bandi" / "config.yaml", "permissions:\n  ask_tools: ['read']\n")
        _write(proj / ".bandi" / "config.local.yaml", "permissions:\n  ask_tools: ['grep']\n")
        cfg = BandiConfig.load()
        assert cfg.permissions.ask_tools == ["grep"], "local 必须覆盖 project"

    def test_flag_layer_overrides_local(self, sandbox: tuple[Path, Path], tmp_path: Path) -> None:
        home, proj = sandbox
        _write(proj / ".bandi" / "config.local.yaml", "permissions:\n  ask_tools: ['grep']\n")
        flag = tmp_path / "flag.yaml"
        _write(flag, "permissions:\n  ask_tools: ['glob']\n")
        os.environ["BANDI_CONFIG"] = str(flag)
        try:
            cfg = BandiConfig.load()
        finally:
            os.environ.pop("BANDI_CONFIG", None)
        assert cfg.permissions.ask_tools == ["glob"], "flag 层必须覆盖 local"

    def test_env_profile_retained(self, sandbox: tuple[Path, Path]) -> None:
        home, proj = sandbox
        _write(home / ".bandi" / "config.dev.yaml", "preferred_language: en\n")
        cfg = BandiConfig.load()
        assert cfg.preferred_language == "en"
        assert "env:dev" in cfg.loaded_sources


class TestLoadedSources:
    def test_all_four_layers_reported(self, sandbox: tuple[Path, Path], tmp_path: Path) -> None:
        home, proj = sandbox
        _write(home / ".bandi" / "config.yaml", "env: dev\n")
        _write(proj / ".bandi" / "config.yaml", "preferred_language: zh-CN\n")
        _write(proj / ".bandi" / "config.local.yaml", "preferred_language: zh-CN\n")
        flag = tmp_path / "flag.yaml"
        _write(flag, "preferred_language: zh-CN\n")
        os.environ["BANDI_CONFIG"] = str(flag)
        try:
            cfg = BandiConfig.load()
        finally:
            os.environ.pop("BANDI_CONFIG", None)
        assert cfg.loaded_sources == ["user", "project", "local", "flag"]

    def test_missing_layers_not_reported(self, sandbox: tuple[Path, Path]) -> None:
        home, proj = sandbox
        _write(home / ".bandi" / "config.yaml", "env: dev\n")
        cfg = BandiConfig.load()
        assert cfg.loaded_sources == ["user"]

    def test_no_files_empty_sources(self, sandbox: tuple[Path, Path]) -> None:
        cfg = BandiConfig.load()
        assert cfg.loaded_sources == []


class TestRulesMerging:
    def test_rules_concatenated_across_layers_with_source(self, sandbox: tuple[Path, Path]) -> None:
        home, proj = sandbox
        _write(
            home / ".bandi" / "config.yaml",
            "permissions:\n  rules:\n    - 'bash:git:*'\n",
        )
        _write(
            proj / ".bandi" / "config.yaml",
            "permissions:\n  rules:\n    - pattern: 'bash:rm *'\n      action: deny\n",
        )
        _write(
            proj / ".bandi" / "config.local.yaml",
            "permissions:\n  rules:\n    - 'read'\n",
        )
        cfg = BandiConfig.load()
        rules = cfg.permissions.rules
        assert [r["source"] for r in rules] == ["user", "project", "local"], (
            "规则必须逐层 concat 且标注来源"
        )
        assert rules[0]["pattern"] == "bash:git:*"
        assert rules[1]["action"] == "deny"
        assert rules[2]["pattern"] == "read"

    def test_flag_rules_appended_last(self, sandbox: tuple[Path, Path], tmp_path: Path) -> None:
        home, proj = sandbox
        _write(home / ".bandi" / "config.yaml", "permissions:\n  rules:\n    - 'read'\n")
        flag = tmp_path / "flag.yaml"
        _write(flag, "permissions:\n  rules:\n    - 'grep'\n")
        os.environ["BANDI_CONFIG"] = str(flag)
        try:
            cfg = BandiConfig.load()
        finally:
            os.environ.pop("BANDI_CONFIG", None)
        assert [r["source"] for r in cfg.permissions.rules] == ["user", "flag"]

    def test_no_rules_default_empty(self, sandbox: tuple[Path, Path]) -> None:
        home, proj = sandbox
        _write(home / ".bandi" / "config.yaml", "env: dev\n")
        assert BandiConfig.load().permissions.rules == []


class TestSettingsSourcesApi:
    def test_api_reports_sources_and_rule_distribution(
        self, sandbox: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        from fastapi.testclient import TestClient

        home, proj = sandbox
        _write(home / ".bandi" / "config.yaml", "env: dev\n")
        _write(
            proj / ".bandi" / "config.local.yaml",
            "permissions:\n  rules:\n    - 'read'\n",
        )
        app_mod = importlib.import_module("bandi.api.app")
        monkeypatch.setattr(app_mod, "_API_KEY", "test")
        client = TestClient(app_mod.app)
        client.headers["Authorization"] = "Bearer test"

        body = client.get("/api/settings/sources").json()

        assert body["sources"] == ["user", "local"]
        assert body["rule_sources"] == {"local": 1}


class TestConfigToPipelineE2E:
    def test_local_rule_reaches_pipeline_behavior(self, sandbox: tuple[Path, Path]) -> None:
        from bandi.core.message import ToolCall
        from bandi.core.permission.rules import parse_permission_rule
        from bandi.tools.base import BaseTool, ToolCategory
        from bandi.tools.permissions_pipeline import PermissionsPipeline

        home, proj = sandbox
        _write(
            proj / ".bandi" / "config.local.yaml",
            "permissions:\n  rules:\n    - 'bash:git:*'\n",
        )
        cfg = BandiConfig.load()
        rules = [
            r for r in (parse_permission_rule(e) for e in cfg.permissions.rules) if r is not None
        ]
        pp = PermissionsPipeline(rules=rules, ask_tools={"bash"})

        class _Bash(BaseTool):
            name = "bash"
            description = ""
            category = ToolCategory.UTIL

            async def execute(self, params, context=None):
                return "ok"

        call = ToolCall(id="c", name="bash", arguments={"command": "git status"})
        assert pp.needs_approval(call, _Bash(), "execute") is False, (
            "local 层配置的规则必须对管道生效（git 命令免询问）"
        )
        other = ToolCall(id="c2", name="bash", arguments={"command": "npm install"})
        assert pp.needs_approval(other, _Bash(), "execute") is True, (
            "规则未命中的命令仍按 ask 名单询问"
        )
