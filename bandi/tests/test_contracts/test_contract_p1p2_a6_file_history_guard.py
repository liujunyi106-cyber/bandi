"""契约：文件快照回滚前校验「写入后 mtime」，用户手改不被覆盖；淘汰有日志。

旧行为 rewind 只比 size+bytes，外部（用户/编辑器）改过的文件会被静默覆盖成备份；
现行为编辑工具写后登记 mtime，回滚时不一致即判 `modified` 跳过，并在淘汰时写日志。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from bandi.agent.file_history import MAX_VERSIONS_PER_SESSION, FileHistoryStore


def _store(tmp_path: Path) -> FileHistoryStore:
    return FileHistoryStore(tmp_path / "file-history")


def test_rewind_refuses_to_overwrite_user_modified_file(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("v1", encoding="utf-8")
    store = _store(tmp_path)

    assert store.record_edit(target, "s1", 0, source_id="call-1") is not None
    target.write_text("agent-edit", encoding="utf-8")
    assert store.mark_written(target, "s1", 0, source_id="call-1") is True

    target.write_text("user-edit", encoding="utf-8")
    stamped = os.stat(target).st_mtime_ns + 1_000_000_000
    os.utime(target, ns=(stamped, stamped))

    actions = {a.path: a.action for a in store.plan_rewind("s1", 0)}
    assert actions[str(target)] == "modified", f"用户手改必须被检出：{actions}"

    store.rewind("s1", 0)
    assert target.read_text(encoding="utf-8") == "user-edit", "手改内容不得被覆盖"


def test_rewind_restores_when_snapshot_mtime_matches(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("v1", encoding="utf-8")
    store = _store(tmp_path)

    store.record_edit(target, "s1", 0, source_id="call-1")
    target.write_text("agent-edit", encoding="utf-8")
    assert store.mark_written(target, "s1", 0, source_id="call-1") is True

    actions = {a.path: a.action for a in store.plan_rewind("s1", 0)}
    assert actions[str(target)] == "restore"

    store.rewind("s1", 0)
    assert target.read_text(encoding="utf-8") == "v1"


def test_source_id_is_persisted_with_version(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("v1", encoding="utf-8")
    store = _store(tmp_path)

    store.record_edit(target, "s1", 0, source_id="call-xyz")

    version = store.versions("s1")[0]
    assert version.source_id == "call-xyz"


def test_prune_logs_dropped_versions(tmp_path: Path, caplog) -> None:
    target = tmp_path / "note.txt"
    target.write_text("v", encoding="utf-8")
    store = _store(tmp_path)

    with caplog.at_level(logging.WARNING, logger="bandi.agent.file_history"):
        for turn in range(MAX_VERSIONS_PER_SESSION + 1):
            store.record_edit(target, "s1", turn)

    assert len(store.versions("s1")) == MAX_VERSIONS_PER_SESSION
    assert any("file_history prune dropped" in r.getMessage() for r in caplog.records), (
        "淘汰必须可观测（旧实现静默删除）"
    )


def test_multi_edit_same_turn_keeps_rewind_available(tmp_path: Path) -> None:
    """同一轮同一文件二次编辑：写后登记应落到最新版本，而不是把回滚误判成「用户手改」。"""
    target = tmp_path / "note.txt"
    target.write_text("v1", encoding="utf-8")
    store = _store(tmp_path)

    store.record_edit(target, "s1", 0, source_id="call-1")
    target.write_text("edit-1", encoding="utf-8")
    assert store.mark_written(target, "s1", 0, source_id="call-1") is True

    # 第二次工具调用（同轮、同文件、不同 tool_call_id）
    target.write_text("edit-2", encoding="utf-8")
    assert store.mark_written(target, "s1", 0, source_id="call-2") is True

    actions = {a.path: a.action for a in store.plan_rewind("s1", 0)}
    assert actions[str(target)] == "restore", f"同轮二次编辑后回滚不应被误判：{actions}"
    store.rewind("s1", 0)
    assert target.read_text(encoding="utf-8") == "v1"
