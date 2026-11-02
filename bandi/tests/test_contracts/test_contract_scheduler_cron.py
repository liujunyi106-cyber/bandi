# 功能：Day134 定时任务调度器契约（A）——CronExpr 5 字段解析 + next_after 触发计算
# 背景：定时自动化内核依赖。方案 A = 自实现 5 字段 cron（零外部依赖，不引 croniter——
# 对齐「Python + stdlib、无 Windows 外部依赖」纪律）。语义对齐 Vixie cron：day-of-month
# 与 day-of-week 同时受限时 OR（任一匹配即触发）。
# 契约钉行为族：合法解析 / 非法拒绝 / 别名展开 / 步进列表范围 / next_after 严格大于 /
# 星期语义 / 跨年·月末·闰年边界 / 域 OR 语义。
from __future__ import annotations

from datetime import datetime

import pytest

from bandi.scheduler.cron import CronExpr, InvalidCronExpressionError


def dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s)


class TestCronParse:
    """解析族：合法结构 / 非法拒绝（字段数/越界/坏 token）。"""

    def test_parse_valid_standard(self) -> None:
        expr = CronExpr("*/5 * * * *")
        assert expr.original == "*/5 * * * *"
        assert expr.minute == {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}

    def test_parse_field_counts(self) -> None:
        # 4 字段 / 6 字段一律拒绝——标准 5 字段 cron
        for bad in ("* * * *", "* * * * * *"):
            with pytest.raises(InvalidCronExpressionError):
                CronExpr(bad)

    def test_parse_range_and_list(self) -> None:
        expr = CronExpr("0 9-17 1,15 * mon-fri")
        assert expr.hour == set(range(9, 18))
        assert expr.day_of_month == {1, 15}
        assert expr.day_of_week == {1, 2, 3, 4, 5}  # mon=1 ... fri=5

    def test_parse_step(self) -> None:
        assert CronExpr("*/15 * * * *").minute == {0, 15, 30, 45}
        assert CronExpr("0-30/10 * * * *").minute == {0, 10, 20, 30}

    def test_parse_out_of_range_rejected(self) -> None:
        for bad in ("60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 7"):
            with pytest.raises(InvalidCronExpressionError):
                CronExpr(bad)

    def test_parse_bad_token_rejected(self) -> None:
        for bad in ("a * * * *", "* * * * x", "1- * * * *", "*/0 * * * *", "1-0 * * * *"):
            with pytest.raises(InvalidCronExpressionError):
                CronExpr(bad)

    def test_aliases(self) -> None:
        assert CronExpr("@daily").minute == {0} and CronExpr("@daily").hour == {0}
        assert CronExpr("@hourly").minute == {0}
        assert CronExpr("@monthly").day_of_month == {1}
        assert CronExpr("@weekly").day_of_week == {0}  # Sunday
        assert CronExpr("@yearly").month == {1} and CronExpr("@yearly").day_of_month == {1}
        with pytest.raises(InvalidCronExpressionError):
            CronExpr("@every_ten_minutes")

    def test_parse_is_idempotent_original_preserved(self) -> None:
        expr = CronExpr("30 4 * * 1-5")
        assert str(expr) == "30 4 * * 1-5"


class TestCronNextAfter:
    """next_after 族：严格大于 now、秒清零、边界与域语义。"""

    def test_next_after_every_minute(self) -> None:
        expr = CronExpr("* * * * *")
        now = dt(2026, 9, 5, 12, 30, 45)
        assert expr.next_after(now) == dt(2026, 9, 5, 12, 31)

    def test_next_after_minute_boundary_rolls_hour(self) -> None:
        expr = CronExpr("*/15 * * * *")
        assert expr.next_after(dt(2026, 9, 5, 12, 50)) == dt(2026, 9, 5, 13, 0)
        assert expr.next_after(dt(2026, 9, 5, 12, 45)) == dt(2026, 9, 5, 13, 0)

    def test_next_after_same_hour_later_minute(self) -> None:
        expr = CronExpr("0 9 * * *")
        assert expr.next_after(dt(2026, 9, 5, 8, 59)) == dt(2026, 9, 5, 9, 0)
        # 已过 9:00 → 明天 9:00
        assert expr.next_after(dt(2026, 9, 5, 9, 0)) == dt(2026, 9, 6, 9, 0)

    def test_next_after_dow(self) -> None:
        expr = CronExpr("0 8 * * 1")  # 每周一 8:00
        assert expr.next_after(dt(2026, 9, 5, 12, 0)) == dt(2026, 9, 7, 8, 0)

    def test_next_after_month_end_rolls(self) -> None:
        expr = CronExpr("0 0 1 * *")  # 每月 1 号
        assert expr.next_after(dt(2026, 9, 30, 23, 59)) == dt(2026, 10, 1, 0, 0)

    def test_next_after_year_end_rolls(self) -> None:
        expr = CronExpr("0 0 1 1 *")  # 每年 1 月 1 日
        assert expr.next_after(dt(2026, 12, 31, 23, 30)) == dt(2027, 1, 1, 0, 0)

    def test_next_after_leap_year_feb_29(self) -> None:
        expr = CronExpr("0 0 29 2 *")  # 2 月 29 日（闰年）
        # 2026 非闰年 → 跳过；2028 闰年
        assert expr.next_after(dt(2026, 1, 1)) == dt(2028, 2, 29, 0, 0)

    def test_next_after_leap_day_already_passed_same_year(self) -> None:
        expr = CronExpr("0 0 29 2 *")
        assert expr.next_after(dt(2028, 3, 1)) == dt(2032, 2, 29, 0, 0)

    def test_dom_and_dow_or_semantics(self) -> None:
        expr = CronExpr("0 0 15 * 0")
        assert expr.next_after(dt(2026, 9, 1)) == dt(2026, 9, 6, 0, 0)
        # 09-07（周一）起算：下一个 OR 命中 = 09-13（周日），不是 15 号
        assert expr.next_after(dt(2026, 9, 7)) == dt(2026, 9, 13, 0, 0)

    def test_next_after_seconds_are_zeroed(self) -> None:
        expr = CronExpr("* * * * *")
        now = datetime(2026, 9, 5, 12, 30, 0, 123456)
        nxt = expr.next_after(now)
        assert nxt.second == 0 and nxt.microsecond == 0

    def test_next_after_weekday_name_in_middle(self) -> None:
        # 周六中午 → 下周一（默认无 step）
        expr = CronExpr("30 9 * * mon-fri")
        assert expr.next_after(dt(2026, 9, 5, 12, 0)) == dt(2026, 9, 7, 9, 30)
