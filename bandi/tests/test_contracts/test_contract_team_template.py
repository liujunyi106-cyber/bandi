# 功能：契约——团队模板数据模型（TeamTemplate / TeamRoleSpec）。
# 钉行为不钉实现：构造即校验（拓扑引用完整性 / 唯一性 / reports_to 无环）
# 默认值恒定、中文逐字往返、extra 严格拒绝、内置模板全部合法（5 蓝图转换 + 选品团队 + 产品开发团队）
# 越界 template_id 拒绝（破坏者）。生产路径族：TeamTemplateStore（save/get/list/delete/
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from bandi.company.team_template import (
    TeamTemplate,
    builtin_templates,
)


def _role(
    role_id: str,
    *,
    reports_to: str | None = None,
    can_spawn: list[str] | None = None,
    **extra: object,
) -> dict:
    payload: dict = {
        "id": role_id,
        "name": role_id.upper(),
        "reports_to": reports_to,
        "can_spawn": list(can_spawn or []),
    }
    payload.update(extra)
    return payload


def _template(**overrides: object) -> TeamTemplate:
    payload: dict = {
        "template_id": "ship-team",
        "name": "发货团队",
        "roles": [
            _role("manager", can_spawn=["worker"]),
            _role("worker", reports_to="manager"),
        ],
    }
    payload.update(overrides)
    return TeamTemplate(**payload)


def test_m1_minimal_template_defaults() -> None:
    tpl = _template()
    assert tpl.template_id == "ship-team"
    assert tpl.team_id == ""
    assert tpl.description == ""
    assert tpl.sop_refs == []
    assert tpl.project_id == ""
    assert tpl.created_at == ""
    assert tpl.updated_at == ""
    assert tpl.roles[0].llm_config == {}
    assert tpl.roles[0].skill_refs == []
    assert tpl.roles[0].tool_tags == []


def test_m2_chinese_roundtrip_verbatim() -> None:
    tpl = _template(
        description="选品与上架流程",
        sop_refs=["选品SOP", "上架SOP"],
        roles=[
            _role("选品经理", responsibility="负责选品与供应商对接", skill_refs=["选品技能"]),
            _role("助理", reports_to="选品经理"),
        ],
    )
    reloaded = TeamTemplate(**json.loads(tpl.model_dump_json()))
    assert reloaded.model_dump() == tpl.model_dump()
    assert reloaded.roles[0].responsibility == "负责选品与供应商对接"
    assert reloaded.sop_refs == ["选品SOP", "上架SOP"]


def test_m3_empty_roles_rejected() -> None:
    with pytest.raises(ValidationError):
        _template(roles=[])


def test_m4_duplicate_role_ids_rejected() -> None:
    with pytest.raises(ValidationError):
        _template(roles=[_role("dup"), _role("dup")])


def test_m5_missing_and_self_reports_to_rejected() -> None:
    with pytest.raises(ValidationError):
        _template(roles=[_role("a", reports_to="ghost")])
    with pytest.raises(ValidationError):
        _template(roles=[_role("a", reports_to="a")])


def test_m6_reports_to_cycle_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _template(roles=[_role("a", reports_to="b"), _role("b", reports_to="a")])
    assert "环" in str(excinfo.value)


def test_m7_missing_can_spawn_reference_rejected() -> None:
    with pytest.raises(ValidationError):
        _template(roles=[_role("a", can_spawn=["ghost"])])


def test_m10_extra_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        _template(unknown_key="x")
    with pytest.raises(ValidationError):
        _template(roles=[_role("a", unknown_role_key="x")])


def test_m11_builtin_templates_valid() -> None:
    templates = builtin_templates()
    ids = {tpl.template_id for tpl in templates}
    assert {
        "startup-studio",
        "enterprise-corp",
        "creative-agency",
        "research-lab",
        "devops-pipeline",
        "selection-team",
        "product-team",
    } <= ids
    for tpl in templates:
        assert tpl.roles, f"{tpl.template_id} 角色集为空"
        json.loads(tpl.model_dump_json())


def test_m12_unsafe_template_id_rejected() -> None:
    for bad in ("../evil", "a/b", "a\\b", ".", "..", ""):
        with pytest.raises(ValidationError):
            _template(template_id=bad)


def test_m13_unicode_template_id_accepted() -> None:
    tpl = _template(template_id="选品团队")
    assert tpl.template_id == "选品团队"


# 四 id 口径合一——role.id 含冒号等会让 profile 建档静默失败）
def test_m14_unsafe_role_id_rejected() -> None:
    for bad in ("a:b", "a/b", "", "..", "a b", "a\\b"):
        with pytest.raises(ValidationError):
            _template(roles=[_role(bad)])


def test_m15_product_team_structure() -> None:
    """产品开发团队拓扑：唯一最终决策者 + 设计/前后端/测试汇报线。"""
    tpl = next(t for t in builtin_templates() if t.template_id == "product-team")
    final_deciders = [role.id for role in tpl.roles if role.is_final_decider]
    assert final_deciders == ["product_lead"]
    lead = next(role for role in tpl.roles if role.id == "product_lead")
    assert set(lead.can_spawn) == {"designer", "frontend_dev", "backend_dev", "qa"}
    assert {role.reports_to for role in tpl.roles if role.id != "product_lead"} == {"product_lead"}
