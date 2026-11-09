from __future__ import annotations

import re

from bandi.datasource.core import DataSourceAdapter

_READONLY_PREFIX_RE = re.compile(r"^\s*(SELECT|WITH|PRAGMA|EXPLAIN)\b", re.IGNORECASE)

# PRAGMA 白名单——只读类 PRAGMA 放行，写类（journal_mode/synchronous/
# cache_size 等改写库文件状态）绕过 query_only 只读，必须拒绝
_READONLY_PRAGMA_RE = re.compile(
    r"^\s*PRAGMA\s+(table_info|index_info|index_list|foreign_key_list|database_list"
    r"|table_xinfo|compile_options|function_list|module_list|pragma_list)"
    r"(?:\s|\(|$)",
    re.IGNORECASE,
)


def _split_statements(sql: str) -> list[str]:
    """分号感知注释/字符串的分段——注释内分号（`/* a; b */`）与字符串字面量内分号
    （`'a;b'`）不得误拆成段，避免假阳性拒绝合法 SQL。"""
    statements: list[str] = []
    current: list[str] = []
    i = 0
    n = len(sql)
    in_single = in_double = False
    in_line_comment = in_block_comment = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            current.append(ch)
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                current.append("*/")
                i += 1
                in_block_comment = False
            else:
                current.append(ch)
        elif in_single:
            current.append(ch)
            if ch == "'":
                in_single = False
        elif in_double:
            current.append(ch)
            if ch == '"':
                in_double = False
        elif ch == "-" and nxt == "-":
            in_line_comment = True
            current.append(ch)
        elif ch == "/" and nxt == "*":
            in_block_comment = True
            current.append(ch)
        elif ch == "'":
            in_single = True
            current.append(ch)
        elif ch == '"':
            in_double = True
            current.append(ch)
        elif ch == ";":
            statements.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    if "".join(current).strip():
        statements.append("".join(current))
    return statements


def _strip_leading_comments(stmt: str) -> str:
    """剥离段前导注释（`/* a; b */ SELECT 2` 段以注释开头时前缀正则
    不匹配 → 假阳性拒绝）。只剥前导注释，注释后必须仍有语句才继续校验。"""
    s = stmt.strip()
    while s:
        if s.startswith("/*"):
            end = s.find("*/")
            if end == -1:
                return ""
            s = s[end + 2 :].strip()
        elif s.startswith("--"):
            nl = s.find("\n")
            if nl == -1:
                return ""
            s = s[nl + 1 :].strip()
        else:
            return s
    return ""


_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+\b", re.IGNORECASE)
_LIMIT_OFFSET_COMMA_RE = re.compile(r"\s*,\s*\d+")


def _limit_clause_kind(sql: str) -> str | None:
    """注释/字符串感知扫描——代码层（非注释/字符串字面量）LIMIT 子句形态。
    返回 "standard"（标准 LIMIT n）/ "comma"（MySQL offset,count 形式）/ None（无 LIMIT）。"""
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch in "'\"":
            quote = ch
            i += 1
            while i < n:
                if sql[i] == quote:
                    i += 1
                    break
                i += 1
        else:
            m = _LIMIT_RE.match(sql, i)
            if m:
                if _LIMIT_OFFSET_COMMA_RE.match(sql, m.end()):
                    return "comma"
                return "standard"
            i += 1
    return None


class BaseSqlAdapter(DataSourceAdapter):
    conn_type: str = ""

    def _assert_readonly(self, sql: str) -> None:
        # 功能：显式只读语句检测——非 SELECT/WITH/PRAGMA/开头即拒绝
        # 分号分段每段都检——`SELECT 1; DROP x` 第二段写入语句不能绕过只读校验
        for stmt in _split_statements(sql):
            stmt = _strip_leading_comments(stmt)
            if not stmt:
                continue
            if not _READONLY_PREFIX_RE.match(stmt):
                raise ValueError("only read-only SQL is allowed")
            # PRAGMA 仅白名单只读类（journal_mode=WAL 改写库文件绕过）
            if re.match(r"^\s*PRAGMA\b", stmt, re.IGNORECASE) and not _READONLY_PRAGMA_RE.match(
                stmt
            ):
                raise ValueError("only read-only SQL is allowed")

    def _apply_limit(self, sql: str, limit: int) -> str:
        kind = _limit_clause_kind(sql)
        if kind == "comma":
            raise ValueError("LIMIT offset,count syntax is not supported; use LIMIT n OFFSET m")
        if kind == "standard":
            return sql
        # PRAGMA 不能被子查询包装（`SELECT * FROM (PRAGMA ...)` 语法非法）
        # PRAGMA 天然只读小结果，原样返回；前导注释先剥离再判定
        if re.match(r"^\s*PRAGMA\b", _strip_leading_comments(sql), re.IGNORECASE):
            return sql
        if self.conn_type == "sqlserver":
            return f"SELECT TOP ({limit}) * FROM ({sql}) AS _q"
        if self.conn_type == "oracle":
            return f"SELECT * FROM ({sql}) WHERE ROWNUM <= {limit}"
        return f"SELECT * FROM ({sql}) AS _q LIMIT {limit}"
