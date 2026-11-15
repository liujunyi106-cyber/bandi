# 功能：契约——技能工具作用域隔离（skill_invoke / skill_discover 不得绕过团队边界）。
#
# 破坏者视角：
# - 模型可以直接报出别团队技能名去 invoke（不靠索引，自动注入收窄了也拦不住）；
# - 报错时的 available 列表会二次泄露别团队技能名；
# - scope 缺失时若默认全放行，等于没隔离——必须 fail-closed 到全局层。
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.skills.manager import create_skill_registry
from bandi.skills.models import SkillDef
from bandi.skills.registry import SkillRegistry
from bandi.tools.base import ToolPermissionContext
from bandi.tools.skill_tool import SkillDiscoverTool, SkillTool


def _install(tmp_path: Path, monkeypatch: Any) -> None:
    reg = SkillRegistry()
    reg.register_all(
        [
            SkillDef(
                name="global-one", description="全局技能", triggers=[], instructions="GLOBAL_BODY"
            ),
            SkillDef(
                name="team-a-one",
                description="A 团队技能",
                triggers=[],
                instructions="TEAM_A_BODY",
                team_id="team-a",
            ),
            SkillDef(
                name="team-b-secret",
                description="B 团队机密技能",
                triggers=[],
                instructions="TEAM_B_BODY",
                team_id="team-b",
            ),
        ]
    )
    monkeypatch.setattr("bandi.tools.skill_tool._REGISTRY", reg, raising=False)


def _ctx(team_id: str = "", global_only: bool = False) -> ToolPermissionContext:
    # 功能：模拟 executor 透传的 per-call 作用域（与 role_fs_scopes 同构）
    return ToolPermissionContext(
        extra={"skill_scope": {"team_id": team_id, "global_only": global_only}}
    )


class TestInvokeScope:
    @pytest.mark.asyncio
    async def test_i1_secretary_cannot_invoke_team_skill(self, tmp_path: Path, monkeypatch) -> None:
        _install(tmp_path, monkeypatch)

        out = await SkillTool().execute({"skill": "team-a-one"}, _ctx(team_id="", global_only=True))

        assert out["success"] is False
        assert "TEAM_A_BODY" not in str(out)

    @pytest.mark.asyncio
    async def test_i2_secretary_error_does_not_leak_team_names(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # 功能：越权报错不得回显别团队技能名（available 列表同样受作用域约束）
        _install(tmp_path, monkeypatch)

        out = await SkillTool().execute(
            {"skill": "team-b-secret"}, _ctx(team_id="", global_only=True)
        )

        assert out["success"] is False
        available = str(out.get("available", ""))
        assert "team-b-secret" not in available, "报错 available 泄露了别团队技能名"
        assert "team-a-one" not in available
        assert "global-one" in available, "全局层技能仍应可见"

    @pytest.mark.asyncio
    async def test_i3_secretary_can_invoke_global(self, tmp_path: Path, monkeypatch) -> None:
        _install(tmp_path, monkeypatch)

        out = await SkillTool().execute({"skill": "global-one"}, _ctx(team_id="", global_only=True))

        assert out["success"] is True
        assert out["instructions"] == "GLOBAL_BODY"

    @pytest.mark.asyncio
    async def test_i4_employee_can_invoke_own_team(self, tmp_path: Path, monkeypatch) -> None:
        _install(tmp_path, monkeypatch)

        out = await SkillTool().execute({"skill": "team-a-one"}, _ctx(team_id="team-a"))

        assert out["success"] is True
        assert out["instructions"] == "TEAM_A_BODY"

    @pytest.mark.asyncio
    async def test_i5_employee_cannot_invoke_other_team(self, tmp_path: Path, monkeypatch) -> None:
        _install(tmp_path, monkeypatch)

        out = await SkillTool().execute({"skill": "team-b-secret"}, _ctx(team_id="team-a"))

        assert out["success"] is False
        assert "team-b-secret" not in str(out.get("available", ""))

    @pytest.mark.asyncio
    async def test_i6_missing_scope_fails_closed_to_global(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # 功能：无作用域上下文时按最严（秘书）处理，不得静默全放行
        _install(tmp_path, monkeypatch)

        out = await SkillTool().execute({"skill": "team-a-one"}, ToolPermissionContext())

        assert out["success"] is False, "scope 缺失时放行团队技能 = 隔离失效"


class TestDiscoverScope:
    @pytest.mark.asyncio
    async def test_d1_secretary_discover_hides_team_skills(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _install(tmp_path, monkeypatch)

        out = await SkillDiscoverTool().execute({}, _ctx(team_id="", global_only=True))

        names = {s["name"] for s in out["skills"]}
        assert names == {"global-one"}

    @pytest.mark.asyncio
    async def test_d2_employee_discover_sees_global_plus_own(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _install(tmp_path, monkeypatch)

        out = await SkillDiscoverTool().execute({}, _ctx(team_id="team-a"))

        names = {s["name"] for s in out["skills"]}
        assert names == {"global-one", "team-a-one"}

    @pytest.mark.asyncio
    async def test_d3_discover_keyword_cannot_smuggle_out_of_scope(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _install(tmp_path, monkeypatch)

        out = await SkillDiscoverTool().execute(
            {"keyword": "B 团队机密"}, _ctx(team_id="", global_only=True)
        )

        assert out["count"] == 0, "关键词过滤不得绕过作用域"
        assert "team-b-secret" not in str(out)

    @pytest.mark.asyncio
    async def test_d4_missing_scope_fails_closed(self, tmp_path: Path, monkeypatch) -> None:
        _install(tmp_path, monkeypatch)

        out = await SkillDiscoverTool().execute({}, ToolPermissionContext())

        names = {s["name"] for s in out["skills"]}
        assert "team-a-one" not in names and "team-b-secret" not in names


class TestToolDescriptionMentionsCatalog:
    def test_t1_invoke_description_points_at_catalog(self) -> None:
        # 功能：工具描述要指向常驻索引（模型据此知道有哪些技能可用）
        assert "可用技能" in SkillTool.description

    def test_t2_registry_factory_still_works(self, tmp_path: Path) -> None:
        # 功能：真实 registry 工厂不受影响
        assert isinstance(create_skill_registry(str(tmp_path)), SkillRegistry)
