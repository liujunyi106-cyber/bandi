from __future__ import annotations

from typing import Any

import pytest

from bandi.core.config import BandiConfig, FeatureFlagsConfig
from bandi.tools.base import ToolCapability
from bandi.tools.feature_flags import FeatureGate
from bandi.tools.registry import ToolRegistry


class _FlaggedTool:
    """最小工具桩：仅暴露 判定所依赖的元数据面（注册器消费的单元）。"""

    name: str
    description: str = "desc"
    category: str = "util"
    enabled: bool = True
    tags: list[str] = ["util"]
    aliases: tuple[str, ...] = ()
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    feature_group: str | None = None
    capability_tags: list[str] = []

    def __init__(self, name: str, feature_group: str | None = None) -> None:
        self.name = name
        self.feature_group = feature_group

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_tool_meta(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "feature_group": self.feature_group,
            "capability_tags": list(self.capability_tags),
        }


def _base_registry(*tools: _FlaggedTool) -> ToolRegistry:
    registry = ToolRegistry()
    for t in tools:
        registry.register_instance(t)
    return registry


# --- 族1：gate 关闭 → 全下游 fail-closed（get/list/schemas/scope 统一不可见） ---


def test_gate_off_tool_invisible_across_all_public_paths() -> None:
    """gate 关闭的 feature_group 工具：get()/list_tools/list_schemas/list_flat_schemas/scoped 全不可见。"""
    registry = ToolRegistry(feature_flags={"workflow": False})
    workflow_tool = _FlaggedTool("workflow", feature_group="workflow")
    normal_tool = _FlaggedTool("read")
    registry.register_instance(workflow_tool)
    registry.register_instance(normal_tool)

    assert registry.get("workflow") is None, "gate 关的工作流工具 get() 必须不可见"
    assert [t.name for t in registry.list_tools()] == ["read"], "list_tools 应只剩未入组工具"
    names = [s["function"]["name"] for s in registry.list_schemas()]
    assert names == ["read"], "list_schemas 不应含 gate 关的工具"
    flat = [s["name"] for s in registry.list_flat_schemas()]
    assert flat == ["read"], "list_flat_schemas 不应含 gate 关的工具"
    assert registry.scoped_role("admin").get("workflow") is None, "scoped_role 包装下仍不可见"


def test_gate_off_does_not_affect_untagged_tools() -> None:
    """gate 只影响声明 feature_group 的工具；feature_group=None 的工具零影响（破坏者绕过起点）。"""
    registry = ToolRegistry(feature_flags={"workflow": False})
    registry.register_instance(_FlaggedTool("apply_patch", feature_group=None))
    registry.register_instance(_FlaggedTool("bash", feature_group=None))
    assert {t.name for t in registry.list_tools()} == {"apply_patch", "bash"}, (
        "无 feature_group 工具不受 gate 影响"
    )


def test_gate_off_with_aliased_exact_lookup_still_blocked() -> None:
    """即便经别名路径查找，gate 关工具仍不可见（防别名绕过 get）。"""
    registry = ToolRegistry(feature_flags={"workflow": False})
    tool = _FlaggedTool("workflow_run", feature_group="workflow")
    tool.aliases = ("workflow",)
    registry.register_instance(tool)
    assert registry.get("workflow") is None and registry.get("workflow_run") is None


def test_legacy_stub_without_new_fields_registers_unaffected() -> None:
    """未声明 feature_group/capability_tags 的历史工具/桩 → 未声明语义，正常注册（向后兼容）。"""

    class _LegacyStub:
        name: str = "legacy"
        description: str = "d"
        category: str = "util"
        enabled: bool = True
        tags: list[str] = ["util"]
        aliases: tuple[str, ...] = ()
        parameters: dict[str, Any] = {"type": "object", "properties": {}}

        def to_openai_schema(self) -> dict[str, Any]:
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": self.description,
                    "parameters": self.parameters,
                },
            }

        def to_tool_meta(self) -> dict[str, Any]:
            return {
                "name": self.name,
                "description": self.description,
                "category": self.category,
                "feature_group": None,
                "capability_tags": [],
            }

    registry = ToolRegistry(feature_flags={"workflow": False})
    registry.register_instance(_LegacyStub())
    assert registry.get("legacy") is not None
    assert registry.list_tool_meta()[0]["capability_tags"] == []


def test_default_no_flags_all_tools_visible() -> None:
    """未传 feature_flags（默认空）→ 全部工具可见，零迁移。"""
    registry = ToolRegistry()
    registry.register_instance(_FlaggedTool("workflow", feature_group="workflow"))
    registry.register_instance(_FlaggedTool("read"))
    assert {t.name for t in registry.list_tools()} == {"workflow", "read"}


def test_gate_on_tools_visible() -> None:
    """gate 显式开启 → 工具可见（显式 True 与缺省等价）。"""
    registry = ToolRegistry(feature_flags={"workflow": True})
    registry.register_instance(_FlaggedTool("workflow", feature_group="workflow"))
    assert registry.get("workflow") is not None


def test_gate_only_touches_declared_group() -> None:
    """gate 配置无关组名 → 不误伤其他 feature_group（如关闭 'workflow' 不影响 'doc' 组）。"""
    registry = ToolRegistry(feature_flags={"workflow": False})
    registry.register_instance(_FlaggedTool("pdf_read", feature_group="doc"))
    assert registry.get("pdf_read") is not None


# --- 族3：ToolCapability 8 标签（暴露 + 合法性 + 未声明降级） ---


def test_capability_tags_surface_in_meta() -> None:
    """list_tool_meta 暴露 capability_tags；未声明工具降级为空集（不炸，合法默认）。"""
    registry = ToolRegistry()
    file_tool = _FlaggedTool("read")
    file_tool.capability_tags = [ToolCapability.FILE_READ.value, ToolCapability.FILE_WRITE.value]
    registry.register_instance(file_tool)
    registry.register_instance(_FlaggedTool("plain"))

    meta = {m["name"]: m for m in registry.list_tool_meta()}
    assert set(meta["read"]["capability_tags"]) == {"file_read", "file_write"}
    assert meta["plain"]["capability_tags"] == [], "未声明标签降级为空集"


def test_capability_tag_enum_covers_eight_standard_tags() -> None:
    """ToolCapability 八标签全量：FILE_READ/FILE_WRITE/NETWORK/CODE_EXEC/SHELL/DATABASE/LLM_CALL/DANGEROUS。"""
    expected = {
        "file_read",
        "file_write",
        "network",
        "code_exec",
        "shell",
        "database",
        "llm_call",
        "dangerous",
    }
    assert {t.value for t in ToolCapability} == expected, "8 标签枚举缺漏"


def test_capability_tags_validation_rejects_unknown() -> None:
    """capability_tags 含未知标签 → FeatureGate 校验拒绝（fail-closed，防手滑输错静默通过）。"""
    registry = ToolRegistry(feature_flags={"workflow": False})
    bad = _FlaggedTool("x", feature_group="workflow")
    bad.capability_tags = ["unknown_tag"]
    with pytest.raises(ValueError):
        registry.register_instance(bad)


# --- 族4：FeatureGate 纯解析器（dict 合并/校验/默认值） ---


def test_feature_gate_true_merges_with_default_true() -> None:
    """FeatureGate 语义：缺省 True；显式 False 关闭；显式 True 只为覆盖。"""
    gate = FeatureGate({"workflow": False, "doc": True})
    assert gate.is_enabled("workflow") is False
    assert gate.is_enabled("doc") is True
    assert gate.is_enabled("unlisted") is True, "未列出的组默认开放"


def test_feature_gate_rejects_non_bool() -> None:
    """feature_flags 值非 bool → 校验拒绝（配置失误 fail-fast）。"""
    with pytest.raises(ValueError):
        FeatureGate({"workflow": "yes"})


# --- 族5：config 装配（FeatureFlagsConfig → BandiConfig → 注册表判定） ---


def test_feature_flags_config_loads_tool_groups() -> None:
    """FeatureFlagsConfig 从 config 读入 tool_groups 映射（默认空=全开）。"""
    cfg = FeatureFlagsConfig(tool_groups={"workflow": False, "doc": True})
    gate = FeatureGate(cfg.tool_groups)
    assert gate.is_enabled("workflow") is False
    assert gate.is_enabled("doc") is True
    cfg_default = FeatureFlagsConfig()
    assert cfg_default.tool_groups == {}, "默认零配置"


def test_bandi_config_embeds_feature_flags() -> None:
    """BandiConfig.tools.feature_flags 装配存在（默认 dataclass 空=未列组全开，config 可覆盖）。"""
    cfg = BandiConfig()
    assert cfg.tools.feature_flags.tool_groups == {}


# --- 族6：capability_tags 不污染 OpenAI schema（LLM 面兼容零破坏） ---


def test_capability_tags_not_in_openai_schema() -> None:
    """capability_tags 仅经 list_tool_meta 暴露，不进 OpenAI function schema（LLM 面零变化）。"""
    registry = ToolRegistry()
    tool = _FlaggedTool("read")
    tool.capability_tags = [ToolCapability.FILE_READ.value]
    registry.register_instance(tool)
    schema = registry.list_schemas()[0]["function"]
    assert "capability_tags" not in schema, "OpenAI schema 不得含 capability_tags"
    assert "feature_group" not in schema


def test_production_tool_schema_excludes_capability_meta() -> None:
    """真实工具（非桩）to_openai_schema 不含 capability_tags/feature_group（生产 BaseTool 面零污染）。"""
    from bandi.tools.apply_patch import ApplyPatchTool

    tool = ApplyPatchTool()
    schema = tool.to_openai_schema()["function"]
    assert "capability_tags" not in schema
    assert "feature_group" not in schema


def test_unknown_feature_group_detection() -> None:
    """配置 feature_flags 含无工具声明的组 → check_feature_group_coverage() 显式暴露（防拼写漂移 fail-open）。"""
    registry = ToolRegistry(feature_flags={"workflow-tools": False, "workflow": False})
    registry.register_instance(_FlaggedTool("wf", feature_group="workflow"))
    assert registry.check_feature_group_coverage() == ["workflow-tools"]
    assert registry.get("wf") is None


def test_matching_feature_groups_not_flagged() -> None:
    """配置组与已声明组完全匹配 → 零未知组（不误报）。"""
    registry = ToolRegistry(feature_flags={"workflow": False})
    registry.register_instance(_FlaggedTool("wf", feature_group="workflow"))
    assert registry.check_feature_group_coverage() == []


def test_assembly_warns_on_unknown_feature_group(caplog: pytest.LogCaptureFixture) -> None:
    """生产装配 build_tool_registry(feature_flags=未知组) → logger 警告暴露组名（关闭意图不被拼写漂移吞掉）。"""
    import logging

    from bandi.agent.factory import build_tool_registry

    with caplog.at_level(logging.WARNING):
        build_tool_registry(feature_flags={"workflow-tools": False})
    assert any("workflow-tools" in r.message for r in caplog.records), "未知组必须启动警告"


def test_assembly_no_unknown_warning_when_flags_default(caplog: pytest.LogCaptureFixture) -> None:
    """配置默认空（零迁移）→ 不触发任何未知组警告。"""
    import logging

    from bandi.agent.factory import build_tool_registry

    with caplog.at_level(logging.WARNING):
        build_tool_registry()
    assert not any("未知组" in r.message for r in caplog.records), "默认零配置不得报警"
