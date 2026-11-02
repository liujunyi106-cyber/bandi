from __future__ import annotations

from typing import Any

import pytest

from bandi.cli.ui_state import UIStateStore


@pytest.fixture
def db_path(tmp_path: Any) -> str:
    return str(tmp_path / "test.db")


class TestUIStateStore:
    def test_default_mode(self, db_path: str) -> None:
        store = UIStateStore(db_path=db_path)
        assert store.mode == "execute"

    def test_set_and_get_mode(self, db_path: str) -> None:
        store = UIStateStore(db_path=db_path)
        store.mode = "plan"
        assert store.mode == "plan"

    def test_default_model(self, db_path: str) -> None:
        store = UIStateStore(db_path=db_path)
        assert store.model == ""

    def test_set_and_get_model(self, db_path: str) -> None:
        store = UIStateStore(db_path=db_path)
        store.model = "deepseek-chat"
        assert store.model == "deepseek-chat"

    def test_persists_across_instances(self, db_path: str) -> None:
        store = UIStateStore(db_path=db_path)
        store.mode = "plan"
        store2 = UIStateStore(db_path=db_path)
        assert store2.mode == "plan"

    def test_concurrent_safety(self, db_path: str) -> None:
        import threading

        store = UIStateStore(db_path=db_path)

        def writer() -> None:
            for i in range(20):
                store.mode = f"mode-{i}"

        threads = [threading.Thread(target=writer) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
