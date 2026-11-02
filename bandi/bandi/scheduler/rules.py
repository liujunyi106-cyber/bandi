# 功能：定时任务规则引擎——周期/按间隔/单次的下一次触发时间计算
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta
from typing import Any

from bandi.scheduler.cron import CronExpr

PERIODIC_UNITS = ("yearly", "monthly", "biweekly", "weekly", "daily")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _day_candidate(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour=hour, minute=minute))


def _month_day(year: int, month: int, day: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(max(1, day), last))


def _periodic_next(spec: dict[str, Any], base: datetime, anchor: date | None) -> datetime | None:
    unit = str(spec.get("unit") or "daily")
    hour = min(23, max(0, _as_int(spec.get("hour"), 9)))
    minute = min(59, max(0, _as_int(spec.get("minute"), 0)))
    if unit == "daily":
        candidate = _day_candidate(base.date(), hour, minute)
        return candidate if candidate > base else candidate + timedelta(days=1)
    if unit == "weekly":
        target = min(7, max(1, _as_int(spec.get("weekday"), 1)))
        delta = (target - base.isoweekday()) % 7
        candidate = _day_candidate(base.date() + timedelta(days=delta), hour, minute)
        if candidate <= base:
            candidate += timedelta(days=7)
        return candidate
    if unit == "biweekly":
        target = min(7, max(1, _as_int(spec.get("weekday"), 1)))
        start = anchor or base.date()
        delta = (target - start.isoweekday()) % 7
        candidate = _day_candidate(start + timedelta(days=delta), hour, minute)
        while candidate <= base:
            candidate += timedelta(days=14)
        return candidate
    if unit == "monthly":
        day = _as_int(spec.get("day"), 1)
        candidate = _day_candidate(_month_day(base.year, base.month, day), hour, minute)
        if candidate > base:
            return candidate
        year = base.year + (1 if base.month == 12 else 0)
        month = 1 if base.month == 12 else base.month + 1
        return _day_candidate(_month_day(year, month, day), hour, minute)
    if unit == "yearly":
        month = min(12, max(1, _as_int(spec.get("month"), 1)))
        day = _as_int(spec.get("day"), 1)
        if month == 2 and day == 29:
            year = base.year
            while not calendar.isleap(year):
                year += 1
            candidate = _day_candidate(_month_day(year, month, day), hour, minute)
            if candidate > base:
                return candidate
            year += 1
            while not calendar.isleap(year):
                year += 1
            return _day_candidate(_month_day(year, month, day), hour, minute)
        candidate = _day_candidate(_month_day(base.year, month, day), hour, minute)
        if candidate > base:
            return candidate
        return _day_candidate(_month_day(base.year + 1, month, day), hour, minute)
    raise ValueError(f"unsupported periodic unit: {unit}")


def _interval_next(spec: dict[str, Any], base: datetime) -> datetime | None:
    every = min(23, max(1, _as_int(spec.get("every_hours"), 2)))
    weekdays = {_as_int(value, 0) for value in spec.get("weekdays") or []}
    weekdays = {value for value in weekdays if 1 <= value <= 7}
    if not weekdays:
        raise ValueError("interval weekdays required")
    for offset in range(0, 15):
        day = base.date() + timedelta(days=offset)
        if day.isoweekday() not in weekdays:
            continue
        for hour in range(0, 24, every):
            candidate = _day_candidate(day, hour, 0)
            if candidate > base:
                return candidate
    return None


def _once_next(spec: dict[str, Any], base: datetime) -> datetime | None:
    day = _parse_date(spec.get("date"))
    if day is None:
        return None
    candidate = _day_candidate(
        day,
        min(23, max(0, _as_int(spec.get("hour"), 9))),
        min(59, max(0, _as_int(spec.get("minute"), 0))),
    )
    return candidate if candidate > base else None


def _next_raw(
    kind: str,
    spec: dict[str, Any],
    base: datetime,
    anchor: date | None,
) -> datetime | None:
    if kind == "periodic":
        return _periodic_next(spec, base, anchor)
    if kind == "interval":
        return _interval_next(spec, base)
    if kind == "once":
        return _once_next(spec, base)
    if kind == "cron":
        cron = str(spec.get("cron") or "").strip()
        if not cron:
            return None
        return CronExpr(cron).next_after(base)
    raise ValueError(f"unsupported schedule kind: {kind}")


def compute_next_run(
    *,
    kind: str,
    spec: dict[str, Any],
    base: datetime,
    effective_from: str = "",
    effective_until: str = "",
    anchor: date | None = None,
) -> datetime | None:
    """功能：按调度类型和生效区间计算 base 之后的下一次触发时间。"""
    normalized = str(kind or "cron")
    resolved_base = base
    start = _parse_date(effective_from)
    if start is not None and resolved_base.date() < start:
        resolved_base = datetime.combine(start, time.min) - timedelta(seconds=1)
    candidate = _next_raw(normalized, spec, resolved_base, anchor)
    if candidate is None:
        return None
    end = _parse_date(effective_until)
    if end is not None and candidate.date() > end:
        return None
    return candidate


def schedule_label(kind: str, spec: dict[str, Any]) -> str:
    """功能：调度类型 → 可读文案（后端日志/接口兜底）。"""
    if kind == "cron":
        return str(spec.get("cron") or "cron")
    if kind == "once":
        return f"单次 {spec.get('date', '')} {_as_int(spec.get('hour'), 9):02d}:{_as_int(spec.get('minute'), 0):02d}"
    if kind == "interval":
        weekdays = spec.get("weekdays") or []
        return f"每 {_as_int(spec.get('every_hours'), 2)} 小时 · {len(weekdays)} 天"
    unit = str(spec.get("unit") or "daily")
    return f"{unit} {_as_int(spec.get('hour'), 9):02d}:{_as_int(spec.get('minute'), 0):02d}"
