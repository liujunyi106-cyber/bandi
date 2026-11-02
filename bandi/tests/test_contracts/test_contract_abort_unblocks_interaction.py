"""功能：「停止」必须解开正阻塞在待决策交互上的轮次。

证据：轮次阻塞在审批卡（`ask_permission` 挂起于 `InteractionStore.get_decision()`）
时点「停止」，内前端不结算——`stop=True` / 「正在回复中…」/ `stream` 未释放，卡片仍在。
根因：`POST /api/sessions/{id}/abort` 只调 `controller.abort()`，而挂起中的协程停在
`await get_decision()` 等 resolve/reject，收不到 abort 信号 ⇒ 永不出终态帧
只能等 TTL 到期。

本文件钉「行为」：中止后该会话的挂起交互必须被拒绝且挂起点立即返回；
且不得波及其他会话的挂起交互（破坏者视角）。
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.core.user_interaction import HTTPUserInteraction, InteractionStore

# `bandi.api.app` 这个名字同时是子模块与其中的 FastAPI 实例，必须显式取模块对象
app_module = importlib.import_module("bandi.api.app")

# 功能：会话 API 受 Bearer 保护（鉴权键在导入期从 BANDI_API_KEY 解析）；未配 key 时
# 中间件回落到「仅回环地址」判定，而 TestClient 默认 host=testclient 不在白名单
# 故显式声明回环 client 地址。
_AUTH = {"Authorization": f"Bearer {app_module._API_KEY}"}
_LOOPBACK = ("127.0.0.1", 50000)


class _Ctrl:
    """功能：最小 abort 控制器替身（只需 abort 面）。"""

    def __init__(self) -> None:
        self.reason: str | None = None

    def abort(self, reason: str) -> None:
        self.reason = reason


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InteractionStore:
    st = InteractionStore()
    monkeypatch.setattr(app_module, "_interaction_store", st)
    return st


@pytest.fixture(autouse=True)
def _clean_controllers() -> Any:
    app_module._active_abort_controllers.clear()
    yield
    app_module._active_abort_controllers.clear()


class TestAbortUnblocksParkedWaiter:
    """核心语义：中止要让停死在 get_decision 的挂起点立刻返回。"""

    @pytest.mark.asyncio
    async def test_parked_waiter_returns_after_abort(self, store: InteractionStore) -> None:
        item = store.register(session_id="s1", kind="permission", payload={"tool": "write"})
        ctrl = _Ctrl()
        app_module._active_abort_controllers["s1"] = ctrl

        task = asyncio.create_task(store.get_decision(item.interaction_id, timeout=300))
        await asyncio.sleep(0.05)
        assert not task.done(), "前置：本用例必须真的挂起，否则测不到中止语义"

        result = await app_module.session_abort("s1")

        assert ctrl.reason == "aborted via API", "控制器必须被 abort"
        assert item.interaction_id in result["rejected_interactions"], (
            "中止必须拒绝该会话的挂起交互，否则轮次停死；"
            f"实际 rejected={result['rejected_interactions']}"
        )
        done, _ = await asyncio.wait({task}, timeout=2.0)
        assert task.done(), "挂起点必须在 2s 内返回（不能等 300s TTL）"
        assert not task.result(), f"被中止的交互不得当作已允许（实际={task.result()!r}）"

    @pytest.mark.asyncio
    async def test_abort_does_not_touch_other_sessions(self, store: InteractionStore) -> None:
        """破坏者视角：中止 A 会话不得顺手拒绝 B 会话的挂起交互。"""
        mine = store.register(session_id="s1", kind="permission", payload={})
        other = store.register(session_id="s2", kind="permission", payload={})
        app_module._active_abort_controllers["s1"] = _Ctrl()

        t_other = asyncio.create_task(store.get_decision(other.interaction_id, timeout=300))
        await asyncio.sleep(0.05)
        result = await app_module.session_abort("s1")

        assert result["rejected_interactions"] == [mine.interaction_id]
        assert not t_other.done(), "其他会话的挂起交互必须原样保留"
        assert store.get(other.interaction_id) is not None
        assert store.get(mine.interaction_id) is not None
        t_other.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t_other


class TestAbortThroughProductionFacade:
    """生产封装层：中止后各挂起 API 必须**返回拒绝**，绝不误放行。

    上一组用例直接挂 `store.get_decision()`，只证明 store 会返回；
    但生产链路走的是 `HTTPUserInteraction` 封装层——它把决策值**再解释一次**
    （如 `ask_permission` 的 `bool(decision)`、`ask_permission_with_feedback` 的
    `decision.get("granted")`）。若解释层把「中止产生的拒绝」误判为批准
    就是**安全漏洞**（中止反而放行了危险操作）。本组不依赖任何模型。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("call", "expect"),
        [
            pytest.param(
                lambda ui: ui.ask_permission("write", {"path": "a.txt"}, "low"),
                False,
                id="ask_permission-不得放行",
            ),
            pytest.param(
                lambda ui: ui.ask_confirmation("确认继续？"),
                False,
                id="ask_confirmation-不得当作已确认",
            ),
        ],
    )
    async def test_suspended_api_returns_denial(
        self, store: InteractionStore, call: Any, expect: Any
    ) -> None:
        ui = HTTPUserInteraction(session_id="s1", store=store)
        app_module._active_abort_controllers["s1"] = _Ctrl()

        task = asyncio.create_task(call(ui))
        await asyncio.sleep(0.05)
        assert not task.done(), "前置：必须真的挂起在交互上"

        result = await app_module.session_abort("s1")
        assert result["rejected_interactions"], "中止必须拒绝挂起交互"

        value = await asyncio.wait_for(task, timeout=2.0)
        assert value == expect, (
            f"中止后的挂起点必须解释为拒绝（{expect!r}），实际={value!r}；"
            "若为 True 说明中止会**放行**危险操作（安全漏洞）"
        )

    @pytest.mark.asyncio
    async def test_permission_with_feedback_denial_has_no_reason(
        self, store: InteractionStore
    ) -> None:
        """破坏者视角：中止产生的拒绝不得凭空带出「同意」语义。"""
        ui = HTTPUserInteraction(session_id="s1", store=store)
        app_module._active_abort_controllers["s1"] = _Ctrl()

        task = asyncio.create_task(
            ui.ask_permission_with_feedback("write", {"path": "a.txt"}, "high")
        )
        await asyncio.sleep(0.05)
        await app_module.session_abort("s1")
        granted, feedback = await asyncio.wait_for(task, timeout=2.0)
        assert granted is False, f"中止不得判为已批准（实际 granted={granted!r}）"
        assert not feedback, "中止的拒绝不应带用户反馈理由（用户从未表达过）"


class TestAbortHttpSurface:
    """HTTP 面：响应形状 + 无控制器仍 404 且不误伤。"""

    def test_response_reports_rejected_ids(self, store: InteractionStore) -> None:
        item = store.register(session_id="s1", kind="permission", payload={})
        app_module._active_abort_controllers["s1"] = _Ctrl()
        with TestClient(app, headers=_AUTH, client=_LOOPBACK) as client:
            r = client.post("/api/sessions/s1/abort")
        assert r.status_code == 200
        assert r.json()["aborted"] is True
        assert r.json()["rejected_interactions"] == [item.interaction_id]

    def test_no_controller_is_404_and_rejects_nothing(self, store: InteractionStore) -> None:
        item = store.register(session_id="s-none", kind="permission", payload={})
        with TestClient(app, headers=_AUTH, client=_LOOPBACK) as client:
            r = client.post("/api/sessions/s-none/abort")
        assert r.status_code == 404
        assert store.get(item.interaction_id) is not None
        assert store.list_pending(session_id="s-none"), "404 路径不得顺手拒绝挂起交互"

    def test_no_pending_interaction_is_not_an_error(self, store: InteractionStore) -> None:
        app_module._active_abort_controllers["s1"] = _Ctrl()
        with TestClient(app, headers=_AUTH, client=_LOOPBACK) as client:
            r = client.post("/api/sessions/s1/abort")
        assert r.status_code == 200
        assert r.json()["rejected_interactions"] == []


class _FakePersistence:
    """功能：交互持久化替身——仅提供水合与落盘最小面。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def sweep_expired(self) -> int:
        return 0

    def load_pending(self) -> list[dict[str, Any]]:
        return self.rows

    def upsert(self, item: Any) -> None:
        return None

    def update_status(self, item: Any) -> None:
        return None

    def delete(self, interaction_id: str) -> None:
        return None


class TestRestartGhostInteraction:
    """重启后水合的交互卡没有等待方，不得作为 pending 返回（否则永久幽灵卡）。"""

    def test_hydrated_orphan_hidden_from_pending(self) -> None:
        import time

        row = {
            "interaction_id": "ghost-1",
            "session_id": "s1",
            "kind": "input",
            "payload": {"prompt": "还在等吗？"},
            "created_at": time.time(),
            "ttl_seconds": 600.0,
        }
        store = InteractionStore(persistence=_FakePersistence([row]))
        assert store.list_pending(include_orphans=False) == []

        fresh = store.register(session_id="s1", kind="input", payload={"prompt": "新问题"})
        assert [item.interaction_id for item in store.list_pending(include_orphans=False)] == [
            fresh.interaction_id
        ]
