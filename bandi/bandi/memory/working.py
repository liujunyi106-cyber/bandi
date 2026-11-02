"""Scratchpad = 当前任务的临时工作区（区别于 long_term 知识层 / episodic 经历层）：

* KV 中间结果：任务快照/上下文片段按 ``turn:{n}`` 等键暂存，TTL 过期惰性清理；
* 工具调用历史：轮末自动追加环形缓冲（上限内保留最新），供复盘与检索。

检索升级：中文子串由 n-gram LIKE 全表扫描升级为 FTS5 索引召回
（同一 UTF-8/2-gram 分词），LIKE 仅在 FTS 失败 / 空条件回退保召回。
语义一一对应；内存 TTLCache 以 SQLite 落盘 + TTL 惰性清理替代
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bandi.memory.episodic import decay_factor, hit_relevance, tokenize_cn
from bandi.memory.long_term import (
    _CJK_RE,
    _fts_preprocess,
    _fts_simple_tokens,
    _simple_tokens,
)
from bandi.utils.db import connect_app_db

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 2 * 60 * 60
_DEFAULT_TOOL_HISTORY_LIMIT = 50
_FTS_CANDIDATE_MULTIPLIER = 8
_MAX_TURN_ROWS_PER_SESSION = 200


def _sanitize_fts_query(query: str) -> str:
    """FTS5 MATCH 安全化——token 化逐词引号 AND（实现见 long_term._fts_simple_tokens）。"""
    return _fts_simple_tokens(query) or " "


def _fts_query_for(query: str) -> str:
    """构造 FTS5 MATCH 串——含 切成 2-gram AND 连接（要求全部 gram 命中）
    非 走 sanitize（保留英文短语/分词语义）；对齐 long_term.search_fts 模式。"""
    has_cjk = bool(_CJK_RE.search(query or ""))
    if has_cjk:
        grams = [g for g in _fts_preprocess(query).split() if g]
        return " AND ".join(f'"{g}"' for g in grams) if grams else " "
    return _sanitize_fts_query(query)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ToolInvocation:
    """单次工具调用记录（轮末自动写入）。

    invocation_id：工具调用的稳定标识（底层 tool_call_id）。提供时按
    (session_id, invocation_id) 唯一去重——同 turn 重放/重跑不会重复入史；
    None 时保持原追加行为（向后兼容无稳定 ID 的调用方）。
    """

    name: str
    success: bool = True
    result_preview: str = ""
    error: str | None = None
    duration_ms: int | None = None
    session_id: str = ""
    created_at: str | None = None
    invocation_id: str | None = None


@dataclass
class ScratchHit:
    """检索命中统一载体——kind 区分 KV 快照与工具历史。"""

    kind: str  # "kv" | "tool"
    key: str
    value: str
    created_at: str = ""


class ScratchpadStore:
    def __init__(
        self,
        db_path: str,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        tool_history_limit: int = _DEFAULT_TOOL_HISTORY_LIMIT,
        half_life_days: float = 14.0,
    ) -> None:
        self.db_path = db_path
        self._ttl = max(1, int(ttl_seconds))
        self._limit = max(1, int(tool_history_limit))
        self._half_life = max(half_life_days, 1e-4)
        self._lock = threading.Lock()
        self._conn = connect_app_db(db_path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS scratch_kv (
                session_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (session_id, key)
            )"""
        )
        # 裁剪时全表扫描（TTL 豁免行是唯一无界增长面，裁剪频率最高）
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scratch_kv_turn "
            "ON scratch_kv(session_id) WHERE key LIKE 'turn:%'"
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS scratch_tools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                invocation_id TEXT,
                name TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 1,
                result_preview TEXT NOT NULL DEFAULT '',
                error TEXT,
                duration_ms INTEGER,
                created_at TEXT NOT NULL
            )"""
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scratch_tools_session ON scratch_tools(session_id)"
        )
        # 迁移：旧库的 scratch_tools 无 invocation_id 列（CREATE TABLE IF NOT
        # 不会重建既有表）——存在性检查后 ALTER 补列，再建唯一索引（防 no such column）。
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(scratch_tools)")}
        if "invocation_id" not in cols:
            self._conn.execute("ALTER TABLE scratch_tools ADD COLUMN invocation_id TEXT")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_scratch_tools_invocation "
            "ON scratch_tools(session_id, invocation_id)"
        )
        # 检索升级：FTS5 索引（content 主列 + session/kv_key 引用列）。
        # 冗余 FTS 行无害；LICENSE 语义见 _purge_expired 孤儿清理。
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS scratch_kv_fts USING fts5("
            "content, session_id UNINDEXED, kv_key UNINDEXED)"
        )
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS scratch_tools_fts USING fts5("
            "content, session_id UNINDEXED, kv_key UNINDEXED)"
        )
        self._fts_backfill()
        self._conn.commit()

    # -- KV --------------------------------------------------------------

    def set(
        self,
        key: str,
        value: str,
        session_id: str = "",
    ) -> None:
        if not (key or "").strip():
            raise ValueError("scratchpad key must be non-empty")
        if not (value or "").strip():
            raise ValueError("scratchpad value must be non-empty")
        with self._lock:
            self._purge_expired()
            self._conn.execute(
                "INSERT OR REPLACE INTO scratch_kv (session_id, key, value, created_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, key, value, _utc_now_iso()),
            )
            self._sync_kv_fts(session_id, key, value)
            self._conn.commit()

    def get(self, key: str, session_id: str = "") -> str | None:
        with self._lock:
            self._purge_expired()
            row = self._conn.execute(
                "SELECT value FROM scratch_kv WHERE session_id = ? AND key = ?",
                (session_id, key),
            ).fetchone()
        return row[0] if row is not None else None

    def all(self, session_id: str = "") -> dict[str, str]:
        with self._lock:
            self._purge_expired()
            rows = self._conn.execute(
                "SELECT key, value FROM scratch_kv WHERE session_id = ?", (session_id,)
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def clear(self, session_id: str = "") -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM scratch_kv WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM scratch_tools WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM scratch_kv_fts WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM scratch_tools_fts WHERE session_id = ?", (session_id,))
            self._conn.commit()
        return cur.rowcount

    # -- tool history ------------------------------------------------------

    def append_tool(self, invocation: ToolInvocation) -> None:
        with self._lock:
            self._purge_expired()
            created_at = invocation.created_at or _utc_now_iso()
            # 时同一次调用重试命中唯一索引，不产生重复历史
            invocation_id = invocation.invocation_id or self._fallback_invocation_id(invocation)
            self._conn.execute(
                "INSERT INTO scratch_tools "
                "(session_id, invocation_id, name, success, result_preview, error, "
                " duration_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, invocation_id) DO UPDATE SET "
                " name=excluded.name, success=excluded.success, "
                " result_preview=excluded.result_preview, error=excluded.error, "
                " duration_ms=excluded.duration_ms, created_at=excluded.created_at",
                (
                    invocation.session_id,
                    invocation_id,
                    invocation.name,
                    int(invocation.success),
                    invocation.result_preview,
                    invocation.error,
                    invocation.duration_ms,
                    created_at,
                ),
            )
            self._conn.execute(
                "DELETE FROM scratch_tools WHERE session_id = ? AND id NOT IN ("
                "SELECT id FROM scratch_tools WHERE session_id = ? ORDER BY id DESC LIMIT ?)",
                (invocation.session_id, invocation.session_id, self._limit),
            )
            # upsert 冲突路径 cursor.lastrowid 不可靠（保持本连接上一次 INSERT 的
            # 旧值）——按业务键回查真实行 id，防 FTS 索引到错行
            row = self._conn.execute(
                "SELECT id FROM scratch_tools WHERE session_id = ? AND invocation_id = ?",
                (invocation.session_id, invocation_id),
            ).fetchone()
            row_id = int(row[0]) if row else None
            if row_id:
                self._sync_tool_fts(invocation.session_id, row_id)
            else:
                self._sync_session_tools_fts(invocation.session_id)
            self._conn.commit()

    @staticmethod
    def _fallback_invocation_id(invocation: ToolInvocation) -> str:
        """功能：空 tool_call_id 的稳定兜底幂等键——按工具名 + 成功态 + 结果内容
        生成（不含 duration_ms/created_at，重试补写时不变）。"""
        raw = "|".join(
            (
                invocation.name,
                "1" if invocation.success else "0",
                invocation.result_preview or "",
                invocation.error or "",
            )
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"auto:{invocation.name}:{digest}"

    def tool_history(self, session_id: str = "", limit: int | None = None) -> list[ToolInvocation]:
        with self._lock:
            self._purge_expired()
            sql = (
                "SELECT name, success, result_preview, error, duration_ms, created_at, "
                "invocation_id "
                "FROM scratch_tools WHERE session_id = ? ORDER BY id DESC"
            )
            args: list[Any] = [session_id]
            if limit is not None:
                sql += " LIMIT ?"
                args.append(max(1, int(limit)))
            rows = self._conn.execute(sql, args).fetchall()
        return [
            ToolInvocation(
                name=r[0],
                success=bool(r[1]),
                result_preview=r[2],
                error=r[3],
                duration_ms=r[4],
                session_id=session_id,
                created_at=r[5],
                invocation_id=r[6],
            )
            for r in rows
        ]

    # -- search ----------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 5,
        session_id: str = "",
    ) -> list[tuple[ScratchHit, float]]:
        """相关性检索本 session 的 KV 快照 + 工具历史。

        FTS5 索引优先召回候选（2-gram + 英文分词），JOIN 主表过滤已删/
        TTL 过期行；FTS 失败或索引异常回退全表 LIKE 扫描保证零漏召。
        相关度 = 命中词占比（tokenize_cn 全中 1.0，部分封顶 0.6；纯英文原串
        包含匹配 1.0）× 时间衰减（半衰期公式，刚写入≈1.0）。
        """
        q = (query or "").strip()
        if not q:
            return []
        tokens = tokenize_cn(q)
        # 纯英文/符号查询 tokenize_cn 为空——须回退符号安全词表（否则 scored
        like_terms = tokens or _simple_tokens(q) or [q]
        with self._lock:
            self._purge_expired()
            kv_rows = self._fts_fetch(
                "SELECT k.key, k.value, k.created_at "
                "FROM scratch_kv_fts f JOIN scratch_kv k "
                "ON k.session_id = f.session_id AND k.key = f.kv_key "
                "WHERE k.session_id = ? AND scratch_kv_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (session_id, q, len(like_terms) * _FTS_CANDIDATE_MULTIPLIER),
            )
            tool_rows = self._fts_fetch(
                "SELECT t.id, t.result_preview, t.error, t.created_at "
                "FROM scratch_tools_fts f JOIN scratch_tools t "
                "ON t.session_id = f.session_id AND f.kv_key = 'tool:' || t.id "
                "WHERE t.session_id = ? AND scratch_tools_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (session_id, q, len(like_terms) * _FTS_CANDIDATE_MULTIPLIER),
            )
            if kv_rows is None or tool_rows is None:
                logger.warning("FTS5 search failed, falling back to LIKE scan")
                like_kv, like_tool = self._like_scan(session_id)
                if kv_rows is None:
                    kv_rows = like_kv
                if tool_rows is None:
                    tool_rows = like_tool

        candidates = [(ScratchHit("kv", r[0], r[1], r[2]), r[1]) for r in kv_rows] + [
            (
                ScratchHit("tool", f"tool:{r[0]}", f"{r[1]} {r[2] or ''}".strip(), r[3]),
                f"{r[1]} {r[2] or ''}",
            )
            for r in tool_rows
        ]

        now = datetime.now(UTC)
        scored: list[tuple[ScratchHit, float]] = []
        for hit, text in candidates:
            hit_count = sum(1 for t in like_terms if t in text)
            if hit_count == 0:
                continue
            relevance = hit_relevance(hit_count, len(like_terms))
            scored.append((hit, relevance * self._decay(hit.created_at, now)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[: max(1, limit)]

    # -- misc --------------------------------------------------------------

    def delete(self, key: str, session_id: str = "") -> bool:
        """功能：显式遗忘单条 KV 快照（主表 + FTS 索引同步，4.3）。

        工具历史为环形缓冲自动淘汰，不在显式遗忘面；返回是否命中。
        """
        k = (key or "").strip()
        if not k:
            return False
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM scratch_kv WHERE session_id = ? AND key = ?", (session_id, k)
            )
            self._conn.execute(
                "DELETE FROM scratch_kv_fts WHERE session_id = ? AND kv_key = ?", (session_id, k)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def count(self) -> int:
        with self._lock:
            self._purge_expired()
            row = self._conn.execute("SELECT COUNT(*) FROM scratch_kv").fetchone()
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- internals ---------------------------------------------------------

    def _decay(self, created_at: str, now: datetime) -> float:
        try:
            born = datetime.fromisoformat(created_at)
            if born.tzinfo is None:
                born = born.replace(tzinfo=UTC)
            age_days = max((now - born).total_seconds() / 86400.0, 0.0)
        except ValueError:
            age_days = 0.0
        return decay_factor(age_days, self._half_life)

    def _fts_fetch(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]] | None:
        """FTS 召回（锁内调用）——MATCH 语法/运行时异常返回 None 触发调用方回退。"""
        try:
            return list(
                self._conn.execute(sql, (params[0], _fts_query_for(params[1]), *params[2:]))
            )
        except sqlite3.Error:
            return None

    def _like_scan(self, session_id: str) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
        kv_rows = self._conn.execute(
            "SELECT key, value, created_at FROM scratch_kv WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        tool_rows = self._conn.execute(
            "SELECT id, result_preview, error, created_at FROM scratch_tools WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        return kv_rows, tool_rows

    def _fts_backfill(self) -> None:
        """存量库迁移：FTS 表空而主表有数据时全量灌入（新库首次写入前调用一次）。
        索引端 content 一律 _fts_preprocess（2-gram）——与查询端分词一致
        否则整段中文被 FTS5 当单 token，中文 grams 无法命中。"""
        kv_cnt = self._conn.execute("SELECT COUNT(*) FROM scratch_kv_fts").fetchone()[0]
        if kv_cnt == 0:
            rows = self._conn.execute("SELECT session_id, key, value FROM scratch_kv").fetchall()
            if rows:
                self._conn.executemany(
                    "INSERT INTO scratch_kv_fts (session_id, kv_key, content) VALUES (?, ?, ?)",
                    [(r[0], r[1], _fts_preprocess(r[2])) for r in rows],
                )
        tool_cnt = self._conn.execute("SELECT COUNT(*) FROM scratch_tools_fts").fetchone()[0]
        if tool_cnt == 0:
            rows = self._conn.execute(
                "SELECT session_id, id, result_preview, error FROM scratch_tools"
            ).fetchall()
            if rows:
                self._conn.executemany(
                    "INSERT INTO scratch_tools_fts (session_id, kv_key, content) VALUES (?, ?, ?)",
                    [(r[0], f"tool:{r[1]}", self._tool_content(r[2], r[3])) for r in rows],
                )

    @staticmethod
    def _tool_content(result_preview: str, error: str | None) -> str:
        return _fts_preprocess(f"{result_preview} {error or ''}".strip())

    def _sync_kv_fts(self, session_id: str, key: str, value: str) -> None:
        self._conn.execute(
            "DELETE FROM scratch_kv_fts WHERE session_id = ? AND kv_key = ?",
            (session_id, key),
        )
        self._conn.execute(
            "INSERT INTO scratch_kv_fts (session_id, kv_key, content) VALUES (?, ?, ?)",
            (session_id, key, _fts_preprocess(value)),
        )

    def _sync_tool_fts(self, session_id: str, tool_id: int) -> None:
        self._conn.execute(
            "DELETE FROM scratch_tools_fts WHERE session_id = ? AND kv_key = ?",
            (session_id, f"tool:{tool_id}"),
        )
        row = self._conn.execute(
            "SELECT result_preview, error FROM scratch_tools WHERE session_id = ? AND id = ?",
            (session_id, tool_id),
        ).fetchone()
        if row:
            self._conn.execute(
                "INSERT INTO scratch_tools_fts (session_id, kv_key, content) VALUES (?, ?, ?)",
                (session_id, f"tool:{tool_id}", self._tool_content(row[0], row[1])),
            )

    def _sync_session_tools_fts(self, session_id: str) -> None:
        """环形裁剪后按会话整体重建工具 FTS 索引（lastrowid 不可得的兜底）。"""
        self._conn.execute("DELETE FROM scratch_tools_fts WHERE session_id = ?", (session_id,))
        rows = self._conn.execute(
            "SELECT id, result_preview, error FROM scratch_tools WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        if rows:
            self._conn.executemany(
                "INSERT INTO scratch_tools_fts (session_id, kv_key, content) VALUES (?, ?, ?)",
                [(session_id, f"tool:{r[0]}", self._tool_content(r[1], r[2])) for r in rows],
            )

    def _prune_turn_rows(self) -> None:
        """功能：`turn:%` 草稿按会话保留最新 N 条（容量上限）。

        `turn:%` 被 TTL 显式豁免（草稿需跨轮可见），但原实现**没有任何容量上限**
        → 每轮一行、会话越久越大且永不回收。此处按会话裁剪，只留最新
        `_MAX_TURN_ROWS_PER_SESSION` 条（时间倒序，同秒用 rowid 兜底稳定）。
        """
        sessions = self._conn.execute(
            "SELECT DISTINCT session_id FROM scratch_kv WHERE key LIKE 'turn:%'"
        ).fetchall()
        for (session_id,) in sessions:
            self._conn.execute(
                "DELETE FROM scratch_kv WHERE session_id = ? AND key LIKE 'turn:%' AND key NOT IN ("
                "  SELECT key FROM scratch_kv WHERE session_id = ? AND key LIKE 'turn:%'"
                "  ORDER BY CAST(strftime('%s', created_at) AS INTEGER) DESC, rowid DESC LIMIT ?"
                ")",
                (session_id, session_id, _MAX_TURN_ROWS_PER_SESSION),
            )

    def _purge_expired(self) -> None:
        """TTL 惰性清理——读写路径顺带删除 age > ttl_seconds 的行（无后台线程）。
        strftime 返回 TEXT，必须 CAST 才能与数值比较（否则比较恒 False 不删）。
        FTS 索引同步清理孤儿行（JOIN 已保证过滤正确，清理仅为防冗余堆积）。"""
        self._conn.execute(
            "DELETE FROM scratch_kv WHERE (created_at IS NULL OR "
            "CAST(strftime('%s', created_at) AS INTEGER) "
            "< CAST(strftime('%s', 'now') AS INTEGER) - ?) "
            "AND key NOT LIKE 'turn:%'",
            (self._ttl,),
        )
        self._prune_turn_rows()
        self._conn.execute(
            "DELETE FROM scratch_tools WHERE created_at IS NULL OR "
            "CAST(strftime('%s', created_at) AS INTEGER) "
            "< CAST(strftime('%s', 'now') AS INTEGER) - ?",
            (self._ttl,),
        )
        self._conn.execute(
            "DELETE FROM scratch_kv_fts WHERE kv_key NOT IN (SELECT key FROM scratch_kv)"
        )
        self._conn.execute(
            "DELETE FROM scratch_tools_fts WHERE kv_key NOT IN "
            "(SELECT 'tool:' || id FROM scratch_tools)"
        )


__all__ = ["ScratchHit", "ScratchpadStore", "ToolInvocation"]
