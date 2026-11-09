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


class TestBaseSqlAdapter:
    def test_apply_limit_default(self) -> None:
        adapter = _ConcreteTestAdapter()
        result = adapter._apply_limit("SELECT * FROM users", 10)
        assert result == "SELECT * FROM (SELECT * FROM users) AS _q LIMIT 10"

    def test_apply_limit_already_has_limit(self) -> None:
        adapter = _ConcreteTestAdapter()
        result = adapter._apply_limit("SELECT * FROM users LIMIT 5", 10)
        assert result == "SELECT * FROM users LIMIT 5"

    def test_apply_limit_oracle(self) -> None:
        adapter = _ConcreteTestAdapter()
        adapter.conn_type = "oracle"
        result = adapter._apply_limit("SELECT * FROM users", 10)
        assert result == "SELECT * FROM (SELECT * FROM users) WHERE ROWNUM <= 10"

    def test_apply_limit_sqlserver(self) -> None:
        adapter = _ConcreteTestAdapter()
        adapter.conn_type = "sqlserver"
        result = adapter._apply_limit("SELECT * FROM users", 10)
        assert result == "SELECT TOP (10) * FROM (SELECT * FROM users) AS _q"

    def test_assert_readonly_rejects_multi_statement(self) -> None:
        """
        第二段写入语句绕过只读校验。"""
        adapter = _ConcreteTestAdapter()
        with pytest.raises(ValueError, match="only read-only"):
            adapter._assert_readonly("SELECT 1; DROP TABLE users")
        with pytest.raises(ValueError, match="only read-only"):
            adapter._assert_readonly("SELECT 1; INSERT INTO users VALUES (1)")
        with pytest.raises(ValueError, match="only read-only"):
            adapter._assert_readonly("PRAGMA table_info(users); DELETE FROM users")

    def test_assert_readonly_allows_multi_select(self) -> None:
        adapter = _ConcreteTestAdapter()
        adapter._assert_readonly("SELECT 1; SELECT 2")

    def test_apply_limit_does_not_wrap_pragma(self) -> None:
        """
        PRAGMA 天然只读小结果，直接原样返回。"""
        adapter = _ConcreteTestAdapter()
        result = adapter._apply_limit("PRAGMA table_info(users)", 10)
        assert result == "PRAGMA table_info(users)"

    def test_assert_readonly_rejects_write_pragma(self) -> None:
        """
        状态）——PRAGMA 必须白名单：只允许只读类（table_info 等）。"""
        adapter = _ConcreteTestAdapter()
        for sql in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA journal_mode = wal",
            "PRAGMA synchronous=OFF",
            "PRAGMA cache_size=-100",
        ):
            with pytest.raises(ValueError, match="only read-only"):
                adapter._assert_readonly(sql), f"写类 PRAGMA 必须拒绝：{sql}"

    def test_assert_readonly_allows_read_pragma(self) -> None:
        adapter = _ConcreteTestAdapter()
        for sql in (
            "PRAGMA table_info(users)",
            "PRAGMA index_list(users)",
            "PRAGMA foreign_key_list(users)",
            "PRAGMA database_list",
        ):
            adapter._assert_readonly(sql)

    def test_assert_readonly_allows_comment_and_string_semicolons(self) -> None:
        """
        合法 SQL）。"""
        adapter = _ConcreteTestAdapter()
        for sql in (
            "SELECT 1; /* a; b */ SELECT 2",
            "SELECT * FROM t WHERE name='a;b'",
            "SELECT 1 /* trail; comment */",
            'SELECT * FROM t WHERE name="a;b"',
            "SELECT 1; -- line; comment\nSELECT 2",
        ):
            adapter._assert_readonly(sql)

    def test_assert_readonly_still_rejects_real_multi_statement(self) -> None:
        adapter = _ConcreteTestAdapter()
        for sql in (
            "SELECT 1; DROP TABLE users",
            "SELECT 1; DELETE FROM users",
        ):
            with pytest.raises(ValueError, match="only read-only"):
                adapter._assert_readonly(sql)
