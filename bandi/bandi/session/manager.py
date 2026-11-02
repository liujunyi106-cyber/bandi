# 功能：会话管理（SessionManager 会话生命周期/存档 + Fork/Revert 三阶段）
from __future__ import annotations

import json
from typing import Any

from bandi.memory.session.store import SessionStore, _safe_json_load

_REVERT_KEY = "revert_state"
_REVERT_OP_KEY = "last_revert_operation_id"
# 功能：fork/compact 等派生会话继承的来源元数据键（项目归属与来源口径）
INHERITED_METADATA_KEYS = ("project_root", "project_id", "origin")


class SessionManager:
    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.session_id: str = ""
        self.last_operation_id: str = ""

    def fork(self, entry_id: str) -> str:
        # fork 可指向边界之前的任意消息——必须取全量历史
        entries = self.store.get_branch(self.session_id, full=True)
        split_idx = next((i for i, e in enumerate(entries) if e["id"] == entry_id), -1)
        if split_idx == -1:
            raise ValueError(f"Entry {entry_id} not found in session {self.session_id}")

        source = self.store.get_session(self.session_id) or {}
        source_meta = _safe_json_load(source.get("metadata"), default={})
        if not isinstance(source_meta, dict):
            source_meta = {}
        metadata: dict[str, Any] = {
            "parent_session_id": self.session_id,
            "forked_from_entry": entry_id,
        }
        for key in INHERITED_METADATA_KEYS:
            value = source_meta.get(key)
            if value:
                metadata[key] = value

        new_sid = self.store.create_session(
            title=f"forked from {self.session_id[:8]}",
            mode="execute",
        )
        self.store.update_session(
            new_sid,
            metadata=json.dumps(metadata, ensure_ascii=False),
        )
        self.store.copy_entries(entries[: split_idx + 1], new_sid)
        return new_sid

    def stage(self, entry_id: str) -> None:
        """Stage：暂存恢复点（软标记，持久化到 sessions 元数据，不删任何数据）。
        -3：entry_id 为空串 = 暂存「清空全部」（编辑首条消息的从此处重开）。"""
        if entry_id:
            self._assert_entry_exists(entry_id)
        self._write_metadata({_REVERT_KEY: entry_id})

    def commit(self) -> int:
        """Commit：兑现恢复点——截断到暂存的恢复点并归档，支持一次撤销。"""
        session = self.store.get_session(self.session_id)
        metadata = _safe_json_load(session.get("metadata") if session else None, default={})
        entry_id = metadata.get(_REVERT_KEY)
        if entry_id is None:
            raise ValueError("no staged revert point")
        removed, operation_id = self.store.truncate_branch_archived(self.session_id, entry_id)
        self.last_operation_id = operation_id
        self._write_metadata(
            {
                _REVERT_KEY: None,
                _REVERT_OP_KEY: operation_id or None,
            }
        )
        return removed

    def undo(self) -> int:
        """功能：撤销上一次 Commit 的截断（按归档恢复；无归档时返回 0）"""
        session = self.store.get_session(self.session_id)
        metadata = _safe_json_load(session.get("metadata") if session else None, default={})
        operation_id = str(metadata.get(_REVERT_OP_KEY) or "").strip()
        if not operation_id:
            return 0
        restored = self.store.restore_archived(self.session_id, operation_id)
        self._write_metadata({_REVERT_OP_KEY: None})
        return restored

    def clear(self) -> None:
        """Clear：撤销暂存恢复点，数据全量保留；无恢复点时幂等无操作。"""
        self._write_metadata({_REVERT_KEY: None})

    def _assert_entry_exists(self, entry_id: str) -> None:
        entries = self.store.get_branch(self.session_id, full=True)
        if not any(e["id"] == entry_id for e in entries):
            raise ValueError(f"Entry {entry_id} not found in session {self.session_id}")

    def _write_metadata(self, patch: dict[str, Any], _retries: int = 3) -> None:
        """读-改-写 sessions.metadata（CAS）：保留既有键，patch 值为 None 删除。
        跨进程并发 stage 经 update_session_if_metadata 原子认领——冲突重读重试
        耗尽抛错。"""
        for _ in range(_retries):
            session = self.store.get_session(self.session_id)
            if session is None:
                raise ValueError(f"Session {self.session_id} not found")
            expected = _safe_json_load(session.get("metadata"), default={})
            metadata = dict(expected)
            for key, value in patch.items():
                if value is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = value
            if self.store.update_session_if_metadata(self.session_id, metadata, expected):
                return
        raise RuntimeError(
            f"session {self.session_id} metadata concurrent update conflict "
            f"after {_retries} retries"
        )
