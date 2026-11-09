# 功能：契约——团队工具（team_list 列模板 / team_create 按模板实例化团队）。
# 钉行为：list 含内置模板 / create 真注册到 org（team_id 前缀）/ 未绑定 fail-closed /
# 未知模板 error / 破坏者：非法 team_id 被拒不留残渣。
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bandi.company.org_engine import OrgEngine
from bandi.tools.team import TeamCreateTool, TeamListTool


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离 home（TeamTemplateStore 默认 ~/.bandi/teams 与 profiles 目录）。"""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))


class TestTeamTools:
    def test_team_list_has_builtin_templates(self) -> None:
        result = asyncio.run(TeamListTool().execute({}))
        assert result["success"] is True
        assert result["count"] >= 1, "内置模板列表不得为空"
        assert all("template_id" in item and "role_count" in item for item in result["templates"])

    def test_team_create_registers_into_org(self) -> None:
        org = OrgEngine()
        tool = TeamCreateTool()
        tool.bind_deps(org_engine=org)
        listing = asyncio.run(TeamListTool().execute({}))
        template_id = listing["templates"][0]["template_id"]
        result = asyncio.run(tool.execute({"template_id": template_id}))
        assert result["success"] is True, result
        report = result["report"]
        prefix = str(report["prefix"])
        assert prefix, "报告必须含前缀"
        role_ids = [str(role.id) for role in org.list_roles()]
        assert any(rid.startswith(prefix) for rid in role_ids), (
            f"实例化必须以 {prefix} 前缀注册角色，实际 {role_ids}"
        )

    def test_breaker_unbound_fail_closed(self) -> None:
        result = asyncio.run(TeamCreateTool().execute({"template_id": "selection-team"}))
        assert result["success"] is False
        assert "org_engine" in result["error"], "未绑定必须 fail-closed 且提示组织环境"

    def test_unknown_template_error(self) -> None:
        tool = TeamCreateTool()
        tool.bind_deps(org_engine=OrgEngine())
        result = asyncio.run(tool.execute({"template_id": "ghost-team"}))
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_breaker_invalid_team_id_rejected(self) -> None:
        tool = TeamCreateTool()
        tool.bind_deps(org_engine=OrgEngine())
        listing = asyncio.run(TeamListTool().execute({}))
        template_id = listing["templates"][0]["template_id"]
        result = asyncio.run(tool.execute({"template_id": template_id, "team_id": "../evil"}))
        assert result["success"] is False, "非法 team_id 必须被拒"
        assert result["error"]
