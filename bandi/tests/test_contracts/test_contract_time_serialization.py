# 功能：时间序列化契约——接口层时间字段一律带时区偏移，前端不必猜时区
# 覆盖四种输入形态（naive datetime / aware UTC / 本地 naive 字符串 / aware 字符串）
# 与空值、不可解析值；破坏者用例钉「naive 不得被当成 UTC」。
from __future__ import annotations

import re
from datetime import UTC, datetime

from bandi.utils.timefmt import iso_local

OFFSET_SUFFIX = re.compile(r"[+-]\d{2}:\d{2}$")


class TestIsoLocal:
    def test_naive_datetime_treated_as_local(self) -> None:
        value = datetime(2026, 10, 27, 23, 35, 12)
        result = iso_local(value)
        assert OFFSET_SUFFIX.search(result)
        assert result.startswith("2026-10-27T23:35:12"), (
            "naive 值按本地口径解释，时钟读数不得被改写"
        )

    def test_aware_utc_converted_to_local(self) -> None:
        value = datetime(2026, 10, 27, 15, 35, 12, tzinfo=UTC)
        result = iso_local(value)
        assert OFFSET_SUFFIX.search(result)
        assert result == iso_local(value.astimezone()), "aware 值必须换算到本地时区"

    def test_naive_string_normalized_with_offset(self) -> None:
        result = iso_local("2026-10-27 23:35:12.123456")
        assert OFFSET_SUFFIX.search(result), "会话库里的本地 naive 字符串必须补偏移"
        assert result.startswith("2026-10-27T23:35:12.123456")

    def test_aware_string_keeps_instant(self) -> None:
        utc_text = "2026-10-27T15:35:12+00:00"
        assert iso_local(utc_text) == iso_local(datetime(2026, 10, 27, 15, 35, 12, tzinfo=UTC))

    def test_z_suffix_parsed_as_utc(self) -> None:
        assert iso_local("2026-10-27T15:35:12Z") == iso_local("2026-10-27T15:35:12+00:00")

    def test_empty_and_unparsable(self) -> None:
        assert iso_local(None) == ""
        assert iso_local("") == ""
        assert iso_local("   ") == ""
        assert iso_local("not-a-timestamp") == "not-a-timestamp", "不可解析值原样返回，不吞数据"

    def test_breaker_naive_not_shifted_as_utc(self) -> None:
        """破坏者：若把本地 naive 当 UTC，输出时钟读数会整体偏移（-8h）。"""
        local_naive = iso_local(datetime(2026, 10, 27, 23, 35, 12))
        as_utc = datetime(2026, 10, 27, 23, 35, 12, tzinfo=UTC).isoformat()
        assert local_naive != as_utc
        assert local_naive[:19] == "2026-10-27T23:35:12"
