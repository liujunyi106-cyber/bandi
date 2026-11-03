# 功能：数字员工能力档案模型契约——AgentProfile/AgentResourceBinding（四类能力
# 绑定即校验）+ CapabilityContext（单机裁剪版）+ CapabilityRegistry（seal + sha256 快照）。
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from bandi.company.agent_profile import (
    AgentProfile,
    AgentProfileDuplicateError,
    AgentProfileError,
    AgentProfileManager,
    AgentProfileNotFoundError,
    AgentResourceBinding,
    AgentResourceDuplicateError,
    AgentResourceInvalidError,
    AgentResourceType,
    CapabilityBinding,
    CapabilityContext,
    CapabilityRegistry,
    CapabilityRegistryError,
    CapabilitySnapshot,
)
from bandi.core.exceptions import BandiError
from bandi.knowledge.store import KnowledgeStore
from bandi.skills.card import SkillCard
from bandi.skills.serializer import save_card
from bandi.tools.base import BaseTool
from bandi.tools.registry import ToolRegistry

# ---------------------------------------------------------------- 公共件


def _card(
    skill_id: str = "sk_ship_order", version: str = "1.0.0", name: str = "发货 SOP"
) -> SkillCard:
    """功能：契约基准卡——两节点线性 SOP。"""
    return SkillCard(
        **{
            "skill_id": skill_id,
            "name": name,
            "version": version,
            "description": "订单发货流程",
            "required_info": ["订单号"],
            "nodes": [
                {
                    "node_id": "collect_order",
                    "name": "收集订单",
                    "type": "collect_info",
                    "instruction": "收集订单号。",
                    "expected_user_info": ["订单号"],
                },
                {
                    "node_id": "reply_final_result",
                    "name": "反馈结果",
                    "type": "response",
                    "instruction": "给出最终回复。",
                },
            ],
            "edges": [
                {
                    "source_node_id": "collect_order",
                    "next_node_id": "reply_final_result",
                    "priority": 0,
                }
            ],
            "start_node_id": "collect_order",
            "terminal_node_ids": ["reply_final_result"],
        }
    )


class _StubTool(BaseTool):
    name = "echo_stub"
    description = "测试桩工具"

    async def execute(
        self, params: dict[str, Any], context: object | None = None
    ) -> str | dict[str, Any]:
        return {"echo": params}


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    base = tmp_path / "skills"
    base.mkdir()
    save_card(_card(), str(base))
    (base / "ship_guide").mkdir()
    (base / "ship_guide" / "SKILL.md").write_text(
        "---\nname: ship_guide\ndescription: 发货指南\n---\n正文", encoding="utf-8"
    )
    return base


@pytest.fixture
def tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_instance(_StubTool())
    return registry


@pytest.fixture
def manager(tmp_path: Path, skills_dir: Path, tool_registry: ToolRegistry) -> AgentProfileManager:
    return AgentProfileManager(
        profiles_dir=str(tmp_path / "profiles"),
        skills_dir=str(skills_dir),
        tool_registry=tool_registry,
    )


# ---------------------------------------------------------------- 档案 CRUD 与持久化


def test_r1_create_profile_roundtrip(manager: AgentProfileManager, tmp_path: Path) -> None:
    profile = manager.create_profile("emp-001", name="客服小张", persona_prompt="礼貌、简洁")

    assert profile.profile_id == "emp-001"
    assert profile.employee_id == "emp-001"
    assert profile.status == "active"
    assert profile.resources == []

    on_disk = json.loads(
        (tmp_path / "profiles" / "emp-001.profile.json").read_text(encoding="utf-8")
    )
    assert on_disk["profile_id"] == "emp-001"

    loaded = manager.get_profile("emp-001")
    assert loaded is not None
    assert loaded.model_dump(mode="json") == profile.model_dump(mode="json")


def test_r2_duplicate_create_rejected(manager: AgentProfileManager) -> None:
    manager.create_profile("emp-001", name="客服小张")
    with pytest.raises(AgentProfileDuplicateError):
        manager.create_profile("emp-001", name="再建一次")


def test_r3_get_missing_returns_none_require_raises(manager: AgentProfileManager) -> None:
    assert manager.get_profile("ghost") is None
    with pytest.raises(AgentProfileNotFoundError):
        manager.require_profile("ghost")


def test_r4_update_fields_and_bump_updated_at(manager: AgentProfileManager) -> None:
    profile = manager.create_profile("emp-001", name="客服小张")
    before = profile.updated_at

    updated = manager.update_profile("emp-001", persona_prompt="更礼貌", status="archived")

    assert updated.persona_prompt == "更礼貌"
    assert updated.status == "archived"
    assert updated.name == "客服小张"
    assert updated.updated_at >= before

    with pytest.raises(AgentProfileError):
        manager.update_profile("emp-001")


# ---------------------------------------------------------------- 绑定即校验（=A）


def _create(manager: AgentProfileManager) -> str:
    manager.create_profile("emp-001", name="客服小张")
    return "emp-001"


def test_r5_bind_skill_resolves_card(manager: AgentProfileManager) -> None:
    pid = _create(manager)
    profile = manager.bind_resource(pid, "skill", "sk_ship_order")

    assert len(profile.resources) == 1
    binding = profile.resources[0]
    assert binding.resource_type == AgentResourceType.SKILL
    assert binding.resource_id == "sk_ship_order"
    assert binding.status == "active"
    assert binding.created_at


def test_r6_duplicate_binding_rejected(manager: AgentProfileManager) -> None:
    pid = _create(manager)
    manager.bind_resource(pid, "skill", "sk_ship_order")
    with pytest.raises(AgentResourceDuplicateError):
        manager.bind_resource(pid, "skill", "sk_ship_order")


def test_r7_bind_unknown_skill_rejected(manager: AgentProfileManager) -> None:
    pid = _create(manager)
    with pytest.raises(AgentResourceInvalidError):
        manager.bind_resource(pid, "skill", "sk_ghost")


def test_r8_bind_general_skill_checks_skill_md(manager: AgentProfileManager) -> None:
    pid = _create(manager)
    ok = manager.bind_resource(pid, "general_skill", "ship_guide")
    assert ok.resources[0].resource_type == AgentResourceType.GENERAL_SKILL

    with pytest.raises(AgentResourceInvalidError):
        manager.bind_resource(pid, "general_skill", "ghost_guide")


def test_r9_bind_tool_checks_registry(manager: AgentProfileManager) -> None:
    pid = _create(manager)
    ok = manager.bind_resource(pid, "tool", "echo_stub")
    assert ok.resources[0].resource_type == AgentResourceType.TOOL

    with pytest.raises(AgentResourceInvalidError):
        manager.bind_resource(pid, "tool", "ghost_tool")


def test_r10_knowledge_base_binds_as_reference(manager: AgentProfileManager) -> None:
    """未注入 KnowledgeStore：knowledge_base 只存引用不解析（兼容环境语义）。"""
    pid = _create(manager)
    profile = manager.bind_resource(pid, "knowledge_base", "kb-product-faq")

    assert profile.resources[0].resource_type == AgentResourceType.KNOWLEDGE_BASE
    assert profile.resources[0].resource_id == "kb-product-faq"


def test_r10b_knowledge_base_validated_against_store(
    tmp_path: Path, skills_dir: Path, tool_registry: ToolRegistry
) -> None:
    """注入 KnowledgeStore：绑定校验 base 存在；幽灵库显式拒绝（破坏者）。"""
    store = KnowledgeStore(db_path=tmp_path / "kb.db")
    store.ensure_base("kb-product-faq")
    manager = AgentProfileManager(
        profiles_dir=str(tmp_path / "profiles-b"),
        skills_dir=str(skills_dir),
        tool_registry=tool_registry,
        knowledge_store=store,
    )
    pid = _create(manager)
    ok = manager.bind_resource(pid, "knowledge_base", "kb-product-faq")
    assert ok.resources[0].resource_type == AgentResourceType.KNOWLEDGE_BASE

    with pytest.raises(AgentResourceInvalidError):
        manager.bind_resource(pid, "knowledge_base", "kb-ghost")


def test_r11_unbind_and_status_toggle(manager: AgentProfileManager) -> None:
    pid = _create(manager)
    manager.bind_resource(pid, "skill", "sk_ship_order")

    inactive = manager.set_resource_status(pid, "skill", "sk_ship_order", status="inactive")
    assert inactive.resources[0].status == "inactive"

    removed = manager.unbind_resource(pid, "skill", "sk_ship_order")
    assert removed.resources == []

    with pytest.raises(AgentProfileError):
        manager.unbind_resource(pid, "skill", "sk_ship_order")


# ---------------------------------------------------------------- 破坏者：路径穿越


def test_r12_profile_id_traversal_rejected(manager: AgentProfileManager, tmp_path: Path) -> None:
    with pytest.raises(AgentProfileError):
        manager.create_profile("../evil", name="越权档案")

    assert not (tmp_path / "evil").exists()
    assert (
        list((tmp_path / "profiles").iterdir()) == [] if (tmp_path / "profiles").exists() else True
    )


# ---------------------------------------------------------------- CapabilityContext（=A 裁剪版）


def test_r13_context_validation_and_from_profile(manager: AgentProfileManager) -> None:
    ctx = CapabilityContext(
        agent_id="emp-001", session_id="sess-1", request_id="req-1", trace_id="tr-1"
    )
    assert ctx.attempt == 1

    with pytest.raises(ValueError):
        CapabilityContext(agent_id="", session_id="sess-1", request_id="req-1")
    with pytest.raises(ValueError):
        CapabilityContext(agent_id="a", session_id="s", request_id="r", attempt=0)

    _create(manager)
    ctx2 = manager.build_context("emp-001", session_id="sess-1", request_id="req-2")
    assert ctx2.agent_id == "emp-001"
    assert ctx2.session_id == "sess-1"


# ---------------------------------------------------------------- CapabilityRegistry（=A）


def _binding(capability: str = "skill_catalog", contract: str = "1.0") -> CapabilityBinding:
    return CapabilityBinding(
        capability=capability,
        provider_id="local",
        provider_deployment_id="local-default",
        service_contract_version=contract,
        provider=object(),
    )


def test_r14_registry_snapshot_id_deterministic_and_require() -> None:
    registry = CapabilityRegistry()
    registry.register(_binding())
    snap1 = registry.snapshot()
    snap2 = registry.snapshot()
    assert snap1.snapshot_id == snap2.snapshot_id
    assert isinstance(snap1, CapabilitySnapshot)
    assert snap1.get("skill_catalog") is not None
    assert snap1.get("ghost") is None
    with pytest.raises(CapabilityRegistryError):
        snap1.require("ghost")

    other = CapabilityRegistry()
    other.register(_binding(contract="2.0"))
    assert other.snapshot().snapshot_id != snap1.snapshot_id

    subset = registry.snapshot(requested={"skill_catalog"})
    assert subset.get("skill_catalog") is not None
    empty = registry.snapshot(requested=set())
    assert empty.get("skill_catalog") is None


def test_r15_registry_duplicate_and_seal() -> None:
    registry = CapabilityRegistry()
    registry.register(_binding())
    with pytest.raises(CapabilityRegistryError):
        registry.register(_binding())

    registry.seal()
    with pytest.raises(CapabilityRegistryError):
        registry.register(_binding(capability="other"))
    with pytest.raises(CapabilityRegistryError):
        registry.register(_binding(capability="another"))


def test_r16_registry_unsupported_contract_rejected() -> None:
    registry = CapabilityRegistry()
    registry.register(_binding(contract="9.9"))
    with pytest.raises(CapabilityRegistryError):
        registry.snapshot(supported_contracts={"skill_catalog": {"1.0", "2.0"}})
    ok = registry.snapshot(supported_contracts={"skill_catalog": {"9.9"}})
    assert ok.get("skill_catalog") is not None


def test_r17_error_family_under_bandi_error() -> None:
    for exc in (
        AgentProfileError,
        AgentProfileNotFoundError,
        AgentProfileDuplicateError,
        AgentResourceInvalidError,
        AgentResourceDuplicateError,
        CapabilityRegistryError,
    ):
        assert issubclass(exc, BandiError)


# ---------------------------------------------------------------- 绑定模型字段钉


def test_r18_binding_model_defaults() -> None:
    binding = AgentResourceBinding(
        resource_type=AgentResourceType.TOOL,
        resource_id="echo_stub",
        created_at="T00:00:00+00:00",
        updated_at="T00:00:00+00:00",
    )
    assert binding.status == "active"
    assert binding.metadata == {}
    assert AgentProfile.model_fields["resources"].is_required() is False


# ---------------------------------------------------------------- status 约束与公开面


def test_r19_status_values_are_constrained() -> None:
    """契约：status 仅接受枚举值（Profile: active|archived；Binding: active|inactive）。"""
    with pytest.raises(ValidationError):
        AgentProfile(
            profile_id="p1",
            employee_id="p1",
            name="n",
            status="bogus",
            created_at="T00:00:00+00:00",
            updated_at="T00:00:00+00:00",
        )
    with pytest.raises(ValidationError):
        AgentResourceBinding(
            resource_type=AgentResourceType.TOOL,
            resource_id="echo_stub",
            status="bogus",
            created_at="T00:00:00+00:00",
            updated_at="T00:00:00+00:00",
        )


def test_r20_public_exports_from_company_package() -> None:
    """契约：档案/能力公开面经 company 包导出（装配点契约）。"""
    from bandi.company import (
        AgentProfile,
        AgentProfileManager,
        AgentResourceType,
        CapabilityContext,
        CapabilityRegistry,
    )

    assert AgentProfile is not None
    assert AgentProfileManager is not None
    assert AgentResourceType is not None
    assert CapabilityContext is not None
    assert CapabilityRegistry is not None


# ---------------------------------------------------------------- KB-1：命名空间冒号 id


def test_bind_knowledge_base_with_namespace_colon(
    tmp_path: Path, skills_dir: Path, tool_registry: ToolRegistry
) -> None:
    """功能：`kb:design-org` 这类命名空间 id 必须能绑定/解绑（引用键不是路径片段）。"""
    store = KnowledgeStore(db_path=tmp_path / "kb-ns.db")
    store.ensure_base("kb:design-org")
    manager = AgentProfileManager(
        profiles_dir=str(tmp_path / "profiles-ns"),
        skills_dir=str(skills_dir),
        tool_registry=tool_registry,
        knowledge_store=store,
    )
    pid = _create(manager)

    bound = manager.bind_resource(pid, "knowledge_base", "kb:design-org")
    assert [item.resource_id for item in bound.resources] == ["kb:design-org"]

    after = manager.unbind_resource(pid, "knowledge_base", "kb:design-org")
    assert after.resources == []


def test_bind_skill_still_rejects_colon(
    tmp_path: Path, skills_dir: Path, tool_registry: ToolRegistry
) -> None:
    """破坏者：技能 id 会拼进文件名，冒号必须继续拒绝（口径只对引用型放宽）。"""
    manager = AgentProfileManager(
        profiles_dir=str(tmp_path / "profiles-ns2"),
        skills_dir=str(skills_dir),
        tool_registry=tool_registry,
    )
    pid = _create(manager)
    with pytest.raises(AgentProfileError):
        manager.bind_resource(pid, "general_skill", "kb:evil")


def test_bind_resource_rejects_path_separator(
    tmp_path: Path, skills_dir: Path, tool_registry: ToolRegistry
) -> None:
    """破坏者：引用型也不能夹带路径分隔符（防越权引用）。"""
    store = KnowledgeStore(db_path=tmp_path / "kb-ns3.db")
    manager = AgentProfileManager(
        profiles_dir=str(tmp_path / "profiles-ns3"),
        skills_dir=str(skills_dir),
        tool_registry=tool_registry,
        knowledge_store=store,
    )
    pid = _create(manager)
    with pytest.raises(AgentResourceInvalidError):
        manager.bind_resource(pid, "knowledge_base", "kb/../../etc")
