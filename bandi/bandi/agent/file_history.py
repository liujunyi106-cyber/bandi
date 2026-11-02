# 功能：文件级快照回滚——编辑前按需备份 + turn 语义回滚。
# 即对每个文件恢复「turn >= T 的最早版本」（该版本内容 = T 轮开始前状态）。
from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

MAX_VERSIONS_PER_SESSION = 200
_INDEX_FILENAME = "history.json"
_INDEX_CORRUPT_SUFFIX = ".corrupt"


@dataclass(frozen=True)
class FileHistoryVersion:
    # 功能：一条版本记录（backup_name=None 表示该版本文件不存在——回滚时删除）
    path: str
    turn_index: int
    version: int
    backup_name: str | None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    # 触发本次编辑的工具调用 id（快照归属锚点；空 = 旧数据/无上下文）
    source_id: str = ""
    # 写入完成后的 mtime（纳秒）——回滚前比对，检出「用户手改」并拒绝覆盖
    post_mtime_ns: int = 0


@dataclass(frozen=True)
class RewindAction:
    # 功能：单文件回滚动作（restore / delete / skip / unavailable / modified）
    path: str
    action: str
    backup_name: str | None = None


@dataclass(frozen=True)
class RewindReport:
    # 功能：回滚报告（changed = 真实发生 restore/delete 的路径）
    session_id: str
    turn_index: int
    dry_run: bool
    actions: list[RewindAction]

    @property
    def changed(self) -> list[str]:
        return [a.path for a in self.actions if a.action in ("restore", "delete")]


class FileHistoryStore:
    # 功能：文件历史存储——record_edit 备份 / plan_rewind 计划 / rewind 执行

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir: Path | None = Path(base_dir) if base_dir is not None else None

    @property
    def base_dir(self) -> Path:
        # 功能：基目录惰性求值（默认 ~/.bandi/file-history；运行期求值防测试污染）
        if self._base_dir is None:
            self._base_dir = Path.home() / ".bandi" / "file-history"
        return self._base_dir

    def _session_dir(self, session_id: str) -> Path:
        # 功能：会话目录（sha256 摘要——company 会话 id 含 :: 等不安全字符）
        digest = sha256(session_id.encode("utf-8")).hexdigest()[:16]
        return self.base_dir / digest

    def record_edit(
        self, path: str | Path, session_id: str, turn_index: int, source_id: str = ""
    ) -> FileHistoryVersion | None:
        """功能：编辑前记录版本——同 (path, turn) 幂等（v1 不被后续编辑覆盖）；
        文件不存在记 backup_name=None（删除标记）；备份失败返回 None 不记（防误删语义）。"""
        if not session_id:
            return None
        target = Path(path)
        resolved = str(target)
        session_dir = self._session_dir(session_id)
        versions = self._load_versions(session_dir)
        for entry in versions:
            if entry.path == resolved and entry.turn_index == turn_index:
                return entry

        next_version = max((e.version for e in versions if e.path == resolved), default=0) + 1
        backup_name: str | None = None
        if target.is_file():
            digest = sha256(resolved.encode("utf-8")).hexdigest()[:16]
            backup_name = f"{digest}@v{next_version}"
            try:
                session_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, session_dir / backup_name)
            except OSError as exc:
                logger.warning("file_history backup failed (%s): %s", resolved, exc)
                return None

        entry = FileHistoryVersion(
            path=resolved,
            turn_index=turn_index,
            version=next_version,
            backup_name=backup_name,
            source_id=str(source_id or ""),
        )
        versions.append(entry)
        dropped = self._prune(versions, session_dir)
        if dropped:
            logger.warning(
                "file_history prune dropped %d old version(s) for session=%s (limit=%d)",
                dropped,
                session_id,
                MAX_VERSIONS_PER_SESSION,
            )
        self._save_versions(session_dir, session_id, versions)
        return entry

    def mark_written(
        self, path: str | Path, session_id: str, turn_index: int, source_id: str = ""
    ) -> bool:
        """功能：写入完成后记录 mtime（回滚前比对的期望值）；无匹配版本返回 False。"""
        if not session_id:
            return False
        target = Path(path)
        resolved = str(target)
        try:
            mtime_ns = target.stat().st_mtime_ns
        except OSError:
            return False
        session_dir = self._session_dir(session_id)
        versions = self._load_versions(session_dir)
        exact = -1
        latest = -1
        for index in range(len(versions) - 1, -1, -1):
            entry = versions[index]
            if entry.path != resolved or entry.turn_index != turn_index:
                continue
            if latest < 0:
                latest = index
            if not source_id or not entry.source_id or entry.source_id == source_id:
                exact = index
                break
        target_index = exact if exact >= 0 else latest
        if target_index < 0:
            return False
        versions[target_index] = replace(versions[target_index], post_mtime_ns=mtime_ns)
        self._save_versions(session_dir, session_id, versions)
        return True

    def versions(self, session_id: str) -> list[FileHistoryVersion]:
        return self._load_versions(self._session_dir(session_id))

    def plan_rewind(self, session_id: str, turn_index: int) -> list[RewindAction]:
        """功能：计算回滚动作——每个路径取 turn>=T 的最早版本；无候选不动。"""
        session_dir = self._session_dir(session_id)
        by_path: dict[str, list[FileHistoryVersion]] = {}
        for entry in self._load_versions(session_dir):
            by_path.setdefault(entry.path, []).append(entry)

        actions: list[RewindAction] = []
        for path in sorted(by_path):
            candidates = [e for e in by_path[path] if e.turn_index >= turn_index]
            if not candidates:
                continue
            target = min(candidates, key=lambda e: (e.turn_index, e.version))
            if target.backup_name is None:
                action = "delete" if Path(path).exists() else "skip"
            else:
                backup_path = session_dir / target.backup_name
                if not backup_path.is_file():
                    action = "unavailable"
                elif self._user_modified_since(target):
                    action = "modified"
                elif self._content_differs(Path(path), backup_path):
                    action = "restore"
                else:
                    action = "skip"
            actions.append(RewindAction(path=path, action=action, backup_name=target.backup_name))
        return actions

    def rewind(self, session_id: str, turn_index: int, *, dry_run: bool = False) -> RewindReport:
        # 功能：执行回滚（dry_run 只计算不动盘；单文件失败记 warning 不中断其余）
        session_dir = self._session_dir(session_id)
        actions = self.plan_rewind(session_id, turn_index)
        if not dry_run:
            for action in actions:
                try:
                    if action.action == "restore" and action.backup_name:
                        self._restore(Path(action.path), session_dir / action.backup_name)
                    elif action.action == "delete":
                        Path(action.path).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("file_history rewind failed (%s): %s", action.path, exc)
        return RewindReport(
            session_id=session_id, turn_index=turn_index, dry_run=dry_run, actions=actions
        )

    @staticmethod
    def _restore(target: Path, backup_path: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_path, target)

    @staticmethod
    def _content_differs(file_path: Path, backup_path: Path) -> bool:
        try:
            current = file_path.stat()
            backup = backup_path.stat()
        except OSError:
            return True
        if current.st_size != backup.st_size:
            return True
        try:
            return file_path.read_bytes() != backup_path.read_bytes()
        except OSError:
            return True

    @staticmethod
    def _user_modified_since(entry: FileHistoryVersion) -> bool:
        # 功能：写入后 mtime 与快照期望值不一致 = 用户/AI 之外的改动，回滚应让路
        if not entry.post_mtime_ns:
            return False
        try:
            return Path(entry.path).stat().st_mtime_ns != entry.post_mtime_ns
        except OSError:
            return False

    @staticmethod
    def _prune(versions: list[FileHistoryVersion], session_dir: Path) -> int:
        # 功能：超上限裁最旧条目并删除对应备份文件；返回淘汰条数（调用方入日志）
        if len(versions) <= MAX_VERSIONS_PER_SESSION:
            return 0
        removed = versions[: len(versions) - MAX_VERSIONS_PER_SESSION]
        del versions[: len(removed)]
        for entry in removed:
            if entry.backup_name:
                try:
                    (session_dir / entry.backup_name).unlink(missing_ok=True)
                except OSError:
                    pass
        return len(removed)

    @staticmethod
    def _load_versions(session_dir: Path) -> list[FileHistoryVersion]:
        # 功能：读版本索引；损坏时隔离为 .corrupt 并重建空（不阻断记录）
        index_path = session_dir / _INDEX_FILENAME
        if not index_path.is_file():
            return []
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            raw = data.get("versions", [])
            if not isinstance(raw, list):
                raise ValueError("versions must be a list")
            return [
                FileHistoryVersion(
                    path=str(item["path"]),
                    turn_index=int(item["turn_index"]),
                    version=int(item["version"]),
                    backup_name=item.get("backup_name"),
                    created_at=str(item.get("created_at", "")),
                    source_id=str(item.get("source_id", "") or ""),
                    post_mtime_ns=int(item.get("post_mtime_ns", 0) or 0),
                )
                for item in raw
            ]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("file_history index corrupt (%s): %s", session_dir, exc)
            try:
                index_path.replace(index_path.with_name(index_path.name + _INDEX_CORRUPT_SUFFIX))
            except OSError:
                pass
            return []

    @staticmethod
    def _save_versions(
        session_dir: Path, session_id: str, versions: list[FileHistoryVersion]
    ) -> None:
        # 功能：原子写索引（tmp + os.replace，失败清理）
        session_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session_id,
            "versions": [
                {
                    "path": v.path,
                    "turn_index": v.turn_index,
                    "version": v.version,
                    "backup_name": v.backup_name,
                    "created_at": v.created_at,
                    "source_id": v.source_id,
                    "post_mtime_ns": v.post_mtime_ns,
                }
                for v in versions
            ],
        }
        tmp = session_dir / f".{_INDEX_FILENAME}.tmp-{os.getpid()}-{uuid4().hex[:8]}"
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, session_dir / _INDEX_FILENAME)
        except (OSError, ValueError):
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


_store: FileHistoryStore | None = None


def get_file_history_store() -> FileHistoryStore:
    # 功能：进程级单例（默认基目录 ~/.bandi/file-history）
    global _store
    if _store is None:
        _store = FileHistoryStore()
    return _store


def reset_file_history_store(base_dir: str | Path | None = None) -> None:
    # 功能：重置单例（测试隔离/换基目录；None=清空待下次惰性重建）
    global _store
    _store = FileHistoryStore(base_dir) if base_dir is not None else None


def record_file_edit(path: str | Path, context: Any) -> FileHistoryVersion | None:
    """功能：工具写前备份入口（best-effort）——读 context.extra 的
    session_id/turn_index/tool_call_id；缺失或失败静默跳过（不阻断编辑主流程）。"""
    extra = getattr(context, "extra", None) or {}
    session_id = str(extra.get("session_id") or "")
    turn_index = extra.get("turn_index")
    if not session_id or not isinstance(turn_index, int):
        return None
    try:
        return get_file_history_store().record_edit(
            path, session_id, turn_index, source_id=str(extra.get("tool_call_id") or "")
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("file_history record failed for %s: %s", path, exc)
        return None


def record_file_written(path: str | Path, context: Any) -> bool:
    """功能：工具写后登记 mtime（回滚前比对用）——同 read 上下文，best-effort。"""
    extra = getattr(context, "extra", None) or {}
    session_id = str(extra.get("session_id") or "")
    turn_index = extra.get("turn_index")
    if not session_id or not isinstance(turn_index, int):
        return False
    try:
        return get_file_history_store().mark_written(
            path, session_id, turn_index, source_id=str(extra.get("tool_call_id") or "")
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("file_history mark_written failed for %s: %s", path, exc)
        return False
