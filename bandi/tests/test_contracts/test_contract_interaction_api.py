# 功能：Day132c interaction API 契约——POST /confirm /clarify 端点行为族 + 会话隔离
# 背景：SSE/WS 流式对话装配 HTTPUserInteraction 后，LLM 工具执行路径遇 ask/提问
# 会 register pending + 推 interaction 帧 + 真挂起。前端渲染卡片 → 用户点选/答复 →
# 契约钉端点行为：批准/拒绝/答复写回、跨会话 404、未知 404、非 input 不可 clarify。
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

# bandi.api.__init__ 执行 from .app import app 后包属性遮蔽子模块——
# 须经 sys.modules 取模块真身（import bandi.api.app as m 会绑到 FastAPI 实例）
app_module = sys.modules["bandi.api.app"]

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重置 interaction store 单例 + 鉴权（每测试独立 store）。"""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with (
        patch("bandi.api.app._interaction_store", None),
        patch("bandi.api.app._API_KEY", "test"),
    ):
        yield


def _register(kind: str = "confirm", session_id: str = "s1") -> str:
    store = app_module._get_interaction_store()
    item = store.register(session_id=session_id, kind=kind, payload={"probe": True})
    return item.interaction_id


class TestConfirmEndpoint:
    def test_confirm_approve_wakes_and_marks_resolved(self) -> None:
        iid = _register("confirm", "s1")
        resp = client.post(
            "/api/sessions/s1/confirm", json={"interaction_id": iid, "decision": True}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"] == "approved"
        store = app_module._get_interaction_store()
        assert store.get(iid).status == "resolved"

    def test_confirm_deny_rejects(self) -> None:
        iid = _register("permission", "s1")
        resp = client.post(
            "/api/sessions/s1/confirm", json={"interaction_id": iid, "decision": False}
        )
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "denied"
        assert app_module._get_interaction_store().get(iid).status == "rejected"

    def test_confirm_missing_interaction_id_400(self) -> None:
        resp = client.post("/api/sessions/s1/confirm", json={})
        assert resp.status_code == 400

    def test_confirm_unknown_interaction_404(self) -> None:
        resp = client.post(
            "/api/sessions/s1/confirm", json={"interaction_id": "no-such", "decision": True}
        )
        assert resp.status_code == 404

    def test_confirm_cross_session_404(self) -> None:
        iid = _register("confirm", "s1")
        resp = client.post("/api/sessions/other/confirm", json={"interaction_id": iid})
        assert resp.status_code == 404, "跨会话决策必须拒绝（interaction 归属隔离）"


class TestClarifyEndpoint:
    def test_clarify_answer_resolves_input(self) -> None:
        iid = _register("input", "s1")
        resp = client.post(
            "/api/sessions/s1/clarify", json={"interaction_id": iid, "answer": "仓库地址 https://x"}
        )
        assert resp.status_code == 200
        assert resp.json()["answered"] is True
        store = app_module._get_interaction_store()
        assert store.get(iid).status == "resolved"
        assert store._futures.get(iid) is None  # resolve 后 future 已移除（决策已送达）

    def test_clarify_missing_answer_ok_empty(self) -> None:
        iid = _register("input", "s1")
        resp = client.post("/api/sessions/s1/clarify", json={"interaction_id": iid, "answer": ""})
        assert resp.status_code == 200
        assert app_module._get_interaction_store().get(iid).status == "resolved"

    def test_clarify_non_input_kind_400(self) -> None:
        iid = _register("confirm", "s1")
        resp = client.post("/api/sessions/s1/clarify", json={"interaction_id": iid, "answer": "x"})
        assert resp.status_code == 400

    def test_clarify_cross_session_404(self) -> None:
        iid = _register("input", "sA")
        resp = client.post("/api/sessions/sB/clarify", json={"interaction_id": iid, "answer": "x"})
        assert resp.status_code == 404


class TestInteractionApiAwakeClosedLoop:
    def test_pending_await_wakes_via_confirm_api(self) -> None:
        """真挂起闭环（API 层）：挂起 get_decision → POST confirm → 决策送达。"""
        import asyncio

        store = app_module._get_interaction_store()
        iid = _register("confirm", "s1")

        async def _wait() -> bool:
            decision = await store.get_decision(iid, timeout=5)
            return bool(decision)

        result: list = []

        async def _drive() -> None:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_wait())
            await asyncio.sleep(0.05)
            assert not task.done(), "confirm 前挂起必须保持等待"
            resp = client.post(
                "/api/sessions/s1/confirm", json={"interaction_id": iid, "decision": True}
            )
            assert resp.status_code == 200
            result.append(await task)

        asyncio.run(_drive())
        assert result == [True], "POST /confirm 后挂起方必须被唤醒且决策为 True"
