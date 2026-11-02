# 功能：LLM 调用台账落库——把 CostTracker 的内存账本持久化（.2）
#
# 修复前：`CostTracker` 只有内存 `_entries`，进程重启即归零，无法回答
# 「上个会话花了多少 / 哪个员工最贵」——前端展示用量时无数据源。
#
# 而非 USD（与 CostTracker.total_cost_cny 同源）。
#
# 写入形态：有界队列 + 独立写线程——
# * 调用方在 LLM 响应热路径上，落库失败/变慢绝不能拖住主链；
# * `event_id` 唯一索引做幂等，事件重放不会重复记账。

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bandi.utils.db import connect_app_db, secure_db_file

logger = logging.getLogger(__name__)

# 队列容量（超出即丢弃并计数——观测设施不得反压主链）
DEFAULT_MAXSIZE = 10000

# close() 排空兜底时限（秒）
_FLUSH_TIMEOUT_SECONDS = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_request_log (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id             TEXT    NOT NULL UNIQUE,
    created_at           TEXT    NOT NULL,
    session_id           TEXT    NOT NULL DEFAULT '',
    provider_name        TEXT    NOT NULL DEFAULT '',
    model                TEXT    NOT NULL DEFAULT '',
    agent_role           TEXT    NOT NULL DEFAULT '',
    call_kind            TEXT    NOT NULL DEFAULT 'llm',
    input_tokens         INTEGER NOT NULL DEFAULT 0,
    output_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_tokens         INTEGER NOT NULL DEFAULT 0,
    total_cost_cny       REAL    NOT NULL DEFAULT 0.0,
    turn_id              INTEGER NOT NULL DEFAULT 0,
    attempt_id           INTEGER NOT NULL DEFAULT 0,
    trace_id             TEXT    NOT NULL DEFAULT '',
    status_code          INTEGER NOT NULL DEFAULT 0,
    error                TEXT    NOT NULL DEFAULT '',
    latency_ms           REAL    NOT NULL DEFAULT 0.0,
    breakdown            TEXT    NOT NULL DEFAULT '{}'
);
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_cost_session ON llm_request_log(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_cost_model ON llm_request_log(model)",
    "CREATE INDEX IF NOT EXISTS idx_cost_provider ON llm_request_log(provider_name)",
    "CREATE INDEX IF NOT EXISTS idx_cost_kind ON llm_request_log(call_kind)",
    "CREATE INDEX IF NOT EXISTS idx_cost_role ON llm_request_log(agent_role)",
    "CREATE INDEX IF NOT EXISTS idx_cost_created ON llm_request_log(created_at)",
)


@dataclass
class CostRecord:
    """一条 LLM 调用台账。字段名即列名，便于 asdict 直插。"""

    session_id: str = ""
    provider_name: str = ""
    model: str = ""
    agent_role: str = ""
    call_kind: str = "llm"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    total_cost_cny: float = 0.0
    turn_id: int = 0
    attempt_id: int = 0
    trace_id: str = ""
    status_code: int = 0
    error: str = ""
    latency_ms: float = 0.0
    breakdown: dict[str, Any] = field(default_factory=dict)
    event_id: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class CostStoreStats:
    enqueued: int = 0
    dropped: int = 0
    written: int = 0
    duplicated: int = 0


def _row_get(row: Any, key: str) -> Any:
    # 功能：兼容 sqlite3.Row 与 dict 两种行形态（契约单测用 dict，生产用 Row）
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _empty_bucket() -> dict[str, Any]:
    # 功能：空分桶（聚合路径与降级路径共用键名）
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_cny": 0.0,
        "unpriced_requests": 0,
    }


def _finalise_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    """功能：分桶收尾（命中率 + 金额精度）——**SQL 路径与降级路径共用**。

    命中率钳制 [0,1]：Anthropic 的 input_tokens 不含已缓存部分，跨协议口径若被误用会算出 >1。
    两条路径共用本函数是**防漂移手段**（否则 SQL 与纯函数会各写一遍收尾逻辑，迟早不一致）。
    """
    denom = bucket["input_tokens"]
    hit = bucket["cache_read_tokens"]
    bucket["cache_hit_rate"] = min(1.0, hit / denom) if denom > 0 else 0.0
    bucket["cost_cny"] = round(float(bucket["cost_cny"]), 6)
    return bucket


def _bucket_from_agg_row(row: Any) -> dict[str, Any]:
    # 功能：SQL 聚合行 → 分桶（列名与 _SQL_AGG_COUNTERS 对应）
    return {
        "requests": int(_row_get(row, "requests") or 0),
        "input_tokens": int(_row_get(row, "input_tokens") or 0),
        "output_tokens": int(_row_get(row, "output_tokens") or 0),
        "cache_read_tokens": int(_row_get(row, "cache_read_tokens") or 0),
        "cache_creation_tokens": int(_row_get(row, "cache_creation_tokens") or 0),
        "cost_cny": float(_row_get(row, "cost_cny") or 0.0),
        "unpriced_requests": int(_row_get(row, "unpriced_requests") or 0),
    }


# 功能：折叠合并时要累加的原始字段（不含派生量 cache_hit_rate —— 合并后重算）
_BUCKET_SUM_FIELDS = (
    "requests",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "unpriced_requests",
)


def fold_buckets_by(
    source: dict[str, dict[str, Any]],
    key_of: Callable[[str], str],
    *,
    default_key: str = "(unassigned)",
) -> dict[str, dict[str, Any]]:
    """功能：把「按会话」分桶按派生键（项目等）折叠合并——命中率合并后统一重算。

    为什么不在 SQL 里直接 GROUP BY 项目：台账只有 `session_id`，项目要从会话 id
    约定（`role-session::<项目>::…`）与**会话元数据**两处派生，属应用层知识；
    而按会话分桶本身就是 SQL 能给出的最小充分中间量（会话数远小于请求数）。
    """
    merged: dict[str, dict[str, Any]] = {}
    for name, bucket in source.items():
        key = key_of(name) or default_key
        target = merged.setdefault(key, _empty_bucket())
        for bucket_field in _BUCKET_SUM_FIELDS:
            target[bucket_field] += int(bucket.get(bucket_field) or 0)
        target["cost_cny"] += float(bucket.get("cost_cny") or 0.0)
    return {key: _finalise_bucket(value) for key, value in merged.items()}


# - 归一总输入：breakdown.input_tokens_total 缺失/为 0 时回落 input_tokens 列；
# - `json_valid` 守卫：坏 JSON 行不得让整条查询报错（旧库/手工改库容错）；
_SQL_AGG_COUNTERS = (
    "COUNT(*) AS requests,"
    " COALESCE(SUM(COALESCE(NULLIF(CASE WHEN json_valid(breakdown) THEN"
    " json_extract(breakdown, '$.input_tokens_total') END, 0), input_tokens)), 0) AS input_tokens,"
    " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
    " COALESCE(SUM(cache_tokens), 0) AS cache_read_tokens,"
    " COALESCE(SUM(COALESCE(CASE WHEN json_valid(breakdown) THEN"
    " json_extract(breakdown, '$.cache_creation_tokens') END, 0)), 0) AS cache_creation_tokens,"
    " COALESCE(SUM(total_cost_cny), 0) AS cost_cny,"
    " COALESCE(SUM(CASE WHEN json_valid(breakdown) AND"
    " json_extract(breakdown, '$.unpriced') THEN 1 ELSE 0 END), 0) AS unpriced_requests"
)


def _aggregate_rows(rows: list[Any]) -> dict[str, Any]:
    """功能：用量与缓存聚合（纯函数，无 DB 依赖）。

    命中率分母 = breakdown.input_tokens_total（provider 归一后的**总输入**）；
    缺失（旧数据 / 未接线的 provider）时回落 input_tokens 列。
    ⚠️ 必须用归一总量：Anthropic 行的 input_tokens **不含**已缓存部分
    直接作分母会算出 >100% 的命中率（故再钳制 [0,1] 兜底）。
    """

    total = _empty_bucket()
    by_model: dict[str, dict[str, Any]] = {}
    by_role: dict[str, dict[str, Any]] = {}
    by_session: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw = _row_get(row, "breakdown")
        try:
            breakdown = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (TypeError, ValueError):
            breakdown = {}
        if not isinstance(breakdown, dict):
            breakdown = {}
        input_tokens = int(_row_get(row, "input_tokens") or 0)
        normalised_total = int(breakdown.get("input_tokens_total") or 0) or input_tokens
        model = str(_row_get(row, "model") or "unknown")
        role = str(_row_get(row, "agent_role") or "unassigned")
        session = str(_row_get(row, "session_id") or "unknown")
        for bucket in (
            total,
            by_model.setdefault(model, _empty_bucket()),
            by_role.setdefault(role, _empty_bucket()),
            by_session.setdefault(session, _empty_bucket()),
        ):
            bucket["requests"] += 1
            bucket["input_tokens"] += normalised_total
            bucket["output_tokens"] += int(_row_get(row, "output_tokens") or 0)
            bucket["cache_read_tokens"] += int(_row_get(row, "cache_tokens") or 0)
            bucket["cache_creation_tokens"] += int(breakdown.get("cache_creation_tokens") or 0)
            bucket["cost_cny"] += float(_row_get(row, "total_cost_cny") or 0.0)
            if breakdown.get("unpriced"):
                bucket["unpriced_requests"] += 1
    for bucket in (total, *by_model.values(), *by_role.values(), *by_session.values()):
        _finalise_bucket(bucket)
    return {"total": total, "by_model": by_model, "by_role": by_role, "by_session": by_session}


def build_event_id(
    session_id: str,
    timestamp: float,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> str:
    """构造确定性事件 id——同一次调用的重放得到同一个 id，由唯一索引拦下。"""
    import hashlib

    raw = f"{session_id}|{timestamp:.6f}|{model}|{prompt_tokens}|{completion_tokens}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class CostStore:
    """LLM 台账的 SQLite 存储（WAL）+ 有界队列写入。"""

    def __init__(self, db_path: str | Path, *, maxsize: int = DEFAULT_MAXSIZE) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect_app_db(self._db_path, row_factory=True)
        self._conn.execute(_SCHEMA)
        for stmt in _INDEXES:
            self._conn.execute(stmt)
        self._conn.commit()
        secure_db_file(self._db_path)

        self._queue: queue.Queue[CostRecord | None] = queue.Queue(maxsize=max(1, int(maxsize)))
        self._lock = threading.Lock()
        self._stats = CostStoreStats()
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._consume_loop, name="bandi-cost-store", daemon=True
        )
        self._thread.start()

    # ---------- 写入 ----------

    def enqueue(self, record: CostRecord) -> None:
        """非阻塞入队。队满即丢弃并计数——绝不阻塞调用方。"""
        if self._stopped.is_set():
            # 停止后入队计入 dropped（统计口径：enqueued+dropped=收到条数）
            self._bump("dropped")
            return
        if not record.event_id:
            record.event_id = build_event_id(
                record.session_id,
                time.time(),
                record.model,
                record.input_tokens,
                record.output_tokens,
            )
        if not record.created_at:
            record.created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._bump("dropped")
            return
        self._bump("enqueued")

    def flush(self, timeout: float = _FLUSH_TIMEOUT_SECONDS) -> None:
        """等待**持久化屏障**（不停止接收）：队列空 **且** 无在途未提交条目。

        此前只等 `queue.empty()`——消费者已 get 未 commit 的窗口内会提前返回
        调用方误以为账已落库。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty() and self._queue.unfinished_tasks == 0:
                return
            time.sleep(0.01)

    def close(self) -> None:
        """停止接收 → 排空 → 关连接。可重复调用。"""
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._push_sentinel()
        if self._thread.is_alive():
            self._thread.join(timeout=_FLUSH_TIMEOUT_SECONDS)
        self._flush_pending_sync()
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 — 关闭失败不影响进程退出
            pass

    def stats(self) -> CostStoreStats:
        with self._lock:
            return CostStoreStats(
                enqueued=self._stats.enqueued,
                dropped=self._stats.dropped,
                written=self._stats.written,
                duplicated=self._stats.duplicated,
            )

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ---------- 查询 ----------

    def list_records(self, *, limit: int = 100, session_id: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM llm_request_log"
        params: tuple[Any, ...] = ()
        if session_id:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self._conn.execute(sql, (*params, limit)).fetchall()
        return [dict(r) for r in rows]

    def session_cost(self, session_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS calls,"
            " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
            " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
            " COALESCE(SUM(total_cost_cny), 0.0) AS total_cost_cny"
            " FROM llm_request_log WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row is not None else {}

    def session_usage_detail(
        self, session_id: str, *, family_prefix: str = ""
    ) -> dict[str, Any] | None:
        """功能：单会话用量（**落库口径**，跨进程重启可读）+ 按模型明细；查库失败返回 None。

        为什么需要它：内存 CostTracker 随进程重启归零，而右栏概览要显示的正是
        「这个会话用了多少」——重启后显示 0 是错的（台账里明明有）。故读面以台账为准
        内存仅作明细补充（per-tool 归因未落库）。

        family_prefix 非空时聚合「会话家族」：本会话 + 前缀匹配的成员会话
        （团队群聊自身不产生 LLM 调用，员工执行 token 记在 role-session 名下——
        传 `role-session::{project_id}::%` 让群聊概览如实显示含员工执行的总用量）。
        """
        try:
            if family_prefix:
                where = "WHERE session_id = ? OR session_id LIKE ?"
                args: tuple[Any, ...] = (session_id, family_prefix)
            else:
                where = "WHERE session_id = ?"
                args = (session_id,)
            totals_row = self._conn.execute(
                "SELECT COUNT(*) AS calls,"
                " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
                " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
                " COALESCE(SUM(total_cost_cny), 0.0) AS total_cost_cny"
                f" FROM llm_request_log {where}",
                args,
            ).fetchone()
            totals = dict(totals_row) if totals_row is not None else {}
            rows = self._conn.execute(
                "SELECT COALESCE(NULLIF(model, ''), 'unknown') AS model,"
                " COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,"
                " COALESCE(SUM(total_cost_cny), 0.0) AS cost"
                f" FROM llm_request_log {where} GROUP BY model",
                args,
            ).fetchall()
        except Exception as exc:  # noqa: BLE001 — 观测面不可用不得阻断面板
            logger.warning("session usage detail failed for %s: %s", session_id, exc)
            return None
        return {
            "total_tokens": int(totals.get("input_tokens") or 0)
            + int(totals.get("output_tokens") or 0),
            "total_cost_cny": round(float(totals.get("total_cost_cny") or 0.0), 6),
            "per_model": {
                str(r["model"]): {"tokens": int(r["tokens"] or 0), "cost": float(r["cost"] or 0.0)}
                for r in rows
            },
        }

    def cost_by_role(self) -> dict[str, dict[str, float]]:
        rows = self._conn.execute(
            "SELECT COALESCE(NULLIF(agent_role, ''), '(unassigned)') AS role,"
            " COUNT(*) AS calls,"
            " COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,"
            " COALESCE(SUM(total_cost_cny), 0.0) AS cost"
            " FROM llm_request_log GROUP BY role ORDER BY cost DESC"
        ).fetchall()
        return {
            str(r["role"]): {
                "calls": float(r["calls"]),
                "tokens": float(r["tokens"]),
                "cost": float(r["cost"]),
            }
            for r in rows
        }

    # ---------- 内部 ----------

    def aggregate_usage(self, *, days: int = 0) -> dict[str, Any]:
        """功能：用量与缓存聚合（供设置页 / 观测面消费）。

        days<=0 = 全部；否则回溯 days 天（created_at 为 'YYYY-MM-DD HH:MM:SS'
        文本，字典序即时间序，可直接比较）。

        实现（修）：**SQL 聚合优先**（GROUP BY，只回 N 行）——原实现
        `fetchall()` 把整张台账载入内存，台账长期增长后会成为可观的内存与延迟开销。
        任何 SQL 侧异常（如极端环境缺 JSON1）则**退化**为逐行扫描 + 纯函数
        `_aggregate_rows`：观测面出错绝不影响主链，也绝不让前端拿到 500。
        """
        where = ""
        args: tuple[Any, ...] = ()
        if days > 0:
            cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
            where, args = " WHERE created_at >= ?", (cutoff,)
        try:
            return self._aggregate_usage_sql(where, args)
        except Exception as exc:  # noqa: BLE001 — 观测面退化，绝不影响主链
            logger.warning("aggregate_usage sql path failed, falling back to row scan: %s", exc)
            try:
                rows = self._conn.execute(
                    "SELECT model, agent_role, session_id, input_tokens, output_tokens,"
                    " cache_tokens, total_cost_cny, breakdown FROM llm_request_log" + where,
                    args,
                ).fetchall()
            except Exception as exc2:  # noqa: BLE001
                logger.warning("aggregate_usage query failed: %s", exc2)
                return _aggregate_rows([])
            return _aggregate_rows(list(rows))

    def _aggregate_usage_sql(self, where: str, args: tuple[Any, ...]) -> dict[str, Any]:
        # 功能：SQL 聚合（4 条查询：总计 / 按模型 / 按角色 / 按会话）——空值分组名与纯函数路径一致
        total_row = self._conn.execute(
            f"SELECT {_SQL_AGG_COUNTERS} FROM llm_request_log{where}", args
        ).fetchone()
        total = _finalise_bucket(
            _bucket_from_agg_row(total_row) if total_row is not None else _empty_bucket()
        )
        return {
            "total": total,
            "by_model": self._agg_grouped("COALESCE(NULLIF(model, ''), 'unknown')", where, args),
            "by_role": self._agg_grouped(
                "COALESCE(NULLIF(agent_role, ''), 'unassigned')", where, args
            ),
            "by_session": self._agg_grouped(
                "COALESCE(NULLIF(session_id, ''), 'unknown')", where, args
            ),
        }

    def _agg_grouped(
        self, group_expr: str, where: str, args: tuple[Any, ...]
    ) -> dict[str, dict[str, Any]]:
        # 功能：单维度分组聚合（分组表达式由调用方给常量，无用户输入拼接）
        sql = (
            f"SELECT {group_expr} AS grp, {_SQL_AGG_COUNTERS}"
            f" FROM llm_request_log{where} GROUP BY grp"
        )
        return {
            str(_row_get(row, "grp")): _finalise_bucket(_bucket_from_agg_row(row))
            for row in self._conn.execute(sql, args)
        }

    def _bump(self, field: str, amount: int = 1) -> None:
        with self._lock:
            self._stats = CostStoreStats(
                enqueued=self._stats.enqueued + (amount if field == "enqueued" else 0),
                dropped=self._stats.dropped + (amount if field == "dropped" else 0),
                written=self._stats.written + (amount if field == "written" else 0),
                duplicated=self._stats.duplicated + (amount if field == "duplicated" else 0),
            )

    def _push_sentinel(self) -> None:
        deadline = time.monotonic() + _FLUSH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                self._queue.put_nowait(None)
                return
            except queue.Full:
                if not self._thread.is_alive():
                    return
                time.sleep(0.005)

    def _consume_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stopped.is_set():
                    return
                continue
            if item is None:
                self._queue.task_done()
                return
            self._insert(item)
            self._queue.task_done()

    def _flush_pending_sync(self) -> None:
        """兜底：消费线程若超时未退，主线程把残余条目同步写完，避免丢账。"""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is None:
                self._queue.task_done()
                continue
            self._insert(item)
            self._queue.task_done()

    def _insert(self, record: CostRecord) -> None:
        payload = asdict(record)
        breakdown = payload.pop("breakdown", {}) or {}
        payload["breakdown"] = json.dumps(breakdown, ensure_ascii=False, default=str)
        payload.pop("event_id", None)

        columns = ["event_id", "created_at", *payload.keys()]
        values = [record.event_id, record.created_at, *payload.values()]
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT OR IGNORE INTO llm_request_log ({', '.join(columns)}) VALUES ({placeholders})"
        )
        try:
            cursor = self._conn.execute(sql, values)
            self._conn.commit()
            if cursor.rowcount == 0:
                self._bump("duplicated")
            else:
                self._bump("written")
        except Exception:  # noqa: BLE001 — 落库失败绝不影响主链；计入 dropped 以便暴露
            self._bump("dropped")


# --------------------------------------------------------------------------
# 进程级共享实例
#
# 按 db 路径 keyed（而非单一全局变量）：测试里 Path.home 被 patch 成 tmp 时各自
# 拿到独立实例，不会出现「首次创建绑定到临时路径、后续复用已删除文件」的串扰
# --------------------------------------------------------------------------

_SHARED_STORES: dict[str, CostStore] = {}
_SHARED_LOCK = threading.Lock()
_ATEXIT_REGISTERED = False


def get_shared_cost_store(db_path: str | Path, *, maxsize: int = DEFAULT_MAXSIZE) -> CostStore:
    """获取（或创建）指定路径的共享台账实例。"""
    global _ATEXIT_REGISTERED

    key = str(db_path)
    with _SHARED_LOCK:
        store = _SHARED_STORES.get(key)
        if store is None:
            store = CostStore(key, maxsize=maxsize)
            _SHARED_STORES[key] = store
            if not _ATEXIT_REGISTERED:
                import atexit

                atexit.register(reset_shared_cost_stores)
                _ATEXIT_REGISTERED = True
        return store


def reset_shared_cost_stores() -> None:
    """关闭并清空共享实例。测试 fixture 与进程退出（atexit）共用。

    关闭会先排空队列——这是「fire-and-forget 不丢账」的兜底：进程正常退出时
    仍会把在途条目落库（风险 的处置）。
    """
    with _SHARED_LOCK:
        stores = list(_SHARED_STORES.values())
        _SHARED_STORES.clear()
    for store in stores:
        try:
            store.close()
        except Exception:  # noqa: BLE001 — 退出路径不应因单个 store 失败而中断
            pass
