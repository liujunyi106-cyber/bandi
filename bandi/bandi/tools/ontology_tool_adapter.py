"""本体工具的框架适配层：把 `ontology.tools` 的四工具包成 `BaseTool` 实例。

`ontology/tools.py` 保持与参考源同构的纯函数形态（不依赖 agent 框架），适配层
负责：拼参数 schema、标只读/破坏性、渲染人读输出。

只读性：`ontology_query` 只读；`ontology_retract` 破坏性（会删事实与实体）；
`ontology_define`/`ontology_assert` 是写入但不删除。
"""

from __future__ import annotations

import json
from typing import Any

from bandi.ontology.tools import OntologyTool, OntologyToolContext
from bandi.tools.base import BaseTool, RetryPolicy, ToolCategory, ToolPermissionContext

# 四工具的只读/破坏性标记
_READ_ONLY = {"ontology_query"}
_DESTRUCTIVE = {"ontology_retract"}


def _normalize_schema(node: Any) -> Any:
    """功能：递归把参考源的 `required: True` 标注转成 JSON Schema 的顶层数组。

    参考源用 `{name: {type, required: true, description}}` 的扁平标注；JSON Schema
    要求 `required` 是数组且只出现在对象节点顶层——故须逐层下钻转换。
    """
    if isinstance(node, list):
        return [_normalize_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    required: list[str] = []
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            properties: dict[str, Any] = {}
            for name, spec in value.items():
                spec = dict(spec) if isinstance(spec, dict) else spec
                if isinstance(spec, dict) and spec.pop("required", None) is True:
                    required.append(name)
                properties[name] = _normalize_schema(spec)
            out["properties"] = properties
        else:
            out[key] = _normalize_schema(value)
    if required:
        out["required"] = required
    return out


def _to_json_schema(props: dict[str, Any]) -> dict[str, Any]:
    """功能：把工具参数描述转成 JSON Schema（必填项进顶层 required 数组）。"""
    return _normalize_schema({"type": "object", "properties": props})


def _coerce_json_strings(schema: dict[str, Any], value: Any) -> Any:
    """功能：按 schema 递归把字符串化的参数还原为真实类型。

    覆盖两类真机形态：①数组/对象被双重编码成 JSON 字符串（`entities:
    "[{...}]"`）；②标量被字符串化（`includeInferred="True"`、`depth="3"`）。
    若不在校验前还原，这些参数永远过不了类型校验。解析失败保持原值，让
    校验器报出真实错误。
    """
    if isinstance(value, str):
        schema_type = schema.get("type")
        if schema_type in ("array", "object"):
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                return value
        if schema_type == "boolean":
            lowered = value.strip().lower()
            if lowered in ("true", "1"):
                return True
            if lowered in ("false", "0"):
                return False
            return value
        if schema_type == "integer":
            try:
                return int(value.strip())
            except ValueError:
                return value
        if schema_type == "number":
            try:
                return float(value.strip())
            except ValueError:
                return value
        return value
    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            return [_coerce_json_strings(item_schema, item) for item in value]
        return value
    if isinstance(value, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            return {
                key: _coerce_json_strings(props.get(key) or {}, item) for key, item in value.items()
            }
        return value
    return value


class _OntologyToolAdapter(BaseTool):
    """功能：单个本体工具的框架适配——参数校验 + 执行 + 人读渲染。"""

    def __init__(self, tool: OntologyTool, context: OntologyToolContext) -> None:
        self._tool = tool
        self._context = context
        self._ui: Any | None = None
        self.name = tool.name
        self.description = tool.description
        self.category = ToolCategory.UTIL
        self.parameters = _to_json_schema(tool.parameters)
        self.is_read_only = tool.name in _READ_ONLY
        self.is_destructive = tool.name in _DESTRUCTIVE
        self.is_idempotent = tool.name in _READ_ONLY | {"ontology_define", "ontology_assert"}
        self.risk_level = "high" if tool.name in _DESTRUCTIVE else "low"
        self.retry_policy = RetryPolicy(retryable=False) if tool.name in _DESTRUCTIVE else None
        self.capability_tags = ["database"]  # 本体读写 SQLite；标签取自受控词表
        self.feature_group = "integration"

    def bind_ui(self, ui: Any) -> None:
        """功能：绑定确认通道（UserInteraction 协议）——孤立实体守卫的人机协同入口。

        与 `request_user_input.bind_ui` 同一装配先例：registry 实例化时无 UI，
        由工厂在装配完成后回填；不绑定 = headless，守卫按 fail-closed 回滚。
        """
        self._ui = ui

    async def _guard_orphan_entities(self, value: dict[str, Any]) -> None:
        """功能：孤立实体合法性检查——本轮新增且零关系的实体走确认通道。

        确认保留 → 记 `confirmed_orphans`；拒绝或无确认通道 → 回滚实体
        （fail-closed，与仓库 headless 拒绝惯例一致）。回滚是零度实体，无级联。
        """
        orphans = value.get("orphan_entities") or []
        if not orphans:
            return
        listing = "、".join(orphans)
        granted = False
        if self._ui is not None:
            granted = bool(
                await self._ui.ask_confirmation(
                    f"以下实体已写入本体但未建立任何关系：{listing}。"
                    "本体不允许孤立实体——确认保留为独立实体？拒绝将回滚这些实体。"
                )
            )
        if granted:
            value["confirmed_orphans"] = orphans
        else:
            rolled_back: list[str] = []
            for entity_id in orphans:
                outcome = self._context.store.retract_entity(entity_id)
                if outcome.removed:
                    rolled_back.append(entity_id)
            value["rolled_back_entities"] = rolled_back
            value["entities"] = [e for e in value.get("entities") or [] if e not in rolled_back]

    def normalize_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """功能：校验前还原字符串化的 array/object 参数（executor 在
        validate_params 之前调用本方法，见 `executor.py:970`）。"""
        if not params:
            return params
        props = self.parameters.get("properties") or {}
        changed = False
        out = dict(params)
        for key, value in out.items():
            schema = props.get(key)
            if isinstance(schema, dict):
                coerced = _coerce_json_strings(schema, value)
                if coerced is not value:
                    out[key] = coerced
                    changed = True
        return out if changed else params

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> str:
        """功能：按框架约定执行——executor 以**位置参数**传 params
        （`executor.py:1220`：`tool.execute(tc.arguments, **kwargs)`）。

        签名必须与 `BaseTool.execute` 一致（`(params, context=None)`）；
        曾用 `**kwargs` 导致真机报 `takes 1 positional argument but 2 were given`。
        """
        try:
            value = self._tool.execute(dict(params or {}))
        except ValueError as exc:
            return f"参数错误：{exc}"
        except Exception as exc:  # noqa: BLE001 — 工具失败不得炸主链
            return f"本体工具 {self.name} 执行失败：{exc}"
        if self.name == "ontology_assert":
            await self._guard_orphan_entities(value)
        return self._tool.render(value)


def build_ontology_tools(context: OntologyToolContext) -> list[BaseTool]:
    """功能：把四工具包成框架可注册的 `BaseTool` 实例。"""
    from bandi.ontology.tools import create_ontology_tools

    return [_OntologyToolAdapter(tool, context) for tool in create_ontology_tools(context)]


__all__ = ["build_ontology_tools"]
