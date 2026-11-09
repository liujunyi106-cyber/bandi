# 功能：记忆/会话生命周期维护——低价值遗忘 + 高价值晋升 + 会话 TTL/归档
#
# 调用、低价值情节无遗忘通道。本模块提供可调度（每日一次）的维护入口
# 全仓无调用方）。
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bandi.memory.episodic import Episode, EpisodicStore
from bandi.memory.long_term import (
    LONG_TERM_DECAY_HALF_LIFE_DAYS,
    LongTermMemory,
    MemoryItem,
)

if TYPE_CHECKING:
    from bandi.memory.distill import DistilledMemory

logger = logging.getLogger(__name__)

FORGET_OLDER_THAN_DAYS = 120
FORGET_MAX_IMPORTANCE = 0.07
# 晋升门槛：达重要性且被真实召回 ≥2 次（与巩固同源；limit 控制单次晋升量）
PROMOTE_LIMIT = 48
PROMOTE_MIN_IMPORTANCE = 0.2
PROMOTE_MIN_RECALLS = 2
# 会话 TTL（清理）+ 归档
SESSION_TTL_DAYS = 90
SESSION_ROTATE_MAX = 1000
SESSION_ARCHIVE_AFTER_DAYS = 30
# 扫描页大小（单分区；遗忘按最旧端扫一页、晋升按最新端扫——页满时翻倍扩查
# 直至候选耗尽，防扫描窗口被已晋升候选占满时窗口外候选饿死）
_SCAN_LIMIT = 1000


def _promote_to_long_term(
    long_term: LongTermMemory,
    episodes: list[Episode],
    *,
    distiller: Callable[[Episode], DistilledMemory] | None = None,
) -> list[str]:
    """功能：晋升写入长期层（维持情节自身作用域），并抑制「漏传维度」计数。

    维护晋升是镜像情节作用域的合法写（无维度情节本就属于全局层），不应污染
    调用方漏传观测；逐条经 `scope_miss_suppressed` 借还净零（long_term 提供
    `suppress_scope_miss` 时优先走其抑制接口）。

    维护晋升属无人值守路径——写入前置 background 来源，gate 判 stage 时
    进暂存区等批准（无自动批准），写后恢复原来源。晋升内容经提炼器转为
    结构化断言（content）+ structured（metadata）——不再是情节原文包装。

    返回**真实进入暂存区**（落盘成功）的 episode id 列表；直写路径为空。
    """

    from bandi.memory.decay import explicit_flags as _explicit_promotion_flags
    from bandi.memory.distill import distill_for_episode, is_discard
    from bandi.memory.ingest_review import is_noise_memory
    from bandi.memory.staging import (
        MemoryWriteGate,
        get_pending_store,
        get_write_origin,
        reset_write_origin,
        scope_miss_suppressed,
        set_write_origin,
    )

    staged: list[str] = []
    token = set_write_origin("background")
    try:
        for episode in episodes:
            summary_noise = is_noise_memory(episode.summary)
            if summary_noise:
                logger.debug(
                    "promotion skipped as noise: %s (%s)", episode.episode_id, summary_noise
                )
                continue
            distilled = distill_for_episode(episode, distiller)
            if is_discard(distilled):
                # 提炼器判定「不值得记」——不进暂存区、不改写情节
                logger.debug("promotion discarded by distiller: %s", episode.episode_id)
                continue
            content_noise = is_noise_memory(distilled.content)
            if content_noise:
                logger.debug(
                    "promotion skipped as noise: %s (%s)", episode.episode_id, content_noise
                )
                continue
            item = MemoryItem(
                item_id=f"consolidated:{episode.episode_id}",
                content=distilled.content,
                importance=min(1.0, max(0.5, episode.importance, distilled.importance)),
                metadata={
                    "source": "episodic_maintenance",
                    "episode_id": episode.episode_id,
                    "structured": distilled.to_structured(),
                    "evidence": episode.summary,
                    "distilled_by": distilled.distilled_by,
                    **_explicit_promotion_flags(episode),
                },
            )
            if MemoryWriteGate.evaluate(get_write_origin()) == "stage":
                if _stage_promotion(get_pending_store(), item, episode):
                    staged.append(episode.episode_id)
                continue
            with scope_miss_suppressed(
                project_id=episode.project_id, employee_id=episode.employee_id
            ):
                long_term.store(
                    item,
                    project_id=episode.project_id,
                    employee_id=episode.employee_id,
                )
    finally:
        reset_write_origin(token)
    return staged


def _stage_promotion(store: Any, item: MemoryItem, episode: Episode) -> bool:
    """功能：晋升写入暂存区（payload 可在批准时按原维度重放；失败不抛出）。

    摘要 = 提炼后结构化内容（中文 kind 标签前缀 + 断言句）——不是情节原文包装。
    draft 元数据供消息中心「系统提名/长期记忆」多标签展示。

    返回是否真实落盘——写盘失败既不算已暂存也不能回落直写（安全失败）。
    """
    from bandi.memory.staging import mask_summary
    from bandi.memory.structured import structured_preview

    try:
        record = store.stage(
            {
                "action": "promote_episode",
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
        logger.warning("promotion staging failed: %s", e)
        return False
    return bool(getattr(record, "persisted", False))


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


def forget_low_value_episodes(
    episodic: EpisodicStore,
    *,
    older_than_days: int = FORGET_OLDER_THAN_DAYS,
    max_importance: float = FORGET_MAX_IMPORTANCE,
    now: datetime | None = None,
) -> list[str]:
    """功能：遗忘陈旧低价值情节（超期 + 低重要性 + 零召回），返回删除 id 列表。

    按「无维度 + 各 distinct 维度」分区遍历，单分区从最旧端扫描——覆盖生产
    带维度情节，且最旧条目不会被最新窗口截断。
    """
    moment = now or datetime.now(UTC)
    forgotten: list[str] = []
    for project_id, employee_id in episodic.list_scopes():
        for episode in episodic.list_for_maintenance(
            project_id=project_id,
            employee_id=employee_id,
            oldest_first=True,
            limit=_SCAN_LIMIT,
        ):
            if episode.recall_count > 0 or episode.importance > max_importance:
                continue
            if _age_days(episode.created_at, moment) < older_than_days:
                continue
            if episodic.delete(episode.episode_id):
                forgotten.append(episode.episode_id)
    if forgotten:
        logger.info("memory maintenance: forgotten %d low-value episode(s)", len(forgotten))
    return forgotten


def _pending_promotion_ids() -> set[str]:
    """功能：暂存区中待批准的晋升目标 id 集合（维护晋升走 暂存门时的去重依据）。

    晋升的稳定目标键为 `consolidated:<episode_id>`；未批准的暂存项不落长期层
    若只查长期层，待批准候选每轮仍占位、后续候选永久饿死。
    """
    try:
        from bandi.memory.staging import get_pending_store

        records = get_pending_store().list_pending()
    except Exception as e:  # noqa: BLE001 — 暂存区不可读时不阻断晋升扫描
        logger.warning("pending promotion lookup failed: %s", e)
        return set()
    ids: set[str] = set()
    for record in records:
        if record.action not in ("promote_episode", "consolidate_episode"):
            continue
        item = (record.payload or {}).get("item")
        if isinstance(item, dict) and item.get("id"):
            ids.add(str(item["id"]))
    return ids


class PromotionResult(list):
    """功能：晋升报告——保持 list[str]（被选中的 episode id）既有契约语义
    额外区分真正落长期层（persisted）与进入暂存待批准（staged）的成员。"""

    def __init__(self, episode_ids: list[str], *, staged: list[str] | None = None) -> None:
        super().__init__(episode_ids)
        staged_ids = set(staged or [])
        self.staged: list[str] = [eid for eid in episode_ids if eid in staged_ids]
        self.persisted: list[str] = [eid for eid in episode_ids if eid not in staged_ids]


def promote_notable_episodes(
    episodic: EpisodicStore,
    long_term: LongTermMemory,
    *,
    limit: int = PROMOTE_LIMIT,
    min_importance: float = PROMOTE_MIN_IMPORTANCE,
    min_recalls: int = PROMOTE_MIN_RECALLS,
    distiller: Callable[[Episode], DistilledMemory] | None = None,
) -> list[str]:
    """功能：高价值情节晋升长期层——分区遍历（最近优先）+ 稳定 item_id 幂等覆盖。

    跳过已晋升候选（长期层已有稳定键）与待批准候选（暂存区同键）——
    否则每轮都从同一批高价值候选取前 N，第 N+1 名永远轮不到（晋升饿死）。
    分区按页扩查直到候选耗尽（单页 `_SCAN_LIMIT` 被已晋升候选占满时，窗口外
    候选仍必须可达）；维护周期低频执行，O(n) 扫描可接受。

    `distiller` 为提名时的提炼器（None = 默认 LLM 提炼、失败降级规则）。
    """
    pending_ids = _pending_promotion_ids()
    checked_promoted: set[str] = set()
    candidates: dict[str, Episode] = {}
    from bandi.memory.consolidation import PROMOTION_REJECTED_TAG, _turn_recorded_inputs
    from bandi.memory.ingest_review import is_noise_memory

    dedup_inputs: set[str] | None = None
    for project_id, employee_id in episodic.list_scopes():
        page_limit = _SCAN_LIMIT
        while True:
            page = episodic.list_for_maintenance(
                project_id=project_id,
                employee_id=employee_id,
                oldest_first=False,
                limit=page_limit,
            )
            for episode in page:
                if PROMOTION_REJECTED_TAG in episode.tags:
                    # 用户已拒绝晋升——维护路径同样粘滞跳过
                    continue
                if is_noise_memory(episode.summary):
                    # 审批等待/回执/寒暄/自我介绍/可推导——不进入晋升候选
                    continue
                if episode.importance < min_importance or episode.recall_count < min_recalls:
                    continue
                stable_id = f"consolidated:{episode.episode_id}"
                if stable_id in pending_ids or stable_id in checked_promoted:
                    continue
                if long_term.get(stable_id) is not None:
                    checked_promoted.add(stable_id)
                    continue
                if dedup_inputs is None:
                    dedup_inputs = _turn_recorded_inputs(long_term)
                turn_input = str(getattr(episode, "user_input", "") or "").strip()
                if turn_input and turn_input in dedup_inputs:
                    # 该轮已由前台直写覆盖——晋升会造成长期层重复，跳过
                    continue
                candidates.setdefault(episode.episode_id, episode)
            if len(page) < page_limit:
                break
            page_limit *= 2
    ordered = sorted(
        candidates.values(),
        key=lambda e: (e.importance, e.created_at or ""),
        reverse=True,
    )
    selected = ordered[: max(0, int(limit))]
    staged = _promote_to_long_term(long_term, selected, distiller=distiller)
    # 🟢-E：返回报告区分「被选中」（promoted 列表，既有契约）与「真正落库」
    # （persisted）；暂存路径下暂存成员只出现在 staged，不再与已晋升混同。
    promoted = PromotionResult([episode.episode_id for episode in selected], staged=staged)
    if promoted:
        logger.info(
            "memory maintenance: promoted %d episode(s), %d staged for approval",
            len(promoted),
            len(promoted.staged),
        )
    return promoted


def run_session_maintenance(
    session_store: Any,
    archive_dir: str | Path,
    *,
    ttl_days: int = SESSION_TTL_DAYS,
    max_sessions: int = SESSION_ROTATE_MAX,
    archive_after_days: int = SESSION_ARCHIVE_AFTER_DAYS,
) -> dict[str, Any]:
    """功能：会话归档 + TTL 清理（rotate 零调用的接线点）。

    先归档（rotate）后删除（delete_expired）——原顺序先删后转，>TTL 会话
    未归档即永久丢失。归档窗口收紧到不超过 TTL（archive_after_days > ttl 时
    min 收敛），保证被删会话此前必有一次归档尝试；rotate 失败则跳过删除
    （未经归档的硬删不可恢复，留待下轮重试）。
    """
    report: dict[str, Any] = {"deleted": 0, "rotated": {}}
    archived_ok = False
    try:
        archive_path = Path(archive_dir)
        archive_path.mkdir(parents=True, exist_ok=True)
        effective_archive_days = int(archive_after_days)
        if effective_archive_days > 0:
            effective_archive_days = min(effective_archive_days, max(0, int(ttl_days)))
        rotated = session_store.rotate(
            archive_path,
            max_sessions=int(max_sessions),
            archive_after_days=effective_archive_days,
        )
        report["rotated"] = rotated if isinstance(rotated, dict) else {}
        archived_ok = True
    except Exception as e:  # noqa: BLE001 - 归档失败不得阻断维护，但跳过删除
        logger.warning("session rotate failed: %s", e)
    if not archived_ok:
        return report
    try:
        report["deleted"] = int(session_store.delete_expired(ttl_days=int(ttl_days)) or 0)
    except Exception as e:  # noqa: BLE001 - 单项失败不阻断其余维护
        logger.warning("session delete_expired failed: %s", e)
    return report


def run_memory_maintenance(
    episodic: EpisodicStore,
    long_term: LongTermMemory,
    session_store: Any | None = None,
    archive_dir: str | Path | None = None,
    *,
    ttl_days: int = SESSION_TTL_DAYS,
    now: datetime | None = None,
    forget_older_than_days: int = FORGET_OLDER_THAN_DAYS,
    promote_limit: int = PROMOTE_LIMIT,
    decay_long_term: bool = True,
) -> dict[str, Any]:
    """功能：一次完整生命周期维护（遗忘 → 晋升 → 长期层衰减/遗忘 → 会话 TTL/归档）。

    长期层衰减排在晋升**之后**：晋升本轮刚写入的条目不该立刻被衰减打折
    （晋升是刚沉淀下来的高价值事实），下一轮周期起才参与衰减。
    """
    promotion = promote_notable_episodes(episodic, long_term, limit=promote_limit)
    report: dict[str, Any] = {
        "forgotten": forget_low_value_episodes(
            episodic, older_than_days=forget_older_than_days, now=now
        ),
        # promoted 含全部被选中 episode id（既有契约）；staged 单列暂存待批准成员
        # 避免无人值守路径「报告已晋升但长期库没有该条」
        "promoted": promotion,
        "staged": list(getattr(promotion, "staged", [])),
    }
    if decay_long_term:
        from bandi.memory.decay import decay_items, forget_stale_items

        try:
            decay_report = decay_items(long_term, now=now)
            report["long_term_decayed"] = decay_report.decayed
            report["long_term_forgotten"] = forget_stale_items(long_term, now=now)
        except Exception as e:  # noqa: BLE001 — 单项失败不阻断其余维护
            logger.warning("long-term decay/forget failed: %s", e)
            report.setdefault("long_term_decayed", 0)
            report.setdefault("long_term_forgotten", [])
    if session_store is not None and archive_dir is not None:
        report["session"] = run_session_maintenance(session_store, archive_dir, ttl_days=ttl_days)
    return report


def default_maintenance_callable(
    *,
    ttl_days: int = SESSION_TTL_DAYS,
    forget_older_than_days: int = FORGET_OLDER_THAN_DAYS,
    promote_limit: int = PROMOTE_LIMIT,
) -> Callable[[], dict[str, Any]]:
    """功能：默认数据目录上的维护入口（API scheduler 装配用，测试不触发）。"""

    def _run() -> dict[str, Any]:
        data_dir = Path.home() / ".bandi" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        episodic: EpisodicStore | None = None
        long_term: LongTermMemory | None = None
        session_store: Any | None = None
        try:
            episodic = EpisodicStore(str(data_dir / "episodic.db"))
            long_term = LongTermMemory(
                str(data_dir / "long_term.db"),
                decay_half_life_days=LONG_TERM_DECAY_HALF_LIFE_DAYS,
            )
            sessions_db = data_dir / "bandi.db"
            if sessions_db.exists():
                from bandi.memory.session.store import SessionStore

                session_store = SessionStore(str(sessions_db))
            result = run_memory_maintenance(
                episodic,
                long_term,
                session_store,
                data_dir / "archive",
                ttl_days=ttl_days,
                forget_older_than_days=forget_older_than_days,
                promote_limit=promote_limit,
            )
            from bandi.memory.staging import MemoryPendingStore, default_memory_store_dir

            try:
                result["pending_purged"] = MemoryPendingStore(
                    default_memory_store_dir()
                ).purge_expired()
            except Exception as e:  # noqa: BLE001 — 清理失败不得阻断维护结果
                logger.warning("pending purge failed: %s", e)
            return result
        finally:
            for owner in (episodic, long_term, session_store):
                if owner is None:
                    continue
                closer = getattr(owner, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception as e:  # noqa: BLE001 — 关闭失败不遮蔽维护结果
                        logger.warning("maintenance store close failed: %s", e)

    return _run


__all__ = [
    "FORGET_MAX_IMPORTANCE",
    "FORGET_OLDER_THAN_DAYS",
    "PROMOTE_LIMIT",
    "PROMOTE_MIN_IMPORTANCE",
    "PromotionResult",
    "SESSION_TTL_DAYS",
    "default_maintenance_callable",
    "forget_low_value_episodes",
    "promote_notable_episodes",
    "run_memory_maintenance",
    "run_session_maintenance",
]
