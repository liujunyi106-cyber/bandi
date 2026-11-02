"""契约：员工绑定 SOP 卡必须进入执行上下文（流程约束可见）。"""

from __future__ import annotations

import asyncio

from bandi.company.agent_profile import AgentProfileManager
from bandi.context.sources.company_source import EmployeeSopContextSource
from bandi.core.message import Message, MessageRole
from bandi.skills.card import SkillCard, SkillGraphEdge, SkillGraphNode
from bandi.skills.serializer import save_card


def _card() -> SkillCard:
    return SkillCard(
        skill_id="pm-sop",
        name="产品经理标准流程",
        description="从需求澄清到上线复盘的标准执行流程",
        goal=["先澄清再产出", "每个阶段留下依据"],
        required_info=["业务目标", "用户场景"],
        response_rules=["未澄清不得直接产出 PRD"],
        nodes=[
            SkillGraphNode(
                node_id="clarify",
                name="需求澄清",
                instruction="先问清业务目标、用户场景与边界，记录待确认项。",
                expected_user_info=["业务目标", "用户场景"],
            ),
            SkillGraphNode(
                node_id="design",
                name="方案设计",
                instruction="输出用户画像、功能清单与页面结构，逐条标注依据。",
            ),
        ],
        edges=[SkillGraphEdge(source_node_id="clarify", next_node_id="design")],
        start_node_id="clarify",
        terminal_node_ids=["design"],
    )


def test_bound_sop_card_catalog_injected_without_body(tmp_path):
    skills_dir = tmp_path / "skills"
    profiles_dir = tmp_path / "profiles"
    save_card(_card(), skills_dir)
    manager = AgentProfileManager(profiles_dir=str(profiles_dir), skills_dir=str(skills_dir))
    manager.create_profile("emp-sop")
    manager.bind_resource("emp-sop", "skill", "pm-sop")

    source = EmployeeSopContextSource("emp-sop", profile_manager=manager, skills_dir=skills_dir)
    items = asyncio.run(
        source.collect(
            Message(role=MessageRole.USER, content="帮我做一份产品方案"),
            "session-sop",
        )
    )

    assert len(items) == 1, "普通对话只注入 SOP 目录"
    assert items[0].source_type == "employee_sop"
    assert items[0].priority == 8
    assert "【绑定 SOP】" in items[0].content
    assert "产品经理标准流程" in items[0].content
    assert "从需求澄清到上线复盘" in items[0].content
    assert "先问清业务目标" not in items[0].content, "正文不得常驻"
    assert "方案设计" not in items[0].content


def test_task_session_injects_full_sop_body(tmp_path):
    skills_dir = tmp_path / "skills"
    profiles_dir = tmp_path / "profiles"
    save_card(_card(), skills_dir)
    manager = AgentProfileManager(profiles_dir=str(profiles_dir), skills_dir=str(skills_dir))
    manager.create_profile("emp-sop")
    manager.bind_resource("emp-sop", "skill", "pm-sop")

    source = EmployeeSopContextSource("emp-sop", profile_manager=manager, skills_dir=skills_dir)
    items = asyncio.run(
        source.collect(
            Message(role=MessageRole.USER, content="执行任务"),
            "role-session::run-1",
        )
    )

    assert len(items) == 2, "任务会话必须注入完整 SOP 正文"
    assert items[1].source_type == "employee_sop"
    assert items[1].priority == 9
    assert "需求澄清" in items[1].content
    assert "先问清业务目标" in items[1].content


def test_explicit_sop_request_injects_full_body(tmp_path):
    skills_dir = tmp_path / "skills"
    profiles_dir = tmp_path / "profiles"
    save_card(_card(), skills_dir)
    manager = AgentProfileManager(profiles_dir=str(profiles_dir), skills_dir=str(skills_dir))
    manager.create_profile("emp-sop")
    manager.bind_resource("emp-sop", "skill", "pm-sop")

    source = EmployeeSopContextSource("emp-sop", profile_manager=manager, skills_dir=skills_dir)
    items = asyncio.run(
        source.collect(
            Message(role=MessageRole.USER, content="按 SOP 帮我梳理一下"),
            "session-sop",
        )
    )

    assert len(items) == 2
    assert "需求澄清" in items[1].content


def test_unbound_employee_gets_no_sop_context(tmp_path):
    skills_dir = tmp_path / "skills"
    profiles_dir = tmp_path / "profiles"
    manager = AgentProfileManager(profiles_dir=str(profiles_dir), skills_dir=str(skills_dir))
    manager.create_profile("emp-empty")

    source = EmployeeSopContextSource("emp-empty", profile_manager=manager, skills_dir=skills_dir)
    items = asyncio.run(
        source.collect(
            Message(role=MessageRole.USER, content="帮我做一份产品方案"),
            "session-empty",
        )
    )

    assert items == []
