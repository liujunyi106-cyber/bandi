# 功能：契约——会话内资源配置工具（创建 / 更新 / 绑定员工）。
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from bandi.tools.base import ToolPermissionContext


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import bandi.knowledge.store as knowledge_store

    knowledge_store._SHARED_STORE = None
    yield tmp_path
    knowledge_store._SHARED_STORE = None


def _run(tool: Any, params: dict[str, Any], employee_id: str = "emp-1") -> dict[str, Any]:
    context = ToolPermissionContext(extra={"employee_id": employee_id})
    return asyncio.run(tool.execute(params, context))


def test_contract_resource_tool_creates_and_binds_skill(home: Path) -> None:
    from bandi.company.agent_profile import AgentProfileManager
    from bandi.tools.resource_tool import ResourceConfigureTool

    result = _run(
        ResourceConfigureTool(),
        {
            "kind": "skill",
            "name": "self-skill",
            "content": "# Self Skill\n\n执行步骤。",
        },
    )
    assert result["success"] is True, result
    assert result["bound_to"] == "emp-1"
    assert (home / ".bandi" / "skills" / "self-skill" / "SKILL.md").is_file()
    manager = AgentProfileManager(
        profiles_dir=str(home / ".bandi" / "agents" / "profiles"),
        skills_dir=str(home / ".bandi" / "skills"),
    )
    profile = manager.get_profile("emp-1")
    assert profile is not None
    assert any(
        str(item.resource_type) == "general_skill" and item.resource_id == "self-skill"
        for item in profile.resources
    )

    updated = _run(
        ResourceConfigureTool(),
        {
            "kind": "skill",
            "name": "self-skill",
            "content": "# Self Skill\n\n更新后的步骤。",
        },
    )
    assert updated["success"] is True, updated
    text = (home / ".bandi" / "skills" / "self-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "更新后的步骤" in text


def test_contract_resource_tool_creates_sop_and_rejects_doc(home: Path) -> None:
    from bandi.company.agent_profile import AgentProfileManager
    from bandi.tools.resource_tool import ResourceConfigureTool

    tool = ResourceConfigureTool()
    rejected = _run(
        tool,
        {
            "kind": "doc",
            "name": "客服口径",
            "content": "# 客服口径\n\n先确认问题，再给方案。",
        },
    )
    assert rejected["success"] is False
    assert "kind" in rejected["error"]

    card = {
        "skill_id": "verify-sop",
        "name": "验证 SOP",
        "version": "1.0.0",
        "description": "验证用",
        "goal": ["完成验证"],
        "nodes": [
            {"node_id": "start", "name": "开始", "instruction": "第一步"},
            {"node_id": "end", "name": "结束", "instruction": "最后一步"},
        ],
        "edges": [{"source_node_id": "start", "next_node_id": "end"}],
        "start_node_id": "start",
        "terminal_node_ids": ["end"],
    }
    sop = _run(
        tool,
        {"kind": "sop", "name": "verify-sop", "content": json.dumps(card, ensure_ascii=False)},
    )
    assert sop["success"] is True, sop
    assert (home / ".bandi" / "skills" / "verify-sop.card.json").is_file()
    manager = AgentProfileManager(
        profiles_dir=str(home / ".bandi" / "agents" / "profiles"),
        skills_dir=str(home / ".bandi" / "skills"),
    )
    profile = manager.get_profile("emp-1")
    assert profile is not None
    assert any(
        str(item.resource_type) == "skill" and item.resource_id == "verify-sop"
        for item in profile.resources
    )
