# 功能：SessionStore 并发安全测试 — 多线程写入/读取无竞态

import os
import tempfile
import threading
import time

import pytest

from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore


@pytest.fixture
def store() -> SessionStore:
    tmp = tempfile.mktemp(suffix=".db")
    s = SessionStore(tmp)
    yield s
    s.close()
    if os.path.exists(tmp):
        os.unlink(tmp)


class TestSessionStoreConcurrency:
    def test_concurrent_append_messages_no_loss(self, store: SessionStore) -> None:
        sid = store.create_session(title="concurrent_test")
        n_threads = 10
        msgs_per_thread = 20
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(worker_id: int) -> None:
            for i in range(msgs_per_thread):
                try:
                    msg = Message(
                        role=MessageRole.USER,
                        content=f"worker_{worker_id}_msg_{i}",
                    )
                    store.append_message(sid, msg)
                except Exception as e:  # noqa: BLE001
                    with lock:
                        errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Concurrent append errors: {errors}"
        branch = store.get_branch(sid)
        assert len(branch) == n_threads * msgs_per_thread

    def test_concurrent_read_write_no_crash(self, store: SessionStore) -> None:
        sids: list[str] = []
        for i in range(5):
            sids.append(store.create_session(title=f"crash_test_{i}"))
        errors: list[Exception] = []
        lock = threading.Lock()

        def writer() -> None:
            for _ in range(30):
                try:
                    sid = sids[int(time.monotonic() * 10) % len(sids)]
                    msg = Message(role=MessageRole.USER, content="data")
                    store.append_message(sid, msg)
                except Exception as e:  # noqa: BLE001
                    with lock:
                        errors.append(e)

        def reader() -> None:
            for _ in range(30):
                try:
                    sid = sids[int(time.monotonic() * 7) % len(sids)]
                    store.get_branch(sid)
                    store.get_session(sid)
                except Exception as e:  # noqa: BLE001
                    with lock:
                        errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Concurrent read/write errors: {errors}"

    def test_concurrent_rotate_and_read(self, store: SessionStore, tmp_path) -> None:
        from datetime import UTC, datetime, timedelta

        for i in range(20):
            sid = store.create_session(title=f"batch_{i}")
            store.append_message(sid, Message(role=MessageRole.USER, content=f"data_{i}"))
        old_sid = store.create_session(title="old")
        store.update_session(
            old_sid, updated_at=(datetime.now(UTC) - timedelta(days=90)).isoformat()
        )
        errors: list[Exception] = []
        lock = threading.Lock()
        rotate_done = threading.Event()

        def rotator() -> None:
            try:
                store.rotate(str(tmp_path), max_sessions=10, archive_after_days=30)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(e)
            finally:
                rotate_done.set()

        def reader_until_done() -> None:
            while not rotate_done.is_set():
                try:
                    store.count_sessions()
                    store.list_sessions()
                except Exception as e:  # noqa: BLE001
                    with lock:
                        errors.append(e)

        threads = [threading.Thread(target=rotator)]
        threads += [threading.Thread(target=reader_until_done) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Concurrent rotate errors: {errors}"
