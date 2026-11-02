# 功能：招聘助手 MVP 契约——秘书组织构建 actions（自动度 B）
# 行为族：LLM 产 register_role/register_employee 草案（不落地）→ 用户 confirm_org_build
# 显式确认 → 批量落地（逐条 fail-closed，单条失败不中断）；草案文件持久化
# （跨 SecretaryService 实例存活——生产 _secretary_chain 每次新建实例）。

from __future__ import annotations

import json

import pytest

from bandi.company.org_engine import OrgEngine
from bandi.company.secretary import SecretaryService
from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


def _service(tmp_path, org: OrgEngine | None, *responses: str) -> SecretaryService:
    llm = MockProvider()
    for r in responses:
        llm.add_response(LLMResponse(content=r))
    return SecretaryService(base_dir=str(tmp_path / "secretary"), llm=llm, org_engine=org)


@pytest.mark.asyncio
async def test_register_role_action_saves_draft_without_applying(tmp_path):
    # 自动度 B 核心：组织构建 action 只入草案，不得直接落地
    org = OrgEngine()
    payload = json.dumps(
        {
            "response": "建议新增开发角色",
            "actions": [
                {
                    "kind": "register_role",
                    "role_id": "dev",
                    "name": "Developer",
                    "responsibility": "Writes code",
                    "tool_tags": ["fs", "bash"],
                }
            ],
        },
        ensure_ascii=False,
    )
    secretary = _service(tmp_path, org, payload)
    result = await secretary.handle_message("帮我加个开发岗", session_id="s1")
    assert org.get_role("dev") is None, "未确认前角色不得落地"
    assert any("draft" in a.lower() for a in result["applied_actions"])


@pytest.mark.asyncio
async def test_confirm_org_build_applies_role_and_employee(tmp_path):
    org = OrgEngine()
    draft_payload = json.dumps(
        {
            "response": "草案：1 角色 1 员工",
            "actions": [
                {
                    "kind": "register_role",
                    "role_id": "dev",
                    "name": "Developer",
                    "responsibility": "Writes code",
                },
                {
                    "kind": "register_employee",
                    "employee_id": "alice",
                    "name": "Alice",
                    "role_id": "dev",
                    "description": "Senior dev",
                    "llm_config": {"default_model": "deepseek-v4-flash"},
                    "execution_agent": "opencode",
                },
            ],
        },
        ensure_ascii=False,
    )
    confirm_payload = json.dumps(
        {
            "response": "已确认",
            "actions": [{"kind": "confirm_org_build", "draft_id": "latest"}],
        },
        ensure_ascii=False,
    )
    secretary = _service(tmp_path, org, draft_payload, confirm_payload)
    await secretary.handle_message("建团队", session_id="s1")
    result = await secretary.handle_message("确认", session_id="s1")
    role = org.get_role("dev")
    assert role is not None and role.name == "Developer"
    emp = org.get_employee("alice")
    assert emp is not None
    assert emp.llm_config == {"default_model": "deepseek-v4-flash"}
    assert emp.metadata.get("selected_execution_agent") == "opencode"
    assert any("registered role" in a for a in result["applied_actions"])
    assert any("registered employee" in a for a in result["applied_actions"])


@pytest.mark.asyncio
async def test_draft_persists_across_instances(tmp_path):
    # 生产 _secretary_chain 每次 handle_message 新建实例——草案必须文件持久化
    org = OrgEngine()
    draft_payload = json.dumps(
        {
            "response": "草案",
            "actions": [{"kind": "register_role", "role_id": "qa", "name": "QA"}],
        },
        ensure_ascii=False,
    )
    confirm_payload = json.dumps(
        {"response": "ok", "actions": [{"kind": "confirm_org_build"}]},
        ensure_ascii=False,
    )
    await _service(tmp_path, org, draft_payload).handle_message("建 QA 岗", session_id="s1")
    # 新实例（同一 base_dir）确认——内存态草案会丢，文件持久化才能过
    second = _service(tmp_path, org, confirm_payload)
    result = await second.handle_message("确认", session_id="s2")
    assert org.get_role("qa") is not None
    assert any("registered role" in a for a in result["applied_actions"])


@pytest.mark.asyncio
async def test_confirm_reports_per_item_failures(tmp_path):
    # 批量落地逐条 fail-closed：employee 指向未注册角色 → 该条失败，其余照常
    org = OrgEngine()
    draft_payload = json.dumps(
        {
            "response": "草案",
            "actions": [
                {"kind": "register_role", "role_id": "dev", "name": "Developer"},
                {
                    "kind": "register_employee",
                    "name": "Bob",
                    "role_id": "ghost-role",
                },
            ],
        },
        ensure_ascii=False,
    )
    confirm_payload = json.dumps(
        {"response": "ok", "actions": [{"kind": "confirm_org_build"}]},
        ensure_ascii=False,
    )
    secretary = _service(tmp_path, org, draft_payload, confirm_payload)
    await secretary.handle_message("建团队", session_id="s1")
    result = await secretary.handle_message("确认", session_id="s1")
    assert org.get_role("dev") is not None, "合法条目必须落地"
    assert org.get_employee("Bob") is None
    assert any("failed" in a.lower() for a in result["applied_actions"])


@pytest.mark.asyncio
async def test_org_engine_missing_fails_closed(tmp_path):
    payload = json.dumps(
        {
            "response": "草案",
            "actions": [{"kind": "register_role", "role_id": "dev", "name": "Developer"}],
        },
        ensure_ascii=False,
    )
    secretary = _service(tmp_path, None, payload)
    result = await secretary.handle_message("建角色", session_id="s1")
    assert any("skipped" in a.lower() for a in result["applied_actions"])


def test_system_prompt_documents_org_build_kinds():
    prompt = SecretaryService._system_prompt()
    assert "register_role" in prompt
    assert "register_employee" in prompt
    assert "confirm_org_build" in prompt


@pytest.mark.asyncio
async def test_use_team_template_action_draft_then_confirm(tmp_path):
    """富化版：LLM 指定团队模板 → 草案（不落地）→ confirm 实例化（同自动度 B）。"""
    from bandi.company.team_template_store import TeamTemplateStore

    org = OrgEngine()
    store = TeamTemplateStore(templates_dir=str(tmp_path / "teams"))
    draft_payload = json.dumps(
        {
            "response": "建议搭一个选品团队",
            "actions": [{"kind": "use_team_template", "template_id": "selection-team"}],
        },
        ensure_ascii=False,
    )
    confirm_payload = json.dumps(
        {"response": "已确认", "actions": [{"kind": "confirm_org_build"}]},
        ensure_ascii=False,
    )
    llm = MockProvider()
    llm.add_response(LLMResponse(content=draft_payload))
    llm.add_response(LLMResponse(content=confirm_payload))
    from bandi.company.agent_profile import AgentProfileManager

    profile_manager = AgentProfileManager(profiles_dir=str(tmp_path / "profiles"))
    secretary = SecretaryService(
        base_dir=str(tmp_path / "secretary"),
        llm=llm,
        org_engine=org,
        template_store=store,
        profile_manager=profile_manager,
    )
    first = await secretary.handle_message("搞个选品团队", session_id="s1")
    assert org.get_role("selection-team_selection_lead") is None, "未确认不得落地"
    assert any("draft" in a.lower() for a in first["applied_actions"])

    result = await secretary.handle_message("确认", session_id="s1")
    assert org.get_role("selection-team_selection_lead") is not None
    assert org.get_employee("selection-team_selection_lead") is not None
    assert any("selection-team" in a for a in result["applied_actions"])
    assert profile_manager.get_profile("selection-team_selection_lead") is not None, (
        "秘书实例化必须与 CLI/API 一致建档（profile_manager 注入）"
    )


@pytest.mark.asyncio
async def test_confirm_org_build_rejects_traversal_draft_id(tmp_path):
    """破坏者：draft_id 路径穿越不得删除草案目录外文件。"""
    org = OrgEngine()
    victim = tmp_path / "victim.json"
    victim.write_text('{"actions": []}', encoding="utf-8")
    payload = json.dumps(
        {
            "response": "确认",
            "actions": [{"kind": "confirm_org_build", "draft_id": "../../victim"}],
        },
        ensure_ascii=False,
    )
    secretary = _service(tmp_path, org, payload)
    result = await secretary.handle_message("确认", session_id="s1")
    assert victim.exists(), "越界 draft_id 不得删除草案目录外文件"
    assert any("failed" in a.lower() for a in result["applied_actions"])


@pytest.mark.asyncio
async def test_confirm_org_build_handles_malformed_draft(tmp_path):
    """破坏者：草案文件为 JSON 数组/字符串 → 不崩，显式失败。"""
    org = OrgEngine()
    drafts_dir = tmp_path / "secretary" / "org_drafts"
    drafts_dir.mkdir(parents=True)
    (drafts_dir / "bad.json").write_text("[]", encoding="utf-8")
    payload = json.dumps(
        {"response": "确认", "actions": [{"kind": "confirm_org_build"}]},
        ensure_ascii=False,
    )
    secretary = _service(tmp_path, org, payload)
    result = await secretary.handle_message("确认", session_id="s1")
    assert any("failed" in a.lower() for a in result["applied_actions"])


def test_system_prompt_documents_team_template_kind():
    prompt = SecretaryService._system_prompt()
    assert "use_team_template" in prompt
