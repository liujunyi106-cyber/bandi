# 功能：长期记忆持久化衰减 + 用进废退回升 + 陈旧低价值遗忘
#
# 与检索侧衰减是两层：检索侧（`LongTermMemory._decay_half_life`，默认 180d +
# floor 0.2）只压排序分、不改数据；本模块在维护周期**持久化**降低 importance，
# 让长期不被使用的事实真正沉底。二者叠加是有意为之——沉底要慢于排序降权。
#
# 参考源：
#   - Hermes `plugins/memory/holographic/retrieval.py:52-71`：`score *=
#     0.5^(age/half_life)`（同构公式；他们只做检索侧）
#   - LeAgent `maintenance.py:144-172`：衰减 + floor 0.02 + 召回保护
#     （他们缺显式条目豁免：EXPLICIT_REMEMBER 不落库，无从豁免）
#   - Hermes `memory_tool_store.py:349`：拒绝把非空库批删空（事故 #103419）
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bandi.memory.long_term import (
    RECALL_IMPORTANCE_BOOST,
    LongTermMemory,
    MemoryItem,
    _as_float,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

# 持久化衰减半衰期（天）——importance *= 0.5^(Δdays/90)
DECAY_HALF_LIFE_DAYS = 90.0
# importance 下限——归零后会被 `LongTermMemory.prune()`（按 importance ASC 删）
# 当作最弱条目误删；下限同时保证「沉底」不等于「消失」
DECAY_IMPORTANCE_FLOOR = 0.02
# 近期召回豁免窗口（天）——近 30 天被真正用过的条目本轮跳过衰减
RECALL_PROTECTION_DAYS = 30.0
# 遗忘门槛：衰减后低于此值才有资格进删除候选
FORGET_IMPORTANCE_THRESHOLD = 0.15
# 遗忘年龄门槛（天）——太新的条目即便当前弱也不删（可能正在形成）
FORGET_MIN_AGE_DAYS = 180.0
# 遗忘的环境开关（`BANDI_MEMORY_FORGET=0` 关闭，取证/调试期间保护数据）
FORGET_ENV_VAR = "BANDI_MEMORY_FORGET"
# 扫描页大小（单分区）
_SCAN_PAGE_SIZE = 500


@dataclass
class DecayReport:
    """功能：衰减执行报告——扫了多少、衰减了多少、按原因跳过多少。"""

    scanned: int = 0
    decayed: int = 0
    skipped_recent_recall: int = 0
    skipped_floor: int = 0
    ids: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"scanned={self.scanned} decayed={self.decayed} "
            f"skip_recent={self.skipped_recent_recall} skip_floor={self.skipped_floor}"
        )


def _parse_iso(value: object) -> datetime | None:
    """功能：解析 ISO 时间串（失败返回 None，调用方按「基准缺失」处理）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _recall_count(item: MemoryItem) -> int:
    return max(0, int(_as_float((item.metadata or {}).get("recall_count"), 0.0)))


def is_explicit(item: MemoryItem) -> bool:
    """功能：显式条目判定——用户明说「记住/记下/别忘了」的内容永不删除。

    写入侧由 `ingest_review._EXPLICIT_REMEMBER_WORDS` 判定并落 `metadata.is_explicit`；
    LeAgent 的对应语义（`EXPLICIT_REMEMBER`）只作 FormationTarget 触发器、不落库，
    导致 decay/forget 对显式与普通条目一视同仁——本项目要求显式豁免删除，
    故必须有可持久化的标记。
    """
    return bool((item.metadata or {}).get("is_explicit"))


def explicit_flags(episode: object) -> dict:
    """功能：由情节推出显式标记的 metadata 片段——晋升路径落库用（无则空 dict）。

    晋升（提名/维护）是从经历层二次写长期层，若不在此带上标记，用户当初明说
    「记住」的内容经晋升后就会失去删除豁免——直写路径有豁免、晋升路径没有，
    同一句话因落库路径不同而命运不同。
    """
    from bandi.memory.ingest_review import has_explicit_remember

    user_input = str(getattr(episode, "user_input", "") or "")
    summary = str(getattr(episode, "summary", "") or "")
    if has_explicit_remember(user_input) or has_explicit_remember(summary):
        return {"is_explicit": True}
    return {}


def decay_reference_instant(item: MemoryItem) -> datetime | None:
    """功能：衰减基准时刻——`last_recalled_at` 与创建时刻取较晚者。

    从创建时刻算是常见实现，但对「3 年前创建、昨天才召回」的条目，一次维护
    就会把它推进 12 个半衰期直接打到地板，与「昨天才用过」矛盾。取较晚者
    表示：一条记忆的「新鲜度」从它最后一次真正被使用开始算。
    """
    created = _parse_iso(_timestamp_iso(item.timestamp))
    recalled = _parse_iso((item.metadata or {}).get("last_recalled_at"))
    if created is None:
        return recalled
    if recalled is None:
        return created
    return max(created, recalled)


def _timestamp_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _iter_scoped(
    long_term: LongTermMemory,
) -> Iterator[tuple[str | None, str | None, list[MemoryItem]]]:
    """功能：分区遍历全部条目（各 distinct 维度 + 无维度），页满继续翻页。"""
    for project_id, employee_id in _list_scopes(long_term):
        offset = 0
        while True:
            page = long_term.list_items(
                project_id=project_id,
                employee_id=employee_id,
                limit=_SCAN_PAGE_SIZE,
                offset=offset,
            )
            if page:
                yield project_id, employee_id, page
            if len(page) < _SCAN_PAGE_SIZE:
                break
            offset += _SCAN_PAGE_SIZE


def _list_scopes(long_term: LongTermMemory) -> list[tuple[str | None, str | None]]:
    """功能：分区清单——无维度 + 各 distinct 维度（扫描面不得被单页 limit 截断）。"""
    scopes: list[tuple[str | None, str | None]] = [(None, None)]
    seen = {(None, None)}
    try:
        rows = long_term.list_scopes()
    except Exception as e:  # noqa: BLE001 — 分区接口不可用时退回单分区（不阻断维护）
        logger.warning("memory decay scope listing failed: %s", e)
        return scopes
    for project_id, employee_id in rows:
        scope = (project_id, employee_id)
        if scope not in seen:
            seen.add(scope)
            scopes.append(scope)
    return scopes


def decay_items(
    long_term: LongTermMemory,
    *,
    now: datetime | None = None,
    half_life_days: float = DECAY_HALF_LIFE_DAYS,
    protection_days: float = RECALL_PROTECTION_DAYS,
    floor: float = DECAY_IMPORTANCE_FLOOR,
) -> DecayReport:
    """功能：持久化衰减——按 `0.5^(Δdays/half_life)` 降低 importance 并落库。

    跳过：基准时刻缺失（无时间戳）、`Δdays < protection_days`（近期被用）、
    已在 floor（无可衰减空间）。衰减后不低于 floor。

    显式条目**参与衰减**（只降权），删除豁免在 `forget_stale_items`——否则
    显式条目会永久停滞在首位。
    """
    moment = now or datetime.now(UTC)
    report = DecayReport()
    for project_id, employee_id, page in _iter_scoped(long_term):
        upserts: list[MemoryItem] = []
        for item in page:
            report.scanned += 1
            reference = decay_reference_instant(item)
            if reference is None:
                continue
            elapsed_days = (moment - reference).total_seconds() / 86400.0
            if elapsed_days < protection_days:
                report.skipped_recent_recall += 1
                continue
            if item.importance <= floor:
                report.skipped_floor += 1
                continue
            factor = 0.5 ** (elapsed_days / max(1e-9, half_life_days))
            decayed_value = max(floor, item.importance * factor)
            if decayed_value >= item.importance - 1e-12:
                report.skipped_floor += 1
                continue
            metadata = dict(item.metadata or {})
            metadata["last_decayed_at"] = moment.isoformat()
            upserts.append(
                MemoryItem(
                    item_id=item.id,
                    content=item.content,
                    metadata=metadata,
                    timestamp=item.timestamp,
                    importance=decayed_value,
                )
            )
            report.decayed += 1
            report.ids.append(item.id)
        if upserts:
            long_term.apply_batch(
                upserts=upserts, deletes=[], project_id=project_id, employee_id=employee_id
            )
    if report.decayed:
        logger.info("memory decay: %s", report.summary)
    return report


def forget_stale_items(
    long_term: LongTermMemory,
    *,
    now: datetime | None = None,
    threshold: float = FORGET_IMPORTANCE_THRESHOLD,
    min_age_days: float = FORGET_MIN_AGE_DAYS,
) -> list[str]:
    """功能：遗忘陈旧低价值长期条目——四个条件全满足才删，返回实际删除 id。

    条件：importance < threshold 且龄 > min_age_days 且 recall_count == 0 且非显式。
    整批安全阀：候选数等于库内总数时**整批放弃**——否则一次阈值误配就会把长期
    记忆清空（Hermes 事故 #103419 的教训）。
    """
    if os.environ.get(FORGET_ENV_VAR, "1") == "0":
        logger.info("memory forget disabled by %s=0", FORGET_ENV_VAR)
        return []
    moment = now or datetime.now(UTC)
    plans: list[tuple[str | None, str | None, list[str]]] = []
    total_seen = 0
    for project_id, employee_id, page in _iter_scoped(long_term):
        total_seen += len(page)
        candidates = [
            item.id
            for item in page
            if item.importance < threshold
            and _recall_count(item) == 0
            and not is_explicit(item)
            and item.timestamp is not None
            and (moment - datetime.fromtimestamp(float(item.timestamp), tz=UTC)).total_seconds()
            / 86400.0
            >= min_age_days
        ]
        if candidates:
            plans.append((project_id, employee_id, candidates))

    deletable = sum(len(ids) for _, _, ids in plans)
    if deletable == 0:
        return []
    if deletable >= total_seen:
        logger.warning(
            "memory forget skipped: every stored item (%d) qualifies for deletion", total_seen
        )
        return []

    forgotten: list[str] = []
    for project_id, employee_id, ids in plans:
        try:
            long_term.apply_batch(
                upserts=[], deletes=ids, project_id=project_id, employee_id=employee_id
            )
        except ValueError as e:
            # 整批全或无：该分区目标已变动（并发删除/预算超限）→ 放弃该分区
            logger.warning(
                "memory forget batch rejected (scope %s/%s): %s", project_id, employee_id, e
            )
            continue
        forgotten.extend(ids)
    if forgotten:
        logger.info("memory forget: removed %d stale low-value item(s)", len(forgotten))
    return forgotten


def run_decay_maintenance(long_term: LongTermMemory, *, now: datetime | None = None) -> DecayReport:
    """功能：维护周期入口——先衰减再遗忘（衰减后才轮到遗忘门槛判定）。"""
    report = decay_items(long_term, now=now)
    forget_stale_items(long_term, now=now)
    return report


__all__ = [
    "DECAY_HALF_LIFE_DAYS",
    "DECAY_IMPORTANCE_FLOOR",
    "FORGET_ENV_VAR",
    "FORGET_IMPORTANCE_THRESHOLD",
    "FORGET_MIN_AGE_DAYS",
    "RECALL_IMPORTANCE_BOOST",
    "RECALL_PROTECTION_DAYS",
    "DecayReport",
    "decay_items",
    "decay_reference_instant",
    "explicit_flags",
    "forget_stale_items",
    "is_explicit",
    "run_decay_maintenance",
]
