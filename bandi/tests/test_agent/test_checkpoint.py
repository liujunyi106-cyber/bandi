import tempfile

from bandi.agent.checkpoint import Checkpoint, CheckpointStore


class TestCheckpointStore:
    def setup_method(self) -> None:
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        self.store = CheckpointStore(db_path=f.name)
        self._path = f.name

    def teardown_method(self) -> None:
        import os

        self.store.close()
        os.unlink(self._path)

    def test_save_and_load(self) -> None:
        cp = Checkpoint(session_id="session1", turn_index=5, state_json='{"key": "val"}')
        self.store.save(cp)
        loaded = self.store.load(cp.id)
        assert loaded is not None
        assert loaded.session_id == "session1"
        assert loaded.turn_index == 5

    def test_list_by_session(self) -> None:
        for i in range(3):
            self.store.save(Checkpoint(session_id="session1", turn_index=i))
        checkpoints = self.store.list_by_session("session1")
        assert len(checkpoints) == 3
        assert checkpoints[0]["turn_index"] == 0

    def test_load_nonexistent(self) -> None:
        loaded = self.store.load("nonexistent")
        assert loaded is None

    def test_list_by_session_tie_breaks_by_created_at(self) -> None:
        """
        (session_id, turn_index) 唯一约束后同键只保留最新一行（C2a UNIQUE 语义）。"""
        cp1 = Checkpoint(session_id="s1", turn_index=3, state_json="old")
        cp2 = Checkpoint(session_id="s1", turn_index=3, state_json="new")
        self.store.save(cp1)
        self.store.save(cp2)
        rows = self.store.list_by_session("s1")
        assert len(rows) == 1, "同 (session,turn) 必须唯一（C2a 约束）"
        latest = self.store.load(rows[-1]["id"])
        assert latest is not None and latest.state_json == "new", "同键必须最新覆盖"

    def test_multirun_recovery_picks_latest_cumulative(self) -> None:
        """
        排序后恢复选中第一轮旧状态 turn=10）。"""
        from datetime import UTC, datetime, timedelta

        base = datetime.now(UTC)
        for i, (turn, age) in enumerate([(1, 10), (5, 5), (10, 2), (11, 1)]):
            self.store.save(
                Checkpoint(
                    session_id="multi",
                    turn_index=turn,
                    state_json=f"state-{turn}",
                    created_at=(base - timedelta(seconds=age)).isoformat(),
                )
            )
        rows = self.store.list_by_session("multi")
        assert rows[-1]["turn_index"] == 11, "恢复点必须选到最新累计轮次"
