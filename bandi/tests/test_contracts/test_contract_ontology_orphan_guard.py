"""契约：ontology_assert 孤立实体守卫。

行为不变式：

**纯逻辑层（ontology.tools）**
 1. 只断言实体零事实 → `orphan_entities` 点名（本轮新增口径）
 2. 实体与事实成对断言 → `orphan_entities` 为空（零打扰）
 3. 预先存在的零度实体再次断言（非本轮新增）→ 不进 orphan

**适配层（tools.ontology_tool_adapter，人机协同）**
 4. 有确认通道：确认保留 → 实体保留并记录 confirmed_orphans
 5. 有确认通道：拒绝 → 回滚（store 中实体消失，render 说明）
 6. 无确认通道（headless）→ fail-closed 直接回滚（与仓库 headless 拒绝惯例一致）
 7. 破坏者：成对断言绝不触发确认（ask_confirmation 零调用）"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bandi.ontology.tools import OntologyToolContext, create_ontology_tools
from bandi.tools.ontology_tool_adapter import build_ontology_tools

NOW = "2026-10-15T00:00:00Z"


class FakeConfirmUI:
    """功能：ask_confirmation 桩——记录提问、按预设返回决策。"""

    def __init__(self, granted: bool) -> None:
        self.granted = granted
        self.prompts: list[str] = []

    async def ask_confirmation(self, prompt: str, timeout: float = 30.0) -> bool:
        self.prompts.append(prompt)
        return self.granted


@pytest.fixture()
def ctx(tmp_path: Path) -> Any:
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore, StoreLimits

    tables = open_ontology(str(tmp_path / "onto.db"))
    store = OntologyStore(tables, StoreLimits(strict=True))
    yield OntologyToolContext(
        store=store, inference=True, default_limit=20, max_limit=100, now=lambda: NOW
    )
    store.close()


def _tools(ctx: Any) -> dict[str, Any]:
    return {tool.name: tool for tool in create_ontology_tools(ctx)}


def _define_vocabulary(ctx: Any) -> None:
    define = _tools(ctx)["ontology_define"]
    define.execute(
        {
            "classes": [{"id": "Company", "label": "公司"}],
            "relations": [
                {
                    "id": "supplies",
                    "domain": ["Company"],
                    "range": ["Company"],
                },
                {
                    "id": "has_hq",
                    "domain": ["Company"],
                    "rangeKind": "literal",
                },
            ],
        }
    )


def _adapters(ctx: Any) -> dict[str, Any]:
    return {tool.name: tool for tool in build_ontology_tools(ctx)}


def _store_entity_ids(ctx: Any) -> set[str]:
    return set(ctx.store.graph().entities)


@pytest.mark.asyncio
async def test_assert_orphan_entities_detected_pure(ctx: Any) -> None:
    """只断言实体零事实 → orphan_entities 点名（本轮新增口径）。"""
    _define_vocabulary(ctx)
    assert_tool = _tools(ctx)["ontology_assert"]
    value = assert_tool.execute({"entities": [{"id": "acme", "classes": ["Company"]}], "facts": []})
    assert value["orphan_entities"] == ["acme"]


@pytest.mark.asyncio
async def test_paired_assert_no_orphans(ctx: Any) -> None:
    """实体与事实成对断言 → 无孤立（零打扰）。"""
    _define_vocabulary(ctx)
    assert_tool = _tools(ctx)["ontology_assert"]
    value = assert_tool.execute(
        {
            "entities": [
                {"id": "acme", "classes": ["Company"]},
                {"id": "beta", "classes": ["Company"]},
            ],
            "facts": [{"subject": "acme", "predicate": "supplies", "object": "beta"}],
        }
    )
    assert value["orphan_entities"] == []
    assert value["entities"] == ["acme", "beta"]


@pytest.mark.asyncio
async def test_preexisting_zero_degree_not_orphan(ctx: Any) -> None:
    """破坏者口径：预先存在且零度的实体再次断言（非本轮新增）→ 不进 orphan。"""
    _define_vocabulary(ctx)
    assert_tool = _tools(ctx)["ontology_assert"]
    assert_tool.execute({"entities": [{"id": "acme", "classes": ["Company"]}], "facts": []})
    value = assert_tool.execute({"entities": [{"id": "acme", "classes": ["Company"]}], "facts": []})
    assert value["orphan_entities"] == [], "再次断言已存在实体不算本轮新增"


@pytest.mark.asyncio
async def test_literal_fact_gives_degree(ctx: Any) -> None:
    """字面量值关系的事实同样给实体带来度数（不误判孤立）。"""
    _define_vocabulary(ctx)
    assert_tool = _tools(ctx)["ontology_assert"]
    value = assert_tool.execute(
        {
            "entities": [{"id": "acme", "classes": ["Company"]}],
            "facts": [{"subject": "acme", "predicate": "has_hq", "object": "深圳"}],
        }
    )
    assert value["orphan_entities"] == []


@pytest.mark.asyncio
async def test_adapter_headless_rolls_back_orphans(ctx: Any) -> None:
    """无确认通道（headless）→ fail-closed 直接回滚；store 中实体消失。"""
    _define_vocabulary(ctx)
    adapters = _adapters(ctx)
    rendered = await adapters["ontology_assert"].execute(
        {"entities": [{"id": "acme", "classes": ["Company"]}], "facts": []}
    )
    assert "回滚" in rendered
    assert "acme" not in _store_entity_ids(ctx)


@pytest.mark.asyncio
async def test_adapter_ui_confirmed_keeps(ctx: Any) -> None:
    """有确认通道且用户确认 → 实体保留，render 说明已确认保留。"""
    _define_vocabulary(ctx)
    adapters = _adapters(ctx)
    ui = FakeConfirmUI(granted=True)
    adapters["ontology_assert"].bind_ui(ui)
    rendered = await adapters["ontology_assert"].execute(
        {"entities": [{"id": "acme", "classes": ["Company"]}], "facts": []}
    )
    assert "acme" in _store_entity_ids(ctx)
    assert "保留" in rendered
    assert ui.prompts and "acme" in ui.prompts[0]


@pytest.mark.asyncio
async def test_adapter_ui_denied_rolls_back(ctx: Any) -> None:
    """有确认通道但用户拒绝 → 回滚（实体消失）。"""
    _define_vocabulary(ctx)
    adapters = _adapters(ctx)
    ui = FakeConfirmUI(granted=False)
    adapters["ontology_assert"].bind_ui(ui)
    rendered = await adapters["ontology_assert"].execute(
        {"entities": [{"id": "acme", "classes": ["Company"]}], "facts": []}
    )
    assert "acme" not in _store_entity_ids(ctx)
    assert "回滚" in rendered


@pytest.mark.asyncio
async def test_paired_call_never_asks(ctx: Any) -> None:
    """破坏者：成对断言绝不触发确认通道（零打扰不变式）。"""
    _define_vocabulary(ctx)
    adapters = _adapters(ctx)
    ui = FakeConfirmUI(granted=False)
    adapters["ontology_assert"].bind_ui(ui)
    rendered = await adapters["ontology_assert"].execute(
        {
            "entities": [
                {"id": "acme", "classes": ["Company"]},
                {"id": "beta", "classes": ["Company"]},
            ],
            "facts": [{"subject": "acme", "predicate": "supplies", "object": "beta"}],
        }
    )
    assert ui.prompts == [], "成对断言不得触发确认"
    assert "acme" in _store_entity_ids(ctx)
    assert "回滚" not in rendered


@pytest.mark.asyncio
async def test_rollback_render_names_entities(ctx: Any) -> None:
    """回滚结果点名实体并给出成对断言的修复指引（模型可据此重试）。"""
    _define_vocabulary(ctx)
    adapters = _adapters(ctx)
    rendered = await adapters["ontology_assert"].execute(
        {"entities": [{"id": "acme", "label": "埃克梅"}], "facts": []}
    )
    assert "acme" in rendered
    assert "成对" in rendered


def _unused_json_guard() -> None:
    # json 导入保留给未来断言扩展使用，避免误删
    _ = json.dumps({})
