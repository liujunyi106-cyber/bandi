# 功能：记忆离线整合（10.7）——情节层高价值经历晋升长期记忆、低价值淘汰
# forget_low_value_episodes；openclaw dreaming 三阶段（本实现取可测的
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandi.memory.decay import explicit_flags as _explicit_flags
from bandi.memory.distill import DistilledMemory, distill_for_episode, is_discard
from bandi.memory.episodic import Episode, EpisodicStore
from bandi.memory.long_term import (
    LONG_TERM_DECAY_HALF_LIFE_DAYS,
    LongTermMemory,
    MemoryItem,
    suppress_scope_miss,
)
from bandi.memory.structured import structured_preview

logger = logging.getLogger(__name__)

# 晋升门槛：importance ≥ 0.7 且被真实召回 ≥ 2 次（主维）——单次召回不构成复用信号
CONSOLIDATION_MIN_IMPORTANCE = 0.7
CONSOLIDATION_MIN_RECALLS = 2
# 第三维（10.7 扩展）：被 ≥2 个不同查询共同召回 ≥2 次且带真实信号——低重要性
# 但复用的经验仍值得晋升（openclaw min_unique_queries 语义）。
# 0.5 底线：纯基准 0.4 的普通轮（无转向/无工具错误/无异常终止信号）即便被
# n-gram 召回互相 bump 也不得走复用通道——否则普通对话会被相似问句顶成
# 提名噪音（真机实证：20 轮普通问答被宽松召回顶到 recall=2 全线提名）。
CONSOLIDATION_MIN_MULTI_RECALLS = 2
CONSOLIDATION_MIN_MULTI_IMPORTANCE = 0.5
CONSOLIDATION_MIN_UNIQUE_QUERIES = 2
CONSOLIDATION_LONG_TERM_IMPORTANCE = 0.8
# 淘汰门槛：importance ≤ 0.3 且零召回且龄 ≥ 7 天
FORGET_MAX_IMPORTANCE = 0.3
FORGET_MIN_AGE_DAYS = 7.0
_DEFAULT_SCAN_LIMIT = 200
# 功能：本体操作轮标记——内容归本体库，不进记忆草稿（提名通道跳过）
ONTOLOGY_TURN_TAG = "ontology_turn"


@dataclass
class ConsolidationReport:
    scanned: int = 0
    promoted: list[str] = field(default_factory=list)
    # 记入 promoted 会造成报告失真（同族已在 maintenance.PromotionResult 区分）
    staged: list[str] = field(default_factory=list)
    forgotten: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    merged_sources: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "promoted": list(self.promoted),
            "staged": list(self.staged),
            "forgotten": list(self.forgotten),
            "merged": list(self.merged),
            "merged_sources": {key: list(value) for key, value in self.merged_sources.items()},
        }


def _age_days(created_at: str | None, now: datetime) -> float:
    if not created_at:
        return 0.0
    try:
        born = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return 0.0
    if born.tzinfo is None:
        born = born.replace(tzinfo=UTC)
    return max(0.0, (now - born).total_seconds() / 86400.0)


def _is_low_value_episode(summary: str) -> bool:
    """功能：判断情节摘要是否只是噪音（审批等待/回执/寒暄/自我介绍/可推导）。"""
    from bandi.memory.ingest_review import is_noise_memory

    return is_noise_memory(summary) is not None


# 功能：用户已拒绝晋升的情节标记——打标后永不进入晋升通道。拒绝必须粘滞：
# 暂存条目被用户丢弃后，若下一轮扫描再次提名，会出现「删了又回来」的循环
# （每小时巩固周期会重扫情节层）。
PROMOTION_REJECTED_TAG = "promotion_rejected"

_DEDUP_SCAN_PAGE = 500

# 功能：单条提炼的墙钟上限（秒）——每条都要过一次 LLM，
# 串行超时会把整轮巩固拖到分钟级（真机 197s，期间主链下一轮 LLM 调用排不上）。
DISTILL_TIMEOUT_SEC = 20.0
# 功能：一轮巩固里提炼的总预算（秒）——超出即放弃剩余提名、本轮只做规则降级，
# 保证巩固始终是「有限耗时」，不会因为待晋升条目多而无限拉长。
DISTILL_ROUND_BUDGET_SEC = 45.0


def _turn_recorded_inputs(long_term: LongTermMemory) -> set[str]:
    """功能：长期层已直写的回合输入集合（`turn:` 条目 metadata.user_input）。

    直写（前台 turn: 条目）与晋升（consolidated: 条目）是两套 id 空间，同一轮
    内容可能各存一份；合并机制又排除 consolidated: 前缀收拾不了。故在提名处按
    「来源回合」去重：该轮已有直写条目时，草稿晋升必是重复，跳过。
    """
    recorded: set[str] = set()
    try:
        scopes = list(long_term.list_scopes())
    except Exception:  # noqa: BLE001 — 去重扫描失败不阻断晋升
        logger.warning("consolidation dedup scan failed to list scopes")
        return recorded
    for project_id, employee_id in scopes:
        offset = 0
        while True:
            page = long_term.list_items(
                project_id=project_id,
                employee_id=employee_id,
                limit=_DEDUP_SCAN_PAGE,
                offset=offset,
            )
            for item in page:
                if not str(item.id).startswith("turn:"):
                    continue
                user_input = str((item.metadata or {}).get("user_input") or "").strip()
                if user_input:
                    recorded.add(user_input)
            if len(page) < _DEDUP_SCAN_PAGE:
                break
            offset += _DEDUP_SCAN_PAGE
    return recorded


def mark_promotion_rejected(episode_id: str, episodic: EpisodicStore | None = None) -> bool:
    """功能：把情节标记为「晋升已被用户拒绝」（幂等；情节不存在返回 False）。

    标签是粘滞判据的唯一来源：召回计数可能因相似问句继续增长，
    只靠清零指标挡不住 `multi_query` 通道复活提名。
    """
    store = episodic
    if store is None:
        from bandi.memory.episodic import get_shared_episodic

        store = get_shared_episodic()
    episode = store.get(episode_id)
    if episode is None:
        return False
    if PROMOTION_REJECTED_TAG in episode.tags:
        return True
    from dataclasses import replace

    store.record(replace(episode, tags=[*episode.tags, PROMOTION_REJECTED_TAG]))
    return True


def _stage_consolidation(item: MemoryItem, episode: Episode) -> None:
    """功能：整合晋升写入暂存区（payload 可在批准时按原维度重放；失败不抛出）。

    摘要 = 提炼后结构化内容（中文 kind 标签前缀 + 断言句）——不是情节原文包装。
    draft 元数据供消息中心「系统提名/长期记忆」多标签展示。
    """
    from bandi.memory.staging import get_pending_store, mask_summary

    try:
        get_pending_store().stage(
            {
                "action": "consolidate_episode",
                "summary": mask_summary(
                    structured_preview(item.content, (item.metadata or {}).get("structured"))
                ),
                "origin": "background",
                "payload": {
                    "target": "long_term",
                    "item": item.to_dict(),
                    "project_id": episode.project_id,
                    "employee_id": episode.employee_id,
                    "conflict": False,
                    "draft": {"kind": "系统提名", "target": "长期记忆"},
                },
            }
        )
    except Exception as e:  # noqa: BLE001 — 暂存失败不得回落直写（安全失败）
        logger.warning("consolidation staging failed: %s", e)


def _merge_scopes(long_term: LongTermMemory) -> list[tuple[str | None, str | None]]:
    """功能：合并遍历的维度分区——全局层 + 库内 distinct 维度（确定性排序）。"""
    try:
        scopes: set[tuple[str | None, str | None]] = set(long_term.list_scopes())
    except Exception:  # noqa: BLE001 — 枚举失败退化为全局层
        scopes = set()
    scopes.add((None, None))
    return sorted(scopes, key=lambda scope: (scope[0] or "", scope[1] or ""))


def consolidate_episodes(
    episodic: EpisodicStore,
    long_term: LongTermMemory,
    *,
    now: datetime | None = None,
    limit: int = _DEFAULT_SCAN_LIMIT,
    project_id: str | None = None,
    employee_id: str | None = None,
    distiller: Callable[[Episode], DistilledMemory] | None = None,
) -> ConsolidationReport:
    """功能：扫描情节层执行晋升/淘汰。晋升用稳定 item_id `consolidated:<episode_id>`
    （INSERT OR REPLACE 幂等，重复整合不产生重复长期记忆）。

    离线整合属无人值守路径——写入前置 background 来源，gate 判 stage 时进
    暂存区等批准（无自动批准），写后恢复原来源。

    `distiller` 为提名时的提炼器（None = 默认 LLM 提炼、失败降级规则）；
    只对通过门槛的情节调用——绝不每轮对话调用。
    """
    from bandi.memory.staging import (
        MemoryWriteGate,
        get_write_origin,
        reset_write_origin,
        set_write_origin,
    )

    moment = now or datetime.now(UTC)
    report = ConsolidationReport()
    token = set_write_origin("background")
    try:
        from bandi.memory.maintenance import _pending_promotion_ids

        pending_ids = _pending_promotion_ids()
        dedup_inputs: set[str] | None = None
        episodes = episodic.list_recent(limit=limit, project_id=project_id, employee_id=employee_id)
        # 功能：先并发提炼候选，再走落库循环——提炼是唯一的网络往返，
        # 串行执行会让一轮巩固的耗时 = 候选数 × 单条超时（真机 197s）。
        distilled_by_id = _distill_candidates(
            episodes, long_term=long_term, pending_ids=pending_ids, distiller=distiller
        )
        for episode in episodes:
            report.scanned += 1
            if PROMOTION_REJECTED_TAG in episode.tags:
                # 用户已拒绝晋升——粘滞跳过，不得因后续召回计数变化复活提案
                continue
            if ONTOLOGY_TURN_TAG in episode.tags:
                # 本体操作轮不进晋升通道——内容归本体库，不进记忆草稿
                continue
            if _is_low_value_episode(episode.summary):
                # 寒暄/自我介绍/能力清单不进晋升通道（相似问句可使其 recall 虚高）
                continue
            high_value = (
                episode.importance >= CONSOLIDATION_MIN_IMPORTANCE
                and episode.recall_count >= CONSOLIDATION_MIN_RECALLS
            )
            # 第三维：多查询共同召回（importance 门槛放宽的经验复用信号）
            multi_query = (
                episode.importance >= CONSOLIDATION_MIN_MULTI_IMPORTANCE
                and episode.recall_count >= CONSOLIDATION_MIN_MULTI_RECALLS
                and len(episode.recall_queries) >= CONSOLIDATION_MIN_UNIQUE_QUERIES
            )
            if high_value or multi_query:
                stable_id = f"consolidated:{episode.episode_id}"
                if long_term.get(stable_id) is not None:
                    # 已晋升过——不得每小时重复提名（否则用户要反复批准同一条）
                    continue
                if stable_id in pending_ids:
                    # 已在暂存区等批准——不得重复提名（避免每轮改写暂存记录）
                    continue
                if dedup_inputs is None:
                    dedup_inputs = _turn_recorded_inputs(long_term)
                turn_input = str(getattr(episode, "user_input", "") or "").strip()
                if turn_input and turn_input in dedup_inputs:
                    # 该轮已由前台直写覆盖——草稿晋升会造成长期层重复，跳过
                    continue
                distilled = distilled_by_id.get(episode.episode_id)
                if distilled is None:
                    distilled = distill_for_episode(episode, distiller)
                if is_discard(distilled):
                    # 提炼器判定「不值得记」——不提名也不落库
                    continue
                item = MemoryItem(
                    item_id=stable_id,
                    content=distilled.content,
                    importance=min(
                        1.0, max(CONSOLIDATION_LONG_TERM_IMPORTANCE, distilled.importance)
                    ),
                    metadata={
                        "source": "episodic_consolidation",
                        "episode_id": episode.episode_id,
                        "structured": distilled.to_structured(),
                        "evidence": episode.summary,
                        "distilled_by": distilled.distilled_by,
                        **_explicit_flags(episode),
                    },
                )
                if MemoryWriteGate.evaluate(get_write_origin()) == "stage":
                    _stage_consolidation(item, episode)
                    report.staged.append(episode.episode_id)
                else:
                    with suppress_scope_miss():
                        long_term.store(
                            item,
                            project_id=episode.project_id,
                            employee_id=episode.employee_id,
                        )
                    report.promoted.append(episode.episode_id)
            elif (
                episode.importance <= FORGET_MAX_IMPORTANCE
                and episode.recall_count == 0
                and _age_days(episode.created_at, moment) >= FORGET_MIN_AGE_DAYS
            ):
                if episodic.delete(episode.episode_id):
                    report.forgotten.append(episode.episode_id)
        # （与 prune/forget 同口径的有意决定，不改变 门语义）；失败不阻断报告
        try:
            from bandi.memory.merge import merge_similar_memories

            merged: list[str] = []
            merged_sources: dict[str, list[str]] = {}
            for scope_project, scope_employee in _merge_scopes(long_term):
                applied = merge_similar_memories(
                    long_term, project_id=scope_project, employee_id=scope_employee
                )
                for suggestion in applied:
                    merged.append(suggestion.target_id)
                    merged_sources.setdefault(suggestion.target_id, []).extend(
                        suggestion.source_ids
                    )
            report.merged = merged
            report.merged_sources = merged_sources
        except Exception as exc:  # noqa: BLE001 — 合并失败不影响晋升/淘汰结果
            logger.warning("consolidation merge failed: %s", exc)
    finally:
        reset_write_origin(token)
    return report


def distill_episodes_parallel(
    episodes: list[Episode],
    *,
    distiller: Callable[[Episode], DistilledMemory] | None,
    budget_sec: float = DISTILL_ROUND_BUDGET_SEC,
) -> dict[str, DistilledMemory]:
    """功能：并发提炼多条情节，返回 episode_id → 提炼结果。

    提炼是记忆后台路径里唯一的网络往返。逐条串行会让整轮耗时 = 条数 × 30s
    超时（真机 maintenance 晋升 14 条、consolidation 197s，期间主链下一轮
    LLM 调用排不上）。这里并发执行并受总预算约束；
    超预算或单条失败的候选不进返回值，调用方自行回退规则提炼。
    """
    if not episodes:
        return {}
    results: dict[str, DistilledMemory] = {}
    try:
        from concurrent.futures import ThreadPoolExecutor

        def _one(episode: Episode) -> tuple[str, DistilledMemory | None]:
            try:
                return episode.episode_id, distill_for_episode(episode, distiller)
            except Exception:  # noqa: BLE001 — 单条失败不影响其他候选
                logger.warning("candidate distill failed for %s", episode.episode_id)
                return episode.episode_id, None

        workers = min(4, len(episodes))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, episode) for episode in episodes]
            deadline = time.monotonic() + budget_sec
            for future in futures:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "distill round budget %.0fs exhausted; %d candidate(s) fall back to rules",
                        budget_sec,
                        sum(1 for f in futures if not f.done()),
                    )
                    break
                try:
                    episode_id, distilled = future.result(timeout=remaining)
                except TimeoutError:
                    logger.warning("candidate distill timed out at round budget")
                    continue
                if distilled is not None:
                    results[episode_id] = distilled
    except Exception:
        logger.warning("parallel distill unavailable; falling back to per-item", exc_info=True)
        return {}
    return results


def _distill_candidates(
    episodes: list[Episode],
    *,
    long_term: LongTermMemory,
    pending_ids: set[str],
    distiller: Callable[[Episode], DistilledMemory] | None,
) -> dict[str, DistilledMemory]:
    """功能：并发提炼本轮待晋升候选，返回 episode_id → 提炼结果。

    只对过了晋升门槛的候选发起提炼（与原逐条路径同判据，不多调一次），
    实际并发与预算控制见 `distill_episodes_parallel`。
    """
    targets = [
        episode
        for episode in episodes
        if _needs_distill(episode, long_term=long_term, pending_ids=pending_ids)
    ]
    return distill_episodes_parallel(targets, distiller=distiller)


def _needs_distill(
    episode: Episode, *, long_term: LongTermMemory, pending_ids: set[str]
) -> bool:
    """功能：该候选是否值得花一次 LLM 提炼（判据与落库循环一致）。"""
    if PROMOTION_REJECTED_TAG in episode.tags or ONTOLOGY_TURN_TAG in episode.tags:
        return False
    if _is_low_value_episode(episode.summary):
        return False
    high_value = (
        episode.importance >= CONSOLIDATION_MIN_IMPORTANCE
        and episode.recall_count >= CONSOLIDATION_MIN_RECALLS
    )
    multi_query = (
        episode.importance >= CONSOLIDATION_MIN_MULTI_IMPORTANCE
        and episode.recall_count >= CONSOLIDATION_MIN_MULTI_RECALLS
        and len(episode.recall_queries) >= CONSOLIDATION_MIN_UNIQUE_QUERIES
    )
    if not (high_value or multi_query):
        return False
    stable_id = f"consolidated:{episode.episode_id}"
    if long_term.get(stable_id) is not None:
        return False
    return stable_id not in pending_ids


def consolidate_default(*, limit: int = _DEFAULT_SCAN_LIMIT) -> ConsolidationReport:
    """功能：默认存储路径上的整合入口（API/scheduler 哨兵任务调用）——与主链
    记忆同库（~/.bandi/data/episodic.db + 默认 long_term）。"""
    data_dir = Path.home() / ".bandi" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    episodic = EpisodicStore(str(data_dir / "episodic.db"))
    long_term = LongTermMemory(
        str(data_dir / "long_term.db"),
        decay_half_life_days=LONG_TERM_DECAY_HALF_LIFE_DAYS,
    )
    try:
        scopes = episodic.list_scopes() or [(None, None)]
        total = ConsolidationReport()
        for project_id, employee_id in scopes:
            report = consolidate_episodes(
                episodic,
                long_term,
                limit=limit,
                project_id=project_id,
                employee_id=employee_id,
            )
            total.scanned += report.scanned
            total.promoted.extend(report.promoted)
            total.staged.extend(report.staged)
            total.forgotten.extend(report.forgotten)
            total.merged.extend(report.merged)
            total.merged_sources.update(report.merged_sources)
        return total
    finally:
        episodic.close()
        long_term.close()


__all__ = [
    "distill_episodes_parallel",
    "CONSOLIDATION_MIN_IMPORTANCE",
    "CONSOLIDATION_MIN_RECALLS",
    "ConsolidationReport",
    "consolidate_default",
    "consolidate_episodes",
]
