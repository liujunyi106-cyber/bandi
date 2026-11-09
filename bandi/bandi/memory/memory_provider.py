# 功能：记忆 Provider（MemoryHit 存取 + 记忆管理）
from __future__ import annotations

import logging
import math
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bandi.memory.enricher import MemoryEnricher
from bandi.memory.long_term import LongTermMemory
from bandi.memory.markdown_memory import MarkdownMemoryStore
from bandi.memory.routing import route_bias

# 功能：工作层锁冲突后的熔断窗口——锁等待按busy_timeout 计（5s/次），多候选词
# 相乘会吃光整轮预算，故锁冲突后短暂跳过工作层，只用其余分类召回。
_WORKING_LOCK_BACKOFF_SEC = 30.0

logger = logging.getLogger(__name__)

_CATEGORY_PRIORITY = {"project_md": 4, "long_term": 3, "working": 2, "episodic": 1}

# 注入实质正文（而非仅「标题 — 钩子」指针），大段记忆由 budget 裁剪约束总量
_PROJECT_MD_SNIPPET_CHARS = 300


def _unit_score(value: float) -> float:
    """跨分类 score 归一化到统一 [0,1] 无量纲（项10 审查补齐）——各分类原始
    score 量纲不同（long_term/episodic 乘未限幅 importance 可 >1，working/
    project_md 恒为 relevance∈[0,1]）。跨分类排序前限幅到 [0,1]，杜绝某一分类
    因自身量纲上限高而系统性压过其余分类，使 score 可比（同量纲下 relevance/
    importance/recency 融合才有意义）。"""
    return max(0.0, min(1.0, value))


_FUSION_W_REL = 0.45
_FUSION_W_IMP = 0.2
_FUSION_W_RECENCY = 0.25
_FUSION_W_RECALL = 0.05
_FUSION_W_INTENT = 0.05
_RECENCY_HALF_LIFE_DAYS = 30.0
# 有差异时才影响排序
_FUSION_RELIABILITY_BASE = 0.85
_FUSION_RELIABILITY_WEIGHT = 0.15
_RELIABILITY_NEUTRAL = 0.5

# 一句话索引（description）命中时的相关性加成——索引是判断相关性用的，
# 索引命中说明这条记忆确实切题；未命中不惩罚（老数据无索引字段）。
_DESCRIPTION_BOOST = 1.25

# plan→规划知识类——IntentRouter 现行词表为 plan/execute，plan 命中知识类偏置）
_INTENT_CATEGORY_BIAS: dict[str, set[str]] = {
    "factual": {"long_term", "project_md"},
    "procedural": {"working"},
    "conversational": {"episodic"},
    "plan": {"project_md", "long_term"},
}


def _intent_bias(category: str, intent_hint: str | None) -> float:
    """功能：intent 权重——命中偏好分类 +0.05（fusion 排序用），无 hint 零加分。"""
    if not intent_hint:
        return 0.0
    prefer = _INTENT_CATEGORY_BIAS.get(intent_hint.strip().lower())
    if prefer is None:
        return 0.0
    return _FUSION_W_INTENT if category in prefer else 0.0


def _iso_timestamp(ts: Any) -> str | None:
    """功能：long_term 的 epoch 秒时间戳 → ISO 字符串。

    long_term 存的是 time.time() 浮点；直接透出会让 _recency_factor 的
    fromisoformat 抛 TypeError 并回退 0.5 中性值，使 long_term 的时间维度失效。
    """
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(ts)


def _fts_relevance(rank: Any) -> float:
    """功能：FTS5 bm25 排序值 → [0,1] 相关度（bm25 为负值，越负越相关）。

    让「相关度」与「重要性」在融合评分中各自独立——此前 score 直接取
    importance，致使 rel 与 imp 两项对 long_term 是同一信号（双重计权
    相关性维度失效）。
    """
    try:
        magnitude = max(0.0, -float(rank))
    except (TypeError, ValueError):
        return 0.0
    return magnitude / (1.0 + magnitude)


def _long_term_score(item: Any) -> float:
    """功能：long_term 注入链相关度——FTS5 bm25 优先；无 rank（LIKE 回退/无
    metadata 提供方）时回退 importance，避免相关度归零使排序退化。"""
    metadata = getattr(item, "metadata", None) or {}
    rank = metadata.get("_fts_rank") if isinstance(metadata, dict) else None
    if rank is None:
        try:
            return max(float(getattr(item, "importance", 0.0) or 0.0), 0.0)
        except (TypeError, ValueError):
            return 0.0
    return _fts_relevance(rank)


def _reliability_of(meta: dict[str, Any]) -> float:
    """功能：命中 metadata 的 reliability（缺省 0.5 中性；坏值回退中性，clamp [0,1]）。"""
    raw = meta.get("reliability")
    if raw is None:
        return _RELIABILITY_NEUTRAL
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return _RELIABILITY_NEUTRAL


def _long_term_metadata(item: Any) -> dict[str, Any]:
    """功能：long_term 命中透出可靠性/召回元数据（缺失键不写，走中性缺省）。"""
    meta = getattr(item, "metadata", None)
    if not isinstance(meta, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("reliability", "recall_count", "last_recalled_at"):
        if meta.get(key) is not None:
            out[key] = meta[key]
    return out


def _recency_factor(ts: str | None, now: datetime | None = None) -> float:
    """功能：时间衰减因子（exp(-age_days/半衰期)）——缺时间/解析失败给 0.5 中性
    值，不得降级为 0（无时间戳记忆不应被彻底压底）。"""
    if not ts:
        return 0.5
    try:
        born = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return 0.5
    if born.tzinfo is None:
        born = born.replace(tzinfo=UTC)
    age_days = ((now or datetime.now(UTC)) - born).total_seconds() / 86400.0
    return math.exp(-max(0.0, age_days) / _RECENCY_HALF_LIFE_DAYS)


def _query_tokens(query: str) -> list[str]:
    """功能：查询 → 可比对词元（中文 2-gram + 英文词），供索引命中判断。"""
    from bandi.memory.episodic import tokenize_cn

    text = str(query or "").strip().lower()
    if not text:
        return []
    return [t for t in (tokenize_cn(text) or [text]) if len(t) >= 2]


def _description_boost(meta: dict[str, Any], query: str) -> float:
    """功能：一句话索引命中加成——description 是「相关性判断」专用索引。

    参考实现 `findRelevantMemories` 的做法（`description` 字段的原文注释：
    "used to decide relevance in future conversations"）：先用索引判断这条记忆
    与当前问题相不相关，再决定要不要读全文。索引命中即给相关性加权，索引缺失
    或不命中保持 1.0（不惩罚老数据）。"""
    desc = str(meta.get("description") or "").strip()
    if not desc or not query:
        return 1.0
    if not any(t in desc.lower() for t in _query_tokens(query)):
        return 1.0
    return _DESCRIPTION_BOOST


def _fusion_score(meta: dict[str, Any], intent_bias: float = 0.0) -> float:
    """
    [0,1] 无量纲可比；缺字段走中性值（0.5/0）不抛错不扭曲排序。

    来源可靠性乘子作用于**最终分** (base + intent_bias)——缺省 0.5 时
    全体 ×0.925 真正均匀（无偏置时排序不变）；乘子放在偏置内才能保证
    intent 偏置不会破坏「缺省即均匀缩放」的口径。
    """
    rel = _unit_score(float(str(meta.get("score") or 0) or 0))
    imp_raw = meta.get("importance")
    imp = _unit_score(float(str(imp_raw) or 0)) if imp_raw is not None else 0.5
    recency = _recency_factor(
        meta.get("timestamp") or meta.get("last_recalled_at") or meta.get("created_at")
    )
    recall = min(float(str(meta.get("recall_count") or 0) or 0) / 10.0, 1.0)
    base = (
        _FUSION_W_REL * rel
        + _FUSION_W_IMP * imp
        + _FUSION_W_RECENCY * recency
        + _FUSION_W_RECALL * recall
    )
    factor = _FUSION_RELIABILITY_BASE + _FUSION_RELIABILITY_WEIGHT * _reliability_of(meta)
    return (base + float(intent_bias)) * factor


def _apply_category_floor(hits: list[MemoryHit], limit: int) -> list[MemoryHit]:
    """审查项3：per-category 公平保底——纯 score 排序下高 importance 分类可能
    占满 top-k，其他有命中的分类被整体挤出。保底：每个有命中的分类至少 1 条进
    top-k（前提：有命中分类数 ≤ limit；limit 本身仍是硬上限）。

    实现：先按 score 排序收敛 top-limit（契约），再把被挤出的分类中 score
    最高的一条，替换掉 top 内「同分类冗余且 score 最低」的条目——只动多余条目
    不改变唯一代表条目的相对位置，保底后其余顺序仍保持 score 降序（J-2b/
    J-2c 契约不受影响）。
    """
    if not hits or limit <= 1 or len(hits) <= limit:
        return hits[:limit]
    top = hits[:limit]
    top_cats = {h.metadata.get("category") for h in top}
    all_cats = {h.metadata.get("category") for h in hits}
    missing = all_cats - top_cats
    if not missing:
        return top
    best_by_missing: dict[str, MemoryHit] = {}
    for h in hits:
        c = h.metadata.get("category")
        if c in missing and c not in best_by_missing:
            best_by_missing[c] = h
    redundant: list[MemoryHit] = [
        h
        for h in top
        if sum(1 for x in top if x.metadata.get("category") == h.metadata.get("category")) > 1
    ]
    redundant.sort(
        key=lambda h: (
            _unit_score(float(h.metadata.get("score") or 0.0)),
            _CATEGORY_PRIORITY.get(h.metadata.get("category", ""), 0),
        )
    )
    out = list(top)
    for i, promoted in enumerate(best_by_missing.values()):
        if i >= len(redundant):
            break
        idx = out.index(redundant[i])
        out[idx] = promoted
    return out


def _apply_fair_quota(
    hits: list[MemoryHit],
    limit: int,
    intent_hint: str | None = None,
    question_hint: str | None = None,
) -> list[MemoryHit]:
    """类别公平——按全局融合分降序取 top-k；被挤出的类别仅在**软保底**条件下
    补位（该类最高分 ≥ 全局中位数），弱相关类别不再靠硬保底挤掉强相关项。

    软保底实现：若某分类（有命中但未进 top-k）的最高分不低于全局中位数，则用
    top-k 内「同分类冗余且最低分」的条目替换之；输出保持 score 降序主体结构。
    question_hint 命中四问时对分类乘倍率（与排序同一口径）。
    """
    if not hits or limit <= 1 or len(hits) <= limit:
        return hits[:limit]

    question_bias = route_bias(question_hint) if question_hint else {}

    def _rank(h: MemoryHit) -> float:
        category = h.metadata.get("category") or ""
        return _fusion_score(h.metadata, _intent_bias(category, intent_hint)) * question_bias.get(
            category, 1.0
        )

    ordered = sorted(hits, key=lambda h: str(h.key))
    ranked = sorted(
        ordered,
        key=lambda h: (
            _rank(h),
            _CATEGORY_PRIORITY.get(h.metadata.get("category", ""), 0),
        ),
        reverse=True,
    )
    top = ranked[:limit]
    top_cats = {h.metadata.get("category") for h in top}
    all_cats = {h.metadata.get("category") for h in hits}
    missing = all_cats - top_cats
    if not missing:
        return top

    threshold = statistics.median([_rank(h) for h in ranked])
    best_by_missing: dict[str, MemoryHit] = {}
    for h in ranked:
        c = h.metadata.get("category")
        if c in missing and c not in best_by_missing and _rank(h) >= threshold:
            best_by_missing[c] = h
    if not best_by_missing:
        return top

    redundant: list[MemoryHit] = [
        h
        for h in top
        if sum(1 for x in top if x.metadata.get("category") == h.metadata.get("category")) > 1
    ]
    # 同分冗余项优先替换 top 中靠后的（保留排序靠前的一条）
    redundant.sort(key=lambda h: top.index(h), reverse=True)
    redundant.sort(
        key=lambda h: (
            _rank(h),
            _CATEGORY_PRIORITY.get(h.metadata.get("category", ""), 0),
        )
    )
    out = list(top)
    for i, promoted in enumerate(best_by_missing.values()):
        if i >= len(redundant):
            break
        idx = out.index(redundant[i])
        out[idx] = promoted
    # soft floor 提升项前置（保持相对顺序），其余按原 top 顺序
    promoted_ids = {id(item) for item in best_by_missing.values()}
    return [h for h in out if id(h) in promoted_ids] + [h for h in out if id(h) not in promoted_ids]


if TYPE_CHECKING:
    from bandi.memory.episodic import EpisodicStore
    from bandi.memory.working import ScratchpadStore


@dataclass
class MemoryHit:
    """跨记忆子系统聚合检索命中的统一载体（与 long_term.MemoryItem 区分命名）。"""

    key: str = ""
    value: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


_NORM_WS = re.compile(r"\s+", re.UNICODE)


def _norm_dedup(value: str) -> str:
    """功能：去重键归一化——折叠连续空白为单空格（实质相同的事实，空白形态不限）。"""
    return _NORM_WS.sub(" ", value.strip())


class MemoryManager:
    def __init__(
        self,
        long_term: LongTermMemory | None = None,
        episodic: EpisodicStore | None = None,
        working: ScratchpadStore | None = None,
        markdown_memory: MarkdownMemoryStore | None = None,
        enricher: MemoryEnricher | None = None,
    ):
        self.long_term = long_term or LongTermMemory()
        self.episodic = episodic
        self.working = working
        self._markdown = markdown_memory
        self.enricher = enricher
        self._working_blocked_until = 0.0

    @staticmethod
    def _is_lock_error(exc: BaseException) -> bool:
        """功能：判定异常是否为 SQLite 锁冲突（非锁错误不得熔断工作层）。"""
        text = str(exc).lower()
        return "locked" in text or "busy" in text

    def _mark_working_blocked(self, exc: BaseException) -> bool:
        """功能：锁冲突时短路标记工作层——锁冲突不是数据损坏，重试只会烧光本轮预算。

        返回 True 表示已标记（调用方降级为空并跳过后续候选词）。
        """
        if not self._is_lock_error(exc):
            return False
        self._working_blocked_until = time.monotonic() + _WORKING_LOCK_BACKOFF_SEC
        return True

    def _working_blocked(self) -> bool:
        """功能：工作层是否处于锁冲突熔断窗口内。"""
        return time.monotonic() < self._working_blocked_until

    def search_all(
        self,
        query: str,
        limit: int = 5,
        project_id: str | None = None,
        employee_id: str | None = None,
        session_id: str | None = None,
        include_project_md: bool = True,
    ) -> dict[str, list[MemoryHit]]:
        """
        （只查无会话桶，fail-closed 不放行任何真实会话草稿）。"""
        results: dict[str, list[MemoryHit]] = {}
        # 单点隔离：任一子库损坏只降级该分类为空 list，其余分类照常召回——
        # 记忆文件损坏不炸检索主链（与 search_unified 全局 try 互补：此处保「其他分类」）
        try:
            results["long_term"] = [
                MemoryHit(
                    key=r.id,
                    value=r.content,
                    metadata={
                        "importance": r.importance,
                        # 相关度取 FTS5 bm25（不再用 importance 冒充）；无 rank（LIKE
                        # 回退/无 metadata 提供方）时回退 importance，不使排序归零
                        "score": _long_term_score(r),
                        "timestamp": _iso_timestamp(getattr(r, "timestamp", None)),
                        **_long_term_metadata(r),
                    },
                )
                # 取代整表 LIKE 扫描；FTS 空召回内部回退 LIKE 兜底保召回
                for r in self.long_term.search_fts_ranked(
                    query, limit, project_id=project_id, employee_id=employee_id
                )
            ]
        except Exception as exc:  # noqa: BLE001 — 单子库损坏降级空 list
            logger.warning("search_all_long_term_failed [id=%s]: %s", id(self), exc)
            results["long_term"] = []
        if self.working is not None:
            if self._working_blocked():
                results["working"] = []
            else:
                try:
                    results["working"] = [
                        MemoryHit(
                            key=f"{hit.kind}:{hit.key}",
                            value=hit.value,
                            metadata={
                                "kind": hit.kind,
                                "score": score,
                                "created_at": getattr(hit, "created_at", None),
                            },
                        )
                        for hit, score in self.working.search(
                            query, limit, session_id=session_id or ""
                        )
                    ]
                except Exception as exc:  # noqa: BLE001 — 单子库损坏降级空 list
                    if self._mark_working_blocked(exc):
                        logger.warning(
                            "search_all_working_blocked [id=%s]: %s（后续候选词跳过工作层）",
                            id(self),
                            exc,
                        )
                    else:
                        logger.warning("search_all_working_failed [id=%s]: %s", id(self), exc)
                    results["working"] = []
        if self.episodic is not None:
            try:
                results["episodic"] = [
                    MemoryHit(
                        key=episode.episode_id,
                        value=episode.summary,
                        metadata={
                            "importance": episode.importance,
                            "score": score,
                            "episode_id": episode.episode_id,
                            "created_at": episode.created_at,
                            "last_recalled_at": episode.last_recalled_at,
                        },
                    )
                    for episode, score in self.episodic.search(
                        query, limit, project_id=project_id, employee_id=employee_id
                    )
                ]
            except Exception as exc:  # noqa: BLE001 — 单子库损坏降级空 list
                logger.warning("search_all_episodic_failed [id=%s]: %s", id(self), exc)
                results["episodic"] = []
        if self._markdown is not None and include_project_md:
            # 命中即正文显式注入，而非仅「标题 — 钩子」指针）；read_entry 唯一生产
            # 调用点 = expand_entry。
            try:
                results["project_md"] = [
                    MemoryHit(
                        key=f"entry:{ptr.entry_id}",
                        value=self._expand_project_md(ptr),
                        metadata={
                            "entry_id": ptr.entry_id,
                            "title": ptr.title,
                            "score": relevance,
                        },
                    )
                    for ptr, relevance in self._markdown.search_entries(query, limit)
                ]
            except Exception as exc:  # noqa: BLE001 — 单子库损坏降级空 list
                logger.warning("search_all_project_md_failed [id=%s]: %s", id(self), exc)
                results["project_md"] = []
        return results

    def _expand_project_md(self, ptr: Any) -> str:
        """project_md 命中正文展开——正文可读则注入正文片段，否则回退标题—钩子
        摘要（穷尽情形：store 无正文解析 / read_entry 异常）。fail-closed 不炸主链。"""
        body = self.expand_entry(ptr.entry_id)
        if body:
            return body[:_PROJECT_MD_SNIPPET_CHARS]
        title = str(getattr(ptr, "title", "") or "")
        hook = str(getattr(ptr, "hook", "") or "")
        return f"{title} — {hook}"

    def expand_entry(self, entry_id: str) -> str:
        """
        fail-closed：无 markdown store / 非法 id / 读异常 → 返回 ""（主链不炸）。
        """
        if self._markdown is None:
            return ""
        try:
            return self._markdown.read_entry(entry_id)
        except Exception as exc:  # noqa: BLE001 — 展开失败降级为空，检索链不中断
            logger.warning("expand_entry_failed [id=%s]: %s", entry_id, exc)
            return ""

    def save_entry(self, title: str, content: str) -> str | None:
        """功能：-11③——教训详情落 entries 档案
        （非 visible，不占每轮上下文；项目根内物理隔离；检索/memory_read 按需读）。
        无 markdown store / 写入失败返回 None（fail-safe，不阻断判定流程）。
        """
        if self._markdown is None:
            return None
        try:
            pointer = self._markdown.save_entry(title, content)
        except Exception as exc:  # noqa: BLE001 — 详情落盘失败不炸教训主链路
            logger.warning("save_entry_failed [title=%s]: %s", title[:40], exc)
            return None
        return pointer.entry_id

    def search_group_all(
        self,
        query: str,
        conversation_id: str,
        *,
        limit: int = 5,
        session_id: str | None = None,
    ) -> dict[str, list[MemoryHit]]:
        """功能：群聊检索面——本群记忆 + 全局记忆，排除员工私有与真实项目记忆。"""
        from bandi.memory.group_scope import group_scope_id

        scope = group_scope_id(conversation_id)
        merged: dict[str, list[MemoryHit]] = {}
        sources = [
            self.search_all(
                query,
                limit=limit,
                project_id=scope,
                employee_id=None,
                session_id=session_id,
                include_project_md=False,
            ),
            self.search_all(
                query,
                limit=limit,
                project_id=None,
                employee_id=None,
                session_id=None,
                include_project_md=False,
            ),
        ]
        for source in sources:
            for category, entries in source.items():
                if category == "working":
                    continue
                merged.setdefault(category, []).extend(entries)
        return merged

    def search_dm_all(
        self,
        query: str,
        conversation_id: str,
        *,
        project_id: str = "",
        limit: int = 5,
        session_id: str | None = None,
    ) -> dict[str, list[MemoryHit]]:
        """功能：员工单聊检索面——本会话记忆 + 全局，排除团队/项目/员工私有/群聊。"""
        from bandi.memory.dm_scope import dm_scope_id

        scope = dm_scope_id(project_id, conversation_id)
        if not scope:
            return {}
        merged: dict[str, list[MemoryHit]] = {}
        sources = [
            self.search_all(
                query,
                limit=limit,
                project_id=scope,
                employee_id=None,
                session_id=None,
                include_project_md=False,
            ),
            self.search_all(
                query,
                limit=limit,
                project_id=None,
                employee_id=None,
                session_id=None,
                include_project_md=False,
            ),
        ]
        for source in sources:
            for category, entries in source.items():
                if category == "working":
                    continue
                merged.setdefault(category, []).extend(entries)
        return merged

    def search_unified(
        self,
        query: str,
        *,
        session_id: str | None = None,
        project_id: str | None = None,
        employee_id: str | None = None,
        conversation_id: str | None = None,
        dm_conversation_id: str | None = None,
        limit: int = 5,
        scoring: str = "fusion",
        intent_hint: str | None = None,
        question_hint: str | None = None,
        note_recalls: bool = True,
    ) -> list[MemoryHit]:
        """
        episodic note_recall，MemoryContextSource 与 Prefetch 的唯一数据源。

        scoring：默认 fusion（relevance×0.45 + importance×0.2 + recency×0.25 +
        recall×0.05 + intent×0.05，权重归一 1.0，跨分类统一排序；起为默认）；
        显式 classified 走各分类 score 降序 + 分类优先破平（旧契约路径保留）。

        question_hint：四问路由——命中 preference/project_background/
        history_conclusion/task_state 之一时按 routing.route_bias 对分类乘倍率
        （确定性；None/未知 hint 零行为）。

        fail-safe：底层存储异常降级空列表，绝不炸主链。"""
        q = (query or "").strip()
        if not q:
            return []
        try:
            from bandi.memory.episodic import tokenize_cn

            candidates = tokenize_cn(q) or [q]
            seen_keys: set[tuple[str, str]] = set()
            hits: list[MemoryHit] = []
            for candidate in candidates[: limit * 3]:
                if self._working_blocked():
                    # 预取线程不得为不可用的工作层烧掉整轮预算——search_all 的
                    # 降级已把它记为 blocked，跳过后续候选词直接用其余分类结果
                    break
                source = (
                    self.search_dm_all(
                        candidate,
                        dm_conversation_id,
                        project_id=project_id or "",
                        limit=limit,
                        session_id=session_id,
                    )
                    if dm_conversation_id
                    else self.search_group_all(
                        candidate,
                        conversation_id,
                        limit=limit,
                        session_id=session_id,
                    )
                    if conversation_id
                    else self.search_all(
                        candidate,
                        limit=limit,
                        project_id=project_id,
                        employee_id=employee_id,
                        session_id=session_id,
                    )
                )
                for category, entries in source.items():
                    for entry in entries:
                        # episodic（经历层）天然可存多条相似经历，保留按 (category, episode_id)
                        # 去重；事实类（long_term/working/project_md）按归一化 value 去重
                        if category == "episodic":
                            key = (category, entry.key)
                        else:
                            key = ("value", _norm_dedup(entry.value or ""))
                        if key in seen_keys or not (entry.value or "").strip():
                            continue
                        seen_keys.add(key)
                        # 分类标签随行透传——打平聚合后消费方仍可识别来源
                        hit = MemoryHit(
                            key=entry.key,
                            value=entry.value,
                            metadata={**(entry.metadata or {}), "category": category},
                        )
                        hits.append(hit)
            # 降序排列（score 由 search_all 各分类随行透传），同名分数按分类优先级
            # ×0.2 + recency×0.25 + recall×0.05）排序，打扰者新近事实可压过老化
            # 高重要性事实；default 不启用，旧契约路径零变化。
            question_bias = route_bias(question_hint) if question_hint else {}
            # 先按 key 升序，再按分数降序（稳定）——同分时保持 key 升序，
            # 不随 search_all 返回顺序漂移
            hits.sort(key=lambda h: str(h.key))
            if scoring == "fusion":
                hits.sort(
                    key=lambda h: (
                        _fusion_score(
                            h.metadata,
                            _intent_bias(h.metadata.get("category", ""), intent_hint),
                        )
                        * _description_boost(h.metadata, query)
                        * question_bias.get(h.metadata.get("category", ""), 1.0),
                        _CATEGORY_PRIORITY.get(h.metadata.get("category", ""), 0),
                    ),
                    reverse=True,
                )
            else:
                hits.sort(
                    key=lambda h: (
                        _unit_score(float(h.metadata.get("score") or 0.0))
                        * _description_boost(h.metadata, query),
                        _CATEGORY_PRIORITY.get(h.metadata.get("category", ""), 0),
                    ),
                    reverse=True,
                )
            if scoring == "fusion":
                result = _apply_fair_quota(hits, max(1, limit), intent_hint, question_hint)
            else:
                result = _apply_category_floor(hits, max(1, limit))
        except Exception as exc:  # noqa: BLE001 — 检索失败不阻断注入链
            import traceback

            logger.warning(
                "search_unified_failed [id=%s]: %s\n%s",
                id(self),
                exc,
                "".join(traceback.format_stack()[-6:-1]),
            )
            return []
        self._note_long_term_recalls(result)
        # （10.7 扩展：召回查询指纹随写——第三维门槛数据源）
        if note_recalls:
            self._note_episodic_recalls(result, query=q)
        self._apply_enrichment(result)
        return result

    def note_episodic_recalls(self, hits: list[MemoryHit], *, query: str = "") -> None:
        """功能：只对实际注入上下文的 episodic 命中写召回计数（供上下文源在裁剪后调用）。"""
        self._note_episodic_recalls(hits, query=query)

    def _note_long_term_recalls(self, hits: list[MemoryHit]) -> None:
        """功能：对真正返回的 long_term 命中写回召回计数——汇总本轮唯一 id 后
        批量单事务写回（同轮同 id 去重）；无批量能力时逐条回退，失败静默。"""
        item_ids = {
            hit.key for hit in hits if hit.metadata.get("category") == "long_term" and hit.key
        }
        if not item_ids:
            return
        batch = getattr(self.long_term, "note_recalled_many", None)
        if callable(batch):
            try:
                batch(sorted(item_ids))
            except Exception as exc:  # noqa: BLE001 — 写回失败不得影响检索结果
                logger.warning("note_recalled_many_failed [%s]: %s", sorted(item_ids), exc)
            return
        note = getattr(self.long_term, "note_recalled", None)
        if not callable(note):
            return
        for item_id in sorted(item_ids):
            try:
                note(item_id)
            except Exception as exc:  # noqa: BLE001 — 写回失败不得影响检索结果
                logger.warning("note_recalled_failed [%s]: %s", item_id, exc)

    def _note_episodic_recalls(self, hits: list[MemoryHit], *, query: str = "") -> None:
        """功能：对真正返回的 episodic 命中写回召回计数——候选/被裁条目不写
        （原候选阶段即写回，recall 虚增污染晋升门槛）；同 key 去重
        失败静默（写回不得影响检索结果）。"""
        store = getattr(self, "episodic", None)
        if store is None:
            return
        seen: set[str] = set()
        for hit in hits:
            if hit.metadata.get("category") != "episodic" or not hit.key or hit.key in seen:
                continue
            seen.add(hit.key)
            try:
                store.note_recall(hit.key, query=query)
            except Exception as exc:  # noqa: BLE001 — 写回失败不得影响检索结果
                logger.warning("note_recall_failed [%s]: %s", hit.key, exc)

    def _apply_enrichment(self, hits: list[MemoryHit]) -> None:
        """功能：记忆增强挂点——实体增强（enricher 在场时）+ 关系推理扩展；
        只往 metadata 追加 enriched_entities/contradiction_with/entailed_facts
        不改 value/key/排序；整体失败静默降级（检索结果原样返回）。"""
        if not hits:
            return
        self._apply_entailment(hits)
        enricher = self.enricher
        if enricher is None:
            return
        try:
            self._enrich_facts(enricher, hits)
            self._enrich_contradictions(enricher, hits)
        except Exception as exc:  # noqa: BLE001 — 增强失败不影响检索结果
            logger.warning("memory_enrichment_failed [id=%s]: %s", id(self), exc)

    def _apply_entailment(self, hits: list[MemoryHit]) -> None:
        """功能：召回命中的结构化三元组按 tbox 特征做连通分量闭包，派生结论
        写入 metadata["entailed_facts"]——只读派生（撤回前提即消失），失败静默。

        三元组从条目本体回读（hit metadata 是轻量白名单，不带 subject/predicate/object）。
        """
        try:
            from bandi.memory.entailment import (
                expand_entailed_facts,
                fact_triple_from_metadata,
                render_entailed_fact,
            )

            seeds = []
            triples_by_key: dict[str, tuple[str, str, str]] = {}
            for hit in hits:
                if hit.metadata.get("category") != "long_term" or not hit.key:
                    continue
                item = self.long_term.get(hit.key)
                triple = fact_triple_from_metadata(getattr(item, "metadata", None))
                if triple is not None:
                    seeds.append(triple)
                    triples_by_key[hit.key] = triple
            if not seeds:
                return
            derived = expand_entailed_facts(self.long_term, seeds)
            if not derived:
                return
            by_subject: dict[str, list[str]] = {}
            for entry in derived:
                by_subject.setdefault(str(entry.get("subject") or ""), []).append(
                    render_entailed_fact(entry)
                )
            for hit in hits:
                triple = triples_by_key.get(hit.key)
                if triple is None:
                    continue
                facts = by_subject.get(triple[0])
                if facts:
                    hit.metadata["entailed_facts"] = facts
        except Exception as exc:  # noqa: BLE001 — 推理扩展失败不影响检索结果
            logger.warning("memory_entailment_failed [id=%s]: %s", id(self), exc)

    @staticmethod
    def _enrich_facts(enricher: MemoryEnricher, hits: list[MemoryHit]) -> None:
        """功能：逐条命中抽取实体→图事实文本写入 metadata["enriched_entities"]。"""
        for hit in hits:
            refs = enricher.extract_entities(hit.value or "")
            if not refs:
                continue
            facts = enricher.find_facts(refs)
            if facts:
                hit.metadata["enriched_entities"] = [str(fact) for fact in facts]

    @staticmethod
    def _enrich_contradictions(enricher: MemoryEnricher, hits: list[MemoryHit]) -> None:
        """功能：命中两两矛盾检测——双向写 metadata["contradiction_with"]（值为对方 key）。"""
        values = [hit.value or "" for hit in hits]
        pairs = enricher.find_contradictions(values) or []
        index_by_value: dict[str, list[int]] = {}
        for index, value in enumerate(values):
            index_by_value.setdefault(value, []).append(index)
        for left, right in pairs:
            left_indexes = index_by_value.get(left)
            right_indexes = index_by_value.get(right)
            if not left_indexes or not right_indexes:
                continue
            left_index, right_index = left_indexes[0], right_indexes[0]
            if left_index == right_index:
                continue
            MemoryManager._note_contradiction(hits, left_index, right_index)
            MemoryManager._note_contradiction(hits, right_index, left_index)

    @staticmethod
    def _note_contradiction(hits: list[MemoryHit], index: int, other_index: int) -> None:
        """功能：单向标注矛盾对象 key（去重；key 为空跳过）。"""
        other_key = hits[other_index].key
        existing = hits[index].metadata.setdefault("contradiction_with", [])
        if other_key and other_key not in existing:
            existing.append(other_key)

    def close(self) -> None:
        """
        新建连接泄漏；run_agent_turn 结束时调用。"""
        closers = [self.long_term, self.episodic, self.working, self._markdown]
        for closer in closers:
            close_fn = getattr(closer, "close", None)
            if close_fn is not None:
                try:
                    close_fn()
                except Exception:  # noqa: BLE001 — 单存储关闭失败不阻断其余
                    pass

    def total_count(self) -> int:
        return self.long_term.count()
