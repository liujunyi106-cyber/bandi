"""
契约族：ask 工具名单单一来源（双源默认值钉死相等）。

背景：
- `core/config.py::PermissionsConfig.ask_tools` 是唯一真实默认源（3 成员）。
- `tools/permissions_pipeline.py::_ASK_TOOLS` 是管道缺省回落名单，注释承诺
  与 config 保持相等，但历史上曾漂移为 5 成员（config=3 生效，`_ASK_TOOLS`
  多出的成员静默无效），且从未有契约钉死相等。

本契约钉的行为族（生产调用路径）：
- `tools/permissions_pipeline.py::PermissionsPipeline.__init__`（缺省回落名单）
- `core/config.py::PermissionsConfig.ask_tools`（唯一真实默认源）
- `agent/factory.py` 装配点（config 透传，见 factory 契约）

断言「最终产物」：装入许可证名单须与 config 默认完全相等；双源任一成员
漂移即契约红；`_ASK_TOOLS` 为模块常量子集防未来改类型。
"""

from __future__ import annotations

from bandi.core.config import PermissionsConfig
from bandi.tools.permissions_pipeline import _ASK_TOOLS, PermissionsPipeline


def test_contract_ask_tools_module_constant_matches_config_default() -> None:
    """`_ASK_TOOLS` 模块常量必须与 config 仅真实默认源完全相等（防漂移）。"""

    cfg_default = set(PermissionsConfig().ask_tools)
    assert _ASK_TOOLS == cfg_default, (
        f"_ASK_TOOLS 与 config 默认漂移：{sorted(_ASK_TOOLS)} vs {sorted(cfg_default)}"
    )


def test_contract_ask_tools_pipeline_default_falls_back_to_single_source() -> None:
    """管道缺省回落必须等于 config 默认——显式声明与默认回落两条生产路径同源。"""
    cfg_injected = PermissionsConfig(ask_tools=["bash", "write"])

    p_from_cfg = PermissionsPipeline(ask_tools=set(cfg_injected.ask_tools))
    assert p_from_cfg._ask_tools == set(cfg_injected.ask_tools)

    p_default = PermissionsPipeline()
    assert p_default._ask_tools == _ASK_TOOLS == set(PermissionsConfig().ask_tools)


def test_contract_ask_tools_entity_create_in_ask_list() -> None:
    """本体写工具在 ask 名单（ontology_define/assert 写 schema/事实须确认）；旧 entity_create 已随删旧批次移除。"""
    assert "ontology_define" in _ASK_TOOLS
    assert "ontology_assert" in _ASK_TOOLS
    assert "entity_create" not in _ASK_TOOLS
    assert "bash" in _ASK_TOOLS
    assert "write" in _ASK_TOOLS
