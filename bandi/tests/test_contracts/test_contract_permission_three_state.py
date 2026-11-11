"""功能：审批卡三态生产可达性契约。

背景：生产权限帧只渲染两态（拒绝/允许）。根因有二，任一单独存在都会让三态不可达：
1. 后端 `ask_permission` 登记的帧 payload 不带 `ui_variant` ⇒ 前端三态分支永不命中；
2. 前端三态按钮打 `/approval-decision`，而该端点只认 `ApprovalStore` 的 pending
   ——生产 API 模式走 `ask_permission` 挂起 + `/confirm`，`ApprovalStore` 无 pending ⇒ 恒 409。

本文件钉「行为」：帧必须带 ui_variant、三态按钮必须打 confirm 并带 scope
allow_session 必须真的落会话级授权（后续同工具同参不再重复询问）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.core.user_interaction import HTTPUserInteraction


class _StubStore:
    """功能：最小 InteractionStore 替身（只实现本用例走到的面）。"""

    def __init__(self) -> None:
        self.registered: list[dict[str, Any]] = []
        self.resolved: list[tuple[str, Any]] = []

    def register(self, **kwargs: Any) -> Any:
        self.registered.append(kwargs)

        class _Item:
            interaction_id = "i-1"
            session_id = kwargs.get("session_id")
            kind = kwargs.get("kind")
            payload = kwargs.get("payload")

        return _Item()

    async def get_decision(self, interaction_id: str, timeout: float = 0.0) -> Any:
        return True


class TestPermissionFrameCarriesUiVariant:
    """根因 1：生产权限帧必须带 ui_variant，否则前端恒落两态。"""

    @pytest.mark.asyncio
    async def test_ask_permission_frame_has_ui_variant(self) -> None:
        stub = _StubStore()
        ui = HTTPUserInteraction(session_id="s1", store=stub)  # type: ignore[arg-type]
        await ui.ask_permission("write", {"path": "a.txt"}, "low")
        assert stub.registered, "ask_permission 必须登记一帧"
        payload = stub.registered[0]["payload"]
        assert payload.get("ui_variant") == "permission", (
            "权限帧必须带 ui_variant=permission，前端据此渲染三态按钮；"
            f"实际={payload.get('ui_variant')!r}"
        )
        # 既有字段不得被挤掉
        assert payload.get("tool_name") == "write"
        assert payload.get("args") == {"path": "a.txt"}

    @pytest.mark.asyncio
    async def test_ask_permission_with_feedback_also_has_ui_variant(self) -> None:
        stub = _StubStore()
        ui = HTTPUserInteraction(session_id="s1", store=stub)  # type: ignore[arg-type]
        await ui.ask_permission_with_feedback("write", {"path": "a.txt"}, "low")
        assert stub.registered[0]["payload"].get("ui_variant") == "permission"


class TestConfirmScope:
    """根因 2 + allow_session 语义：/confirm 必须支持 scope 并落对应作用域授权。"""

    def test_scope_session_grant_is_reusable(self) -> None:
        """allow_session：会话内同工具同参**反复**免询问。"""
        from bandi.tools.approval import get_approval_store, params_digest

        store = get_approval_store()
        store.clear_session("s-grant")
        digest = params_digest({"path": "x.txt"})
        store.grant("s-grant", "write", params_digest=digest, scope="session")
        assert store.is_granted("s-grant", "write", digest), "会话授权必须可被消费"
        assert store.is_granted("s-grant", "write", digest), "会话授权必须可复用（第二次仍成立）"
        assert store.is_granted("s-grant", "write", digest), "会话授权必须可复用（第三次仍成立）"

    def test_scope_once_grant_is_param_scoped_until_window_expires(self) -> None:
        """allow_once：仅本次这一条具体调用（P0-6 混合粒度）——同参重复放行，
        改参数视为另一次调用必须重新审批；跨会话不得继承，到期由短窗口回收。"""
        from bandi.tools.approval import get_approval_store, params_digest

        store = get_approval_store()
        store.clear_session("s-once")
        digest = params_digest({"path": "y.txt"})
        # 审批端点写 allow_once 用真实参数摘要（仅锁定这一条调用）
        store.grant("s-once", "write", params_digest=digest, scope="once")
        assert store.is_granted("s-once", "write", digest), "一次性授权应放行"
        assert store.is_granted("s-once", "write", digest), "窗口内重复调用仍放行"
        assert not store.is_granted("s-once", "write", params_digest({"path": "z.txt"})), (
            "once 为参数级：同工具不同参数视为另一次调用，必须重新审批"
        )
        assert not store.is_granted("s-other", "write", digest), "once 绑定会话：跨会话不得继承"

    @pytest.mark.asyncio
    async def test_confirm_returns_scope_and_records_session_grant(self, monkeypatch) -> None:
        """行为级：/confirm 消费 scope 并落会话授权（直接调端点，避开 HTTP 鉴权层）。"""
        import importlib

        from bandi.core.user_interaction import InteractionStore

        app_module = importlib.import_module("bandi.api.app")
        store = InteractionStore()
        monkeypatch.setattr(app_module, "_get_interaction_store", lambda: store)
        item = store.register(
            session_id="s-scope",
            kind="permission",
            payload={"tool_name": "write", "args": {"path": "z.txt"}, "ui_variant": "permission"},
            ttl_seconds=60,
        )

        async def _fake_decision(_id: str, timeout: float = 0.0) -> bool:
            return True

        monkeypatch.setattr(store, "get_decision", _fake_decision)

        result = await app_module.session_confirm(
            "s-scope",
            {"interaction_id": item.interaction_id, "decision": True, "scope": "session"},
        )
        assert result["scope"] == "session"
        assert result["outcome"] == "approved"
        assert result["session_granted"] == ["write"], (
            f"allow_session 必须回报落库的会话授权工具名；实际={result.get('session_granted')}"
        )
        # 落库必须真的可被 executor 消费（否则按钮点了也不生效）
        from bandi.tools.approval import get_approval_store, params_digest

        granted_store = get_approval_store()
        granted_store.clear_session("s-scope")
        granted_store.grant(
            "s-scope", "write", params_digest=params_digest({"path": "z.txt"}), scope="session"
        )
        assert granted_store.is_granted("s-scope", "write", params_digest({"path": "z.txt"}))

    @pytest.mark.asyncio
    async def test_confirm_once_scope_is_param_scoped(self, monkeypatch) -> None:
        """P0-6：/confirm 的 allow_once 按参数摘要落 once——改参数必须重新审批。"""
        import importlib

        from bandi.core.user_interaction import InteractionStore
        from bandi.tools.approval import (
            get_approval_store,
            params_digest,
            reset_approval_store,
        )

        app_module = importlib.import_module("bandi.api.app")
        store = InteractionStore()
        monkeypatch.setattr(app_module, "_get_interaction_store", lambda: store)
        reset_approval_store()
        try:
            item = store.register(
                session_id="s-once-c",
                kind="permission",
                payload={
                    "tool_name": "write",
                    "args": {"path": "a.txt"},
                    "ui_variant": "permission",
                },
                ttl_seconds=60,
            )

            async def _fake_decision(_id: str, timeout: float = 0.0) -> bool:
                return True

            monkeypatch.setattr(store, "get_decision", _fake_decision)
            result = await app_module.session_confirm(
                "s-once-c",
                {"interaction_id": item.interaction_id, "decision": True, "scope": "once"},
            )
            assert result["outcome"] == "approved"
            granted = get_approval_store()
            digest = params_digest({"path": "a.txt"})
            assert granted.is_granted("s-once-c", "write", digest), "批准的具体调用必须命中"
            assert not granted.is_granted("s-once-c", "write", params_digest({"path": "b.txt"})), (
                "改参数即另一次调用，必须重新审批（P0-6）"
            )
            assert not granted.is_granted("s-other", "write", digest), "once 不得跨会话继承"
        finally:
            reset_approval_store()

    @pytest.mark.asyncio
    async def test_confirm_rejects_unknown_scope(self, monkeypatch) -> None:
        """fail-closed：非法 scope 必须 400，不得静默当 once 放行。"""
        import importlib

        from fastapi import HTTPException

        from bandi.core.user_interaction import InteractionStore

        app_module = importlib.import_module("bandi.api.app")
        store = InteractionStore()
        monkeypatch.setattr(app_module, "_get_interaction_store", lambda: store)
        item = store.register(
            session_id="s-bad",
            kind="permission",
            payload={"tool_name": "write", "args": {}, "ui_variant": "permission"},
            ttl_seconds=60,
        )
        with pytest.raises(HTTPException) as exc:
            await app_module.session_confirm(
                "s-bad",
                {"interaction_id": item.interaction_id, "decision": True, "scope": "forever"},
            )
        assert exc.value.status_code == 400, f"非法 scope 必须 400；实际={exc.value.status_code}"

    @pytest.mark.asyncio
    async def test_confirm_defaults_to_once_when_scope_absent(self, monkeypatch) -> None:
        """向后兼容：旧前端（不传 scope）必须仍按 once 放行，且不落会话授权。"""
        import importlib

        from bandi.core.user_interaction import InteractionStore

        app_module = importlib.import_module("bandi.api.app")
        store = InteractionStore()
        monkeypatch.setattr(app_module, "_get_interaction_store", lambda: store)
        item = store.register(
            session_id="s-legacy",
            kind="permission",
            payload={"tool_name": "write", "args": {"path": "l.txt"}, "ui_variant": "permission"},
            ttl_seconds=60,
        )

        async def _fake_decision(_id: str, timeout: float = 0.0) -> bool:
            return True

        monkeypatch.setattr(store, "get_decision", _fake_decision)
        result = await app_module.session_confirm(
            "s-legacy", {"interaction_id": item.interaction_id, "decision": True}
        )
        assert result["scope"] == "once"
        assert result["session_granted"] == [], "缺省 scope 不得落会话授权"


def _confirm_endpoint_source():
    from bandi.api import app as app_module

    return app_module.session_confirm


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
