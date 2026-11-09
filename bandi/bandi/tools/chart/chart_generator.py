# 功能：ChartGeneratorTool — 结构化数据 → 内联 SVG 图表（纯 Python，无 matplotlib 依赖）
from __future__ import annotations

import html
import math
from typing import Any

from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool

CHART_THEMES: dict[str, dict[str, Any]] = {
    "presentation": {
        "palette": ["#2563EB", "#DC2626", "#16A34A", "#CA8A04", "#9333EA", "#0891B2"],
        "bg": "#FFFFFF",
        "text": "#111827",
        "grid": "#E5E7EB",
        "font_size": 14,
        "title_size": 20,
    },
    "report": {
        "palette": ["#1F4E79", "#2E75B6", "#9DC3E6", "#ED7D31", "#A5A5A5", "#FFC000"],
        "bg": "#FFFFFF",
        "text": "#1F2937",
        "grid": "#D1D5DB",
        "font_size": 12,
        "title_size": 16,
    },
    "dashboard": {
        "palette": ["#6366F1", "#EC4899", "#14B8A6", "#F59E0B", "#8B5CF6", "#EF4444"],
        "bg": "#F8FAFC",
        "text": "#0F172A",
        "grid": "#CBD5E1",
        "font_size": 13,
        "title_size": 18,
    },
    "minimal": {
        "palette": ["#111111", "#555555", "#999999", "#CCCCCC", "#E5E5E5", "#F5F5F5"],
        "bg": "#FFFFFF",
        "text": "#111111",
        "grid": "#E5E5E5",
        "font_size": 11,
        "title_size": 13,
    },
}

_WIDTH, _HEIGHT = 800, 480
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 70, 24, 60, 60
_SVG_NS = "http://www.w3.org/2000/svg"


def _esc(value: str) -> str:
    return html.escape(str(value), quote=True)


def _theme(name: str) -> dict[str, Any]:
    return CHART_THEMES.get(name, CHART_THEMES["presentation"])


class ChartGeneratorTool(FsTool):
    name: str = "chart_generator"
    description: str = (
        "Generate an SVG chart from structured data (categories/series/values/labels). "
        "Supports bar, line, pie, and scatter chart types with built-in themes "
        "(presentation, report, dashboard, minimal). Returns the chart as inline SVG and "
        "writes it to output_path (.svg). No external plotting dependency."
    )
    category: ToolCategory = ToolCategory.CHART
    feature_group: str = "chart"
    capability_tags: list[str] = [ToolCapability.FILE_WRITE.value]
    risk_level: str = "low"
    is_concurrency_safe: bool = True
    is_idempotent: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "chart_type": {
                "type": "string",
                "enum": ["bar", "line", "pie", "scatter"],
                "description": "Chart type to generate.",
            },
            "data": {
                "type": "object",
                "description": (
                    "Chart data. Structure depends on chart_type: bar/line use "
                    "{categories, series:[{name, values}]}; pie uses {values, labels}; "
                    "scatter uses {x, y}."
                ),
                "properties": {
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "series": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "values": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                },
                            },
                        },
                    },
                    "values": {"type": "array", "items": {"type": "number"}},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "x": {"type": "array", "items": {"type": "number"}},
                    "y": {"type": "array", "items": {"type": "number"}},
                },
            },
            "title": {"type": "string", "description": "Chart title."},
            "x_label": {"type": "string", "description": "X-axis label."},
            "y_label": {"type": "string", "description": "Y-axis label."},
            "theme": {
                "type": "string",
                "enum": list(CHART_THEMES),
                "description": "Visual theme (default: presentation).",
            },
            "output_path": {"type": "string", "description": "Destination .svg file path."},
            "show_legend": {
                "type": "boolean",
                "description": "Whether to show legend (default: true for multi-series).",
            },
            "stacked": {
                "type": "boolean",
                "description": "Stack bars (bar only, default: false).",
            },
        },
        "required": ["chart_type", "data", "output_path"],
        "additionalProperties": False,
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        try:
            chart_type = params["chart_type"]
            data = params.get("data") or {}
            title = params.get("title") or ""
            x_label = params.get("x_label") or ""
            y_label = params.get("y_label") or ""
            theme = _theme(params.get("theme"))
            show_legend = params.get("show_legend")
            stacked = bool(params.get("stacked", False))
            output_path = params.get("output_path") or ""

            if chart_type not in ("bar", "line", "pie", "scatter"):
                return {"success": False, "error": f"unsupported chart_type: {chart_type}"}
            if not self._data_valid(chart_type, data):
                return {"success": False, "error": "data is empty or malformed for chart_type"}
            if not output_path:
                return {"success": False, "error": "output_path is required"}

            svg = self._render_svg(
                chart_type=chart_type,
                data=data,
                title=title,
                x_label=x_label,
                y_label=y_label,
                theme=theme,
                show_legend=show_legend,
                stacked=stacked,
            )
            file: str | None = None
            if output_path:
                path = self._resolve_output(output_path, context)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(svg, encoding="utf-8")
                file = str(path)
            return {
                "success": True,
                "svg": svg,
                "chart_type": chart_type,
                "file": file,
            }
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def _data_valid(chart_type: str, data: dict[str, Any]) -> bool:
        if chart_type in ("bar", "line"):
            series = data.get("series")
            if not series:
                return False
            return any(s.get("values") for s in series)
        if chart_type == "pie":
            if data.get("values"):
                return True
            series = data.get("series")
            return bool(series and series[0].get("values"))
        if chart_type == "scatter":
            return bool(data.get("x")) and bool(data.get("y"))
        return False

    def _render_svg(
        self,
        *,
        chart_type: str,
        data: dict[str, Any],
        title: str,
        x_label: str,
        y_label: str,
        theme: dict[str, Any],
        show_legend: bool | None,
        stacked: bool,
    ) -> str:
        palette = theme["palette"]
        if chart_type == "pie":
            body = self._render_pie(data, palette, theme)
        elif chart_type == "scatter":
            body = self._render_scatter(data, palette, theme, show_legend, x_label, y_label)
        else:
            body = self._render_axes(
                data, palette, theme, show_legend, x_label, y_label, horizontal=False
            )
            body += self._render_cartesian(chart_type, data, palette, theme, stacked)
        title_el = (
            f'<text x="{_WIDTH / 2}" y="{30}" text-anchor="middle" '
            f'font-size="{theme["title_size"]}" font-weight="bold" '
            f'fill="{theme["text"]}">{_esc(title)}</text>'
            if title
            else ""
        )
        return (
            f'<svg xmlns="{_SVG_NS}" width="{_WIDTH}" height="{_HEIGHT}" '
            f'viewBox="0 0 {_WIDTH} {_HEIGHT}" font-family="sans-serif">'
            f'<rect width="{_WIDTH}" height="{_HEIGHT}" fill="{theme["bg"]}"/>'
            f"{title_el}{body}</svg>"
        )

    def _render_cartesian(
        self,
        chart_type: str,
        data: dict[str, Any],
        palette: list[str],
        theme: dict[str, Any],
        stacked: bool,
    ) -> str:
        categories = data.get("categories") or []
        series = data.get("series") or []
        n_cat = max(len(categories), 1)
        plot_w = _WIDTH - _PAD_L - _PAD_R
        plot_h = _HEIGHT - _PAD_T - _PAD_B
        bw = plot_w / n_cat
        y_max = self._max_value(series)
        y_max = y_max or 1
        if stacked:
            col_max = [
                sum(s["values"][i] for s in series if i < len(s["values"])) for i in range(n_cat)
            ] or [1]
            y_max = max(col_max, default=1) or 1

        def y_scale(v: float) -> float:
            return _PAD_T + plot_h - (v / y_max) * plot_h

        def x_center(i: int) -> float:
            return _PAD_L + bw * (i + 0.5)

        parts: list[str] = []
        if chart_type == "bar":
            n_series = len(series) or 1
            bar_w = bw * 0.8 / n_series
            for si, s in enumerate(series):
                values = s["values"]
                for i in range(n_cat):
                    v = values[i] if i < len(values) else 0
                    bottom = 0.0
                    if stacked:
                        bottom = sum(
                            (series[j]["values"][i] if i < len(series[j]["values"]) else 0)
                            for j in range(si)
                        )
                        y_bottom = y_scale(bottom)
                        y_top = y_scale(bottom + v)
                    else:
                        offset = (si - (n_series - 1) / 2) * bar_w
                        y_bottom = _PAD_T + plot_h
                        y_top = y_scale(v)
                        xb = x_center(i) + offset - bar_w / 2
                        parts.append(
                            f'<rect x="{xb:.1f}" y="{y_top:.1f}" width="{bar_w:.1f}" '
                            f'height="{max(y_bottom - y_top, 0):.1f}" fill="{palette[si % len(palette)]}" '
                            f'opacity="0.9" data-role="bar"/>'
                        )
                        continue
                    xb = x_center(i) - bar_w * n_series / 2
                    parts.append(
                        f'<rect x="{xb:.1f}" y="{y_top:.1f}" width="{bar_w * n_series:.1f}" '
                        f'height="{max(y_bottom - y_top, 0):.1f}" fill="{palette[si % len(palette)]}" '
                        f'opacity="0.9" data-role="bar"/>'
                    )
        else:  # line
            pts: list[str] = []
            for si, s in enumerate(series):
                values = s["values"]
                coords = " ".join(
                    f"{x_center(i):.1f},{y_scale(v):.1f}"
                    for i in range(n_cat)
                    for v in [values[i] if i < len(values) else 0]
                )
                pts.append(
                    f'<polyline points="{coords}" fill="none" stroke="{palette[si % len(palette)]}" '
                    f'stroke-width="2.5"/>'
                )
            parts.extend(pts)
        for i, c in enumerate(categories):
            parts.append(
                f'<text x="{x_center(i):.1f}" y="{_PAD_T + plot_h + 20}" '
                f'text-anchor="middle" font-size="{theme["font_size"] - 2}" fill="{theme["text"]}">'
                f"{_esc(c)}</text>"
            )
        return "".join(parts)

    def _render_axes(
        self,
        data: dict[str, Any],
        palette: list[str],
        theme: dict[str, Any],
        show_legend: bool | None,
        x_label: str,
        y_label: str,
        *,
        horizontal: bool,
    ) -> str:
        parts: list[str] = []
        plot_w = _WIDTH - _PAD_L - _PAD_R
        plot_h = _HEIGHT - _PAD_T - _PAD_B
        parts.append(
            f'<line x1="{_PAD_L}" y1="{_PAD_T + plot_h}" x2="{_PAD_L + plot_w}" '
            f'y2="{_PAD_T + plot_h}" stroke="{theme["grid"]}" stroke-width="1"/>'
        )
        parts.append(
            f'<line x1="{_PAD_L}" y1="{_PAD_T}" x2="{_PAD_L}" y2="{_PAD_T + plot_h}" '
            f'stroke="{theme["grid"]}" stroke-width="1"/>'
        )
        if x_label:
            parts.append(
                f'<text x="{_PAD_L + plot_w / 2}" y="{_HEIGHT - 8}" text-anchor="middle" '
                f'font-size="{theme["font_size"]}" fill="{theme["text"]}">{_esc(x_label)}</text>'
            )
        if y_label:
            parts.append(
                f'<text x="18" y="{_PAD_T + plot_h / 2}" text-anchor="middle" '
                f'font-size="{theme["font_size"]}" fill="{theme["text"]}" '
                f'transform="rotate(-90 18 {_PAD_T + plot_h / 2})">{_esc(y_label)}</text>'
            )
        series = data.get("series") or []
        if show_legend is True or (show_legend is None and len(series) > 1):
            legend_x = _PAD_L + plot_w - 30
            for si, s in enumerate(series):
                ly = 24 + si * 20
                parts.append(
                    f'<rect x="{legend_x}" y="{ly - 12}" width="12" height="12" '
                    f'fill="{palette[si % len(palette)]}"/>'
                )
                parts.append(
                    f'<text x="{legend_x + 18}" y="{ly}" font-size="{theme["font_size"] - 2}" '
                    f'fill="{theme["text"]}">{_esc(s.get("name", ""))}</text>'
                )
        return "".join(parts)

    def _render_pie(self, data: dict[str, Any], palette: list[str], theme: dict[str, Any]) -> str:
        series = data.get("series") or []
        values: list[float] = list(data.get("values") or (series[0]["values"] if series else []))
        labels: list[str] = list(
            data.get("labels")
            or (
                data.get("categories")
                if data.get("categories")
                else [f"项{i + 1}" for i in range(len(values))]
            )
        )
        total = sum(values) or 1
        cx, cy, r = _WIDTH / 2 - 40, _PAD_T + (_HEIGHT - _PAD_T - _PAD_B) / 2, 130

        def polar(angle: float) -> tuple[float, float]:
            return cx + r * math.cos(angle), cy + r * math.sin(angle)

        parts: list[str] = []
        start = 0.0
        for i, v in enumerate(values):
            frac = v / total
            end = start + frac * 2 * math.pi
            (x0, y0), (x1, y1) = polar(start), polar(end)
            large = 1 if (end - start) > math.pi else 0
            parts.append(
                f'<path d="M {cx},{cy} L {x0:.1f},{y0:.1f} A {r},{r} 0 {large} 1 '
                f'{x1:.1f},{y1:.1f} Z" fill="{palette[i % len(palette)]}" opacity="0.9"/>'
            )
            mid = (start + end) / 2
            lx, ly = polar(mid)
            if labels and i < len(labels):
                parts.append(
                    f'<text x="{cx + (r + 24) * math.cos(mid):.1f}" '
                    f'y="{cy + (r + 22) * math.sin(mid):.1f}" text-anchor="middle" '
                    f'font-size="{theme["font_size"] - 3}" fill="{theme["text"]}">'
                    f"{_esc(labels[i])}</text>"
                )
            start = end
        return "".join(parts)

    def _render_scatter(
        self,
        data: dict[str, Any],
        palette: list[str],
        theme: dict[str, Any],
        show_legend: bool | None,
        x_label: str,
        y_label: str,
    ) -> str:
        xs = list(data.get("x") or [])
        ys = list(data.get("y") or [])
        plot_w = _WIDTH - _PAD_L - _PAD_R
        plot_h = _HEIGHT - _PAD_T - _PAD_B
        x_min, x_max = (min(xs), max(xs)) if xs else (0, 1)
        y_min, y_max = (min(ys), max(ys)) if ys else (0, 1)
        if x_max == x_min:
            x_max = x_min + 1
        if y_max == y_min:
            y_max = y_min + 1

        def sx(v: float) -> float:
            return _PAD_L + ((v - x_min) / (x_max - x_min)) * plot_w

        def sy(v: float) -> float:
            return _PAD_T + plot_h - ((v - y_min) / (y_max - y_min)) * plot_h

        parts: list[str] = []
        parts.append(
            f'<line x1="{_PAD_L}" y1="{_PAD_T + plot_h}" x2="{_PAD_L + plot_w}" '
            f'y2="{_PAD_T + plot_h}" stroke="{theme["grid"]}" stroke-width="1"/>'
        )
        parts.append(
            f'<line x1="{_PAD_L}" y1="{_PAD_T}" x2="{_PAD_L}" y2="{_PAD_T + plot_h}" '
            f'stroke="{theme["grid"]}" stroke-width="1"/>'
        )
        for vx, vy in zip(xs, ys):
            parts.append(f'<circle cx="{sx(vx):.1f}" cy="{sy(vy):.1f}" r="5" fill="{palette[0]}"/>')
        if x_label:
            parts.append(
                f'<text x="{_PAD_L + plot_w / 2}" y="{_HEIGHT - 8}" text-anchor="middle" '
                f'font-size="{theme["font_size"]}" fill="{theme["text"]}">{_esc(x_label)}</text>'
            )
        if y_label:
            parts.append(
                f'<text x="18" y="{_PAD_T + plot_h / 2}" text-anchor="middle" '
                f'font-size="{theme["font_size"]}" fill="{theme["text"]}" '
                f'transform="rotate(-90 18 {_PAD_T + plot_h / 2})">{_esc(y_label)}</text>'
            )
        return "".join(parts)

    @staticmethod
    def _max_value(series: list[dict[str, Any]]) -> float:
        return max(
            (v for s in series for v in s.get("values") if isinstance(v, (int, float))),
            default=0.0,
        )
