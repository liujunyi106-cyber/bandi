# 功能：Checkpoint 每步持久化 — checkpoint entry 写入 session_entries
from __future__ import annotations

import os
import tempfile

from bandi.memory.session.store import SessionStore


class TestCheckpointEntry:
    def setup_method(self) -> None:
        tmp = tempfile.mktemp(suffix=".db")
        self.store = SessionStore(tmp)
        self._path = tmp

    def teardown_method(self) -> None:
        self.store.close()
        if os.path.exists(self._path):
            os.unlink(self._path)

    def test_save_and_list_checkpoint_entry(self) -> None:
        sid = self.store.create_session("s1")
        self.store.save_checkpoint_entry(sid, turn_index=3)
        self.store.save_checkpoint_entry(sid, turn_index=7)
        cps = self.store.list_checkpoint_entries(sid)
        assert len(cps) == 2
        assert cps[0]["turn_index"] == 3
        assert cps[1]["turn_index"] == 7

    def test_checkpoint_entry_has_type_marker(self) -> None:
        sid = self.store.create_session("s2")
        self.store.save_checkpoint_entry(sid, turn_index=1)
        entries = self.store.get_branch(sid)
        cp_entries = [e for e in entries if e["entry_type"] == "checkpoint"]
        assert len(cp_entries) == 1
        assert cp_entries[0]["turn_index"] == 1

    def test_save_checkpoint_entry_noop_for_missing_session(self) -> None:
        """对不存在的 session 写入 checkpoint entry 不得抛异常。"""
        self.store.save_checkpoint_entry("nonexistent", turn_index=1)
