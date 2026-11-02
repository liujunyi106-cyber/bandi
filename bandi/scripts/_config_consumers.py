"""配置字段「定义→消费者」检测 —— P0R-2 型缺陷的常规门禁。

由 `wiring-check.ps1 -ConfigAudit` 调用（也可独立跑 / 单测）。

## 检测什么

P0R-2 的形态：**配置字段有定义、文档有承诺，但生产零消费者 → 用户改了配置什么也不发生**
（"能跑通但不符合使用场景"）。历史实例：
    ask_risk_levels / models_dev.url / preferred_language / tools.temp_dir /
    LLMConfig.context_window / ToolsConfig.temp_dir / TasksConfig.title_prompt

此前靠人工 audit 才发现，每次都重读全仓。本脚本把它机械化。

## 证据口径

对每个配置字段 field，在生产代码（`bandi/bandi/**/*.py`，排除 `core/config.py` 自身
与注释行）中统计两类引用：
  1. 属性访问   `.<field>`      —— 常规消费
  2. 字符串键   `"<field>"`     —— 覆盖本仓大量 `getattr(cfg, "field", default)` 形态

分档：
  OK     —— 引用 > 2 处
  REVIEW —— 引用 1~2 处（引用过少，须人工确认是否真接线；不判 FAIL，列清单）
  DEAD   —— 引用 0 处  → **FAIL**（确证死配置：字段名在生产代码里从不出现）

## 已知盲区（诚实声明）

`alias = cfg.<section>` 之后经 alias 访问字段（如 `perm_config = cfg.permissions` 后
`perm_config.ask_risk_levels`）仍能被"字段名出现"捕获，因此不会漏报为 DEAD；
但反过来，**同名字段撞车**（如 `.mode` 到处出现）会让真死字段被假绿放过。
故本脚本定位是"高精度低召回"的守门员：**报出来的必须是真的**，漏网的靠 REVIEW 清单
与人工审查补。

## 允许清单

`ALLOWLIST` 中每项必须附理由（设计决定 / 整体消费 / 公共工具载体）。
若某允许项**已产生引用**（不再需要豁免），脚本输出 STALE 提示——防止清单长期腐化。

用法：
    python scripts/_config_consumers.py            # 人读报告，退出码 0/1
    python scripts/_config_consumers.py --json     # 机器读
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).resolve()
_REPO = _SCRIPT.parent.parent  # ...\bandi
_SRC = _REPO / "bandi"  # ...\bandi\bandi  (production package)
_CONFIG_FILE = "core/config.py"

# 豁免清单：字段 → 理由。新增条目须先实证（勿用来掩盖真死配置）。
# 原 4 项
# safety.block_threshold / llm.budget_limit）均已真接线，审计 STALE 提示后移除；
# 机制保留备将来「整体消费不逐字段引用」型字段登记。
ALLOWLIST: dict[str, str] = {}

_ATTR_RE = re.compile(r"\.([A-Za-z_]\w*)\b")
_STRKEY_RE = re.compile(r"""["']([A-Za-z_]\w*)["']""")
_COMMENT_RE = re.compile(r"^\s*#")


def _submodels(annotation: Any) -> list[type]:
    from pydantic import BaseModel

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type] = []
    for arg in getattr(annotation, "__args__", ()) or ():
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            found.append(arg)
    return found


def enumerate_fields() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """返回 (section 字段列表[带点路径], 顶层标量列表)。"""
    from bandi.core.config import BandiConfig

    sections: list[dict[str, str]] = []
    top_level: list[dict[str, str]] = []

    def walk(model: type, prefix: list[str]) -> None:
        for name, info in model.model_fields.items():
            subs = _submodels(info.annotation)
            if subs:
                for sub in subs:
                    walk(sub, [*prefix, name])
                continue
            default = "required" if info.is_required() else repr(info.default)
            if prefix:
                sections.append(
                    {"dotted": ".".join(prefix) + "." + name, "field": name, "default": default}
                )
            else:
                top_level.append({"dotted": name, "field": name, "default": default})

    walk(BandiConfig, [])
    return sections, top_level


def scan_references() -> dict[str, tuple[int, str]]:
    """单趟扫描生产代码，返回 {名字: (引用数, 首个位置 file:line)}。"""
    refs: dict[str, list[Any]] = {}

    def bump(name: str, where: str) -> None:
        slot = refs.get(name)
        if slot is None:
            refs[name] = [1, where]
        else:
            slot[0] += 1

    for path in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(_SRC.parent.parent).as_posix()
        if rel.endswith(_CONFIG_FILE):
            continue  # 定义处不算消费者
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _COMMENT_RE.match(line):
                continue
            where = f"{rel}:{lineno}"
            for m in _ATTR_RE.finditer(line):
                bump(m.group(1), where)
            for m in _STRKEY_RE.finditer(line):
                bump(m.group(1), where)

    return {k: (v[0], v[1]) for k, v in refs.items()}


def _classify(dotted: str, field: str, refs: dict[str, tuple[int, str]]) -> dict[str, Any]:
    count, where = refs.get(field, (0, ""))
    if dotted in ALLOWLIST:
        # 已被豁免但已产生引用 → 清单腐化提示
        stale = count > 0
        return {"status": "ALLOW", "count": count, "where": where, "stale": stale}
    if count == 0:
        return {"status": "DEAD", "count": 0, "where": "", "stale": False}
    if count <= 2:
        return {"status": "REVIEW", "count": count, "where": where, "stale": False}
    return {"status": "OK", "count": count, "where": where, "stale": False}


def run() -> dict[str, Any]:
    sections, top_level = enumerate_fields()
    refs = scan_references()
    results = []
    for item in [*sections, *top_level]:
        row = {"dotted": item["dotted"], "default": item["default"]}
        row.update(_classify(item["dotted"], item["field"], refs))
        results.append(row)

    dead = [r for r in results if r["status"] == "DEAD"]
    review = [r for r in results if r["status"] == "REVIEW"]
    allowed = [r for r in results if r["status"] == "ALLOW"]
    stale_allow = [r for r in allowed if r["stale"]]
    return {
        "checked": len(results),
        "dead": dead,
        "review": review,
        "allow": allowed,
        "stale_allow": stale_allow,
        "results": results,
    }


def render(report: dict[str, Any], verbose: bool = False) -> str:
    lines: list[str] = []
    add = lines.append
    add("=== Config field consumer audit (definition -> consumer) ===")
    add("")
    add(f"Allowlist ({len(ALLOWLIST)} 项，逐项附理由):")
    for key in sorted(ALLOWLIST):
        add(f"  - {key}：{ALLOWLIST[key]}")
    add("")

    add("=== 字段逐项 ===")
    for r in report["results"]:
        if r["status"] == "OK":
            if verbose:
                add(f"  OK     {r['dotted']}  ({r['count']} 处引用)")
        elif r["status"] == "ALLOW":
            tag = "  [STALE→可移除]" if r["stale"] else ""
            add(f"  ALLOW  {r['dotted']}  (default={r['default']}){tag}")
        elif r["status"] == "REVIEW":
            if verbose:
                add(
                    f"  REVIEW {r['dotted']}  (default={r['default']})  仅 {r['count']} 处，如 {r['where']}"
                )
        else:
            add(f"  DEAD   {r['dotted']}  (default={r['default']})  <-- config.py 外零引用")

    n_ok = sum(1 for r in report["results"] if r["status"] == "OK")
    add("")
    add(
        f"Checked {report['checked']} fields: OK={n_ok} / "
        f"ALLOW={len(report['allow'])} / REVIEW={len(report['review'])} / DEAD={len(report['dead'])}"
    )
    add(
        "（REVIEW = 引用仅 1~2 处；本仓大量配置经 factory 单点透传，属正常，故默认不展开——加 --verbose 看全量）"
    )

    if report["stale_allow"]:
        add("")
        add("=== 允许清单腐化提示（下列项已有引用，可移除豁免）===")
        for r in report["stale_allow"]:
            add(f"      {r['dotted']}  ({r['count']} 处引用)")

    if report["review"] and verbose:
        add("")
        add("=== REVIEW 清单（引用 ≤2 处，须人工确认是否真接线）===")
        for r in report["review"]:
            add(f"      {r['dotted']}  (default={r['default']})  → {r['where']}")

    add("")
    if not report["dead"]:
        add("Clean — 无字段在生产代码中零引用（P0R-2 型死配置 = 0）。")
        add(
            f"--- DoD-11 outcome: CONFIG_NO_DEAD_FIELDS ({report['checked']} fields, "
            f"{len(report['review'])} low-ref advisory) ---"
        )
        return "\n".join(lines)

    add(
        f"FAIL — {len(report['dead'])} 个配置字段在生产代码中零引用（P0R-2 型：设有定义、改不生效）："
    )
    for r in report["dead"]:
        add(f"      {r['dotted']}  (default={r['default']})")
    add("")
    add("=== 每项必须三选一并在交付文档记录：接线 / 删除 / 登记不修（附理由） ===")
    add("--- DoD-11 outcome: CONFIG_DEAD_FIELDS — wire or delete before delivery ---")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    # 允许从仓库任意位置调用：把 bandi 包目录挂进 sys.path
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    report = run()
    if "--json" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report, verbose="--verbose" in argv))
    return 1 if report["dead"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
