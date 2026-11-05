# 功能：LongTermMemory — 长期记忆（SQLite 持久化 + 重要性排序）

from __future__ import annotations

import contextvars
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from bandi.utils.db import connect_app_db

logger = logging.getLogger(__name__)

# 0 值表示关闭（默认零行为变化，仅生产装配显式开启）
LONG_TERM_DECAY_HALF_LIFE_DAYS = 180.0

# 召回带来的 importance 回升（用进废退的「用进」侧）：持久化衰减是单向的，
# 只加 recall_count 的条目会一路沉到 floor，实际还在被读的事实应能回升。
# 上限 `_RECALL_IMPORTANCE_CEILING` 防「被反复召回」把冷门条目顶成高价值。
RECALL_IMPORTANCE_BOOST = 0.02
_RECALL_IMPORTANCE_CEILING = 0.6

# 超限拒绝整批而非裁剪）
BATCH_PARTITION_MAX_ITEMS = 300

RELIABILITY_DEFAULT = 0.5
RELIABILITY_HELPFUL_DELTA = 0.05
RELIABILITY_UNHELPFUL_DELTA = -0.10


def _as_float(value: object, default: float) -> float:
    """功能：宽松数值解析（None/坏值回退默认，不抛错）。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _norm_dim(value: object) -> str | None:
    """功能：维度归一——None 与空白字符串（strip 后为空）一律归一为 None（全局层）。

    写读边界统一语义：写入落 NULL，读入参 None/""/" " 等价；不新增其他语义。
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# `too many SQL variables`；该异常在上层被吞 → 合并/整理链路静默失效。
_SQL_VAR_CHUNK = 500


def _chunks(items: list[str], size: int = _SQL_VAR_CHUNK) -> Iterator[list[str]]:
    """功能：把 id 列表切成不超过 SQL 参数上限的批（同 session store 分批口径）。"""
    for i in range(0, len(items), size):
        yield items[i : i + size]


# 维度漏传计数——写入路径漏传 project/employee 会静默落全局层
_scope_strict_miss_counts: dict[str, int] = {"project_id": 0, "employee_id": 0}
_scope_strict_miss_lock = threading.Lock()

# 进入抑制上下文后不计漏传；ContextVar 只影响当前上下文，不跨线程污染
_scope_miss_suppressed: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "bandi_scope_miss_suppressed", default=False
)


@contextmanager
def suppress_scope_miss() -> Iterator[None]:
    """功能：抑制当前上下文的维度漏传计数（维护类写入专用，退出自动恢复）。"""
    token = _scope_miss_suppressed.set(True)
    try:
        yield
    finally:
        _scope_miss_suppressed.reset(token)


def scope_strict_misses() -> dict[str, int]:
    """功能：读取维度漏传计数快照（project_id/employee_id）。"""
    with _scope_strict_miss_lock:
        return dict(_scope_strict_miss_counts)


def reset_scope_strict_misses() -> None:
    """功能：重置维度漏传计数（测试隔离用）。"""
    with _scope_strict_miss_lock:
        for key in _scope_strict_miss_counts:
            _scope_strict_miss_counts[key] = 0


def _publish_scope_miss(name: str, amount: int) -> None:
    """功能：把一次维度漏传（或维护借还）同步到进程级观测注册表（出口）。"""
    from bandi.observability.counters import increment, scope_miss_counter_name

    increment(scope_miss_counter_name(name), amount)


def _apply_scope_miss_delta(name: str, delta: int) -> None:
    """功能：漏传计数统一增减入口——模块计数与进程级观测同步（维护借还共用）。"""
    with _scope_strict_miss_lock:
        _scope_strict_miss_counts[name] += delta
    _publish_scope_miss(name, delta)


def _record_scope_strict_miss(project_id: str | None, employee_id: str | None) -> None:
    """功能：记录一次漏传维度（计数 + 首个命中 warning，不阻断写入）。"""
    if _scope_miss_suppressed.get():
        return
    missing = [
        name
        for name, value in (("project_id", project_id), ("employee_id", employee_id))
        if not value
    ]
    if not missing:
        return
    first_hits: list[str] = []
    with _scope_strict_miss_lock:
        for name in missing:
            _scope_strict_miss_counts[name] += 1
            if _scope_strict_miss_counts[name] == 1:
                first_hits.append(name)
    for name in missing:
        _publish_scope_miss(name, 1)
    if first_hits:
        logger.warning(
            "long_term scope dims missing: %s (writes will land on the shared global layer; "
            "pass project_id/employee_id to isolate)",
            ", ".join(first_hits),
        )


# bigram 分词建索引/查询（否则整段 是一个 token，中文子串无法匹配）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def _fts_preprocess(text: str) -> str:
    """把连续 串切成重叠 2-gram，非 原样保留。"""

    def repl(match: re.Match) -> str:
        run = match.group(0)
        n = len(run)
        if n == 1:
            return run
        return " ".join(run[i : i + 2] for i in range(n - 1))

    return _CJK_RE.sub(repl, text or "")


def _simple_tokens(query: str) -> list[str]:
    """非 查询词表——按非字母数字切分（`-`/`*` 等符号剔除）。"""
    return [t for t in re.split(r"[^0-9A-Za-z_]+", query or "") if t]


def _fts_simple_tokens(query: str) -> str:
    """非 查询的 FTS5 MATCH 串——词表逐词加引号 AND。

    `-`/`*` 等 FTS5 运算符字符必须剔除：`-py` 被解析为 NOT、`*.py` 直接语法
    错误（查询失败静默回退/空召回，文件名与通配类查询恒失效）。"""
    return " AND ".join(f'"{t}"' for t in _simple_tokens(query))


class MemoryItem:
    def __init__(
        self,
        item_id: str,
        content: str,
        metadata: dict | None = None,
        timestamp: float | None = None,
        importance: float = 0.5,
    ):
        self.id = item_id
        self.content = content
        self.metadata = metadata or {}
        self.timestamp = timestamp or time.time()
        self.importance = max(0.0, min(1.0, importance))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
            "importance": self.importance,
        }

    @classmethod
    def from_dict(cls, d: dict) -> MemoryItem:
        meta = d.get("metadata", {})
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        return cls(
            item_id=d["id"],
            content=d["content"],
            metadata=meta,
            timestamp=d.get("timestamp", time.time()),
            importance=d.get("importance", 0.5),
        )


class LongTermMemory:
    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        decay_half_life_days: float = 0.0,
        decay_floor: float = 0.2,
    ):
        self._lock = threading.Lock()
        self._conn = connect_app_db(db_path)
        self._decay_half_life = float(decay_half_life_days)
        self._decay_floor = max(0.0, min(1.0, float(decay_floor)))
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS long_term_memory ("
                "  id TEXT PRIMARY KEY,"
                "  content TEXT NOT NULL,"
                "  metadata TEXT DEFAULT '{}',"
                "  timestamp REAL NOT NULL,"
                "  importance REAL DEFAULT 0.5,"
                "  project_id TEXT DEFAULT NULL,"
                "  employee_id TEXT DEFAULT NULL"
                ")"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_importance ON long_term_memory(importance DESC)"
            )
            # 旧库缺维度列时迁移（幂等）——必须先补列再建 scope 索引
            # 否则已有旧表 CREATE TABLE IF NOT 跳过时索引引用缺失列报错
            self._ensure_column("project_id", "TEXT DEFAULT NULL")
            self._ensure_column("employee_id", "TEXT DEFAULT NULL")
            # 功能：历史维度行归一（幂等）——旧版本把 ""/" " 落成维度值，读路径按
            # 读入参归一为 'p1' 后按等值查询永远命中不了（list_scopes 归一显示
            for column in ("project_id", "employee_id"):
                self._conn.execute(
                    f"UPDATE long_term_memory SET {column} = NULL "
                    f"WHERE {column} IS NOT NULL AND TRIM({column}) = ''"
                )
                self._conn.execute(
                    f"UPDATE long_term_memory SET {column} = NULLIF(TRIM({column}), '') "
                    f"WHERE {column} IS NOT NULL AND {column} <> TRIM({column})"
                )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scope ON long_term_memory(project_id, employee_id)"
            )
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS long_term_fts USING fts5(id UNINDEXED, content)"
            )
            self._conn.commit()

    def _ensure_column(self, name: str, decl: str) -> None:
        try:
            cols = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(long_term_memory)").fetchall()
            }
        except sqlite3.Error:
            return
        if name not in cols:
            self._conn.execute(f"ALTER TABLE long_term_memory ADD COLUMN {name} {decl}")

    def store(
        self,
        item: MemoryItem,
        project_id: str | None = None,
        employee_id: str | None = None,
        *,
        require_scope: bool = False,
    ) -> None:
        """写入长期记忆（INSERT OR REPLACE + FTS 镜像重建）。

        project_id/employee_id 先归一（空白字符串视同 None，落全局层）；
        require_scope=True 时两者归一后皆空即 ValueError——供「必须带作用域」
        的调用方显式选用（维护/整合晋升候选可为全局情节，默认不强制）。
        """
        project_id = _norm_dim(project_id)
        employee_id = _norm_dim(employee_id)
        if require_scope and project_id is None and employee_id is None:
            raise ValueError(
                "long_term.store(require_scope=True) requires project_id or employee_id; "
                "got both empty — pass the caller's scope or drop require_scope for the "
                "global layer"
            )
        # 写入路径漏传维度计数（不阻断写入；NULL 维度 = 共享全局层需可观测）
        _record_scope_strict_miss(project_id, employee_id)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO long_term_memory "
                "(id, content, metadata, timestamp, importance, project_id, employee_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.content,
                    json.dumps(item.metadata),
                    item.timestamp,
                    item.importance,
                    project_id,
                    employee_id,
                ),
            )
            # 同 id 重复 store 会追加重复索引行（搜索重复命中）；先按 id 清旧行再插
            self._conn.execute("DELETE FROM long_term_fts WHERE id = ?", (item.id,))
            self._conn.execute(
                "INSERT INTO long_term_fts (id, content) VALUES (?, ?)",
                (item.id, _fts_preprocess(item.content)),
            )
            self._conn.commit()

    def retrieve(
        self,
        query: str,
        limit: int = 5,
        project_id: str | None = None,
        employee_id: str | None = None,
    ) -> list[MemoryItem]:
        project_id = _norm_dim(project_id)
        employee_id = _norm_dim(employee_id)
        scope_clause, scope_args = self._scope_clause(project_id, employee_id)
        params = (
            f"%{self._escape_like(query)}%",
            f"%{self._escape_like(query)}%",
            *scope_args,
        )
        select = "SELECT id, content, metadata, timestamp, importance FROM long_term_memory "
        where = "(content LIKE ? ESCAPE '\\' OR metadata LIKE ? ESCAPE '\\')"
        with self._lock:
            if self._decay_half_life <= 0:
                rows = self._conn.execute(
                    f"{select}WHERE {where}{scope_clause} "
                    "ORDER BY importance DESC, timestamp DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()
            else:
                # 衰减开启：排序分含时间因子，SQL 排序无法表达——取匹配集后 Python 排序
                rows = self._conn.execute(
                    f"{select}WHERE {where}{scope_clause}",
                    params,
                ).fetchall()
        items = [
            MemoryItem.from_dict(
                {
                    "id": r[0],
                    "content": r[1],
                    "metadata": r[2],
                    "timestamp": r[3],
                    "importance": r[4],
                }
            )
            for r in rows
        ]
        if self._decay_half_life > 0:
            items.sort(
                key=lambda m: (
                    self._rank_score(m.importance, m.timestamp),
                    _as_float(m.timestamp, 0.0),
                ),
                reverse=True,
            )
            return items[: max(0, limit)]
        return items

    def search_fts(
        self,
        query: str,
        limit: int = 5,
        project_id: str | None = None,
        employee_id: str | None = None,
    ) -> list[MemoryItem]:
        project_id = _norm_dim(project_id)
        employee_id = _norm_dim(employee_id)
        scope_clause, scope_args = self._scope_clause(project_id, employee_id)
        # 无 走原 FTS 查询（保留英文短语/分词语义）
        has_cjk = bool(_CJK_RE.search(query or ""))
        if has_cjk:
            grams = [g for g in _fts_preprocess(query).split() if g]
            fts_query = " AND ".join(f'"{g}"' for g in grams) if grams else " "
        else:
            fts_query = self._sanitize_fts_query(query)
        rows = None
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT m.id, m.content, m.metadata, m.timestamp, m.importance, f.rank "
                    "FROM long_term_fts f JOIN long_term_memory m ON f.id = m.id "
                    "WHERE long_term_fts MATCH ?"
                    f"{scope_clause} "
                    "ORDER BY rank "
                    "LIMIT ?",
                    (fts_query, *scope_args, limit),
                ).fetchall()
            except Exception:  # noqa: BLE001
                rows = None  # 失败信号——在锁外走回退，避免可重入锁死锁
        # 回退必须在锁外执行（retrieve 会重新获取 _lock，非可重入锁会死锁）
        if rows is None:
            logger.warning("FTS5 search failed, falling back to non-FTS retrieval")
            return self.retrieve(query, limit, project_id=project_id, employee_id=employee_id)
        if not rows and has_cjk:
            return self.retrieve(query, limit, project_id=project_id, employee_id=employee_id)
        items = [
            MemoryItem.from_dict(
                {
                    "id": r[0],
                    "content": r[1],
                    "metadata": r[2],
                    "timestamp": r[3],
                    "importance": r[4],
                }
            )
            for r in rows
        ]
        # 使 rank 在融合评分中完全缺席（相关性维度失效、importance 被双重计权）。
        for item, row in zip(items, rows, strict=False):
            item.metadata["_fts_rank"] = row[5]
        return items

    @staticmethod
    def _scope_clause(
        project_id: str | None, employee_id: str | None, *, strict: bool = False
    ) -> tuple[str, list[str]]:
        """维度过滤 SQL 片段——project/employee 等值过滤；未传维度一律补
        `IS NULL`（仅匹配该维度全局层），全局检索不命中维度行。入参先归一
        （空白字符串视同未传）。

        ``strict=True`` 保留为显式读语义（唯一用途 = 测试/调用方的漏传观测计数
        见 契约）；生产写入的强校验走 `store(require_scope=True)`。
        """
        project_id = _norm_dim(project_id)
        employee_id = _norm_dim(employee_id)
        if strict:
            _record_scope_strict_miss(project_id, employee_id)
        conditions: list[str] = []
        args: list[str] = []
        if project_id:
            conditions.append("project_id = ?")
            args.append(project_id)
        else:
            conditions.append("project_id IS NULL")
        if employee_id:
            conditions.append("employee_id = ?")
            args.append(employee_id)
        else:
            conditions.append("employee_id IS NULL")
        return f" AND {' AND '.join(conditions)}", args

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        return _fts_simple_tokens(query) or " "

    def search_fts_ranked(
        self,
        query: str,
        limit: int = 5,
        project_id: str | None = None,
        employee_id: str | None = None,
    ) -> list[MemoryItem]:
        """项11：FTS5 候选召回 + 重要性排序——主记忆注入链（search_all）的无全表
        LIKE 扫描召回。先经 FTS5 索引召回候选（中文 2-gram），再按 importance DESC
        timestamp DESC 排序（与 retrieve 排序契约一致，保既有序）；FTS 空召回时
        回退 LIKE retrieve 保召回不丢数据。维度隔离与 retrieve/search_fts 一致。"""
        if not (query or "").strip():
            return []
        project_id = _norm_dim(project_id)
        employee_id = _norm_dim(employee_id)
        fts_hits = self.search_fts(query, limit, project_id=project_id, employee_id=employee_id)
        if not fts_hits:
            return self.retrieve(query, limit, project_id=project_id, employee_id=employee_id)
        ordered = sorted(
            (h for h in fts_hits),
            key=lambda m: (
                self._rank_score(m.importance, m.timestamp),
                _as_float(m.timestamp, 0.0),
            ),
            reverse=True,
        )
        return ordered[: max(1, limit)]

    def _rank_score(self, importance: float, timestamp: float) -> float:
        """功能：检索排序分——importance × 时间衰减（half_life<=0 时保持 importance）。"""
        if self._decay_half_life <= 0:
            return float(importance)
        factor = max(self._decay_floor, 0.5 ** (self._age_days(timestamp) / self._decay_half_life))
        return float(importance) * factor

    @staticmethod
    def _age_days(timestamp: float) -> float:
        """功能：记忆年龄（天）——未来/不可解析按 0（衰减因子 1.0，不放大）。"""
        try:
            value = float(timestamp)
        except (TypeError, ValueError):
            return 0.0
        age = (time.time() - value) / 86400.0
        return age if age > 0 else 0.0

    def check_conflict(self, item_id: str, content: str) -> bool:
        """功能：冲突标记——同 id 旧内容与待写内容不同即冲突（批准重放才允许覆盖）。"""
        existing = self.get(item_id)
        if existing is None:
            return False
        return existing.content != content

    def get(self, item_id: str) -> MemoryItem | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, content, metadata, timestamp, importance FROM long_term_memory WHERE id = ?",
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        return MemoryItem.from_dict(
            {
                "id": row[0],
                "content": row[1],
                "metadata": row[2],
                "timestamp": row[3],
                "importance": row[4],
            }
        )

    def list_items(
        self,
        *,
        project_id: str | None = None,
        employee_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[MemoryItem]:
        """功能：按 scope 只读列出条目（timestamp DESC，同刻按 id 升序保确定性）；
        offset 翻页供维护扫描遍历全量（单页 limit 不得截断合并扫描面）。"""
        project_id = _norm_dim(project_id)
        employee_id = _norm_dim(employee_id)
        scope_clause, scope_args = self._scope_clause(project_id, employee_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, content, metadata, timestamp, importance FROM long_term_memory "
                f"WHERE 1=1{scope_clause} "
                "ORDER BY timestamp DESC, id ASC LIMIT ? OFFSET ?",
                (*scope_args, max(0, int(limit)), max(0, int(offset))),
            ).fetchall()
        return [
            MemoryItem.from_dict(
                {
                    "id": row[0],
                    "content": row[1],
                    "metadata": row[2],
                    "timestamp": row[3],
                    "importance": row[4],
                }
            )
            for row in rows
        ]

    def scope_of(self, item_id: str) -> tuple[str | None, str | None] | None:
        """功能：读取条目所属 (project_id, employee_id)；条目不存在返回 None。
        输出维度归一（历史空白串行视为 None，与写读边界一致）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT project_id, employee_id FROM long_term_memory WHERE id = ?",
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        return _norm_dim(row[0]), _norm_dim(row[1])

    def list_scopes(self) -> list[tuple[str | None, str | None]]:
        """功能：列出库内 distinct 维度分区（含 (None, None) 全局分区），只读。
        输出维度归一后去重——空白串分区与全局分区合并（写读边界统一口径）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT project_id, employee_id FROM long_term_memory "
                "ORDER BY project_id, employee_id"
            ).fetchall()
        scopes: dict[tuple[str | None, str | None], None] = {}
        for row in rows:
            scopes.setdefault((_norm_dim(row[0]), _norm_dim(row[1])), None)
        return list(scopes)

    def delete(self, item_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM long_term_memory WHERE id = ?", (item_id,))
            self._conn.execute("DELETE FROM long_term_fts WHERE id = ?", (item_id,))
            self._conn.commit()

    def delete_scope(
        self,
        project_id: str | None,
        employee_id: str | None = None,
    ) -> int:
        """功能：按维度分区批量删除长期记忆（主表 + FTS 索引），返回删除条数。"""
        scope_clause, scope_args = self._scope_clause(project_id, employee_id)
        with self._lock:
            count = int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM long_term_memory WHERE 1=1{scope_clause}",
                    scope_args,
                ).fetchone()[0]
            )
            if not count:
                return 0
            self._conn.execute(
                "DELETE FROM long_term_fts WHERE id IN ("
                f"SELECT id FROM long_term_memory WHERE 1=1{scope_clause})",
                scope_args,
            )
            self._conn.execute(f"DELETE FROM long_term_memory WHERE 1=1{scope_clause}", scope_args)
            self._conn.commit()
        return count

    def apply_batch(
        self,
        *,
        upserts: list[MemoryItem],
        deletes: list[str],
        project_id: str | None = None,
        employee_id: str | None = None,
    ) -> None:
        """功能：批次原子写入——单连接事务内完成全部 upsert+delete；删除目标不
        存在、删除行不属于参数 scope、或分区净增后超过预算（BATCH_PARTITION_MAX_ITEMS）
        即 ValueError 整批回滚（不做裁剪）。维度入参先归一（空白串视同 None）。"""
        if not upserts and not deletes:
            return
        project_id = _norm_dim(project_id)
        employee_id = _norm_dim(employee_id)
        _record_scope_strict_miss(project_id, employee_id)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                before_counts = self._partition_counts(project_id, employee_id)
                if deletes:
                    self._ensure_deletes_exist(deletes, project_id, employee_id)
                for item in upserts:
                    self._upsert_row(item, project_id, employee_id)
                if deletes:
                    self._drop_rows(list(deletes))
                self._ensure_partition_budget(project_id, employee_id, before_counts)
            except BaseException:
                self._conn.rollback()
                raise
            self._conn.commit()

    def _ensure_deletes_exist(
        self, ids: list[str], project_id: str | None, employee_id: str | None
    ) -> None:
        """功能：删除目标必须全部存在且属于参数 scope，否则整批拒绝（全或无口径）。
        查询按 `_SQL_VAR_CHUNK` 分批（超 SQLite 变量上限会整批拒绝，与超限报错
        相比行为等价但不再炸）；行内维度归一后与归一后的参数 scope 比较。"""
        existing: dict[str, tuple[str | None, str | None]] = {}
        for chunk in _chunks(ids):
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT id, project_id, employee_id FROM long_term_memory "
                f"WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                existing[row[0]] = (_norm_dim(row[1]), _norm_dim(row[2]))
        missing = sorted(set(ids) - set(existing))
        if missing:
            raise ValueError(f"batch delete target not found: {missing}")
        foreign = sorted(
            item_id
            for item_id, scope in existing.items()
            if scope != (_norm_dim(project_id), _norm_dim(employee_id))
        )
        if foreign:
            raise ValueError(f"batch delete scope mismatch: {foreign}")

    def _partition_counts(self, project_id: str | None, employee_id: str | None) -> dict[str, int]:
        """功能：当前 scope 的 (kind → 条数) 快照（预算净增/净减判定基线）。"""
        scope_clause, scope_args = self._scope_clause(project_id, employee_id)
        rows = self._conn.execute(
            f"SELECT id FROM long_term_memory WHERE 1=1{scope_clause}", scope_args
        ).fetchall()
        counts: dict[str, int] = {}
        for (item_id,) in rows:
            kind = self._kind_of(item_id)
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    def _ensure_partition_budget(
        self, project_id: str | None, employee_id: str | None, before_counts: dict[str, int]
    ) -> None:
        """功能：最终态分区预算校验——仅净增超限拒绝（(scope, kind) 计数 > 上限且
        高于批次前基线）；净减/持平一律放行，已超预算分区仍可整理。"""
        counts = self._partition_counts(project_id, employee_id)
        over = {
            kind: count
            for kind, count in counts.items()
            if count > BATCH_PARTITION_MAX_ITEMS and count > before_counts.get(kind, 0)
        }
        if over:
            raise ValueError(f"batch partition budget exceeded: {over}")

    def _upsert_row(
        self, item: MemoryItem, project_id: str | None, employee_id: str | None
    ) -> None:
        """功能：主表 upsert + FTS 镜像重建（调用方必须已持锁且在事务内）。"""
        self._conn.execute(
            "INSERT OR REPLACE INTO long_term_memory "
            "(id, content, metadata, timestamp, importance, project_id, employee_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                item.id,
                item.content,
                json.dumps(item.metadata),
                item.timestamp,
                item.importance,
                project_id,
                employee_id,
            ),
        )
        self._conn.execute("DELETE FROM long_term_fts WHERE id = ?", (item.id,))
        self._conn.execute(
            "INSERT INTO long_term_fts (id, content) VALUES (?, ?)",
            (item.id, _fts_preprocess(item.content)),
        )

    @staticmethod
    def _parse_metadata(raw: object) -> dict:
        """功能：metadata 反序列化——缺失给空 dict；损坏/非 dict 抛 ValueError
        （拒绝覆盖未知数据，反馈端点据此映射 409）。"""
        if raw in (None, ""):
            return {}
        try:
            meta = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("corrupt long_term metadata") from exc
        if not isinstance(meta, dict):
            raise ValueError("corrupt long_term metadata (not a dict)")
        return meta

    def note_feedback(self, item_id: str, *, helpful: bool) -> bool:
        """功能：来源可靠性反馈（默认 0.5，helpful +0.05 / unhelpful −0.10
        clamp [0,1]）；条目不存在返回 False。BEGIN IMMEDIATE 事务内重读-改-写
        并发连接不丢更新。"""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT metadata FROM long_term_memory WHERE id = ?", (item_id,)
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    return False
                meta = self._parse_metadata(row[0])
                delta = RELIABILITY_HELPFUL_DELTA if helpful else RELIABILITY_UNHELPFUL_DELTA
                # 功能：坏 reliability 宽容策略——不可解析值回退中性 0.5
                # 单条脏数据不拒绝反馈；与 note_recalled 坏 metadata 静默跳过口径区分
                current = _as_float(meta.get("reliability"), RELIABILITY_DEFAULT)
                meta["reliability"] = round(max(0.0, min(1.0, current + delta)), 4)
                self._conn.execute(
                    "UPDATE long_term_memory SET metadata = ? WHERE id = ?",
                    (json.dumps(meta), item_id),
                )
            except BaseException:
                self._conn.rollback()
                raise
            self._conn.commit()
        return True

    def note_recalled(self, item_id: str) -> None:
        """功能：召回写回（recall_count+1、last_recalled_at=now）——复用批量单事务
        路径；条目不存在/损坏 metadata 时跳过（不覆盖、不抛出）。"""
        self.note_recalled_many([item_id])

    def note_recalled_many(self, item_ids: Iterable[str]) -> None:
        """功能：批量召回写回（recall_count+1、last_recalled_at=now）——单事务内
        逐条 UPDATE、一次 commit；重复 id 去重、负值 clamp ≥0、损坏 metadata 跳过。
        查询按 `_SQL_VAR_CHUNK` 分批（超 SQLite 变量上限会抛错，批量召回静默丢失）。

        SELECT 与 UPDATE 同处 `BEGIN IMMEDIATE` 写事务内（与 note_feedback
        同模式）——原 SELECT 在事务外，两实例同读旧值各自 +1 覆盖写丢更新。
        """
        unique_ids = list(dict.fromkeys(str(item_id) for item_id in item_ids if item_id))
        if not unique_ids:
            return
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                rows: list[tuple] = []
                for chunk in _chunks(unique_ids):
                    placeholders = ",".join("?" * len(chunk))
                    rows.extend(
                        self._conn.execute(
                            f"SELECT id, metadata, importance FROM long_term_memory "
                            f"WHERE id IN ({placeholders})",
                            chunk,
                        ).fetchall()
                    )
                moment = datetime.now(UTC).isoformat()
                updates: list[tuple[str, str, float]] = []
                for item_id, raw, current_importance in rows:
                    try:
                        meta = self._parse_metadata(raw)
                    except ValueError:
                        # 功能：旁路旁落不炸主链——坏 metadata 条目跳过召回写回
                        # 该条召回永不计入（观测缺口：warning 可定位，不阻断其余条目）
                        logger.warning("note_recalled skipped (corrupt metadata): %s", item_id)
                        continue
                    meta["recall_count"] = max(0, int(_as_float(meta.get("recall_count"), 0.0))) + 1
                    meta["last_recalled_at"] = moment
                    # 功能：召回回升 importance（用进废退的「用进」侧）——只加
                    # recall_count 的衰减是单向不可逆的，久未被用的条目会一路沉到
                    # floor，而实际还在被读的事实应能回升。回升封顶只限制低值
                    # 条目虚高，已有高值不得被降级。
                    current_value = max(0.0, float(current_importance or 0.0))
                    boosted = max(
                        current_value,
                        min(_RECALL_IMPORTANCE_CEILING, current_value + RECALL_IMPORTANCE_BOOST),
                    )
                    updates.append((json.dumps(meta), item_id, boosted))
                for metadata_json, item_id, importance in updates:
                    self._conn.execute(
                        "UPDATE long_term_memory SET metadata = ?, importance = ? WHERE id = ?",
                        (metadata_json, importance, item_id),
                    )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def prune(self, max_items: int = 1000) -> int:
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM long_term_memory").fetchone()[0]
            if count <= max_items:
                return 0
            excess = count - max_items
            self._conn.execute(
                "DELETE FROM long_term_fts WHERE id IN ("
                "  SELECT id FROM long_term_memory ORDER BY importance ASC, timestamp ASC LIMIT ?"
                ")",
                (excess,),
            )
            self._conn.execute(
                "DELETE FROM long_term_memory WHERE id IN ("
                "  SELECT id FROM long_term_memory ORDER BY importance ASC, timestamp ASC LIMIT ?"
                ")",
                (excess,),
            )
            self._conn.commit()
        return excess

    def prune_partitioned(
        self,
        max_items: int = 1000,
        per_partition_max: int | None = None,
    ) -> set[str]:
        """/按 (project_id, employee_id, kind) 分区独立裁剪到
        per_partition_max，再按全局 max_items 兜底——防止单个项目海量记忆淘汰
        其他项目高价值记忆；kind 分区让 turn/compaction 各自独立配额。

        分区内排序：importance DESC（高价值优先）→ last_recalled_at DESC
        （近期被召回的记忆优先保留，retention 语义）→ timestamp DESC；
        超过配额后的低价值成员裁剪。返回被裁剪条目 id 集合（审计记录）。
        per_partition_max 缺省回退为全局 max_items，保证不放大总量。
        """
        if per_partition_max is None:
            per_partition_max = max_items
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, importance, timestamp, project_id, employee_id, metadata "
                "FROM long_term_memory"
            ).fetchall()
            partitions: dict[
                tuple[str | None, str | None, str], list[tuple[str, float, str, str]]
            ] = {}
            for rid, imp, ts, pid, eid, meta in rows:
                kind = self._kind_of(rid)
                partitions.setdefault((pid, eid, kind), []).append(
                    (rid, float(imp), self._last_recalled(meta), ts)
                )
            pruned: set[str] = set()
            for members in partitions.values():
                # 降序排序：importance 高、被召回过（last_recalled 非空）
                # timestamp 新 排前保留；末尾（低价值）超过配额后裁剪
                members.sort(
                    key=lambda m: (
                        -m[1],
                        -(1 if m[2] else 0),
                        -float(m[3]) if m[3] is not None else 0.0,
                    )
                )
                for rid, _, _, _ in members[per_partition_max:]:
                    pruned.add(rid)
            if pruned:
                self._drop_rows(list(pruned))
            kept_count = len(rows) - len(pruned)
            if kept_count > max_items:
                backstop = self._conn.execute(
                    "SELECT id FROM long_term_memory "
                    "ORDER BY importance ASC, timestamp ASC LIMIT ?",
                    (kept_count - max_items,),
                ).fetchall()
                backstop_ids = [r[0] for r in backstop]
                self._drop_rows(backstop_ids)
                pruned.update(backstop_ids)
            self._conn.commit()
        return pruned

    @staticmethod
    def _kind_of(item_id: str) -> str:
        """功能：记忆种类维度（id 前缀推断）——turn/compaction 独立分区配额。"""
        if item_id.startswith("compaction:"):
            return "compaction"
        if item_id.startswith("turn:"):
            return "turn"
        return "other"

    @staticmethod
    def _last_recalled(metadata_json: str) -> str:
        """功能：从 metadata 提取 last_recalled_at（无/损坏返回空串——未召回先裁）。"""
        try:
            meta = json.loads(metadata_json)
            return str(meta.get("last_recalled_at") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""

    def _drop_rows(self, ids: list[str]) -> None:
        """批量删除主表与 FTS 表行（幂等，双删对齐 delete/prune 语义）。
        按 `_SQL_VAR_CHUNK` 分批——超 SQLite 变量上限抛错会被上层吞（合并/整理
        静默失效）；复用调用方事务/持锁，不在此提交。"""
        unique_ids = list(dict.fromkeys(str(item_id) for item_id in ids if item_id))
        if not unique_ids:
            return
        for chunk in _chunks(unique_ids):
            placeholders = ",".join("?" * len(chunk))
            self._conn.execute(f"DELETE FROM long_term_fts WHERE id IN ({placeholders})", chunk)
            self._conn.execute(f"DELETE FROM long_term_memory WHERE id IN ({placeholders})", chunk)

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM long_term_memory").fetchone()[0]

    def close(self) -> None:
        # 与兄弟类（episodic/working）一致：close 必须持锁——prefetch 经
        # asyncio.to_thread 并发检索时，主线程 close 撞上查询中的连接是原生崩溃。
        with self._lock:
            self._conn.close()
