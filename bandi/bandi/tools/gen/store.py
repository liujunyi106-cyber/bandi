# 功能：gen 工具族 JSON store——template/theme 持久化 + 品牌色推导/WCAG 对比度纯函数
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_NAME_LEN_MAX = 64


def validate_store_name(name: str) -> None:
    if not name or len(name) > _NAME_LEN_MAX or not _NAME_RE.match(name):
        raise ValueError(
            f"invalid name {name!r}: use lowercase letters/digits/-/_ (max {_NAME_LEN_MAX})"
        )


def default_gen_dir() -> Path:
    return Path.home() / ".bandi" / "data" / "gen"


class JsonStore:
    """# 功能：按名存 JSON 对象的扁平文件 store（每个对象一文件，防 traversal）"""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else default_gen_dir()

    def _group_dir(self, group: str) -> Path:
        validate_store_name(group)
        return self.base_dir / group

    def _file(self, group: str, name: str) -> Path:
        validate_store_name(name)
        return self._group_dir(group) / f"{name}.json"

    def save(self, group: str, name: str, payload: dict[str, Any], overwrite: bool = True) -> bool:
        path = self._file(group, name)
        if path.exists() and not overwrite:
            raise ValueError(f"{group} {name!r} already exists (set overwrite to replace)")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    def load(self, group: str, name: str) -> dict[str, Any] | None:
        path = self._file(group, name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise ValueError(f"corrupt {group} store file: {path}")
        return data if isinstance(data, dict) else {}

    def delete(self, group: str, name: str) -> bool:
        path = self._file(group, name)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list(self, group: str) -> list[dict[str, Any]]:
        gdir = self._group_dir(group)
        if not gdir.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for f in sorted(gdir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                out.append({**data, "name": f.stem})
        return out


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", color.strip())
    if not m:
        raise ValueError(f"invalid hex color: {color!r} (expected #RRGGBB)")
    raw = m.group(1)
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def _mix(c1: str, c2: str, t: float) -> str:
    a = hex_to_rgb(c1)
    b = hex_to_rgb(c2)
    return rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def _linearize(channel: float) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    r, g, b = hex_to_rgb(color)
    lr, lg, lb = _linearize(r), _linearize(g), _linearize(b)
    return 0.2126 * lr + 0.7152 * lg + 0.0722 * lb


def contrast_ratio(c1: str, c2: str) -> float:
    l1, l2 = relative_luminance(c1), relative_luminance(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def derive_theme_payload(
    kind: str = "document",
    primary: str = "",
    accent: str | None = None,
    mode: str = "light",
    heading_font: str | None = None,
    body_font: str | None = None,
    east_asia_font: str | None = None,
) -> dict[str, Any]:
    """# 功能：从品牌主色推导完整主题 payload（确定性，light/dark 两套）"""
    hex_to_rgb(primary)
    if mode not in ("light", "dark"):
        raise ValueError(f"mode must be light or dark, got {mode!r}")
    acc = accent or (primary if mode == "light" else _mix(primary, "#FFFFFF", 0.30))
    if mode == "light":
        colors = {
            "primary": primary,
            "secondary": _mix(primary, "#FFFFFF", 0.55),
            "accent": acc,
            "text": "#1A1D21",
            "text_light": "#5F6670",
            "background": "#FFFFFF",
            "surface": _mix(primary, "#FFFFFF", 0.90),
            "border": _mix(primary, "#FFFFFF", 0.70),
        }
    else:
        colors = {
            "primary": _mix(primary, "#FFFFFF", 0.15),
            "secondary": _mix(primary, "#000000", 0.40),
            "accent": acc,
            "text": "#EDEFF2",
            "text_light": "#9AA1A9",
            "background": "#0F1115",
            "surface": "#1A1D23",
            "border": "#2A2F37",
        }
    payload: dict[str, Any] = {
        "kind": kind,
        "mode": mode,
        "colors": colors,
        "fonts": {
            "heading": heading_font or ("Roboto" if mode == "light" else "Inter"),
            "body": body_font or "Inter",
            "mono": "Consolas",
            "east_asia": east_asia_font
            or ("Microsoft YaHei" if mode == "dark" else "Noto Sans SC"),
        },
        "sizes": {
            "title": 32,
            "h1": 24,
            "h2": 20,
            "h3": 16,
            "body": 14,
            "small": 12,
            "code": 12,
        },
        "spacing": {"line_spacing": 1.5, "paragraph_spacing": 6},
        "deck": {
            "dark": mode == "dark",
            "title_size": 40,
            "slide_title_size": 28,
            "body_size": 18,
        },
        "zebra_tables": True,
    }
    return payload


_THEME_TEXT_PAIRS = (("text", "background"), ("text", "surface"), ("text_light", "background"))
_WCAG_MAIN = 4.5
_WCAG_LARGE = 3.0


def lint_theme(theme: dict[str, Any]) -> list[str]:
    """# 功能：WCAG 对比度 lint——text/background/surface 成对检查，低于阈值返回警告"""
    colors = theme.get("colors") or {}
    warnings: list[str] = []
    for fg_key, bg_key in _THEME_TEXT_PAIRS:
        fg, bg = colors.get(fg_key), colors.get(bg_key)
        if not fg or not bg:
            continue
        try:
            ratio = contrast_ratio(fg, bg)
        except ValueError:
            continue
        threshold = _WCAG_MAIN if fg_key == "text" else _WCAG_LARGE
        label = "正文" if fg_key == "text" else "次要文字"
        if ratio < threshold:
            warnings.append(f"{label} {fg} on {bg} 对比度 {ratio:.2f}:1 低于 {threshold}:1 阈值")
    return warnings
