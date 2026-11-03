# 功能：记忆写入暂存区 + origin 门 + 批准重放
#
# 语义 写入审批：前台（用户在场）可直写；后台/无人值守与未知来源
# 一律暂存等待批准，无自动批准路径（安全失败：暂存写盘失败也不回落直写）。
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from bandi.observability.secret_mask import mask_sensitive

logger = logging.getLogger(__name__)

ORIGIN_FOREGROUND = "foreground"
ORIGIN_BACKGROUND = "background"

# 暂存根目录 → 并发锁（同目录多实例——单例重置/测试注入——也串行化 stage 临界区）
_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()

# 暂存目录 → 已分配的最大 stage 序号（进程内写序；同刻 created_at 的胜负依据）。
# Windows time.time() 粒度约 15.6ms，连续 stage 常取到同一时间戳，若按随机 id8
# 排序则「后写者存活」契约不稳定（旧记录可能留存）——序号在持目录锁时分配
# 保证严格递增，supersede 比较 (created_at, seq, id) 恒有全序。
_STAGE_SEQ: dict[str, int] = {}


def _store_dir_key(store_dir: str | Path) -> str:
    """功能：目录稳定键（并发锁与解析缓存共用；大小写不敏感文件系统归一）。"""
    try:
        return str(Path(store_dir).resolve()).lower()
    except OSError:
        return str(store_dir).lower()


def _store_lock_for(store_dir: str | Path) -> threading.RLock:
    """功能：按根目录取共享可重入锁——并发 stage+supersede 不得互删。"""
    key = _store_dir_key(store_dir)
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _STORE_LOCKS[key] = lock
        return lock


# 功能：摘要中的疑似密钥片段（sk- 前缀形态 / key=value 形态）——打码依据
_SECRET_TEXT_RE = re.compile(
    r"(?i)sk-[A-Za-z0-9_\-]{4,}"
    r"|(?:api[_-]?key|access[_-]?key|token|secret|password|credential)"
    r"\s*[:=]\s*[A-Za-z0-9_\-./]+"
)

_write_origin: ContextVar[str] = ContextVar("bandi_memory_write_origin", default=ORIGIN_FOREGROUND)


def set_write_origin(origin: str) -> Token:
    """功能：设置当前上下文的记忆写入来源（ContextVar——不随参数透传）。"""
    return _write_origin.set(str(origin or ORIGIN_FOREGROUND))


def reset_write_origin(token: Token) -> None:
    """功能：恢复写入来源上下文（与 set_write_origin 配对使用）。"""
    _write_origin.reset(token)


def get_write_origin() -> str:
    """功能：读取当前上下文的记忆写入来源（默认前台）。"""
    return _write_origin.get()


def mask_summary(text: str) -> str:
    """功能：摘要文本脱敏——掩码疑似密钥片段，其余内容保留（列表/暂存共用）。"""
    return _SECRET_TEXT_RE.sub(lambda match: mask_sensitive(match.group(0)), str(text or ""))


def _payload_item_key(payload: Any) -> tuple[str, str] | None:
    """功能：待批准 payload 的去重键（技能名 / target + 目标条目稳定 id）；无法解析 None。"""
    if not isinstance(payload, dict):
        return None
    skill_name = payload.get("skill_name")
    if isinstance(skill_name, str) and skill_name.strip():
        return "skill", skill_name.strip()
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    item_id = str(item.get("id") or item.get("item_id") or item.get("episode_id") or "").strip()
    if not item_id:
        return None
    return str(payload.get("target", "")), item_id


@dataclass
class MemoryWriteRecord:
    """功能：一条待批准的记忆写入记录（payload 为批准时重放参数）。"""

    id8: str
    subsystem: str = "memory"
    action: str = ""
    summary: str = ""
    origin: str = ORIGIN_FOREGROUND
    created_at: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)
    # 功能：同目录 stage 序号（写序总排序；created_at 同刻时由它决定 supersede 胜负）
    seq: int = 0
    # 功能：是否真实落盘（写盘失败仍返回记录但不计为已暂存）
    persisted: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id8": self.id8,
            "subsystem": self.subsystem,
            "action": self.action,
            "summary": self.summary,
            "origin": self.origin,
            "created_at": self.created_at,
            "payload": self.payload,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryWriteRecord:
        return cls(
            id8=str(data.get("id8", "")),
            subsystem=str(data.get("subsystem", "memory")),
            action=str(data.get("action", "")),
            summary=str(data.get("summary", "")),
            origin=str(data.get("origin", ORIGIN_FOREGROUND)),
            created_at=float(data.get("created_at") or 0.0),
            payload=dict(data.get("payload") or {}),
            seq=int(data.get("seq") or 0),
        )


# 目录 → {文件名: ((st_mtime_ns, st_size), 记录)}——supersede 高频扫描按文件元数据
_PARSED_CACHE: dict[str, dict[str, tuple[tuple[int, int], MemoryWriteRecord]]] = {}
_PARSED_CACHE_LOCK = threading.Lock()


class MemoryPendingStore:
    """功能：待批准队列（按子系统隔离目录；原子落盘；写盘失败不抛出——审批门安全失败）。"""

    def __init__(self, store_dir: str | Path, subsystem: str = "memory") -> None:
        self._subsystem = str(subsystem or "memory").strip() or "memory"
        self._dir = Path(store_dir) / "pending" / self._subsystem
        self._lock = _store_lock_for(self._dir)
        self._cache_key = _store_dir_key(self._dir)

    def stage(self, record_fields: dict[str, Any]) -> MemoryWriteRecord:
        """功能：暂存一条待批准写入（同目标条目去重；写盘失败返回未落盘记录）。

        写盘在短临界区内；supersede 的扫描/删除移出写锁（🟡-3）——避免
        「持锁全量扫描 pending」阻塞并发 stage（500 条积压时尤为明显）。
        """
        with self._lock:
            created = record_fields.get("created_at")
            seq = _STAGE_SEQ.get(self._cache_key, 0) + 1
            _STAGE_SEQ[self._cache_key] = seq
            record = MemoryWriteRecord(
                id8=self._new_id(),
                subsystem=self._subsystem,
                action=str(record_fields.get("action", "")),
                summary=str(record_fields.get("summary", "")),
                origin=str(record_fields.get("origin", ORIGIN_FOREGROUND)),
                created_at=float(created) if created is not None else time.time(),
                payload=dict(record_fields.get("payload") or {}),
                seq=seq,
            )
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                path = self._dir / f"{record.id8}.json"
                temp_path = path.with_suffix(path.suffix + ".tmp")
                temp_path.write_text(
                    json.dumps(record.to_dict(), ensure_ascii=False), encoding="utf-8"
                )
                temp_path.replace(path)
                record.persisted = path.exists()
                if record.persisted:
                    self._remember(record, path)
            except Exception as e:  # noqa: BLE001 — 暂存失败不得回落直写（安全失败）
                record.persisted = False
                logger.warning("memory staging write failed (id=%s): %s", record.id8, e)
        if record.persisted:
            self._supersede_duplicates(record)
        return record

    def _supersede_duplicates(self, record: MemoryWriteRecord) -> None:
        """功能：新记录落盘后作废同 target/item 的旧待批准记录（重跑/重整合不堆积）。

        🟡-3：候选扫描在写锁外，只删 `(created_at, seq, id)` 更旧的同 key 记录；
        seq 为持目录锁分配的写序——created_at 同刻（时钟粒度）时不按随机 id8 排序
        保证「后写者存活」。若扫描发现更新的同 key 记录（并发窗口内后落盘者）
        当前记录让位删除自身——同名最终恰 1 条且「新的活」。删除各自短临界区。
        """
        key = _payload_item_key(record.payload)
        if key is None:
            return
        mine = (record.created_at, record.seq, record.id8)
        for old in self.list_pending():
            if old.id8 == record.id8:
                continue
            if _payload_item_key(old.payload) != key:
                continue
            old_key = (old.created_at, old.seq, old.id8)
            if old_key > mine:
                with self._lock:
                    self.discard_pending(record.id8)
                return
            if old_key < mine:
                with self._lock:
                    self.discard_pending(old.id8)

    def list_pending(self) -> list[MemoryWriteRecord]:
        """功能：列出待批准记录（created_at 升序，同刻按 id 稳定；解析结果增量缓存）。"""
        records = self._load_records()
        records.sort(key=lambda record: (record.created_at, record.id8))
        return records

    def _load_records(self) -> list[MemoryWriteRecord]:
        """功能：读取 pending 目录全部记录——按 (mtime_ns, size) 复用已解析结果。

        记录文件一经落盘不再改写，故文件元数据命中即可跳过 JSON 重解析。
        """
        if not self._dir.exists():
            self._prune_cache(set())
            return []
        records: list[MemoryWriteRecord] = []
        present: set[str] = set()
        for path in self._dir.glob("*.json"):
            name = path.name
            present.add(name)
            try:
                stat = path.stat()
            except OSError:
                continue
            meta = (stat.st_mtime_ns, stat.st_size)
            record = self._cached(name, meta)
            if record is None:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    record = MemoryWriteRecord.from_dict(data)
                except (OSError, ValueError, TypeError) as e:
                    logger.warning("skipping unreadable pending record %s: %s", name, e)
                    continue
                self._cache(name, meta, record)
            records.append(record)
        self._prune_cache(present)
        return records

    def _cached(self, name: str, meta: tuple[int, int]) -> MemoryWriteRecord | None:
        with _PARSED_CACHE_LOCK:
            cached = _PARSED_CACHE.get(self._cache_key, {}).get(name)
        if cached is not None and cached[0] == meta:
            return cached[1]
        return None

    def _cache(self, name: str, meta: tuple[int, int], record: MemoryWriteRecord) -> None:
        with _PARSED_CACHE_LOCK:
            _PARSED_CACHE.setdefault(self._cache_key, {})[name] = (meta, record)

    def _remember(self, record: MemoryWriteRecord, path: Path) -> None:
        """功能：刚落盘记录直接写入缓存（避免随后扫描重读自己）。"""
        try:
            stat = path.stat()
        except OSError:
            return
        self._cache(path.name, (stat.st_mtime_ns, stat.st_size), record)

    def _forget(self, name: str) -> None:
        with _PARSED_CACHE_LOCK:
            cache = _PARSED_CACHE.get(self._cache_key)
            if cache is not None:
                cache.pop(name, None)

    def _prune_cache(self, present: set[str]) -> None:
        with _PARSED_CACHE_LOCK:
            cache = _PARSED_CACHE.get(self._cache_key)
            if cache is None:
                return
            for name in [key for key in cache if key not in present]:
                cache.pop(name, None)

    def get_pending(self, id8: str) -> MemoryWriteRecord | None:
        path = self._path(id8)
        if path is None or not path.exists():
            return None
        try:
            return MemoryWriteRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def purge_expired(self, max_age_days: float = 30.0) -> int:
        """（4）：清理超龄待批准记录（不同 episode 的晋升键互不相同，未批
        记录永久留盘且占用晋升窗口造成饿死）。返回清理数。"""
        cutoff = time.time() - max(0.0, float(max_age_days)) * 86400
        removed = 0
        for record in self.list_pending():
            if float(record.created_at or 0.0) < cutoff and self.discard_pending(record.id8):
                removed += 1
        return removed

    def discard_pending(self, id8: str) -> bool:
        path = self._path(id8)
        if path is None or not path.exists():
            return False
        try:
            path.unlink()
        except OSError as e:
            logger.warning("memory pending discard failed %s: %s", id8, e)
            return False
        self._forget(path.name)
        return True

    def update_payload(self, id8: str, payload: dict[str, Any]) -> bool:
        """功能：更新暂存记录 payload（Phase2 上报意见存档用）——幂等覆写。"""
        record = self.get_pending(id8)
        if record is None:
            return False
        path = self._path(id8)
        if path is None:
            return False
        record.payload = dict(payload)
        # 原子写盘——_remember 只更新缓存不落盘
        # 而 get_pending 直读文件，漏写盘 = 更新静默丢失
        try:
            temp_path = path.with_suffix(path.suffix + ".tmp")
            temp_path.write_text(json.dumps(record.to_dict(), ensure_ascii=False), encoding="utf-8")
            temp_path.replace(path)
        except OSError as e:
            logger.warning("memory pending update failed %s: %s", id8, e)
            return False
        self._remember(record, path)
        return True

    def pending_count(self) -> int:
        if not self._dir.exists():
            return 0
        return sum(1 for _ in self._dir.glob("*.json"))

    def _new_id(self) -> str:
        """功能：生成可排序记录 id——时间前缀（跨进程同刻时判定后写者
        原纯随机 id 在 Windows 时钟粒度下无法排序）+ 随机后缀（唯一兜底）。"""
        for _ in range(5):
            candidate = f"{time.time_ns():016x}{uuid4().hex[:8]}"
            if not (self._dir / f"{candidate}.json").exists():
                return candidate
        return f"{time.time_ns():016x}{uuid4().hex[:8]}"

    def _path(self, id8: str) -> Path | None:
        """功能：id → 文件路径（拒绝路径穿越/空 id）。"""
        safe = str(id8 or "").strip()
        if not safe or "/" in safe or "\\" in safe or ".." in safe:
            return None
        return self._dir / f"{safe}.json"


class MemoryWriteGate:
    """功能：记忆写入 origin 门——无人值守（background）与未知来源一律暂存。"""

    @staticmethod
    def evaluate(origin: str, subsystem: str = "memory") -> str:
        if str(origin or "").strip().lower() == ORIGIN_FOREGROUND:
            return "allow"
        return "stage"


_pending_store: MemoryPendingStore | None = None
_skill_pending_store: MemoryPendingStore | None = None
_pending_store_lock = threading.Lock()


def default_memory_store_dir() -> Path:
    """功能：默认记忆目录（env 可覆盖；接线暂存与 API 单例共用同一解析）。"""
    override = os.environ.get("BANDI_MEMORY_PENDING_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".bandi" / "memory"


def get_pending_store() -> MemoryPendingStore:
    """功能：进程级记忆待批准队列单例（写入接线与审批端点共用）。"""
    global _pending_store
    with _pending_store_lock:
        if _pending_store is None:
            _pending_store = MemoryPendingStore(default_memory_store_dir())
    return _pending_store


def get_skill_pending_store() -> MemoryPendingStore:
    """功能：进程级技能待批准队列单例（子系统 skills，与记忆队列目录隔离）。"""
    global _skill_pending_store
    with _pending_store_lock:
        if _skill_pending_store is None:
            _skill_pending_store = MemoryPendingStore(
                default_memory_store_dir(), subsystem="skills"
            )
    return _skill_pending_store


def reset_pending_store(store: MemoryPendingStore | None = None) -> None:
    """功能：测试钩子——重置/注入进程级待批准队列单例（记忆+技能子系统）。"""
    global _pending_store, _skill_pending_store
    with _pending_store_lock:
        _pending_store = store
        _skill_pending_store = None


def reset_skill_pending_store(store: MemoryPendingStore | None = None) -> None:
    """功能：测试钩子——重置/注入进程级技能待批准队列单例。"""
    global _skill_pending_store
    with _pending_store_lock:
        _skill_pending_store = store


@contextmanager
def scope_miss_suppressed(
    *, project_id: str | None = None, employee_id: str | None = None
) -> Iterator[None]:
    """功能：抑制本次直写触发的「漏传维度」计数——重放/维护借还净零。

    优先使用 long_term 提供的 `suppress_scope_miss` 上下文（接口冻结）；其未提供
    或签名不匹配时按预期增量借还：写入前预扣、未写入时归还（净零不污染观测）。
    """
    suppressor = None
    try:
        from bandi.memory.long_term import suppress_scope_miss

        suppressor = suppress_scope_miss
    except ImportError:
        suppressor = None
    if suppressor is not None:
        try:
            cm = suppressor()
        except TypeError:
            cm = None
        if cm is not None:
            with cm:
                yield
            return
    try:
        from bandi.memory.long_term import _apply_scope_miss_delta
    except ImportError:  # 接口变更时退化为不调整（行为与直写一致）
        yield
        return
    expected = {
        "project_id": 0 if project_id else 1,
        "employee_id": 0 if employee_id else 1,
    }
    for name, count in expected.items():
        if count:
            _apply_scope_miss_delta(name, -count)
    written = False
    try:
        yield
        written = True
    finally:
        if not written:
            for name, count in expected.items():
                if count:
                    _apply_scope_miss_delta(name, count)


def stage_write(*, action: str, summary: str, payload: dict[str, Any], origin: str) -> bool:
    """功能：把一次记忆写入排入暂存区（失败不抛出，返回是否落盘成功）。"""
    try:
        record = get_pending_store().stage(
            {"action": action, "summary": summary, "origin": origin, "payload": payload}
        )
        return bool(getattr(record, "persisted", False))
    except Exception as e:  # noqa: BLE001 — 暂存失败必须安全失败（由调用方不落库）
        logger.warning("memory staging failed (action=%s): %s", action, e)
        return False


def stage_long_term_write(
    item: Any,
    *,
    project_id: str | None,
    employee_id: str | None,
    action: str,
    summary: str,
    origin: str,
    conflict: bool = False,
) -> bool:
    """功能：把一次 long_term 写入排入暂存区（payload 可在批准时原样重放）。

    draft 元数据（草稿类型 + 写入目标）供消息中心多标签结构化展示。
    """
    return stage_write(
        action=action,
        summary=summary,
        origin=origin,
        payload={
            "target": "long_term",
            "item": item.to_dict(),
            "project_id": project_id,
            "employee_id": employee_id,
            "conflict": bool(conflict),
            "draft": {
                "kind": "冲突裁决" if conflict else "后台直写暂存",
                "target": "长期记忆",
            },
        },
    )


def replay_record(
    record: MemoryWriteRecord,
    *,
    long_term: Any | None = None,
    episodic: Any | None = None,
    markdown: Any | None = None,
) -> bool:
    """功能：批准重放一条暂存写入——复用目标 store 公开 API（不绕过写入校验）。"""
    payload = record.payload or {}
    target = str(payload.get("target", ""))
    if target == "long_term":
        if long_term is None:
            return False
        from bandi.memory.long_term import MemoryItem

        item = MemoryItem.from_dict(dict(payload.get("item") or {}))
        with scope_miss_suppressed(
            project_id=payload.get("project_id"), employee_id=payload.get("employee_id")
        ):
            long_term.store(
                item,
                project_id=payload.get("project_id"),
                employee_id=payload.get("employee_id"),
            )
        return True
    if target == "episodic":
        if episodic is None:
            return False
        from bandi.memory.episodic import Episode

        stored = episodic.record(Episode(**dict(payload.get("item") or {})))
        return bool(getattr(stored, "episode_id", ""))
    if target == "markdown":
        if markdown is None:
            return False
        markdown.save_visible_text(str(payload.get("content", "")))
        return True
    return False


def replay_skill_record(record: MemoryWriteRecord, *, skills_dir: str | Path | None = None) -> bool:
    """功能：批准重放一条技能暂存写入——原子落盘 SKILL.md（失败 False 保留记录）。"""
    payload = record.payload or {}
    skill_name = str(payload.get("skill_name") or "").strip()
    content = str(payload.get("content") or "")
    if not skill_name or not content.strip():
        return False
    from bandi.skills.pending_apply import default_skills_dir, write_skill_md

    root = Path(skills_dir) if skills_dir else default_skills_dir()
    return write_skill_md(root, skill_name, content)
