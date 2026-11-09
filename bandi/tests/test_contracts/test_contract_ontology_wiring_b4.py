"""契约：本体装配与 API 接入。

B4c：把三元组本体接进生产链路。

行为不变式：

**装配（factory）**
 1. 四个本体工具在 factory 里注册（`ontology_define`/`_assert`/`_query`/`_retract`），
 用 `register_instance`（`OntologyStore` 需注入，无参 `register` 不适用）
 2. 未配置本体库时**不注册**——四工具缺席而非半残（execute 里 store 为 None 会崩）
 3. 工具 schema 通过注册门禁（`Draft202012Validator` fail-closed）

**API**
 4. `GET /api/ontology/stats` 返回四计数 + 可推出事实数
 5. `GET /api/ontology/graph` 输出 `{entities:[{id,classes,name}], edges:[{source,target,type}]}`
 —— 与旧 `/api/entities/graph` 同形，前端渲染管线可平移；`type` 取实体的
 最具体类（子类优先），无类时回落 `unknown`
 6. 边的 `objectKind=literal` 的事实**不进图**（字面量不是节点）
 7. `GET /api/ontology/facts` 支持 `includeInferred`，返回带 `via` 的事实
 8. 旧 `/api/entities*` 4 端点在 B4-删旧批次移除（此处只保证新端点可用）

**容量与截断**
 9. 图导出**不静默截断**（旧实现硬编码 limit=10000）——超限显式报 truncated"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bandi.ontology.graph_api import (
    build_graph_payload,
    facts_payload,
    stats_payload,
)
from bandi.ontology.spec import open_ontology
from bandi.ontology.triple_store import OntologyStore, StoreLimits


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    tables = open_ontology(str(tmp_path / "onto.db"))
    instance = OntologyStore(tables, StoreLimits())
    yield instance
    instance.close()


def _seed(store: OntologyStore) -> None:
    now = "2026-10-13T00:00:00Z"
    store.define_class({"id": "Person"}, now)
    store.define_class({"id": "Employee", "sub_class_of": ["Person"]}, now)
    store.define_class({"id": "Company"}, now)
    store.define_relation({"id": "worksAt", "domain": ["Person"], "range": ["Company"]}, now)
    store.define_relation({"id": "age", "range_kind": "literal"}, now)
    store.define_relation({"id": "knows", "characteristics": ["symmetric"]}, now)
    store.assert_entity({"id": "alice", "classes": ["Employee"], "label": "爱丽丝"}, now)
    store.assert_entity({"id": "bob", "classes": ["Person"]}, now)
    store.assert_entity({"id": "acme", "classes": ["Company"]}, now)
    store.assert_fact({"subject": "alice", "predicate": "worksAt", "object": "acme"}, now)
    store.assert_fact({"subject": "alice", "predicate": "knows", "object": "bob"}, now)
    store.assert_fact({"subject": "alice", "predicate": "age", "object": "30"}, now)


class TestStatsPayload:
    def test_counts_all_four(self, store: OntologyStore) -> None:
        _seed(store)
        payload = stats_payload(store)
        assert payload["classes"] == 3
        assert payload["relations"] == 3
        assert payload["entities"] == 3
        assert payload["facts"] == 3

    def test_inferred_counted(self, store: OntologyStore) -> None:
        """knows 对称 ⇒ 可推出 1 条；age 字面量不推论。"""
        _seed(store)
        assert stats_payload(store)["inferredFacts"] == 1

    def test_empty_graph_zeroes(self, store: OntologyStore) -> None:
        payload = stats_payload(store)
        assert payload == {
            "classes": 0,
            "relations": 0,
            "entities": 0,
            "facts": 0,
            "inferredFacts": 0,
        }


class TestGraphPayload:
    def test_shape_matches_legacy_contract(self, store: OntologyStore) -> None:
        """与旧 `/api/entities/graph` 同形——前端渲染管线可平移。

        另带 `truncated`/`totalEntities` 两个显式截断字段（旧实现硬编码
        limit=10000 静默截断）。
        """
        _seed(store)
        payload = build_graph_payload(store)
        assert {"entities", "edges"} <= set(payload)
        for entity in payload["entities"]:
            assert set(entity) == {"id", "type", "name"}
        for edge in payload["edges"]:
            assert set(edge) == {"source", "target", "type"}

    def test_entity_type_is_most_specific_class(self, store: OntologyStore) -> None:
        """子类优先——alice 是 Employee，直接给 Person 会让图谱颜色全同。"""
        _seed(store)
        payload = build_graph_payload(store)
        by_id = {e["id"]: e for e in payload["entities"]}
        assert by_id["alice"]["type"] == "Employee"

    def test_entity_type_prefers_class_label(self, store: OntologyStore) -> None:
        """类声明了中文标签时，type 显示标签——图例/卡片对中文用户可读。

        类 id 受标识符语法约束（ASCII），中文展示名承载在 label；载荷的 type
        按「最具体类 → label 优先」解析。
        """
        now = "2026-10-13T00:00:00Z"
        store.define_class({"id": "SalesRep", "label": "销售代表"}, now)
        store.define_class({"id": "Employee", "label": "员工"}, now)
        store.assert_entity({"id": "rep_zhang", "classes": ["SalesRep"], "label": "张伟"}, now)
        payload = build_graph_payload(store)
        by_id = {e["id"]: e for e in payload["entities"]}
        assert by_id["rep_zhang"]["type"] == "销售代表"

    def test_entity_without_class_falls_back(self, store: OntologyStore) -> None:
        now = "2026-10-13T00:00:00Z"
        store.assert_entity({"id": "loner"}, now)
        payload = build_graph_payload(store)
        assert next(e for e in payload["entities"] if e["id"] == "loner")["type"] == "unknown"

    def test_literal_facts_excluded_from_edges(self, store: OntologyStore) -> None:
        """字面量不是节点——「alice age 30」不能变成边。"""
        _seed(store)
        edges = build_graph_payload(store)["edges"]
        assert all("30" not in (e["source"], e["target"]) for e in edges)
        assert {e["type"] for e in edges} == {"worksAt", "knows"}

    def test_entity_name_prefers_label(self, store: OntologyStore) -> None:
        _seed(store)
        payload = build_graph_payload(store)
        assert next(e for e in payload["entities"] if e["id"] == "alice")["name"] == "爱丽丝"

    def test_entity_name_falls_back_to_id(self, store: OntologyStore) -> None:
        _seed(store)
        payload = build_graph_payload(store)
        assert next(e for e in payload["entities"] if e["id"] == "bob")["name"] == "bob"

    def test_include_inferred_adds_edges(self, store: OntologyStore) -> None:
        _seed(store)
        asserted = build_graph_payload(store)["edges"]
        inferred = build_graph_payload(store, include_inferred=True)["edges"]
        assert len(inferred) > len(asserted), "对称事实应补出镜像边"

    def test_truncation_is_explicit(self, store: OntologyStore) -> None:
        """破坏者：旧实现硬编码 limit=10000 静默截断——超限必须显式报。"""
        now = "2026-10-13T00:00:00Z"
        store.define_class({"id": "P"}, now)
        store.define_relation({"id": "rel"}, now)
        for i in range(12):
            store.assert_entity({"id": f"e{i}", "classes": ["P"]}, now)
        payload = build_graph_payload(store, limit=5)
        assert len(payload["entities"]) == 5
        assert payload["truncated"] is True
        assert payload["totalEntities"] == 12

    def test_no_truncation_flag_when_fits(self, store: OntologyStore) -> None:
        _seed(store)
        assert build_graph_payload(store, limit=100)["truncated"] is False

    def test_empty_graph_returns_empty_lists(self, store: OntologyStore) -> None:
        payload = build_graph_payload(store)
        assert payload["entities"] == []
        assert payload["edges"] == []

    def test_payload_is_json_serialisable(self, store: OntologyStore) -> None:
        """端点要返回 JSON——不可序列化的对象会在上炸。"""
        _seed(store)
        json.dumps(build_graph_payload(store), ensure_ascii=False)


class TestFactsPayload:
    def test_default_excludes_inferred(self, store: OntologyStore) -> None:
        _seed(store)
        payload = facts_payload(store)
        assert len(payload["facts"]) == 3
        assert {f["via"] for f in payload["facts"]} == {"asserted"}

    def test_include_inferred_marks_via(self, store: OntologyStore) -> None:
        _seed(store)
        payload = facts_payload(store, include_inferred=True)
        assert {f["via"] for f in payload["facts"]} == {"asserted", "symmetric"}

    def test_via_tagged_fact_names_rule(self, store: OntologyStore) -> None:
        _seed(store)
        payload = facts_payload(store, include_inferred=True)
        derived = next(f for f in payload["facts"] if f["via"] == "symmetric")
        assert derived["subject"] == "bob"
        assert derived["object"] == "alice"

    def test_subject_filter(self, store: OntologyStore) -> None:
        _seed(store)
        payload = facts_payload(store, subject="alice")
        assert len(payload["facts"]) == 3
        assert facts_payload(store, subject="bob")["facts"] == []

    def test_object_kind_reported(self, store: OntologyStore) -> None:
        _seed(store)
        payload = facts_payload(store)
        kinds = {f["object"]: f["objectKind"] for f in payload["facts"]}
        assert kinds["30"] == "literal"
        assert kinds["acme"] == "entity"


class TestToolRegistration:
    def test_tools_absent_without_store(self) -> None:
        """未配置本体库时四工具缺席——不注册半残工具。"""
        from bandi.agent.factory import build_tool_registry

        registry = build_tool_registry(None)
        names = {s["name"] for s in registry.list_flat_schemas()}
        assert not (
            {"ontology_define", "ontology_assert", "ontology_query", "ontology_retract"} & names
        )

    def test_registered_schemas_are_valid(self, store: OntologyStore) -> None:
        """注册门禁 fail-closed——schema 不合法的工具进不来。"""
        from bandi.agent.factory import build_tool_registry

        registry = build_tool_registry(store)
        for schema in registry.list_flat_schemas():
            if schema["name"].startswith("ontology_"):
                assert schema.get("description"), f"{schema['name']} 缺描述"
                assert schema.get("parameters"), f"{schema['name']} 缺参数 schema"

    def test_real_wiring_kernel_assembly_registers_four_tools(
        self, store: OntologyStore, tmp_path: Path
    ) -> None:
        """装配点对齐：镜像真实内核装配 `_build_tool_registry_with_config`。

        真实内核（agent_runner）第一参传 None（旧实体 store 已随删旧批次移除），
        三元组 store 以关键字传入。历史 bug 是把三元组 store 当第二位置参数传，
        绑定到了 `ontology_inference`——四工具从未注册而契约全绿。"""
        from bandi.core.agent_runner import _build_tool_registry_with_config
        from bandi.core.config import BandiConfig

        registry = _build_tool_registry_with_config(None, BandiConfig(), None, None, store)
        names = {s["name"] for s in registry.list_flat_schemas()}
        assert {
            "ontology_define",
            "ontology_assert",
            "ontology_query",
            "ontology_retract",
        } <= names, f"内核装配下四工具未注册：{sorted(names)}"
        assert "entity_create" not in names

    def test_triple_store_alone_still_registers(self, store: OntologyStore) -> None:
        """兼容路径：首参就是三元组 store（无旧 store）时四工具同样注册。"""
        from bandi.agent.factory import build_tool_registry

        registry = build_tool_registry(store)
        names = {s["name"] for s in registry.list_flat_schemas()}
        assert {"ontology_define", "ontology_assert", "ontology_query", "ontology_retract"} <= names

    def test_query_tool_is_read_only(self, store: OntologyStore) -> None:
        """查询是只读工具——须进只读名单，否则会被 plan 模式拒掉。"""
        from bandi.tools.registry import ToolRegistry

        assert "ontology_query" in ToolRegistry.READONLY_TOOL_NAMES

    def test_adapter_execute_matches_framework_call_convention(self, store: OntologyStore) -> None:
        """装配点对齐：executor 以**位置参数**调 execute
        （`executor.py:1220`：`tool.execute(tc.arguments, **kwargs)`）。

        适配器曾用 `**kwargs` 签名——探针按关键字调用掩盖了差异，报
        `takes 1 positional argument but 2 were given`。本条按框架方式调用。"""
        import asyncio

        from bandi.agent.factory import build_tool_registry

        registry = build_tool_registry(store)
        tool = registry.get("ontology_query")
        assert tool is not None
        output = asyncio.run(tool.execute({"mode": "stats"}))  # 位置传参，同 executor
        assert output, "execute 返回空串"

    def test_adapter_accepts_permission_context_kwarg(self, store: OntologyStore) -> None:
        """executor 可能透传 context 关键字——签名必须能接。"""
        import asyncio

        from bandi.agent.factory import build_tool_registry

        registry = build_tool_registry(store)
        tool = registry.get("ontology_define")
        assert tool is not None
        output = asyncio.run(tool.execute({"classes": [{"id": "Probe", "label": "探针"}]}, None))
        assert "Probe" in output

    def test_adapter_reports_bad_args_as_text(self, store: OntologyStore) -> None:
        """破坏者：参数错误不得抛出炸 Agent 循环——返回可读文本。"""
        import asyncio

        from bandi.agent.factory import build_tool_registry

        registry = build_tool_registry(store)
        tool = registry.get("ontology_query")
        output = asyncio.run(tool.execute({"mode": "neighbors"}))  # 缺 entity
        assert "错误" in output


class TestStringifiedParamsTolerance:
    """（DeepSeek）：数组/对象参数会被双重编码成 JSON 字符串——
    适配器须在校验前还原，否则 `ontology_assert` 永远「参数校验失败」。"""

    def test_stringified_array_params_coerced_before_validation(self, store: OntologyStore) -> None:
        from bandi.ontology.tools import OntologyToolContext
        from bandi.tools.ontology_tool_adapter import build_ontology_tools

        _seed(store)
        tools = {t.name: t for t in build_ontology_tools(OntologyToolContext(store=store))}
        tool = tools["ontology_assert"]
        args = {
            "entities": json.dumps(
                [{"id": "carol", "classes": ["Person"], "label": "卡罗"}], ensure_ascii=False
            ),
            "facts": json.dumps(
                [{"subject": "carol", "predicate": "knows", "object": "alice"}],
                ensure_ascii=False,
            ),
        }
        normalized = tool.normalize_params(args)
        assert isinstance(normalized["entities"], list), "字符串化 entities 未还原为数组"
        assert isinstance(normalized["facts"], list), "字符串化 facts 未还原为数组"
        assert tool.validate_params(normalized) == [], "还原后必须通过 JSON Schema 校验"

    def test_stringified_params_execute_end_to_end(self, store: OntologyStore) -> None:
        import asyncio

        from bandi.ontology.tools import OntologyToolContext
        from bandi.tools.ontology_tool_adapter import build_ontology_tools

        _seed(store)
        tools = {t.name: t for t in build_ontology_tools(OntologyToolContext(store=store))}
        tool = tools["ontology_assert"]
        # 实体与事实成对断言（孤立实体会被守卫回滚，见 orphan-guard 契约）
        args = {
            "entities": json.dumps(
                [{"id": "carol", "classes": ["Person"], "label": "卡罗"}], ensure_ascii=False
            ),
            "facts": json.dumps(
                [{"subject": "carol", "predicate": "knows", "object": "bob"}], ensure_ascii=False
            ),
        }
        output = asyncio.run(tool.execute(tool.normalize_params(args), None))
        assert "1 个实体" in output, f"字符串容错后应断言成功，实际输出：{output}"
        assert "1 条事实" in output, f"字符串容错后事实应一并落库，实际输出：{output}"
        assert store.graph().entities.get("carol") is not None, "字符串容错后实体必须落库"

    def test_stringified_nested_class_list_coerced(self, store: OntologyStore) -> None:
        """破坏者：深层嵌套也可能被字符串化（classes 列表）——递归还原。"""
        from bandi.ontology.tools import OntologyToolContext
        from bandi.tools.ontology_tool_adapter import build_ontology_tools

        _seed(store)
        tools = {t.name: t for t in build_ontology_tools(OntologyToolContext(store=store))}
        tool = tools["ontology_assert"]
        args = {"entities": [{"id": "dave", "classes": json.dumps(["Person"])}]}
        normalized = tool.normalize_params(args)
        assert normalized["entities"][0]["classes"] == ["Person"]
        assert tool.validate_params(normalized) == []

    def test_stringified_scalar_params_coerced(self, store: OntologyStore) -> None:
        """破坏者：布尔/整数也可能被字符串化——按 schema 还原为标量，否则查询永远校验失败。"""
        from bandi.ontology.tools import OntologyToolContext
        from bandi.tools.ontology_tool_adapter import build_ontology_tools

        _seed(store)
        tools = {t.name: t for t in build_ontology_tools(OntologyToolContext(store=store))}
        query = tools["ontology_query"]
        normalized = query.normalize_params({"mode": "entities", "includeInferred": "True"})
        assert normalized["includeInferred"] is True
        assert query.validate_params(normalized) == []
        normalized2 = query.normalize_params({"mode": "neighbors", "entity": "alice", "depth": "3"})
        assert normalized2["depth"] == 3
        assert query.validate_params(normalized2) == []
