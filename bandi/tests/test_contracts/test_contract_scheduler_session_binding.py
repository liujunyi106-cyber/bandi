"""功能：定时任务结果落地——结果必须进独立运行会话，不能埋进发起时的对话。

真机缺陷：在某个长对话里创建定时任务时，`session_id` 被一并写进任务 payload，
任务结果就追加回那段对话。用户点「自动化」条目跳过去，只看到一屏旧消息，
表现为「任务跑了但前端没有回复」。
"""

from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

from bandi.scheduler.store import ScheduledTask, TaskStore

# `bandi.api.app` 这个名字同时是子模块与其中的 FastAPI 实例，必须显式取模块对象
app_module = importlib.import_module("bandi.api.app")
app = app_module.app
_AUTH = {"Authorization": f"Bearer {app_module._API_KEY}"}
# TestClient 默认 host=testclient 不在回环白名单，需显式声明。
_LOOPBACK = ("127.0.0.1", 50000)


@pytest.fixture()
def store(tmp_path, monkeypatch) -> TaskStore:
    """功能：把调度器存储指向临时库，避免写穿真实 ~/.bandi。

    `bandi.api.app` 这个名字在包内被 FastAPI 实例遮蔽，因此从 sys.modules
    取真模块再打补丁。"""
    module = sys.modules["bandi.api.app"]
    task_store = TaskStore(str(tmp_path / "scheduler.db"))
    monkeypatch.setattr(module, "_task_store", task_store, raising=False)
    monkeypatch.setattr(module, "_get_task_store", lambda: task_store)
    return task_store


@pytest.fixture()
def client() -> TestClient:
    """功能：取装配好的应用客户端（复用生产装配 + 鉴权头 + 回环地址）。"""
    return TestClient(app, headers=_AUTH, client=_LOOPBACK)


def test_create_task_does_not_inherit_caller_session(client: TestClient, store: TaskStore) -> None:
    """功能：创建任务时不继承发起方会话——结果要落到自己的运行会话。"""
    response = client.post(
        "/api/scheduler/tasks",
        json={
            "name": "每日统计",
            "cron": "0 9 * * *",
            "prompt": "统计文件数",
            "session_id": "dc1621db-2443-4467-86db-19f1f5471270",
        },
    )
    assert response.status_code == 200, response.text
    task = store.get(response.json()["task_id"])
    assert task is not None
    assert "session_id" not in (task.payload or {}), "不得继承发起方会话"


def test_create_task_honours_explicit_reuse(client: TestClient, store: TaskStore) -> None:
    """功能：破坏者视角——显式要求复用时仍可绑定指定会话（不能被一刀切掉）。"""
    response = client.post(
        "/api/scheduler/tasks",
        json={
            "name": "复用会话任务",
            "cron": "0 9 * * *",
            "prompt": "写点什么",
            "session_id": "keep-me",
            "reuse_session": True,
        },
    )
    assert response.status_code == 200, response.text
    task = store.get(response.json()["task_id"])
    assert (task.payload or {}).get("session_id") == "keep-me"


def test_new_task_carries_no_session(tmp_path) -> None:
    """功能：新建任务记录默认不带会话——由执行体首次运行时补建 automation 会话。"""
    task = ScheduledTask(
        task_id="st_test",
        name="每日统计",
        cron="0 9 * * *",
        prompt="统计文件数",
        enabled=True,
    )
    assert "session_id" not in (task.payload or {})


def test_task_record_carries_output_for_frontend(tmp_path) -> None:
    """功能：任务记录带 last_output——前端「自动化」条目据此展示最近结果。"""
    store = TaskStore(str(tmp_path / "scheduler.db"))
    task = ScheduledTask(
        task_id="st_out",
        name="每日统计",
        cron="0 9 * * *",
        prompt="统计",
        enabled=True,
    )
    store.save(task)
    store.set_last_output("st_out", "统计到 502 个 .py 文件")
    saved = store.get("st_out")
    assert saved is not None
    assert "502" in (saved.last_output or "")
