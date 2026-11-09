# 功能：5 字段 cron 表达式解析 + 下次触发时间计算（零外部依赖，Vixie OR 语义）
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# 标准别名 → 5 字段展开
_ALIASES: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

# 字段范围：[name, lo, hi, names_map]
_FIELDS: list[tuple[str, int, int, dict[str, int] | None]] = [
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("day_of_month", 1, 31, None),
    (
        "month",
        1,
        12,
        {
            "jan": 1,
            "feb": 2,
            "mar": 3,
            "apr": 4,
            "may": 5,
            "jun": 6,
            "jul": 7,
            "aug": 8,
            "sep": 9,
            "oct": 10,
            "nov": 11,
            "dec": 12,
        },
    ),
    ("day_of_week", 0, 6, {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}),
]

_CRON_DOW_TO_PY = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
_PY_WEEKDAY_TO_CRON = {v: k for k, v in _CRON_DOW_TO_PY.items()}

_MAX_SEARCH_DAYS = 366 * 8  # 任意合法表达式的下次触发必然落在 8 年内（闰日每 4 年必到）


class InvalidCronExpressionError(ValueError):
    """cron 表达式非法（字段数/越界/坏 token/未知别名）。"""


def _resolve_names(token: str, names: dict[str, int] | None) -> str:
    if names is None:
        return token
    return names.get(token.lower(), token)


def _parse_field_value(token: str, lo: int, hi: int, names: dict[str, int] | None) -> set[int]:
    """解析单个字段 token（允许 * / 单值 / a-b 范围 / step）。"""
    step = 1
    if "/" in token:
        base, step_str = token.split("/", 1)
        token = base
        try:
            step = int(step_str)
        except ValueError as e:
            raise InvalidCronExpressionError(f"bad step: {step_str}") from e
        if step <= 0:
            raise InvalidCronExpressionError(f"step must be > 0: {step_str}")

    if token == "*":
        return set(range(lo, hi + 1, step))

    if "-" in token:
        a_str, b_str = token.split("-", 1)
        a_raw = _resolve_names(a_str.strip(), names)
        b_raw = _resolve_names(b_str.strip(), names)
        try:
            a = int(a_raw)
            b = int(b_raw)
        except ValueError as e:
            raise InvalidCronExpressionError(f"bad range: {token}") from e
        if not (lo <= a <= b <= hi):
            raise InvalidCronExpressionError(f"range out of bounds: {token}")
        return set(range(a, b + 1, step))

    raw = _resolve_names(token.strip(), names)
    try:
        v = int(raw)
    except ValueError as e:
        raise InvalidCronExpressionError(f"bad value: {token}") from e
    if not (lo <= v <= hi):
        raise InvalidCronExpressionError(f"value out of bounds: {token}")
    return {v}


def _parse_field(spec: str, lo: int, hi: int, names: dict[str, int] | None) -> set[int]:
    """按逗号拆列表，逐 token 解析后合并。"""
    values: set[int] = set()
    for token in spec.split(","):
        if not token.strip():
            raise InvalidCronExpressionError(f"empty token in: {spec!r}")
        values |= _parse_field_value(token.strip(), lo, hi, names)
    return values


def _is_day_match(
    date: datetime,
    dom: set[int],
    month: set[int],
    dow: set[int],
    dom_restricted: bool,
    dow_restricted: bool,
) -> bool:
    if date.month not in month:
        return False
    dom_hit = date.day in dom
    dow_hit = _PY_WEEKDAY_TO_CRON[date.weekday()] in dow
    if dom_restricted and dow_restricted:
        return dom_hit or dow_hit
    if dom_restricted:
        return dom_hit
    if dow_restricted:
        return dow_hit
    return True


@dataclass
class CronExpr:
    """标准 5 字段 cron（minute hour day-of-month month day-of-week）。"""

    original: str
    minute: set[int]
    hour: set[int]
    day_of_month: set[int]
    month: set[int]
    day_of_week: set[int]

    _dom_restricted: bool
    _dow_restricted: bool

    def __init__(self, expression: str) -> None:
        expr = _ALIASES.get(expression.strip(), expression.strip())
        parts = expr.split()
        if len(parts) != 5:
            raise InvalidCronExpressionError(
                f"expected 5 fields (minute hour dom month dow), got {len(parts)}: {expression!r}"
            )
        parsed = {
            name: _parse_field(spec, lo, hi, names)
            for (name, lo, hi, names), spec in zip(_FIELDS, parts, strict=True)
        }
        self.original = expr
        self.minute = parsed["minute"]
        self.hour = parsed["hour"]
        self.day_of_month = parsed["day_of_month"]
        self.month = parsed["month"]
        self.day_of_week = parsed["day_of_week"]
        self._dom_restricted = len(self.day_of_month) < 31
        self._dow_restricted = len(self.day_of_week) < 7

    def __str__(self) -> str:
        return self.original

    def matches(self, moment: datetime) -> bool:
        """moment 是否命中（分粒度；秒/微秒忽略）。"""
        if moment.minute not in self.minute or moment.hour not in self.hour:
            return False
        return _is_day_match(
            moment,
            self.day_of_month,
            self.month,
            self.day_of_week,
            self._dom_restricted,
            self._dow_restricted,
        )

    def next_after(self, now: datetime) -> datetime:
        """返回严格大于 now 的下一次触发时刻（秒/微秒清零）。

        无 8 年内触发（理论上仅合法表达式不存在此情形）→ InvalidCronExpressionError。
        """
        cand = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        deadline = cand + timedelta(days=_MAX_SEARCH_DAYS)
        while cand < deadline:
            if _is_day_match(
                cand,
                self.day_of_month,
                self.month,
                self.day_of_week,
                self._dom_restricted,
                self._dow_restricted,
            ):
                if cand.hour in self.hour:
                    for m in sorted(self.minute):
                        if m >= cand.minute:
                            return cand.replace(minute=m)
                    # 当小时内无剩余命中分钟 → 推进到下一整点
                    cand = cand.replace(minute=0) + timedelta(hours=1)
                    continue
                cand = cand.replace(minute=0) + timedelta(hours=1)
                continue
            cand = (cand + timedelta(days=1)).replace(hour=0, minute=0)
        raise InvalidCronExpressionError(f"no future occurrence within 8 years: {self.original!r}")
