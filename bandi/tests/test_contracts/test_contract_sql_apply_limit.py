# 功能：_apply_limit 注释/字符串感知契约（防批3-2 limit 空转复发）
# 契约原则：注释/字符串字面量内的 LIMIT 不得被当作真实 LIMIT；PRAGMA 前导注释仍不包装。
from __future__ import annotations

import pytest

from bandi.datasource.adapters.base_sql import BaseSqlAdapter
from bandi.datasource.core import (
    AdapterExecutionInput,
    AdapterPreviewInput,
    AdapterSqlInput,
)


class _ConcreteTestAdapter(BaseSqlAdapter):
    conn_type = "test"

    async def inspect_schema(self, input: AdapterExecutionInput = AdapterExecutionInput()) -> None:
        return None

    async def preview_table(self, input: AdapterPreviewInput) -> None:
        return None

    async def run_sql_readonly(self, input: AdapterSqlInput) -> None:
        return None


def test_contract_limit_inside_comment_still_wraps() -> None:
    """契约 1：块注释/行注释内的 `LIMIT 5` 不得被当作已有 LIMIT——"""
    adapter = _ConcreteTestAdapter()
    for sql in (
        "SELECT * FROM users /* LIMIT 10 */",
        "SELECT * FROM users -- LIMIT 10",
    ):
        result = adapter._apply_limit(sql, 10)
        assert result.startswith("SELECT * FROM ("), (
            f"注释内 LIMIT 必须仍包装：{sql!r} → {result!r}"
        )


def test_contract_limit_inside_string_still_wraps() -> None:
    """契约 2：字符串字面量内（含 LIKE 模式）的 LIMIT 不得被当作已有 LIMIT。"""
    adapter = _ConcreteTestAdapter()
    for sql in (
        "SELECT * FROM users WHERE name = 'LIMIT 5'",
        "SELECT * FROM users WHERE note LIKE '%LIMIT 10%'",
    ):
        result = adapter._apply_limit(sql, 10)
        assert result.startswith("SELECT * FROM ("), (
            f"字符串内 LIMIT 必须仍包装：{sql!r} → {result!r}"
        )


def test_contract_real_limit_not_wrapped_again() -> None:
    """契约 3：真实 LIMIT 不重复包装（回归）。"""
    adapter = _ConcreteTestAdapter()
    result = adapter._apply_limit("SELECT * FROM users LIMIT 5", 10)
    assert result == "SELECT * FROM users LIMIT 5"


def test_contract_pragma_with_leading_comment_not_wrapped() -> None:
    """契约 4：PRAGMA 前有前导注释时仍不得包装——"""
    adapter = _ConcreteTestAdapter()
    result = adapter._apply_limit("/* c */ PRAGMA table_info(users)", 10)
    assert "PRAGMA" in result and "FROM (" not in result, f"PRAGMA 不得包装：{result!r}"


def test_contract_limit_adjacent_variants_still_wrap() -> None:
    """契约 5：`LIMIT5`（无空格）、`LIMIT ALL`（非数字）不得被当作数字 LIMIT。"""
    adapter = _ConcreteTestAdapter()
    for sql in ("SELECT * FROM users LIMIT5", "SELECT * FROM users LIMIT ALL"):
        result = adapter._apply_limit(sql, 10)
        assert f"LIMIT {10}" in result, f"必须仍包装：{sql!r} → {result!r}"


def test_contract_nested_comment_and_string_still_wraps() -> None:
    """契约 6：注释内字符串内分号组合——`/* 'LIMIT 9' */` 不得被当作真实 LIMIT。"""
    adapter = _ConcreteTestAdapter()
    result = adapter._apply_limit("SELECT * FROM users /* 'LIMIT 9' */", 10)
    assert result.startswith("SELECT * FROM (")


def test_contract_mysql_style_limit_rejected() -> None:
    """契约 7：`LIMIT 10,20`（MySQL 逗号形式）SQLite 无法执行 → 显式拒绝，不得静默透传。"""
    adapter = _ConcreteTestAdapter()
    with pytest.raises(ValueError, match="LIMIT"):
        adapter._apply_limit("SELECT * FROM users LIMIT 10,20", 10)


def test_contract_comma_limit_inside_string_not_rejected() -> None:
    """契约 8：字符串/注释内的 `LIMIT 10,20` 不触发拒绝（仍正常包装）。"""
    adapter = _ConcreteTestAdapter()
    result = adapter._apply_limit("SELECT * FROM users WHERE note = 'LIMIT 10,20'", 10)
    assert result.startswith("SELECT * FROM (")
