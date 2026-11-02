# 功能：canvas 工具单测——schema/校验/轻量行为（深行为契约覆盖）
from __future__ import annotations

from typing import Any

import pytest

from bandi.tools.base import ToolCategory, ToolPermissionContext
from bandi.tools.canvas import (
    CanvasPublishTool,
    EmitUiTreeTool,
    GetGenuiGuideTool,
    GetHtmlCanvasGuideTool,
    ListUiComponentsTool,
)
from bandi.tools.canvas.ui_components import validate_ui_tree


def _ctx(tmp_path: Any, **extra: Any) -> ToolPermissionContext:
    merged: dict[str, Any] = {"project_root": str(tmp_path)}
    merged.update(extra)
    return ToolPermissionContext(user_id="unit-test", role="tester", mode="execute", extra=merged)


def test_canvas_tools_meta() -> None:
    from bandi.agent.factory import build_tool_registry

    registry = build_tool_registry(feature_flags={"canvas": True, "web": True})
    for name in (
        "canvas_publish",
        "emit_ui_tree",
        "list_ui_components",
        "get_genui_guide",
        "get_html_canvas_guide",
    ):
        tool = registry.get(name)
        assert tool is not None, f"{name} 未注册"
        assert tool.category == ToolCategory.CANVAS
        assert tool.feature_group == "canvas"
        assert tool.to_tool_meta()["category"] == "canvas"


def test_canvas_publish_requires_title_mode(tmp_path: Any) -> None:
    tool = CanvasPublishTool()
    errors = tool.validate_params({})
    assert errors
    errors2 = tool.validate_params({"title": "t"})
    assert any("mode" in e for e in errors2)


@pytest.mark.asyncio
async def test_canvas_publish_rejects_invalid_mode(tmp_path: Any) -> None:
    result = await CanvasPublishTool().execute(
        {"title": "t", "mode": "bogus", "html": "<h1>x</h1>"}, _ctx(tmp_path)
    )
    assert result["success"] is False
    assert "mode" in str(result.get("error", ""))


def test_canvas_publish_schema_mode_enum() -> None:
    schema = CanvasPublishTool().parameters
    assert schema["properties"]["mode"]["enum"] == ["html", "embed_url", "gen_ui"]
    assert schema["required"] == ["title", "mode"]
    assert schema["additionalProperties"] is False


def test_ui_components_catalog_sorted() -> None:
    from bandi.tools.canvas.ui_components import _COMPONENT_CATALOG

    assert len(_COMPONENT_CATALOG) >= 15
    assert "Heading" in _COMPONENT_CATALOG and "Chart" in _COMPONENT_CATALOG
    assert _COMPONENT_CATALOG["Stack"]["props"] == {
        "direction": "vertical|horizontal",
        "gap": "sm|md|lg",
    }


def test_validate_ui_tree_rejects_bad_kinds() -> None:
    with pytest.raises(ValueError, match="unknown component kind"):
        validate_ui_tree({"kind": "Nope", "props": {}})
    ok = validate_ui_tree({"kind": "Heading", "props": {"level": 1, "text": "x"}})
    assert ok["kind"] == "Heading"


@pytest.mark.asyncio
async def test_list_ui_components_execute(tmp_path: Any) -> None:
    result = await ListUiComponentsTool().execute({}, _ctx(tmp_path))
    assert result["success"] is True
    assert len(result["components"]) >= 15
    assert "node_shape" in result and "rules" in result


@pytest.mark.asyncio
async def test_guides_read_only_and_no_write(tmp_path: Any) -> None:
    for tool_cls in (GetGenuiGuideTool, GetHtmlCanvasGuideTool):
        result = await tool_cls().execute({}, _ctx(tmp_path))
        assert result["success"] is True
        assert "guide" in result
    # 指引类工具只读：不产生文件
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_emit_ui_tree_requires_tree(tmp_path: Any) -> None:
    result = await EmitUiTreeTool().execute({}, _ctx(tmp_path))
    assert result["success"] is False
    assert "tree" in str(result.get("error", ""))
