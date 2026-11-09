# 功能：date_calculator — 日期计算
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext

COMMON_DATE_FORMATS: list[str] = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%d %b %Y",
    "%d %B %Y",
    "%B %d, %Y",
    "%b %d, %Y",
]

OPS = {
    "add",
    "subtract",
    "difference",
    "business_days",
    "add_business_days",
    "format",
    "convert_timezone",
    "now",
    "compare",
    "parse",
}


class DateCalculatorTool(BaseTool):
    """功能：日期加减/差值/工作日/时区转换/格式化（10 个 op）。"""

    name = "date_calculator"
    description = "日期时间计算与格式化：add/subtract/difference/business_days/add_business_days/format/convert_timezone/now/compare/parse"
    category = ToolCategory.UTIL
    feature_group = "util"
    is_read_only = True
    is_concurrency_safe = True
    capability_tags: list[str] = []
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": sorted(OPS),
                "description": "要执行的操作",
            },
            "date": {"type": "string", "description": "日期字符串"},
            "date2": {
                "type": "string",
                "description": "compare/difference 的第二个日期；difference 缺省=今天",
            },
            "days": {"type": "integer", "description": "天偏移量"},
            "weeks": {"type": "integer", "description": "周偏移量"},
            "months": {"type": "integer", "description": "月偏移量"},
            "years": {"type": "integer", "description": "年偏移量"},
            "input_format": {"type": "string", "description": "输入解析格式"},
            "format": {"type": "string", "description": "输出格式"},
            "to_timezone": {"type": "string", "description": "目标时区"},
            "from_timezone": {"type": "string", "description": "原时区（convert_timezone）"},
            "include_end": {"type": "boolean", "description": "business_days 含结束日"},
            "holidays": {"type": "array", "items": {"type": "string"}, "description": "节假日列表"},
            "weekend_days": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "周末工作日索引",
            },
        },
        "required": ["operation"],
    }

    def _parse_input_date(self, date_str: str, input_format: str | None = None) -> datetime:
        if input_format:
            return datetime.strptime(date_str, input_format)
        for fmt in COMMON_DATE_FORMATS:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        raise ValueError(f"Could not parse date: {date_str}. Please provide input_format.")

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        op = params.get("operation", "now")
        if op not in OPS:
            raise ValueError(f"未知操作: {op}")
        handler = getattr(self, f"_op_{op}", None)
        if handler is None:
            raise ValueError(f"未知操作: {op}")
        return await handler(params)

    async def _op_add(self, params: dict[str, Any]) -> dict[str, Any]:
        date_str = params.get("date")
        if not date_str:
            raise ValueError("Date is required for add operation")
        input_format = params.get("input_format")
        dt = self._parse_input_date(date_str, input_format)
        result_dt = dt
        if params.get("days"):
            result_dt += timedelta(days=params["days"])
        if params.get("weeks"):
            result_dt += timedelta(weeks=params["weeks"])
        if params.get("months"):
            month = dt.month - 1 + params["months"]
            year = dt.year + month // 12
            month = month % 12 + 1
            day = min(
                dt.day,
                [
                    31,
                    29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                    31,
                    30,
                    31,
                    30,
                    31,
                    31,
                    30,
                    31,
                    30,
                    31,
                ][month - 1],
            )
            result_dt = result_dt.replace(year=year, month=month, day=day)
        if params.get("years"):
            try:
                result_dt = result_dt.replace(year=result_dt.year + params["years"])
            except ValueError:
                result_dt = result_dt.replace(year=result_dt.year + params["years"], day=28)
        output_format = params.get("format", "%Y-%m-%d")
        return {
            "original": date_str,
            "result": result_dt.strftime(output_format),
            "operation": "add",
        }

    async def _op_subtract(self, params: dict[str, Any]) -> dict[str, Any]:
        flipped = dict(params)
        for k in ("days", "weeks", "months", "years"):
            if params.get(k) is not None:
                flipped[k] = -params[k]
        result = await self._op_add(flipped)
        result["operation"] = "subtract"
        return result

    async def _op_difference(self, params: dict[str, Any]) -> dict[str, Any]:
        date1_str = params.get("date")
        if not date1_str:
            raise ValueError("Date is required for difference operation")
        date2_str = params.get("date2")
        date2_defaulted = False
        if not date2_str:
            # 报错、3 连败触发工具暂停——缺省 date2=今天，且方向按「从今天到目标」
            # 解释（未来日期为正，与「距今还有多久」语义一致）
            date2_str = datetime.now().date().isoformat()
            date2_defaulted = True
            date1_str, date2_str = date2_str, date1_str
        input_format = params.get("input_format")
        dt1 = self._parse_input_date(date1_str, input_format)
        dt2 = self._parse_input_date(date2_str, input_format)
        difference = dt2 - dt1
        return {
            "date1": date1_str,
            "date2": date2_str,
            "date2_defaulted": date2_defaulted,
            "difference": {
                "days": difference.days,
                "total_seconds": int(difference.total_seconds()),
                "weeks": difference.days // 7,
                "hours": int(difference.total_seconds() // 3600),
                "minutes": int(difference.total_seconds() // 60),
            },
            "is_positive": difference.total_seconds() > 0,
        }

    async def _op_business_days(self, params: dict[str, Any]) -> dict[str, Any]:
        date1_str = params.get("date")
        date2_str = params.get("date2")
        if not date1_str or not date2_str:
            raise ValueError("Both date and date2 are required for business_days operation")
        input_format = params.get("input_format")
        dt1 = self._parse_input_date(date1_str, input_format).date()
        dt2 = self._parse_input_date(date2_str, input_format).date()
        holidays = set()
        for h in params.get("holidays", []):
            try:
                holidays.add(self._parse_input_date(h, input_format).date())
            except ValueError:
                continue
        weekend_days = set(params.get("weekend_days", [5, 6]))
        include_end = params.get("include_end", False)
        current = dt1
        count = 0
        calendar_days = 0
        while current < dt2 or (include_end and current <= dt2):
            if current.weekday() not in weekend_days and current not in holidays:
                count += 1
            current += timedelta(days=1)
            calendar_days += 1
        return {
            "start_date": date1_str,
            "end_date": date2_str,
            "business_days": count,
            "calendar_days": calendar_days,
            "holidays_excluded": [h for h in params.get("holidays", [])],
        }

    async def _op_add_business_days(self, params: dict[str, Any]) -> dict[str, Any]:
        date_str = params.get("date")
        days = params.get("days", 0)
        if not date_str:
            raise ValueError("Date is required for add_business_days operation")
        if days == 0:
            raise ValueError("Days is required for add_business_days operation")
        input_format = params.get("input_format")
        dt = self._parse_input_date(date_str, input_format).date()
        holidays = set()
        for h in params.get("holidays", []):
            try:
                holidays.add(self._parse_input_date(h, input_format).date())
            except ValueError:
                continue
        weekend_days = set(params.get("weekend_days", [5, 6]))
        direction = 1 if days > 0 else -1
        remaining = abs(days)
        current = dt
        while remaining > 0:
            current += timedelta(days=direction)
            if current.weekday() not in weekend_days and current not in holidays:
                remaining -= 1
        output_format = params.get("format", "%Y-%m-%d")
        return {
            "original": date_str,
            "result": current.strftime(output_format),
            "business_days_added": days,
            "calendar_days_elapsed": (current - dt).days,
        }

    async def _op_format(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._format_date(params)

    def _format_date(self, params: dict[str, Any]) -> dict[str, Any]:
        date_str = params.get("date")
        if not date_str:
            raise ValueError("Date is required for format operation")
        input_format = params.get("input_format")
        dt = self._parse_input_date(date_str, input_format)
        output_format = params.get("format", "%Y-%m-%d")
        common_formats = {
            "iso": dt.isoformat(),
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%H:%M:%S"),
            "datetime_iso": dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "short": dt.strftime("%d/%m/%Y"),
            "short_time": dt.strftime("%d/%m/%Y %H:%M"),
        }
        return {
            "original": date_str,
            "formatted": dt.strftime(output_format),
            "common_formats": common_formats,
        }

    async def _op_convert_timezone(self, params: dict[str, Any]) -> dict[str, Any]:
        date_str = params.get("date")
        from_tz = params.get("from_timezone", "UTC")
        to_tz_name = params.get("to_timezone", "UTC")
        if not date_str:
            raise ValueError("Date is required for convert_timezone operation")
        try:
            input_tz = ZoneInfo(from_tz)
            output_tz = ZoneInfo(to_tz_name)
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            raise ValueError(f"未知时区: {from_tz} 或 {to_tz_name}")
        dt = self._parse_input_date(date_str, params.get("input_format"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=input_tz)
        result_dt = dt.astimezone(output_tz)
        offset = result_dt.utcoffset()
        offset_hours = offset.total_seconds() / 3600 if offset else 0
        output_format = params.get("format", "%Y-%m-%dT%H:%M:%S %Z %z")
        return {
            "original": date_str,
            "from_timezone": from_tz,
            "to_timezone": to_tz_name,
            "result": result_dt.strftime(output_format),
            "result_iso": result_dt.isoformat(),
            "offset": result_dt.isoformat()[-6:],
            "offset_hours": offset_hours,
        }

    async def _op_now(self, params: dict[str, Any]) -> dict[str, Any]:
        timezone = params.get("to_timezone", "UTC")
        try:
            tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            tz = ZoneInfo("UTC")
            timezone = "UTC"
        now = datetime.now(tz)
        output_format = params.get("format", "%Y-%m-%dT%H:%M:%S%z")
        return {
            "timezone": timezone,
            "datetime": now.strftime(output_format),
            "iso": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "unix_timestamp": int(now.timestamp()),
            "components": {
                "year": now.year,
                "month": now.month,
                "day": now.day,
                "hour": now.hour,
                "minute": now.minute,
                "second": now.second,
                "weekday": now.weekday(),
                "weekday_name": now.strftime("%A"),
            },
        }

    async def _op_compare(self, params: dict[str, Any]) -> dict[str, Any]:
        date1_str = params.get("date")
        date2_str = params.get("date2")
        if not date1_str or not date2_str:
            raise ValueError("Both date and date2 are required for compare operation")
        input_format = params.get("input_format")
        dt1 = self._parse_input_date(date1_str, input_format)
        dt2 = self._parse_input_date(date2_str, input_format)
        if dt1 < dt2:
            comparison = "before"
        elif dt1 > dt2:
            comparison = "after"
        else:
            comparison = "equal"
        return {
            "date1": date1_str,
            "date2": date2_str,
            "comparison": comparison,
            "date1_is_before": comparison == "before",
            "date1_is_after": comparison == "after",
            "dates_are_equal": comparison == "equal",
            "same_day": dt1.date() == dt2.date(),
            "same_month": (dt1.year, dt1.month) == (dt2.year, dt2.month),
            "same_year": dt1.year == dt2.year,
        }

    async def _op_parse(self, params: dict[str, Any]) -> dict[str, Any]:
        result = self._format_date(params)
        result["operation"] = "parse"
        return result
