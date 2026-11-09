# 功能：台账重算（把修复前的「编造金额」历史行按**当前定价链**重算）
#
# 背景：之前，目录外 / 网关模型按硬编码兜底价（¥1.584/¥4.752）计费 ⇒ 台账里留下
# 而按真实价应为 ¥0.032012）；之前，该 provider 的转售价也不会被采用。
# 运行期只影响**新**记录，历史行不会自己变，故提供本工具做一次性回溯。
#
# 用法（**默认干跑，不写库**）：
# python -m bandi.observability.reprice # 只报告将要改哪些行
# python -m bandi.observability.reprice --apply # 写库（先自动备份 .bak-<时间戳>）
#
# 设计要点：
# - **不重复实现定价**：解析走 `CostTracker._resolve_pricing`（与记账同一入口）
# 金额走 `compute_cost_cny`（与记账同一公式）⇒ 重算结果必然与新记账同口径。
# - **幂等**：已带 `breakdown.pricing_source` 的行跳过（= 修复后写入的）；
# 金额本就与重算一致的「未定价占位」行也跳过。
# - **保守**：解析不到价格的行会被标为「未定价」（金额 0 + breakdown.unpriced）
# 而不是保留编造值——这正是 的语义（宁可显示未定价，也不编数字）。

from __future__ import annotations

import json
import shutil
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bandi.utils.db import connect_app_db

# 解析器签名：(provider, model) -> (pricing | None, pricing_source)
PricingResolver = Callable[[str, str], "tuple[Any, str]"]


@dataclass
class RepriceItem:
    """功能：一行待重算记录（旧值 → 新值 + 定价来源）。"""

    event_id: str
    model: str
    provider: str
    old_cost: float
    new_cost: float | None
    pricing_source: str
    breakdown: dict[str, Any]


@dataclass
class RepricePlan:
    # 功能：一次重算计划（含跳过原因计数，便于判断是否需要 --apply）
    items: list[RepriceItem] = field(default_factory=list)
    stats: dict[str, int] = field(
        default_factory=lambda: {
            "scanned": 0,
            "changed": 0,
            "unchanged": 0,
            "skipped_reprised": 0,  # 已带 pricing_source（修复后写入）
            "skipped_unpriced": 0,  # 已标记未定价（无金额可纠）
        }
    )


def _val(row: Any, key: str) -> Any:
    # 功能：兼容 sqlite3.Row 与 dict 两种行形态
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _breakdown(row: Any) -> dict[str, Any]:
    # 功能：读 breakdown JSON（坏数据/非字典一律视为空，与聚合路径同口径）
    raw = _val(row, "breakdown")
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def default_resolver(provider: str, model: str) -> tuple[Any, str]:
    """功能：生产定价解析器——**复用记账入口**，不另写一套规则。"""
    from bandi.llm.cost_tracker import CostTracker
    from bandi.llm.model_catalog import ModelCatalog

    return CostTracker._resolve_pricing(provider, model, ModelCatalog().get(model))


def plan_reprice(rows: Iterable[Any], resolver: PricingResolver) -> RepricePlan:
    """功能：算出「哪些行需要改、改成多少」（纯函数，不碰库——便于契约测试）。"""
    from bandi.llm.cost_tracker import compute_cost_cny

    plan = RepricePlan()
    for row in rows:
        plan.stats["scanned"] += 1
        breakdown = _breakdown(row)
        # 幂等：修复后写入的行（带 pricing_source）或已标记未定价的行一律跳过
        if breakdown.get("pricing_source"):
            plan.stats["skipped_reprised"] += 1
            continue
        if breakdown.get("unpriced"):
            plan.stats["skipped_unpriced"] += 1
            continue
        provider = str(_val(row, "provider_name") or "")
        model = str(_val(row, "model") or "")
        pricing, source = resolver(provider, model)
        old_cost = float(_val(row, "total_cost_cny") or 0.0)
        if pricing is None:
            new_cost: float | None = None
            breakdown = {**breakdown, "unpriced": True, "pricing_source": source}
        else:
            new_cost = round(
                compute_cost_cny(
                    pricing,
                    # 归一总输入：优先 breakdown.input_tokens_total（记账时已归一），否则回落列值
                    input_tokens_total=int(breakdown.get("input_tokens_total") or 0)
                    or int(_val(row, "input_tokens") or 0),
                    cache_read_tokens=int(_val(row, "cache_tokens") or 0),
                    cache_creation_tokens=int(breakdown.get("cache_creation_tokens") or 0),
                    completion_tokens=int(_val(row, "output_tokens") or 0),
                ),
                6,
            )
            breakdown = {**breakdown, "pricing_source": source}
        if new_cost is not None and abs(new_cost - old_cost) < 1e-9:
            plan.stats["unchanged"] += 1
            continue
        plan.stats["changed"] += 1
        plan.items.append(
            RepriceItem(
                event_id=str(_val(row, "event_id") or ""),
                model=model,
                provider=provider,
                old_cost=old_cost,
                new_cost=new_cost,
                pricing_source=source,
                breakdown=breakdown,
            )
        )
    return plan


def reprice_ledger(db_path: str | Path, *, apply: bool = False) -> dict[str, Any]:
    """功能：按当前定价链重算历史台账；默认干跑。返回报告（含 plan 计数与样例）。

    `apply=True` 时**先备份**到 `<db>.bak-<YYYYmmdd-HHMMSS>`，再按 event_id 逐行更新
    `total_cost_cny` 与 `breakdown`。任何异常都不吞：调用方（CLI）负责报错退出。
    """
    path = Path(db_path).expanduser()
    conn = connect_app_db(path, row_factory=True)
    try:
        rows = conn.execute(
            "SELECT event_id, provider_name, model, input_tokens, output_tokens,"
            " cache_tokens, total_cost_cny, breakdown FROM llm_request_log ORDER BY rowid"
        ).fetchall()
        plan = plan_reprice(list(rows), default_resolver)
        report: dict[str, Any] = {
            "db": str(path),
            "applied": False,
            "backup": "",
            **plan.stats,
            "samples": [
                {
                    "event_id": item.event_id,
                    "model": item.model,
                    "provider": item.provider,
                    "old_cost_cny": item.old_cost,
                    "new_cost_cny": item.new_cost,
                    "pricing_source": item.pricing_source,
                }
                for item in plan.items[:20]
            ],
        }
        if not apply or not plan.items:
            return report
        backup = path.with_suffix(path.suffix + time.strftime(".bak-%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
        report["backup"] = str(backup)
        with conn:  # 单事务：要么全改，要么不改
            for item in plan.items:
                conn.execute(
                    "UPDATE llm_request_log SET total_cost_cny = ?, breakdown = ?"
                    " WHERE event_id = ?",
                    (
                        float(item.new_cost or 0.0),
                        json.dumps(item.breakdown, ensure_ascii=False),
                        item.event_id,
                    ),
                )
        report["applied"] = True
        return report
    finally:
        conn.close()


def _main(argv: list[str] | None = None) -> int:
    from bandi.core.config import default_cost_db_path

    args = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in args
    db_path = str(default_cost_db_path())
    for index, arg in enumerate(args):
        if arg == "--db" and index + 1 < len(args):
            db_path = args[index + 1]
    report = reprice_ledger(db_path, apply=apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["applied"] and report["changed"]:
        print("\n[干跑] 未写库。确认无误后加 --apply 执行（会先自动备份）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
