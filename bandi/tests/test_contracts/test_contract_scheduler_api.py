# 功能：定时任务 API 契约（设置面板消费面）
# 装配点：api/app.py _task_store 单例（~/.bandi/data/scheduler.db）+ _task_runner（默认
# 包装 run_agent_turn；测试可 patch 替换为 spy——API 契约不钉执行体内核）。
# 契约钉：CRUD 全端点 / 非法 cron 400 / 缺字段 400 / 404 / 启停 PATCH / cron 变更重排程 /
# trigger 手动触发调 runner 且不推进自动排程。
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.scheduler.store import ScheduledTask, TaskStore

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    app_mod = sys.modules["bandi.api.app"]

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    fresh = TaskStore(":memory:")
    with (
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._task_store", fresh),
        patch("bandi.api.app._task_runner", None),
        patch("bandi.api.app._task_scheduler", None),
    ):
        yield
        # teardown：清理测试中 start 起的后台调度循环（防跨测试 task 泄漏）
        import asyncio

        leaked = app_mod._task_scheduler
        if leaked is not None:
            try:
                asyncio.run(leaked.stop())
            except RuntimeError:
                pass


class TestSchedulerApiE2E:
    """端到端族（DoD-12）：真实文件 sqlite + API 全链 + runner 真实接线，断言最终产物。

    不 patch _task_store → 走生产惰性装配 ~/.bandi/data/scheduler.db（真实落库）。
    """

    @pytest.fixture(autouse=True)
    def _isolate_real_store(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        app_mod = sys.modules["bandi.api.app"]

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        with (
            patch("bandi.api.app._API_KEY", "test"),
            patch("bandi.api.app._task_store", None),  # 惰性装配真实文件 store
            patch("bandi.api.app._task_runner", None),
            patch("bandi.api.app._task_scheduler", None),
        ):
            yield
            import asyncio

            leaked = app_mod._task_scheduler
            if leaked is not None:
                try:
                    asyncio.run(leaked.stop())
                except RuntimeError:
                    pass

    def test_create_trigger_read_back_real_sqlite(self) -> None:
        """API 创建 → trigger（默认 runner 走真实 headless？不——生产默认 runner 调
        run_agent_turn 会真实起 LLM 链路，契约注入 spy 替换执行体，验证接线完整）。"""
        calls: list[tuple[str, str]] = []

        async def fake_runner(task: ScheduledTask) -> None:
            calls.append((task.task_id, task.prompt))

        with patch("bandi.api.app._task_runner", fake_runner):
            resp = client.post(
                "/api/scheduler/tasks",
                json={"name": "e2e", "cron": "0 9 * * *", "prompt": "hello from cron"},
            )
            assert resp.status_code == 200
            tid = resp.json()["task_id"]

            tr = client.post(f"/api/scheduler/tasks/{tid}/trigger")
            assert tr.status_code == 200
            assert calls == [(tid, "hello from cron")]

            # 断言真实 sqlite 落库可读回（重启级持久化）
            detail = client.get(f"/api/scheduler/tasks/{tid}")
            assert detail.status_code == 200
            assert detail.json()["last_run_at"] is not None

    def test_task_survives_api_store_reopen(self, tmp_path: Path) -> None:
        """生产装配路径下的持久化：新 store 实例读同一 db 文件。"""
        from bandi.scheduler.store import TaskStore

        client.post(
            "/api/scheduler/tasks",
            json={"name": "persist", "cron": "*/15 * * * *", "prompt": "p"},
        )
        real = TaskStore(str(tmp_path / ".bandi" / "data" / "scheduler.db"))
        tasks = real.list()
        assert len(tasks) == 1
        assert tasks[0].name == "persist"
        assert tasks[0].cron == "*/15 * * * *"


class TestSchedulerApiCrud:
    """CRUD 族：创建/列表/详情/404/非法 cron/缺字段。"""

    def test_create_task(self) -> None:
        resp = client.post(
            "/api/scheduler/tasks",
            json={"name": "morning brief", "cron": "0 9 * * *", "prompt": "brief me"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "morning brief"
        assert data["enabled"] is True
        assert data["task_id"]
        assert data["next_run_at"]  # 创建即排程

    def test_create_disabled_task(self) -> None:
        resp = client.post(
            "/api/scheduler/tasks",
            json={
                "name": "weekly digest",
                "cron": "0 10 * * 1",
                "prompt": "digest",
                "enabled": False,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_create_enabled_string_false_closes(self) -> None:
        """破坏者防线：字符串 \"false\" 不得被裸 bool() 当 True（显式关闭被绕过）。"""
        resp = client.post(
            "/api/scheduler/tasks",
            json={"name": "a", "cron": "0 9 * * *", "prompt": "pa", "enabled": "false"},
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_create_enabled_ambiguous_value_400(self) -> None:
        resp = client.post(
            "/api/scheduler/tasks",
            json={"name": "a", "cron": "0 9 * * *", "prompt": "pa", "enabled": "maybe"},
        )
        assert resp.status_code == 400

    def test_create_invalid_cron_400(self) -> None:
        resp = client.post(
            "/api/scheduler/tasks",
            json={"name": "bad", "cron": "not cron", "prompt": "x"},
        )
        assert resp.status_code == 400

    def test_create_missing_fields_400(self) -> None:
        resp = client.post("/api/scheduler/tasks", json={"name": "noprompt"})
        assert resp.status_code == 400

    def test_list_tasks(self) -> None:
        client.post(
            "/api/scheduler/tasks",
            json={"name": "a", "cron": "0 9 * * *", "prompt": "pa"},
        )
        client.post(
            "/api/scheduler/tasks",
            json={"name": "b", "cron": "*/10 * * * *", "prompt": "pb"},
        )
        resp = client.get("/api/scheduler/tasks")
        assert resp.status_code == 200
        assert len(resp.json()["tasks"]) == 2

    def test_get_task_detail(self) -> None:
        created = client.post(
            "/api/scheduler/tasks",
            json={"name": "a", "cron": "0 9 * * *", "prompt": "pa"},
        ).json()
        resp = client.get(f"/api/scheduler/tasks/{created['task_id']}")
        assert resp.status_code == 200
        assert resp.json()["prompt"] == "pa"

    def test_get_missing_404(self) -> None:
        resp = client.get("/api/scheduler/tasks/nope")
        assert resp.status_code == 404

    def test_delete_task(self) -> None:
        created = client.post(
            "/api/scheduler/tasks",
            json={"name": "a", "cron": "0 9 * * *", "prompt": "pa"},
        ).json()
        resp = client.delete(f"/api/scheduler/tasks/{created['task_id']}")
        assert resp.status_code == 200
        resp = client.get(f"/api/scheduler/tasks/{created['task_id']}")
        assert resp.status_code == 404


class TestSchedulerApiUpdateAndTrigger:
    """PATCH 族 + trigger 族。"""

    def _create(self, **body: Any) -> str:
        payload = {"name": "a", "cron": "0 9 * * *", "prompt": "pa"}
        payload.update(body)
        return client.post("/api/scheduler/tasks", json=payload).json()["task_id"]

    def test_patch_disable(self) -> None:
        tid = self._create()
        resp = client.patch(f"/api/scheduler/tasks/{tid}", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_patch_change_cron_reschedules_next_run(self) -> None:
        tid = self._create(cron="0 9 * * *")
        before = client.get(f"/api/scheduler/tasks/{tid}").json()
        resp = client.patch(f"/api/scheduler/tasks/{tid}", json={"cron": "0 23 * * *"})
        assert resp.status_code == 200
        after = resp.json()
        assert after["cron"] == "0 23 * * *"
        assert after["next_run_at"] != before["next_run_at"], "cron 变更必须重排程"

    def test_patch_invalid_cron_400_keeps_original(self) -> None:
        tid = self._create()
        resp = client.patch(f"/api/scheduler/tasks/{tid}", json={"cron": "bad"})
        assert resp.status_code == 400
        detail = client.get(f"/api/scheduler/tasks/{tid}").json()
        assert detail["cron"] == "0 9 * * *", "非法更新不得污染原任务"

    def test_trigger_runs_runner_once(self) -> None:
        called: list[str] = []

        async def fake_runner(task: ScheduledTask) -> None:
            called.append(task.task_id)

        with patch("bandi.api.app._task_runner", fake_runner):
            tid = self._create()
            resp = client.post(f"/api/scheduler/tasks/{tid}/trigger")
            assert resp.status_code == 200
            assert called == [tid]

    def test_trigger_does_not_reschedule_automatic_next_run(self) -> None:
        """手动 trigger 不得推进自动排程（区别于 tick 触发的 next_run 推进）。"""

        async def fake_runner(task: ScheduledTask) -> None:
            return None

        with patch("bandi.api.app._task_runner", fake_runner):
            tid = self._create()
            before = client.get(f"/api/scheduler/tasks/{tid}").json()["next_run_at"]
            client.post(f"/api/scheduler/tasks/{tid}/trigger")
            after = client.get(f"/api/scheduler/tasks/{tid}").json()["next_run_at"]
            assert after == before

    def test_trigger_missing_404(self) -> None:
        resp = client.post("/api/scheduler/tasks/nope/trigger")
        assert resp.status_code == 404

    def test_trigger_records_last_run(self) -> None:
        async def fake_runner(task: ScheduledTask) -> None:
            return None

        with patch("bandi.api.app._task_runner", fake_runner):
            tid = self._create()
            client.post(f"/api/scheduler/tasks/{tid}/trigger")
            detail = client.get(f"/api/scheduler/tasks/{tid}").json()
            assert detail["last_run_at"], "手动触发后 last_run_at 应可见"


class TestSchedulerApiLifecycle:
    """调度循环族：start/stop/status 幂等 + 后台到期自动触发（DoD-12 生产主链路）。"""

    def test_start_stop_status(self) -> None:
        resp = client.post("/api/scheduler/start")
        assert resp.status_code == 200 and resp.json()["running"] is True
        resp = client.get("/api/scheduler/status")
        assert resp.status_code == 200 and resp.json()["running"] is True
        resp = client.post("/api/scheduler/start")  # 幂等
        assert resp.status_code == 200
        resp = client.post("/api/scheduler/stop")
        assert resp.status_code == 200 and resp.json()["running"] is False
        resp = client.post("/api/scheduler/stop")  # 幂等
        assert resp.status_code == 200

    def test_background_due_task_auto_fires(self) -> None:
        """后台循环（真实 tick_loop + runner spy）：到期任务自动触发一次。"""
        import asyncio
        import sys

        app_mod = sys.modules["bandi.api.app"]

        fired: list[str] = []

        async def fake_runner(task: ScheduledTask) -> None:
            fired.append(task.task_id)

        with patch("bandi.api.app._task_runner", fake_runner):
            # 建一个 cron=每分钟、next_run 立即到期的任务（save 已按 now 排 next_run；
            # 手动把 next_run 拨到过去，模拟错过窗口 → 补跑一次）
            created = client.post(
                "/api/scheduler/tasks",
                json={"name": "auto", "cron": "* * * * *", "prompt": "auto prompt"},
            ).json()
            tid = created["task_id"]
            sched = app_mod._get_task_scheduler()
            assert sched is not None

            from datetime import datetime, timedelta

            past = datetime.now() - timedelta(minutes=1)
            sched.store.set_next_run(tid, past)
            client.post("/api/scheduler/start")
            try:
                # 等真实后台 tick（interval 钳制 ≥1s → 最多等 2 次 tick）
                for _ in range(10):
                    if fired:
                        break
                    asyncio.run(asyncio.sleep(0.4))
            finally:
                client.post("/api/scheduler/stop")
        assert fired == [tid], "后台 tick 必须自动触发到期任务"
        detail = client.get(f"/api/scheduler/tasks/{tid}").json()
        assert detail["last_run_at"] is not None
        assert detail["status"] == "idle"
