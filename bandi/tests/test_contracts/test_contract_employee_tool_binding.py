# 功能：契约测试——员工档案里的工具绑定必须在运行时收窄工具面（此前只是记账）
from __future__ import annotations

from typing import Any

import pytest

from bandi.agent import factory as factory_mod
from bandi.core import agent_runner as runner_mod
from bandi.tools.registry import ToolRegistry, with_always_available


class _Tool:
    """功能：注册表工具桩——只需 name/tags。"""

    def __init__(self, name: str, tags: list[str] | None = None) -> None:
        self.name = name
        self.tags = tags or []


class _Registry:
    """功能：注册表桩——只实现被消费的 list_tools。"""

    def __init__(self, names: list[str]) -> None:
        self._tools = [_Tool(name) for name in names]

    def list_tools(self) -> list[_Tool]:
        return list(self._tools)


class _Binding:
    """功能：档案资源绑定桩。"""

    def __init__(self, resource_type: str, resource_id: str, status: str = "active") -> None:
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.status = status


class _Profile:
    """功能：档案桩。"""

    def __init__(self, resources: list[_Binding]) -> None:
        self.resources = resources


ALL = ["read_file", "bash", "write", "send_dm", "request_user_input", "web_search"]


def test_no_binding_keeps_full_surface() -> None:
    """未绑定（None / 空）= 不限制，行为与改造前一致。"""
    registry = _Registry(ALL)
    assert factory_mod._narrow_tools_by_resources(ALL, registry, None) == ALL
    assert factory_mod._narrow_tools_by_resources(ALL, registry, []) == ALL


def test_binding_narrows_to_bound_tools() -> None:
    """绑了 read_file + web_search ⇒ 只剩这两个（协作工具豁免）。"""
    kept = factory_mod._narrow_tools_by_resources(ALL, _Registry(ALL), ["read_file", "web_search"])
    # 顺序沿用上游工具面顺序，只保留绑定项 + 协作豁免项
    assert kept == ["read_file", "send_dm", "web_search"]


def test_collab_tools_survive_narrowing() -> None:
    """组织结构性权利不受工具绑定影响：send_dm 必须还在。"""
    kept = factory_mod._narrow_tools_by_resources(ALL, _Registry(ALL), ["read_file"])
    assert "send_dm" in kept
    assert "bash" not in kept


def test_manager_only_collab_dropped_for_non_manager() -> None:
    """无派发面的员工卡：管理者专属协作工具不进工具面。"""
    names = ["read_file", "manager_board_read", "send_dm"]
    kept = factory_mod._narrow_tools_by_resources(
        names, _Registry(names), ["read_file"], manager_capable=False
    )
    assert "manager_board_read" not in kept
    assert "send_dm" in kept


def test_binding_cannot_resurrect_tool_filtered_earlier() -> None:
    """只收窄不放宽：绑定里写了 bash，但上游（模式/角色标签）已裁掉 ⇒ 不复活。"""
    names_without_bash = ["read_file", "send_dm"]
    kept = factory_mod._narrow_tools_by_resources(
        names_without_bash, _Registry([*names_without_bash, "bash"]), ["bash", "read_file"]
    )
    assert "bash" not in kept
    assert "read_file" in kept


def test_unknown_bound_tool_is_ignored() -> None:
    """绑定里含未注册工具：忽略并只保留有效项，不至于把工具面打成空。"""
    kept = factory_mod._narrow_tools_by_resources(ALL, _Registry(ALL), ["read_file", "not_a_tool"])
    assert "read_file" in kept
    assert "not_a_tool" not in kept


def test_empty_result_still_keeps_interaction_channel() -> None:
    """全被裁掉时（无协作项可豁免），交互通道仍由 with_always_available 兜住。"""
    names = ["read_file", "bash"]
    kept = factory_mod._narrow_tools_by_resources(names, _Registry(names), ["not_a_tool"])
    assert kept == []
    assert with_always_available(kept) == ["request_user_input"]


# ---------------------------------------------------------------- 档案读取口径


@pytest.fixture
def fake_profiles(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Profile]:
    """功能：把 AgentProfileManager 换成内存桩，避免读真实 ~/.bandi 档案。"""
    profiles: dict[str, _Profile] = {
        "emp-bound": _Profile(
            [
                _Binding("tool", "read_file"),
                _Binding("tool", "web_search"),
                _Binding("general_skill", "design-review"),
                _Binding("knowledge_base", "kb:design-org"),
                _Binding("tool", "bash", status="paused"),
            ]
        ),
        "emp-empty": _Profile([_Binding("general_skill", "design-review")]),
    }

    class _Manager:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def get_profile(self, profile_id: str) -> _Profile | None:
            return profiles.get(profile_id)

    import bandi.company.agent_profile as profile_mod

    monkeypatch.setattr(profile_mod, "AgentProfileManager", _Manager)
    return profiles


def test_bound_tool_names_reads_only_active_tool_resources(
    fake_profiles: dict[str, _Profile],
) -> None:
    """只取 active 的 tool 绑定：技能/知识库/暂停绑定都不算。"""
    names = runner_mod._employee_bound_tool_names("emp-bound")
    assert names == ["read_file", "web_search"]


def test_bound_tool_names_empty_when_no_tool_binding(
    fake_profiles: dict[str, _Profile],
) -> None:
    """只有技能绑定的员工 ⇒ 空清单 ⇒ 不收窄（不会被误判成「零工具」）。"""
    assert runner_mod._employee_bound_tool_names("emp-empty") == []


def test_bound_tool_names_tolerates_missing_profile(
    fake_profiles: dict[str, _Profile],
) -> None:
    """档案不存在 / 员工 id 为空 ⇒ 空清单，不抛错。"""
    assert runner_mod._employee_bound_tool_names("ghost") == []
    assert runner_mod._employee_bound_tool_names("") == []


def test_registry_collab_set_is_reused() -> None:
    """收窄用的豁免清单必须与角色标签路径同源（防止两处口径漂移）。"""
    assert "send_dm" in ToolRegistry.COLLAB_ALWAYS_AVAILABLE_TOOLS
    assert "manager_board_read" in ToolRegistry.COLLAB_ALWAYS_AVAILABLE_TOOLS


def test_build_agent_context_applies_bound_tools() -> None:
    """构造点 → 消费点：装配时传绑定名单，active_tools 必须真的变少。"""
    from bandi.agent.factory import build_agent_context
    from bandi.core.config import BandiConfig

    cfg = BandiConfig()
    cfg.llm.api_key = ""

    full = build_agent_context(user_input="x", bandi_config=cfg)
    narrowed = build_agent_context(
        user_input="x", bandi_config=cfg, resource_tool_allowlist=["read"]
    )

    def names(tools: list[dict[str, Any]]) -> set[str]:
        return {tool["function"]["name"] for tool in tools}

    full_names = names(full.active_tools)
    narrow_names = names(narrowed.active_tools)

    assert "read" in full_names
    assert len(narrow_names) < len(full_names)
    assert "read" in narrow_names
    assert "bash" not in narrow_names
    assert "write" not in narrow_names
    # 交互通道必须保留，否则员工无法提问
    assert "request_user_input" in narrow_names


def test_build_agent_context_without_binding_keeps_full_surface() -> None:
    """不传绑定名单时行为不变（opt-in）。"""
    from bandi.agent.factory import build_agent_context
    from bandi.core.config import BandiConfig

    cfg = BandiConfig()
    cfg.llm.api_key = ""

    base = build_agent_context(user_input="x", bandi_config=cfg)
    explicit_none = build_agent_context(
        user_input="x", bandi_config=cfg, resource_tool_allowlist=None
    )
    assert len(base.active_tools) == len(explicit_none.active_tools)
