# 功能：项目级文件读取缓存（stat mtime+size 判定，热重载免显式失效）——每轮
# run_agent_turn 重建 ContextLoader 时避免 AGENTS.md/CLAUDE.md/重复读盘
from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 128


class ProjectFileCache:
    """进程级文件文本缓存。

    判定键 = (mtime_ns, size)：任一变化即视为文件已修改并重新读取（热重载）
    未变化则直接返回缓存文本（零磁盘 IO）。文件不存在缓存为 None（可区分
    「未缓存」与「缓存了不存在」，避免出现后误判）。
    """

    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        self._max_entries = max_entries
        self._entries: dict[str, tuple[tuple[int, int] | None, str | None]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _stat_key(path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def read_text(self, path: str | Path) -> str | None:
        """按 mtime+size 缓存读取 UTF-8 文本；文件不存在返回 None。"""
        p = Path(path)
        key = str(p.resolve())
        stat_key = self._stat_key(p)
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and cached[0] == stat_key:
                return cached[1]
            if stat_key is None:
                self._entries[key] = (None, None)
                return None
        # 读盘在锁外执行（避免锁内 IO）；竞争窗口内重复读可接受（幂等覆盖）
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("project file cache read failed %s: %s", p, e)
            with self._lock:
                self._entries[key] = (stat_key, None)
            return None
        with self._lock:
            self._entries[key] = (stat_key, text)
            self._evict_locked(key)
        return text

    def _evict_locked(self, fresh_key: str) -> None:
        if len(self._entries) <= self._max_entries:
            return
        # 简单 LRU 近似：溢出时按插入序清最旧一半（fresh_key 已在 dict 中保持新序）
        excess = len(self._entries) - self._max_entries
        for k in list(self._entries):
            if excess <= 0:
                break
            if k != fresh_key:
                self._entries.pop(k, None)
                excess -= 1

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_instance: ProjectFileCache | None = None
_instance_lock = threading.Lock()


def get_project_file_cache() -> ProjectFileCache:
    """模块级单例——跨 ContextLoader/pipeline 实例共享（每轮新建仍命中）。"""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = ProjectFileCache()
    return _instance


__all__ = ["ProjectFileCache", "get_project_file_cache"]
