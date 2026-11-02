from datetime import datetime

from bandi.scheduler.rules import compute_next_run

BASE = datetime(2026, 10, 30, 15, 0)


def test_periodic_daily_and_weekly() -> None:
    assert compute_next_run(
        kind="periodic", spec={"unit": "daily", "hour": 9, "minute": 0}, base=BASE
    ) == datetime(2026, 10, 31, 9, 0)
    assert compute_next_run(
        kind="periodic",
        spec={"unit": "weekly", "weekday": 1, "hour": 9, "minute": 30},
        base=BASE,
    ) == datetime(2026, 11, 2, 9, 30)


def test_periodic_biweekly_monthly_yearly() -> None:
    assert compute_next_run(
        kind="periodic",
        spec={"unit": "biweekly", "weekday": 2, "hour": 9, "minute": 0},
        base=BASE,
        anchor=datetime(2026, 10, 20).date(),
    ) == datetime(2026, 11, 3, 9, 0)
    assert compute_next_run(
        kind="periodic", spec={"unit": "monthly", "day": 31, "hour": 9, "minute": 0}, base=BASE
    ) == datetime(2026, 10, 31, 9, 0)
    assert compute_next_run(
        kind="periodic",
        spec={"unit": "yearly", "month": 2, "day": 29, "hour": 9, "minute": 0},
        base=BASE,
    ) == datetime(2028, 2, 29, 9, 0)


def test_interval_skips_unselected_weekdays() -> None:
    assert compute_next_run(
        kind="interval",
        spec={"every_hours": 6, "weekdays": [1]},
        base=BASE,
    ) == datetime(2026, 11, 2, 0, 0)


def test_once_and_effective_window() -> None:
    assert compute_next_run(
        kind="once",
        spec={"date": "2026-10-31", "hour": 9, "minute": 15},
        base=BASE,
    ) == datetime(2026, 10, 31, 9, 15)
    assert compute_next_run(
        kind="periodic", spec={"unit": "daily", "hour": 9, "minute": 0}, base=BASE
    ) == datetime(2026, 10, 31, 9, 0)
    assert compute_next_run(
        kind="periodic",
        spec={"unit": "daily", "hour": 9, "minute": 0},
        base=BASE,
        effective_from="2026-11-01",
    ) == datetime(2026, 11, 1, 9, 0)
    assert (
        compute_next_run(
            kind="periodic",
            spec={"unit": "daily", "hour": 9, "minute": 0},
            base=BASE,
            effective_until="2026-10-30",
        )
        is None
    )
