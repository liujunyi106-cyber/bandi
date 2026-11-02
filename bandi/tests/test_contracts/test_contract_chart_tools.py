# 行为族级覆盖（DoD-10）：bar/line/pie/scatter 全部生产调用路径逐条有契约
from __future__ import annotations

from typing import Any

import pytest

from bandi.tools.chart.chart_generator import ChartGeneratorTool

_CHART_TYPES = ("bar", "line", "pie", "scatter")


def _ctx(tmp_path: Any, **extra: Any) -> Any:
    """功能：注入 project_root 的项目上下文——写盘工具沙箱放行。"""
    from bandi.tools.base import ToolPermissionContext

    merged: dict[str, Any] = {"project_root": str(tmp_path)}
    merged.update(extra)
    return ToolPermissionContext(user_id="unit-test", role="tester", mode="execute", extra=merged)


def _parse_svg(svg: str) -> Any:
    """功能：把生成 SVG 解析为树，断言它是合法 XML/SVG 结构（不钉实现细节）。"""
    from xml.etree import ElementTree as ET

    root = ET.fromstring(svg)
    assert root.tag.endswith("svg"), f"根元素应为 <svg>，实际: {root.tag!r}"
    return root


_SVG_NS = "{http://www.w3.org/2000/svg}"


def _bar_data() -> dict[str, Any]:
    return {
        "categories": ["华东", "华南", "华北"],
        "series": [
            {"name": "2024", "values": [10, 20, 15]},
            {"name": "2025", "values": [12, 25, 18]},
        ],
    }


# ---------- SVG 产物结构族（四类全有可解析 SVG，DoD-10 全生产路径） ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("chart_type", _CHART_TYPES)
async def test_each_chart_type_yields_parseable_svg(tmp_path: Any, chart_type: str) -> None:
    tool = ChartGeneratorTool()
    params: dict[str, Any] = {
        "chart_type": chart_type,
        "data": _bar_data() if chart_type != "scatter" else {"x": [1, 2, 3], "y": [4, 5, 6]},
        "title": "示例标题",
        "output_path": str(tmp_path / f"{chart_type}.svg"),
    }
    result = await tool.execute(params, _ctx(tmp_path))
    assert result["success"] is True, f"{chart_type} 失败: {result}"
    assert "svg" in result and isinstance(result["svg"], str)
    _parse_svg(result["svg"])
    assert result["chart_type"] == chart_type
    assert (tmp_path / f"{chart_type}.svg").exists()


@pytest.mark.asyncio
async def test_bar_svg_has_one_rect_per_series_per_category(tmp_path: Any) -> None:
    tool = ChartGeneratorTool()

    data = _bar_data()
    result = await tool.execute(
        {"chart_type": "bar", "data": data, "output_path": str(tmp_path / "b.svg")}, _ctx(tmp_path)
    )
    assert result["success"] is True
    root = _parse_svg(result["svg"])
    rects = root.findall(".//{http://www.w3.org/2000/svg}rect[@data-role='bar']")
    assert len(rects) == 3 * 2  # 3 categories x 2 series


@pytest.mark.asyncio
async def test_line_svg_has_polyline_per_series(tmp_path: Any) -> None:
    tool = ChartGeneratorTool()

    result = await tool.execute(
        {"chart_type": "line", "data": _bar_data(), "output_path": str(tmp_path / "l.svg")},
        _ctx(tmp_path),
    )
    assert result["success"] is True
    root = _parse_svg(result["svg"])
    polys = root.findall(f".//{_SVG_NS}polyline")
    assert len(polys) == 2  # 2 series -> 2 polylines


@pytest.mark.asyncio
async def test_pie_svg_present_labels(tmp_path: Any) -> None:
    tool = ChartGeneratorTool()

    result = await tool.execute(
        {
            "chart_type": "pie",
            "data": {"values": [30, 50, 20], "labels": ["产品A", "产品B", "产品C"]},
            "output_path": str(tmp_path / "p.svg"),
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True
    root = _parse_svg(result["svg"])
    paths = root.findall(f".//{_SVG_NS}path")
    assert len(paths) >= 3  # 每块饼至少一个 path
    texts = "".join(t.text or "" for t in root.findall(f".//{_SVG_NS}text"))
    assert "产品A" in texts and "产品B" in texts


@pytest.mark.asyncio
async def test_scatter_svg_has_circle_per_point(tmp_path: Any) -> None:
    tool = ChartGeneratorTool()

    result = await tool.execute(
        {
            "chart_type": "scatter",
            "data": {"x": [1, 2, 3, 4], "y": [10, 20, 15, 25]},
            "output_path": str(tmp_path / "s.svg"),
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True
    root = _parse_svg(result["svg"])
    circles = root.findall(f".//{_SVG_NS}circle")
    assert len(circles) == 4


# ---------- 标题 / 轴标签 / 图例 呈现族 ----------


@pytest.mark.asyncio
async def test_title_and_axis_labels_present(tmp_path: Any) -> None:
    tool = ChartGeneratorTool()
    result = await tool.execute(
        {
            "chart_type": "bar",
            "data": _bar_data(),
            "title": "季度营收",
            "x_label": "地区",
            "y_label": "万元",
            "show_legend": True,
            "output_path": str(tmp_path / "t.svg"),
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True
    texts = "\n".join(t.text or "" for t in _parse_svg(result["svg"]).findall(f".//{_SVG_NS}text"))
    assert "季度营收" in texts and "地区" in texts and "万元" in texts
    assert "2024" in texts and "2025" in texts  # 图例


# ---------- 主题族（行为：不同 theme 不同色板；默认 presentation） ----------


@pytest.mark.asyncio
async def test_different_themes_yield_different_fill_colors(tmp_path: Any) -> None:
    tool = ChartGeneratorTool()
    params = lambda theme: {  # noqa: E731
        "chart_type": "bar",
        "data": {"categories": ["a", "b"], "series": [{"name": "s", "values": [1, 2]}]},
        "theme": theme,
        "output_path": str(tmp_path / f"{theme}.svg"),
    }
    fills_by_theme: dict[str, set[str]] = {}
    for theme in ("presentation", "report", "dashboard", "minimal"):
        result = await tool.execute(params(theme), _ctx(tmp_path))
        assert result["success"] is True
        root = _parse_svg(result["svg"])
        fills = {r.get("fill") for r in root.findall(f".//{_SVG_NS}rect")}
        fills_by_theme[theme] = fills
    assert fills_by_theme["presentation"] != fills_by_theme["minimal"]


@pytest.mark.asyncio
async def test_unknown_theme_falls_back_to_presentation(tmp_path: Any) -> None:
    tool = ChartGeneratorTool()
    result = await tool.execute(
        {
            "chart_type": "bar",
            "data": {"categories": ["a"], "series": [{"name": "s", "values": [1]}]},
            "theme": "no_such_theme",
            "output_path": str(tmp_path / "fb.svg"),
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True


# ---------- 落盘族（写 .svg 可读回 + 内联返回） ----------


@pytest.mark.asyncio
async def test_svg_file_readable_back_matches_inline(tmp_path: Any) -> None:
    tool = ChartGeneratorTool()
    out = tmp_path / "chart.svg"
    result = await tool.execute(
        {"chart_type": "bar", "data": _bar_data(), "output_path": str(out)}, _ctx(tmp_path)
    )
    assert result["success"] is True
    written = out.read_text(encoding="utf-8")
    assert written == result["svg"]  # 落盘内容 == 返回的内联 SVG
    _parse_svg(written)


# ---------- 校验族（非法输入拒绝，破坏者视角 DoD-3） ----------


@pytest.mark.asyncio
async def test_invalid_chart_type_rejected(tmp_path: Any) -> None:
    tool = ChartGeneratorTool()
    result = await tool.execute(
        {"chart_type": "explode", "data": {}, "output_path": str(tmp_path / "x.svg")},
        _ctx(tmp_path),
    )
    assert result["success"] is False


@pytest.mark.asyncio
async def test_empty_data_rejected(tmp_path: Any) -> None:
    tool = ChartGeneratorTool()
    result = await tool.execute(
        {"chart_type": "bar", "data": {}, "output_path": str(tmp_path / "e.svg")}, _ctx(tmp_path)
    )
    assert result["success"] is False


@pytest.mark.asyncio
async def test_pie_empty_series_not_crash(tmp_path: Any) -> None:
    tool = ChartGeneratorTool()
    result = await tool.execute(
        {"chart_type": "pie", "data": {"series": []}, "output_path": str(tmp_path / "p.svg")},
        _ctx(tmp_path),
    )
    assert result["success"] is False  # 空数据拒绝而非 IndexError 崩溃
    assert not (tmp_path / "p.svg").exists()


@pytest.mark.asyncio
async def test_missing_output_path_rejected(tmp_path: Any) -> None:
    tool = ChartGeneratorTool()
    result = await tool.execute({"chart_type": "bar", "data": _bar_data()})
    assert result["success"] is False


# ---------- 路径安全族（破坏者：越界拒绝，DoD-3） ----------


@pytest.mark.asyncio
async def test_output_path_traversal_rejected(tmp_path: Any) -> None:
    tool = ChartGeneratorTool()
    result = await tool.execute(
        {"chart_type": "bar", "data": _bar_data(), "output_path": "../escape.svg"}, _ctx(tmp_path)
    )
    assert result["success"] is False
    assert "outside workspace" in str(result.get("error", "")).lower()
    assert not (tmp_path.parent / "escape.svg").exists()


# ---------- schema 校验族 ----------


def test_chart_schema_rejects_unknown_params() -> None:
    tool = ChartGeneratorTool()
    errors = tool.validate_params(
        {"chart_type": "bar", "data": {}, "output_path": "o.svg", "nope": 1}
    )
    assert any("nope" in e for e in errors)
    assert tool.validate_params({"chart_type": "bar", "data": {}, "output_path": "o.svg"}) == []


# ---------- 装配族（factory 注册 + feature gate fail-closed） ----------


def test_chart_tool_registered_with_gate() -> None:
    from bandi.agent.factory import build_tool_registry
    from bandi.tools.base import ToolCategory

    registry = build_tool_registry(feature_flags={"chart": True})
    tool = registry.get("chart_generator")
    assert tool is not None
    assert tool.category == ToolCategory.CHART
    assert tool.feature_group == "chart"
    assert tool.to_tool_meta()["capability_tags"] == ["file_write"]

    closed = build_tool_registry(feature_flags={"chart": False})
    assert closed.get("chart_generator") is None


@pytest.mark.asyncio
async def test_registry_wired_chart_writes_final_artifact(tmp_path: Any) -> None:
    """端到端：生产装配点取工具执行，真实写盘并读回最终产物（DoD-12 主链路）。"""
    from bandi.agent.factory import build_tool_registry

    registry = build_tool_registry(feature_flags={"chart": True})
    tool = registry.get("chart_generator")
    assert tool is not None
    out = tmp_path / "e2e.svg"
    result = await tool.execute(
        {
            "chart_type": "bar",
            "data": _bar_data(),
            "title": "端到端营收",
            "output_path": str(out),
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True, f"装配链路执行失败: {result}"
    written = out.read_text(encoding="utf-8")
    assert written == result["svg"]
    _parse_svg(written)
    assert "端到端营收" in written
