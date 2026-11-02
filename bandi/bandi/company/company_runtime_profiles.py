# 功能：公司配置档——CompanyProfile/RoleProfile 内置档

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CompanyProfile(StrEnum):
    CORPORATE = "corporate"
    FLAT = "flat"
    CUSTOM = "custom"


@dataclass(frozen=True)
class CommunicationPolicyConfig:
    default_mode: str = "dm"
    blocking_default: bool = False
    allow_broadcast: bool = True
    meeting_required_for: tuple[str, ...] = ()
    max_daily_messages: int = 50


@dataclass(frozen=True)
class MemoryPolicyConfig:
    include_role_memory: bool = True
    include_project_memory: bool = False
    include_decision_log: bool = True
    recent_history_lines: int = 12


@dataclass(frozen=True)
class HandoffPolicyConfig:
    require_structured_handoff: bool = True
    require_ack: bool = False
    include_risks: bool = True


@dataclass(frozen=True)
class ReviewPolicyConfig:
    enable_gates: bool = True
    strict_gate_inference: bool = False
    require_reviewer_role: bool = True
    allow_human_override: bool = True


@dataclass(frozen=True)
class RuntimeProfileConfig:
    communication: CommunicationPolicyConfig = field(default_factory=CommunicationPolicyConfig)
    memory: MemoryPolicyConfig = field(default_factory=MemoryPolicyConfig)
    handoff: HandoffPolicyConfig = field(default_factory=HandoffPolicyConfig)
    review: ReviewPolicyConfig = field(default_factory=ReviewPolicyConfig)
    max_concurrent_work_items: int = 3
    allow_auto_process: bool = True


@dataclass
class RoleProfile:
    id: str
    name: str
    responsibility: str = ""
    reports_to: str | None = None
    can_spawn: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    prompt_refs: list[str] = field(default_factory=list)
    skill_refs: list[str] = field(default_factory=list)
    runtime_policy: dict[str, Any] = field(default_factory=dict)


_READ_TOOLS = ["file_read", "grep", "glob", "list_dir"]
_WRITE_TOOLS = ["file_write", "file_edit", "apply_patch"]
_SHELL_TOOLS = ["bash"]
_WEB_TOOLS = ["web_search", "web_fetch"]
_BROWSER_TOOLS = ["browser"]
_TODO_TOOLS = ["todo_read", "todo_write"]

_COORDINATION_TOOLS = [*_READ_TOOLS, *_WRITE_TOOLS, *_TODO_TOOLS]
_EXECUTION_TOOLS = [*_COORDINATION_TOOLS, *_SHELL_TOOLS, *_WEB_TOOLS, *_BROWSER_TOOLS]
_REVIEW_TOOLS = [*_READ_TOOLS, *_WEB_TOOLS]


def get_profile_description(profile: CompanyProfile) -> str:
    descriptions = {
        CompanyProfile.CORPORATE: (
            "Hierarchical corporate runtime with manager-specialist role topology. "
            "Work items flow from intake through execution and review."
        ),
        CompanyProfile.FLAT: (
            "Peer-to-peer runtime with flat role topology. "
            "All agents share full tool access and coordinate directly."
        ),
        CompanyProfile.CUSTOM: (
            "User-defined company runtime. Roles and policies are fully configurable."
        ),
    }
    return descriptions.get(profile, "")


def get_builtin_profiles() -> dict[CompanyProfile, RuntimeProfileConfig]:
    return {
        CompanyProfile.CORPORATE: RuntimeProfileConfig(
            communication=CommunicationPolicyConfig(allow_broadcast=True),
            memory=MemoryPolicyConfig(include_role_memory=True, include_decision_log=True),
            handoff=HandoffPolicyConfig(require_structured_handoff=True),
            review=ReviewPolicyConfig(enable_gates=True, require_reviewer_role=True),
            max_concurrent_work_items=3,
        ),
        CompanyProfile.FLAT: RuntimeProfileConfig(
            communication=CommunicationPolicyConfig(allow_broadcast=True),
            memory=MemoryPolicyConfig(include_role_memory=True),
            handoff=HandoffPolicyConfig(require_structured_handoff=False),
            review=ReviewPolicyConfig(enable_gates=False, require_reviewer_role=False),
            max_concurrent_work_items=5,
        ),
        CompanyProfile.CUSTOM: RuntimeProfileConfig(),
    }


def get_builtin_roles(profile: CompanyProfile) -> list[RoleProfile]:
    if profile == CompanyProfile.CORPORATE:
        return _corporate_roles()
    if profile == CompanyProfile.FLAT:
        return _flat_roles()
    return []


def _corporate_roles() -> list[RoleProfile]:
    return [
        RoleProfile(
            id="manager",
            name="Manager",
            responsibility="Task intake, work item creation, assignment, and final delivery.",
            can_spawn=["worker", "reviewer"],
            tools=_COORDINATION_TOOLS,
            prompt_refs=[
                "Decompose requests into clear work items with defined turn types.",
                "Route to appropriate workers; aggregate results.",
            ],
        ),
        RoleProfile(
            id="worker",
            name="Worker",
            responsibility="Execute assigned work items: research, code, write, analyze.",
            reports_to="manager",
            tools=_EXECUTION_TOOLS,
            prompt_refs=[
                "Complete each work item with thoroughness.",
                "Report progress and surface blockers early.",
            ],
        ),
        RoleProfile(
            id="reviewer",
            name="Reviewer",
            responsibility="Review work item outputs for quality, correctness, and completeness.",
            reports_to="manager",
            tools=_REVIEW_TOOLS,
            prompt_refs=[
                "Verify outputs against requirements.",
                "Approve, request rework, or escalate with clear rationale.",
            ],
        ),
    ]


def _flat_roles() -> list[RoleProfile]:
    return [
        RoleProfile(
            id="agent",
            name="Agent",
            responsibility="Execute all work items independently.",
            tools=_EXECUTION_TOOLS,
            prompt_refs=[
                "Complete each work item end-to-end.",
                "Signal when human input is needed.",
            ],
        ),
    ]
