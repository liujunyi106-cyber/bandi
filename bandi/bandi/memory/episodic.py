"""Episode = 一次用户/Agent 交互的经历摘要（区别于 long_term 的知识事实层）。

与 LeAgent episodic.py 的映射：record/note_recall best-effort/空 summary 跳过
语义一一对应；Milvus 向量检索以「n-gram LIKE 命中比评分」替代（ADR-33 无 RAG）；
重要性加权与时间衰减（LeAgent 仅存字段）在本 store 的 search 排序中真正生效：

    score = 命中相关度 × importance × 0.5 ** (age_days / half_life_days)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from bandi.memory.long_term import _fts_preprocess, _fts_simple_tokens, _simple_tokens
from bandi.utils.db import connect_app_db

logger = logging.getLogger(__name__)

_DEFAULT_HALF_LIFE_DAYS = 90.0
# 部分命中相关度线性映射下限（0.3~1.0）——保区分度，防长查询多 gram 场景分数近零
_MIN_HIT_RELEVANCE = 0.3
_MIN_TOKEN_LEN = 2
_MAX_TOKEN_LEN = 4
# 重排（候选集须覆盖 limit 同分级，200 对单次检索足够且避免全表 JOIN 放大）
_FTS_CANDIDATE_LIMIT = 200

# 功能：通用词（停用词）——单/双 token 查询命中「所有含该 gram 的条目」，
# 零区分度，召回即噪音（真机证据：20 轮普通对话中 5 条被「什么/回答/用户」
# 顶到 recall 2~4，喂进 consolidation 提名通道）。覆盖 tokenize_cn 产出的
# 2~4 gram 组合（同一句里「是什么」「怎么」这类衔接词）。
_QUERY_STOP_TERMS = frozenset(
    {
        "什么",
        "是什么",
        "是什",
        "为何",
        "为什么",
        "是为什么",
        "为什",
        "怎么",
        "是怎",
        "怎",
        "么样",
        "怎么样",
        "如何",
        "是如",
        "如",
        "这个",
        "是这个",
        "那个",
        "是那",
        "一个",
        "一些",
        "我们",
        "你们",
        "他们",
        "可以",
        "能否",
        "可以不",
        "请问",
        "请",
        "用户",
        "回答",
        "是回",
        "回答了",
        "记录",
        "是记",
        "现在",
        "一下",
        "告诉",
        "帮我",
        "我想",
        "需要",
        "知道",
        "看看",
        "看下",
        "东西",
        "时候",
        "有关",
        "关于",
        "多少",
        "是多",
        "几个",
        "是几",
    }
)
# 功能：命中占比门槛（部分命中过低视为噪音；默认口径宽松，参数可调）
_MIN_SEARCH_HIT_RATIO = 0.3
_MIN_SEARCH_HITS = 1


_GRAM_PUNCT_RE = re.compile(r"[^\w一-鿿]+")


def _effective_query_terms(terms: list[str]) -> list[str]:
    """功能：剔除查询 token 中的通用词——全部为通用词时返回空（调用方不检索）。

    gram 可能黏着标点（「什么？」「用户:」）——比对停用表前先剥标点，避免
    带标点的通用词漏网成有效词。剥离后仍有效（非纯通用）的 gram 原样保留，
    保持与索引口径一致。
    """
    filtered = [term for term in terms if _GRAM_PUNCT_RE.sub("", term) not in _QUERY_STOP_TERMS]
    return filtered or []


def hit_relevance(hit_count: int, total: int) -> float:
    """功能：命中相关度线性映射 [0.3, 1.0]——部分命中不再封顶 0.6（低区分度）
    或随查询 gram 数放大趋近于零；全中=1.0，零命中=0.0。"""
    if total <= 0 or hit_count <= 0:
        return 0.0
    ratio = min(1.0, hit_count / total)
    return _MIN_HIT_RELEVANCE + (1.0 - _MIN_HIT_RELEVANCE) * ratio


def tokenize_cn(query: str) -> list[str]:
    """中文 n-gram 切分：提取连续 2-4 字片段作为检索词（LIKE/FTS 中文分词不可靠）。

    仅当查询含 字符时切分；纯英文/数字返回空列表，由调用方用原串检索。
    """
    if not any("\u4e00" <= c <= "\u9fff" for c in query):
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    q = "".join(c for c in query if c.isalnum())
    for n in range(_MAX_TOKEN_LEN, _MIN_TOKEN_LEN - 1, -1):
        for i in range(0, max(1, len(q) - n + 1)):
            gram = q[i : i + n]
            if len(gram) < _MIN_TOKEN_LEN or gram in seen:
                continue
            if gram in query:
                seen.add(gram)
                tokens.append(gram)
    return tokens


def decay_factor(age_days: float, half_life_days: float) -> float:
    """指数半衰期衰减——每过 half_life_days 权重减半；未来时间不放大。"""
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / max(half_life_days, 1e-4))


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Episode:
    summary: str
    episode_id: str = ""
    session_id: str = ""
    project_id: str | None = None
    employee_id: str | None = None
    team_id: str | None = None
    user_input: str = ""
    tags: list[str] = field(default_factory=list)
    importance: float = 0.4
    recall_count: int = 0
    last_recalled_at: str | None = None
    created_at: str | None = None
    # 10.7 扩展：召回过的查询指纹（去重后 ≤ _MAX_RECALL_QUERIES）——consolidation
    # 第三维门槛（unique queries）数据源
    recall_queries: list[str] = field(default_factory=list)


# 每情节保留的查询指纹上限（防无界增长）
_MAX_RECALL_QUERIES = 20


_COLUMNS = (
    "episode_id, session_id, project_id, employee_id, team_id, summary, user_input, "
    "tags, importance, recall_count, last_recalled_at, created_at, recall_queries"
)


MAX_EPISODES_PER_SESSION = 500


class EpisodicStore:
    def __init__(
        self,
        db_path: str,
        half_life_days: float = _DEFAULT_HALF_LIFE_DAYS,
        max_per_session: int = MAX_EPISODES_PER_SESSION,
    ) -> None:
        self._half_life = max(half_life_days, 1e-4)
        self._max_per_session = max(1, int(max_per_session))
        self._lock = threading.Lock()
        self._conn = connect_app_db(db_path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS episodes (
                episode_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                project_id TEXT,
                employee_id TEXT,
                team_id TEXT,
                summary TEXT NOT NULL,
                user_input TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                importance REAL NOT NULL DEFAULT 0.4,
                recall_count INTEGER NOT NULL DEFAULT 0,
                last_recalled_at TEXT,
                created_at TEXT NOT NULL,
                recall_queries TEXT NOT NULL DEFAULT '[]'
            )"""
        )
        # 10.7 迁移：旧库补 recall_queries 列（幂等）
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(episodes)")}
        if "recall_queries" not in cols:
            self._conn.execute(
                "ALTER TABLE episodes ADD COLUMN recall_queries TEXT NOT NULL DEFAULT '[]'"
            )
        if "team_id" not in cols:
            self._conn.execute("ALTER TABLE episodes ADD COLUMN team_id TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_episodic_created ON episodes(created_at DESC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_episodes_session "
            "ON episodes(session_id, importance, created_at)"
        )
        # 幂等（INSERT OR REPLACE 以主表为源覆盖，升级旧库/重复打开均一致，含行更新）
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts "
            "USING fts5(episode_id UNINDEXED, summary)"
        )
        legacy_rows = self._conn.execute("SELECT rowid, summary FROM episodes").fetchall()
        self._conn.executemany(
            "INSERT OR REPLACE INTO episodes_fts (rowid, summary) VALUES (?, ?)",
            [(rid, _fts_preprocess(rs)) for rid, rs in legacy_rows],
        )
        self._conn.commit()

    # -- write ----------------------------------------------------------

    def record(self, episode: Episode) -> Episode:
        """持久化一条经历；空/纯空白 summary 拒绝写入（返回原样不带 id）。"""
        summary = (episode.summary or "").strip()
        if not summary:
            logger.debug("skipping_empty_episode")
            return episode
        stored = replace(
            episode,
            summary=summary,
            episode_id=episode.episode_id or uuid4().hex,
            tags=list(episode.tags),
            created_at=episode.created_at or _utc_now_iso(),
            # 与 None 错位会让 IS NULL 查询漏取
            project_id=(str(episode.project_id).strip() or None) if episode.project_id else None,
            employee_id=(str(episode.employee_id).strip() or None) if episode.employee_id else None,
            team_id=(str(episode.team_id).strip() or None) if episode.team_id else None,
        )
        with self._lock:
            # 主键 REPLACE = DELETE + INSERT（rowid 变更）——先清旧 rowid 的 FTS 行
            old = self._conn.execute(
                "SELECT rowid FROM episodes WHERE episode_id = ?", (stored.episode_id,)
            ).fetchone()
            if old is not None:
                self._conn.execute("DELETE FROM episodes_fts WHERE rowid = ?", (int(old[0]),))
            self._conn.execute(
                f"INSERT OR REPLACE INTO episodes ({_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored.episode_id,
                    stored.session_id,
                    stored.project_id,
                    stored.employee_id,
                    stored.team_id,
                    stored.summary,
                    stored.user_input,
                    json.dumps(stored.tags, ensure_ascii=False),
                    float(stored.importance),
                    int(stored.recall_count),
                    stored.last_recalled_at,
                    stored.created_at,
                    json.dumps(list(stored.recall_queries), ensure_ascii=False),
                ),
            )
            rowid = self._conn.execute(
                "SELECT rowid FROM episodes WHERE episode_id = ?", (stored.episode_id,)
            ).fetchone()[0]
            self._conn.execute(
                "INSERT OR REPLACE INTO episodes_fts (rowid, summary) VALUES (?, ?)",
                (int(rowid), _fts_preprocess(stored.summary)),
            )
            self._conn.commit()
        self._prune_session(stored.session_id)
        return stored

    def _prune_session(self, session_id: str) -> int:
        """功能：单会话超容量裁剪——按 importance ASC, created_at ASC 淘汰最旧，返回条数。

        空 session_id 视为「无会话全局桶」：与普通会话同上限独立裁剪（残余
        原实现对空 id 直接跳过，无会话情节可无限增长绕过容量约束）。
        """
        if self._max_per_session <= 0:
            return 0
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            excess = int(total) - self._max_per_session
            if excess <= 0:
                return 0
            rows = self._conn.execute(
                "SELECT rowid, episode_id FROM episodes WHERE session_id = ? "
                "ORDER BY importance ASC, created_at ASC LIMIT ?",
                (session_id, excess),
            ).fetchall()
            for row in rows:
                self._conn.execute("DELETE FROM episodes_fts WHERE rowid = ?", (int(row[0]),))
                self._conn.execute("DELETE FROM episodes WHERE episode_id = ?", (row[1],))
            self._conn.commit()
        return len(rows)

    # -- read -----------------------------------------------------------

    def get(self, episode_id: str) -> Episode | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
        return self._row_to_episode(row) if row is not None else None

    def list_recent(
        self,
        limit: int = 10,
        project_id: str | None = None,
        employee_id: str | None = None,
        team_id: str | None = None,
    ) -> list[Episode]:
        scope_clause, scope_args = self._scope_clause(project_id, employee_id, team_id=team_id)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM episodes WHERE 1=1{scope_clause} "
                "ORDER BY created_at DESC, episode_id DESC LIMIT ?",
                (*scope_args, max(1, limit)),
            ).fetchall()
        return [e for e in (self._row_to_episode(r) for r in rows) if e is not None]

    def list_scopes(self) -> list[tuple[str | None, str | None]]:
        """功能：列出当前库的 distinct 维度分区（含 (None, None) 全局分区）。

        维护路径按分区扫描——生产情节带 project/employee 维度，无维度调用
        只能命中 NULL 分区（对主数据流 no-op）。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT project_id, employee_id FROM episodes "
                "ORDER BY project_id, employee_id"
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def list_for_maintenance(
        self,
        *,
        project_id: str | None,
        employee_id: str | None,
        team_id: str | None = None,
        oldest_first: bool = True,
        limit: int = 1000,
    ) -> list[Episode]:
        """功能：维护专用分区扫描——按维度等值取一页，可选最旧/最新优先。

        `oldest_first=True` 供遗忘路径从最旧端扫描：只按最新窗口取数会让最该
        遗忘的最旧条目永远进不了扫描窗口。
        """
        scope_clause, scope_args = self._scope_clause(project_id, employee_id, team_id=team_id)
        direction = "ASC" if oldest_first else "DESC"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM episodes WHERE 1=1{scope_clause} "
                f"ORDER BY created_at {direction}, episode_id {direction} LIMIT ?",
                (*scope_args, max(1, limit)),
            ).fetchall()
        return [e for e in (self._row_to_episode(r) for r in rows) if e is not None]

    def search(
        self,
        query: str,
        limit: int = 5,
        project_id: str | None = None,
        employee_id: str | None = None,
        team_id: str | None = None,
        *,
        min_hit_ratio: float = _MIN_SEARCH_HIT_RATIO,
        min_hits: int = _MIN_SEARCH_HITS,
    ) -> list[tuple[Episode, float]]:
        """相关性检索并按 score 排序——score = 命中相关度 × importance × 时间衰减。

        主链 = FTS5 中文 2-gram AND（精确语义，替代 n-gram LIKE 全表扫描；与
        long_term 同源预处理）：中文查询逐 gram AND，无 走 sanitize 原词。
        仅 FTS 抛异常（MATCH 方言）或查询无可用 token 时回退 LIKE——空结果
        不回落（AND 空 = 真无重叠项，回落会带出部分命中噪音/覆盖幽灵）。

        通用词闸门：token 全为「什么/回答/用户」这类衔接词时不检索（零区分度，
        召回即 recall 噪音）；部分通用词从查询中剔除，混合查询仍可召回。
        命中门槛：命中 token 数与占比低于 `min_hits` / `min_hit_ratio` 的候选
        不得返回（防止长查询只靠少数 gram 搭桥）。
        """
        q = (query or "").strip()
        if not q:
            return []
        # 检索词口径 = 2-gram（与 episodes_fts 索引写入侧 `_fts_preprocess` 同源）：
        # 用 3/4-gram 会让长查询的 AND 必然落空（长 gram 不在索引里）。
        grams = [g for g in _fts_preprocess(q).split() if g]
        # 纯英文/符号查询无中文 gram——回退符号安全词表（原串含 `*`/`-`
        # 时子串匹配恒不命中，相关度被误判为 0）
        like_terms = grams or _simple_tokens(q) or [q]
        effective_terms = _effective_query_terms(like_terms)
        if not effective_terms:
            # 全通用词查询：零区分度，命中任何条目都是噪音
            return []
        like_terms = effective_terms
        scope_clause, scope_args = self._scope_clause(project_id, employee_id, team_id=team_id)
        or_clause = " OR ".join("summary LIKE ? ESCAPE '\\'" for _ in like_terms)
        like_params = [*([f"%{_escape_like(t)}%" for t in like_terms]), *scope_args]

        fts_rows: list[tuple] | None = None
        main_columns = ", ".join(f"e.{col}" for col in _COLUMNS.split(", "))
        with self._lock:
            fts_query = self._fts_query_for(q, like_terms)
            if fts_query:
                try:
                    fts_rows = self._conn.execute(
                        f"SELECT {main_columns} FROM episodes_fts f JOIN episodes e "
                        "ON f.rowid = e.rowid WHERE episodes_fts MATCH ?"
                        f"{scope_clause} ORDER BY rank LIMIT ?",
                        (fts_query, *scope_args, _FTS_CANDIDATE_LIMIT),
                    ).fetchall()
                except Exception as exc:  # noqa: BLE001 — FTS5 方言异常回退 LIKE 不炸
                    logger.warning("episodic_fts_fallback: %s", exc)
        if fts_rows is None:  # 未执行（无可用 token）或 MATCH 异常时兜底；[] 是合法空
            with self._lock:
                fts_rows = self._conn.execute(
                    f"SELECT {_COLUMNS} FROM episodes WHERE ({or_clause}){scope_clause}",
                    like_params,
                ).fetchall()

        now = datetime.now(UTC)
        scored: list[tuple[Episode, float]] = []
        for row in fts_rows:
            episode = self._row_to_episode(row)
            if episode is None:
                continue
            hit_count = sum(1 for t in like_terms if t in episode.summary)
            # 命中门槛：命中 token 数与占比不足 = 只靠少数 gram 搭桥的噪音
            if hit_count < max(0, int(min_hits)):
                continue
            if like_terms and hit_count / len(like_terms) < max(0.0, float(min_hit_ratio)):
                continue
            relevance = hit_relevance(hit_count, len(like_terms))
            age_days = self._age_days(episode.created_at, now)
            score = (
                relevance * max(episode.importance, 0.0) * decay_factor(age_days, self._half_life)
            )
            scored.append((episode, score))
        scored.sort(key=lambda pair: (pair[1], pair[0].episode_id), reverse=True)
        return scored[: max(1, limit)]

    # -- recall feedback --------------------------------------------------

    def note_recall(self, episode_id: str, query: str | None = None) -> None:
        try:
            with self._lock:
                # 只加计数，无法区分「同一问题反复召回」与「多问题共同召回」
                queries: list[str] = []
                row = self._conn.execute(
                    "SELECT recall_queries FROM episodes WHERE episode_id = ?",
                    (episode_id,),
                ).fetchone()
                if row is not None:
                    try:
                        queries = [str(q) for q in json.loads(row[0] or "[]")]
                    except (TypeError, ValueError):
                        queries = []
                    raw_query = str(query or "").strip().lower()
                    if raw_query:
                        fingerprint = hashlib.sha1(raw_query.encode("utf-8")).hexdigest()[:12]
                        if fingerprint not in queries:
                            queries = [*queries, fingerprint][-_MAX_RECALL_QUERIES:]
                cur = self._conn.execute(
                    "UPDATE episodes SET recall_count = recall_count + 1, "
                    "last_recalled_at = ?, recall_queries = ? WHERE episode_id = ?",
                    (_utc_now_iso(), json.dumps(queries, ensure_ascii=False), episode_id),
                )
                self._conn.commit()
            if cur.rowcount == 0:
                logger.debug("note_recall miss: %s", episode_id)
        except Exception as exc:  # noqa: BLE001 — 记录失败不阻断注入链
            logger.warning("episodic_note_recall_failed: %s", exc)

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])

    def delete(self, episode_id: str) -> bool:
        """功能：显式遗忘单条经历——主表行 + FTS 索引（rowid 键）双删。

        4.3：此前经历层只能等容量/衰减淘汰，错误或敏感经历无法主动清除。
        返回是否命中（未命中不报错，调用方据 returned 值如实回传）。
        """
        eid = (episode_id or "").strip()
        if not eid:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT rowid FROM episodes WHERE episode_id = ?", (eid,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM episodes_fts WHERE rowid = ?", (int(row[0]),))
            self._conn.execute("DELETE FROM episodes WHERE episode_id = ?", (eid,))
            self._conn.commit()
        return True

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- internals --------------------------------------------------------

    @staticmethod
    def _fts_query_for(query: str, like_terms: list[str] | None = None) -> str:
        """FTS5 MATCH 串构造：串切重叠 2-gram 以 AND 连接（_fts_preprocess
        与 long_term 同源，AND 精确语义防部分命中噪音）；无 时字母/空格保留
        sanitize 原词。无可用 token 返回空串（调用方跳过 FTS 走 LIKE 兜底）。

        `like_terms` 为通用词过滤后的实际检索词（已是 2-gram）——直接构 AND，
        避免用原 query 重切把被剔除的衔接词塞回 MATCH。
        """
        terms = list(like_terms) if like_terms else [g for g in _fts_preprocess(query).split() if g]
        if terms:
            return " AND ".join(f'"{t}"' for t in terms)
        # 非 token 化逐词引号 AND（-/* 运算符字符致语法错误/NOT 语义，须剔除）
        return _fts_simple_tokens(query)

    @staticmethod
    def _scope_clause(
        project_id: str | None,
        employee_id: str | None,
        team_id: str | None = None,
    ) -> tuple[str, list[Any]]:
        """维度过滤 SQL 片段（语义对齐 long_term）：传值等值过滤；未传补 IS NULL
        ——仅匹配该维度全局层，全局检索不命中维度行（防跨项目串写）。team_id
        同构：未传只命中非团队行，团队行必须带 team_id 显式查询。"""
        conditions: list[str] = []
        args: list[Any] = []
        if project_id is not None:
            conditions.append(" AND project_id = ?")
            args.append(project_id)
        else:
            conditions.append(" AND project_id IS NULL")
        if employee_id is not None:
            conditions.append(" AND employee_id = ?")
            args.append(employee_id)
        else:
            conditions.append(" AND employee_id IS NULL")
        if team_id is not None:
            conditions.append(" AND team_id = ?")
            args.append(team_id)
        else:
            conditions.append(" AND team_id IS NULL")
        return "".join(conditions), args

    @staticmethod
    def _age_days(created_at: str | None, now: datetime) -> float:
        if not created_at:
            return 0.0
        try:
            born = datetime.fromisoformat(created_at)
        except ValueError:
            return 0.0
        if born.tzinfo is None:
            born = born.replace(tzinfo=UTC)
        return max((now - born).total_seconds() / 86400.0, 0.0)

    @staticmethod
    def _row_to_episode(row: tuple) -> Episode | None:
        try:
            tags = json.loads(row[7]) if row[7] else []
            if not isinstance(tags, list):
                tags = []
        except (TypeError, ValueError):
            tags = []
        try:
            recall_queries = json.loads(row[12]) if row[12] else []
            if not isinstance(recall_queries, list):
                recall_queries = []
        except (TypeError, ValueError):
            recall_queries = []
        return Episode(
            episode_id=row[0],
            session_id=row[1],
            project_id=row[2],
            employee_id=row[3],
            team_id=row[4],
            summary=row[5],
            user_input=row[6],
            tags=[str(t) for t in tags],
            importance=float(row[8]),
            recall_count=int(row[9]),
            last_recalled_at=row[10],
            created_at=row[11],
            recall_queries=[str(q) for q in recall_queries],
        )


__all__ = [
    "Episode",
    "EpisodicStore",
    "decay_factor",
    "get_shared_episodic",
    "hit_relevance",
    "tokenize_cn",
]

_shared_episodic: EpisodicStore | None = None
_shared_episodic_lock = threading.Lock()


def get_shared_episodic() -> EpisodicStore:
    """功能：进程级共享 episodic 面（~/.bandi/data/episodic.db，镜像
    knowledge.get_shared_store 模式）——供 seat 级团队记忆摘要等旁路读消费；
    与 agent_runner 每 run 实例并存（SQLite 多连接 WAL 安全，本面只读）。"""
    global _shared_episodic
    with _shared_episodic_lock:
        if _shared_episodic is None:
            from pathlib import Path

            data_dir = Path.home() / ".bandi" / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            _shared_episodic = EpisodicStore(str(data_dir / "episodic.db"))
        return _shared_episodic
