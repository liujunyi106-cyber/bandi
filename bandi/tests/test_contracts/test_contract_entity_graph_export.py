# 功能：本体图谱 JSON 导出契约——GET /api/ontology/graph（前端渲染数据源）
# 结构 = {entities:[{id,type,name}], edges:[{source,target,type}]} 最小字段。
# 契约钉：全图导出、字面量不进图、推理边可显式开启、空图、截断显式回报、
# 与旧端点的关系（旧 graph 端点已按 B4 决策 D2 移除，故此处是 404 而非 200）。
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"

NOW = "2026-10-13T00:00:00Z"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with (
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._ontology_store", None),
        patch("bandi.api.app._ontology_store", None),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


def _seed() -> None:
    """造 3 实体 + 2 关系 + 1 条字面量属性：p1 → t1 → c1。"""
    from bandi.api.app import _get_ontology_store

    store = _get_ontology_store()
    store.define_class({"id": "Project"}, NOW)
    store.define_class({"id": "Task", "sub_class_of": ["Project"]}, NOW)
    store.define_class({"id": "Contact"}, NOW)
    store.define_relation({"id": "has_task", "domain": ["Project"]}, NOW)
    store.define_relation({"id": "involves", "domain": ["Task"]}, NOW)
    store.define_relation({"id": "knows", "characteristics": ["symmetric"]}, NOW)
    store.define_relation({"id": "age", "range_kind": "literal"}, NOW)
    store.assert_entity({"id": "p1", "classes": ["Project"], "label": "项目一"}, NOW)
    store.assert_entity({"id": "t1", "classes": ["Task"], "label": "任务一"}, NOW)
    store.assert_entity({"id": "c1", "classes": ["Contact"], "label": "联系人一"}, NOW)
    store.assert_fact({"subject": "p1", "predicate": "has_task", "object": "t1"}, NOW)
    store.assert_fact({"subject": "t1", "predicate": "involves", "object": "c1"}, NOW)
    store.assert_fact({"subject": "t1", "predicate": "age", "object": "30"}, NOW)


class TestGraphExport:
    def test_full_snapshot(self) -> None:
        _seed()
        resp = client.get("/api/ontology/graph")
        assert resp.status_code == 200
        data = resp.json()
        assert {e["id"] for e in data["entities"]} == {"p1", "t1", "c1"}
        edges = {(e["source"], e["target"], e["type"]) for e in data["edges"]}
        assert ("p1", "t1", "has_task") in edges
        assert ("t1", "c1", "involves") in edges
        # 最小字段：实体仅 id/type/name（不泄 comment/classes 全文）
        sample = next(e for e in data["entities"] if e["id"] == "p1")
        assert set(sample.keys()) == {"id", "type", "name"}

    def test_literal_fact_not_an_edge(self) -> None:
        """字面量不是节点——「t1 age 30」不得变成边。"""
        _seed()
        edges = client.get("/api/ontology/graph").json()["edges"]
        assert all("30" not in (e["source"], e["target"]) for e in edges)

    def test_entity_type_is_most_specific_class(self) -> None:
        """t1 声明的是 Task（Project 的子类）——type 取子类。"""
        _seed()
        by_id = {e["id"]: e["type"] for e in client.get("/api/ontology/graph").json()["entities"]}
        assert by_id["t1"] == "Task"
        assert by_id["p1"] == "Project"

    def test_inferred_edges_only_on_request(self) -> None:
        """断言边默认出现，推理边须显式开启才出现。

        先断言 p1 knows c1，故默认就有这条边；开启 includeInferred 后应额外
        出现镜像的 c1 knows p1（对称关系的推论）。
        """
        from bandi.api.app import _get_ontology_store

        _seed()
        store = _get_ontology_store()
        store.assert_fact({"subject": "p1", "predicate": "knows", "object": "c1"}, NOW)

        default_mirrored = {
            (e["source"], e["target"])
            for e in client.get("/api/ontology/graph").json()["edges"]
            if e["type"] == "knows"
        }
        assert default_mirrored == {("p1", "c1")}, "默认只有断言边，无镜像"

        inferred_mirrored = {
            (e["source"], e["target"])
            for e in client.get("/api/ontology/graph", params={"includeInferred": True}).json()[
                "edges"
            ]
            if e["type"] == "knows"
        }
        assert ("c1", "p1") in inferred_mirrored, "对称关系应补出镜像边"

    def test_empty_graph(self) -> None:
        resp = client.get("/api/ontology/graph")
        assert resp.status_code == 200
        data = resp.json()
        assert data["entities"] == []
        assert data["edges"] == []

    def test_truncation_is_explicit(self) -> None:
        """超限显式回报 truncated + total——旧实现硬编码 limit=10000 静默截断。"""
        from bandi.api.app import _get_ontology_store

        _seed()
        store = _get_ontology_store()
        for i in range(6):
            store.assert_entity({"id": f"extra{i}", "classes": ["Contact"]}, NOW)

        data = client.get("/api/ontology/graph", params={"limit": 3}).json()
        assert len(data["entities"]) == 3
        assert data["truncated"] is True
        assert data["totalEntities"] == 9


class TestGraphRouteNotShadowed:
    def test_stats_and_facts_endpoints_live(self) -> None:
        _seed()
        assert client.get("/api/ontology/stats").status_code == 200
        assert client.get("/api/ontology/facts").status_code == 200
        assert client.get("/api/ontology/entities").status_code == 200

    def test_legacy_entity_graph_endpoint_removed(self) -> None:
        """B4 决策 D2：旧 `/api/entities/graph` 不再是可用读端点。

        返回 405 而非 404——`/api/entities/{entity_id}` 的 PUT/DELETE 路由仍在
        （B4-删旧批次才移除），`graph` 被匹配成 entity_id 但方法不符。关键断言是
        **不再是 200**（即不再是图谱数据源）。
        """
        _seed()
        resp = client.get("/api/entities/graph")
        assert resp.status_code != 200, "旧图谱端点不得继续返回图数据"
        assert "entities" not in resp.json()
