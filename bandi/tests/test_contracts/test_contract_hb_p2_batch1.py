# 功能：契约——批 1（8 项低风险快修）
# 钉行为：
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture()
def task_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    from bandi.scheduler.store import TaskStore

    fresh = TaskStore(":memory:")
    with (
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._task_store", fresh),
        patch("bandi.api.app._task_runner", None),
        patch("bandi.api.app._task_scheduler", None),
    ):
        yield fresh


class TestSchedulerPatchKeepsTarget:
    def test_patch_name_keeps_target_metadata(self, task_store) -> None:
        from bandi.scheduler.store import ScheduledTask

        store = task_store
        store.save(
            ScheduledTask(
                task_id="t-target",
                name="old",
                cron="0 9 * * *",
                prompt="p",
                target_type="company",
                target_id="w-1",
                description="从工作项登记",
                payload={"k": 1},
            )
        )
        resp = client.patch("/api/scheduler/tasks/t-target", json={"name": "new-name"})
        assert resp.status_code == 200
        saved = store.get("t-target")
        assert saved is not None
        assert saved.name == "new-name"
        assert saved.target_type == "company", "PATCH 不得清空 target_type"
        assert saved.target_id == "w-1", "PATCH 不得清空 target_id"
        assert saved.description == "从工作项登记", "PATCH 不得清空 description"
        assert saved.payload == {"k": 1}, "PATCH 不得清空 payload"


class TestSchedulerTaskIdUniqueness:
    def test_same_millisecond_creates_two_distinct_tasks(self, task_store) -> None:
        with patch("bandi.api.app.time.time", return_value=1234.567):
            r1 = client.post(
                "/api/scheduler/tasks", json={"name": "a", "cron": "0 9 * * *", "prompt": "p"}
            ).json()
            r2 = client.post(
                "/api/scheduler/tasks", json={"name": "b", "cron": "0 9 * * *", "prompt": "p"}
            ).json()
        assert r1["task_id"] != r2["task_id"], "同毫秒两次创建不得同 id（后者会覆盖前者）"
        assert len(task_store.list()) == 2


class TestWsFrameSizeLimit:
    def test_oversize_frame_rejected_with_error(self) -> None:
        from bandi.api.app import _WS_MAX_FRAME_CHARS

        with patch("bandi.api.app._API_KEY", "test"):
            with client.websocket_connect(
                "/api/agent/stream", headers={"Authorization": "Bearer test"}
            ) as ws:
                ws.send_text("x" * (_WS_MAX_FRAME_CHARS + 1))
                frame = ws.receive_json()
        assert frame.get("type") == "error", f"超长帧必须回 error: {frame}"
        assert "too large" in str(frame.get("data", {}).get("message", ""))


class TestSchedulerListCorruptRow:
    def test_corrupt_row_skipped_not_fatal(self) -> None:
        from bandi.scheduler.store import ScheduledTask, TaskStore

        store = TaskStore(":memory:")
        store.save(ScheduledTask(task_id="ok1", name="n", cron="0 9 * * *", prompt="p"))
        store.save(ScheduledTask(task_id="bad1", name="b", cron="0 9 * * *", prompt="p"))
        store._conn.execute("UPDATE scheduled_tasks SET status='bogus_status' WHERE task_id='bad1'")
        store._conn.commit()
        tasks = store.list()
        assert [t.task_id for t in tasks] == ["ok1"], "坏行必须跳过且不炸整表"


class TestMemoryMdAtomicWrite:
    def test_failed_replace_keeps_previous_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bandi.memory import markdown_memory as mm
        from bandi.memory.markdown_memory import MarkdownMemoryStore

        store = MarkdownMemoryStore(str(tmp_path / "mem"), project_root=str(tmp_path))
        memory_md = store.ensure_memory_file()
        store.save_visible_text("第一篇可见内容")
        before = memory_md.read_text(encoding="utf-8")
        assert "第一篇可见内容" in before

        def boom(src: object, dst: object) -> None:
            raise OSError("simulated crash before replace")

        monkeypatch.setattr(mm.os, "replace", boom)
        with pytest.raises(OSError):
            store.save_visible_text("第二篇可见内容")
        monkeypatch.undo()
        after = memory_md.read_text(encoding="utf-8")
        assert "第二篇可见内容" not in after, "replace 失败不得半写 MEMORY.md"
        assert "第一篇可见内容" in after, "旧内容必须完整保留"


class TestFileLocksBounded:
    def test_lock_table_cleans_unheld_entries(self) -> None:
        # file_edit.py 内，迁移后契约须跟随新归属；**钉行为不钉符号位置**）。
        from bandi.tools import fs_write

        limit = fs_write.FILE_LOCKS_MAX
        for i in range(limit + 100):
            fs_write.file_lock(Path("/nonexistent/dir") / f"f{i}.txt")
        assert len(fs_write._FILE_LOCKS) <= limit, (
            f"锁表必须有界（实际 {len(fs_write._FILE_LOCKS)} > {limit}）"
        )
        same = fs_write.file_lock(Path("/nonexistent/dir/f0.txt"))
        assert fs_write.file_lock(Path("/nonexistent/dir/f0.txt")) is same

    def test_file_edit_reuses_shared_fs_write_impl(self) -> None:
        """防行为分叉：`file_edit` 必须复用共享实现（迁移后仍指向同一处）。"""
        from bandi.tools import file_edit, fs_write

        assert file_edit.file_lock is fs_write.file_lock, "file_edit 须共用 fs_write.file_lock"
        assert file_edit.atomic_write is fs_write.atomic_write
        assert file_edit.decode_with_bom is fs_write.decode_with_bom
        assert file_edit.check_external_modification is fs_write.check_external_modification


class TestSteerNoEmptyResidue:
    def test_drain_removes_empty_queue_entry(self) -> None:
        from bandi.agent.steer import SessionSteerRegistry

        reg = SessionSteerRegistry()
        reg.push("s1", "转向一", queue_only=True)
        assert reg.drain("s1") == ["转向一"]
        assert "s1" not in reg._queues, "drain 后空条目不驻留（防会话海量时无界）"
        reg.push("s1", "转向二", queue_only=True)
        assert reg.drain("s1") == ["转向二"]
        assert reg.has_pending("s1") is False


class TestSseQueueBounded:
    def test_full_queue_drops_oldest_keeps_latest(self) -> None:
        from bandi.api.app import _SSE_QUEUE_MAXSIZE, _SSEStreamHandler

        async def _run() -> str:
            handler = _SSEStreamHandler()
            total = _SSE_QUEUE_MAXSIZE + 5
            for i in range(total):
                await handler.put("tick", {"i": i})
            assert handler._queue.qsize() <= _SSE_QUEUE_MAXSIZE, "队列必须有界"
            last = ""
            while not handler._queue.empty():
                last = handler._queue.get_nowait()
            return last

        last_frame = asyncio.run(_run())
        assert f'"i": {_SSE_QUEUE_MAXSIZE + 4}' in last_frame, "满队列必须保最新帧"
