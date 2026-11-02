# 功能：契约——T-smoke-5 checkpoint 单库卫生（每会话保留最近 N 轮存档，防全局单库无限增长）。
# 钉行为不钉实现：超量写入后每会话恰保留最近 MAX_CHECKPOINTS_PER_SESSION 条（最早淘汰）
# 最新存档可读回（断点恢复主链不受影响）、会话间互不影响（破坏者：裁剪不得跨会话误伤）
# 上限常量可运行时配置。
from __future__ import annotations

from pathlib import Path

from bandi.agent import checkpoint as checkpoint_module
from bandi.agent.checkpoint import Checkpoint, CheckpointStore


def _store(tmp_path: Path) -> CheckpointStore:
    return CheckpointStore(db_path=str(tmp_path / "checkpoints.db"))


# 超量写入保留最近 N 条，最早档被清；最新档可读回
def test_retention_keeps_most_recent_checkpoints(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(checkpoint_module, "MAX_CHECKPOINTS_PER_SESSION", 3)
    store = _store(tmp_path)
    try:
        for turn in range(5):
            store.save(
                Checkpoint(session_id="s1", turn_index=turn, state_json="{}", messages_json="")
            )

        rows = store.list_by_session("s1")
        assert [row["turn_index"] for row in rows] == [2, 3, 4]
        latest = rows[-1]
        loaded = store.load(str(latest["id"]))
        assert loaded is not None and loaded.turn_index == 4
    finally:
        store.close()


# 破坏者：裁剪按会话隔离——另一会话的存档不受影响
def test_retention_is_per_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(checkpoint_module, "MAX_CHECKPOINTS_PER_SESSION", 2)
    store = _store(tmp_path)
    try:
        for turn in range(4):
            store.save(
                Checkpoint(session_id="busy", turn_index=turn, state_json="{}", messages_json="")
            )
        store.save(Checkpoint(session_id="quiet", turn_index=0, state_json="{}", messages_json=""))

        assert [row["turn_index"] for row in store.list_by_session("busy")] == [2, 3]
        assert [row["turn_index"] for row in store.list_by_session("quiet")] == [0]
    finally:
        store.close()


# 未超限时不裁剪（上限内逐条完整保留）
def test_below_cap_keeps_all(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(checkpoint_module, "MAX_CHECKPOINTS_PER_SESSION", 10)
    store = _store(tmp_path)
    try:
        for turn in range(3):
            store.save(
                Checkpoint(session_id="s3", turn_index=turn, state_json="{}", messages_json="")
            )
        assert [row["turn_index"] for row in store.list_by_session("s3")] == [0, 1, 2]
    finally:
        store.close()
