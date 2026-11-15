# 功能：批量授权卡契约——同批 ≥2 条高危调用只弹一张卡，一次决策对全部生效；
# 拒绝时全部收口为 permission_denied；单条调用保持逐条询问的原有语义。
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.core.message import ToolCall
from bandi.core.user_interaction import (
    PERMISSION_INTERACTION_TTL,
    HTTPUserInteraction,
    InteractionStore,
)
from bandi.tools.approval import get_approval_store, params_digest
from bandi.tools.executor import ToolExecutor
from bandi.tools.permissions_pipeline import PermissionsPipeline

app_module = sys.modules["bandi.api.app"]
client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


async def _wait_callback(seen: dict[str, Any]) -> None:
    for _ in range(200):
        if "item" in seen:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("批量授权卡未送达")


class _Registry:
    def __init__(self, tools: list[Any]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> Any:
        return self._tools.get(name)


class _HighRiskTool:
    risk_level = "high"
    is_concurrency_safe = True
    is_idempotent = True
    is_read_only = False
    tags: list[str] = []
    cache_ttl = 0.0
    enabled = True
    permissions_override = None
    permission_action = ""
    mcp_remote = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"high risk probe {name}"
        self.parameters: dict[str, Any] = {"type": "object", "properties": {}}
        self.calls = 0

    async def execute(self, params: dict[str, Any], **kwargs: Any) -> str:
        self.calls += 1
        return "ok"

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        return []

    def to_openai_schema(self) -> dict[str, Any]:
        return {}


class _UiChannel:
    def __init__(self, decision: dict[str, Any], *, single_granted: bool = False) -> None:
        self.decision = decision
        self.single_granted = single_granted
        self.batch_items: list[list[dict[str, Any]]] = []
        self.single_asks = 0

    async def ask_permission_batch(
        self, items: list[dict[str, Any]], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.batch_items.append([dict(item) for item in items])
        return dict(self.decision)

    async def ask_permission(self, **kwargs: Any) -> bool:
        self.single_asks += 1
        return self.single_granted

    async def ask_permission_with_feedback(self, **kwargs: Any) -> tuple[bool, str]:
        self.single_asks += 1
        return self.single_granted, ""


def _executor(ui: _UiChannel, tools: list[_HighRiskTool]) -> ToolExecutor:
    executor = ToolExecutor(
        _Registry(tools),
        permissions_pipeline=PermissionsPipeline(user_interaction=ui),
        mode="execute",
        agent_id="a1",
    )
    executor.bind_session_meta({"session_id": "s-batch"})
    return executor


@pytest.mark.asyncio
class TestBatchCardFrame:
    async def test_one_frame_carries_all_items(self) -> None:
        store = InteractionStore()
        seen: dict[str, Any] = {}

        async def on_interaction(item: Any) -> None:
            seen["item"] = item

        ui = HTTPUserInteraction(store, "s1", on_interaction=on_interaction)
        items = [
            {
                "tool_call_id": "c1",
                "tool_name": "bash",
                "args": {"command": "rm -rf build"},
                "risk": "high",
            },
            {
                "tool_call_id": "c2",
                "tool_name": "fs_write",
                "args": {"path": "a.txt"},
                "risk": "high",
            },
        ]
        task = asyncio.create_task(ui.ask_permission_batch(items))
        await _wait_callback(seen)
        pending = seen["item"]
        assert pending.kind == "permission"
        assert pending.payload["batch"] is True
        assert [raw["tool_name"] for raw in pending.payload["items"]] == ["bash", "fs_write"]
        assert pending.ttl_seconds == PERMISSION_INTERACTION_TTL

        store.resolve(pending.interaction_id, {"granted": True, "scope": "session"})
        assert await task == {"granted": True, "scope": "session", "feedback": ""}

    async def test_empty_items_fail_closed_without_card(self) -> None:
        store = InteractionStore()
        seen: dict[str, Any] = {}

        async def on_interaction(item: Any) -> None:
            seen["item"] = item

        ui = HTTPUserInteraction(store, "s1", on_interaction=on_interaction)
        decision = await ui.ask_permission_batch([])
        assert decision["granted"] is False
        assert "item" not in seen, "空清单不得弹出无内容卡片"

    async def test_deny_keeps_feedback(self) -> None:
        store = InteractionStore()
        seen: dict[str, Any] = {}

        async def on_interaction(item: Any) -> None:
            seen["item"] = item

        ui = HTTPUserInteraction(store, "s1", on_interaction=on_interaction)
        task = asyncio.create_task(
            ui.ask_permission_batch(
                [{"tool_call_id": "c1", "tool_name": "bash", "args": {}, "risk": "high"}]
            )
        )
        await _wait_callback(seen)
        store.resolve(seen["item"].interaction_id, {"granted": False, "feedback": "先解释"})
        decision = await task
        assert decision["granted"] is False
        assert decision["feedback"] == "先解释"


@pytest.mark.asyncio
class TestBatchPreflight:
    async def test_two_high_risk_calls_share_one_card(self) -> None:
        tools = [_HighRiskTool("tool_a"), _HighRiskTool("tool_b")]
        ui = _UiChannel({"granted": True, "scope": "once", "feedback": ""})
        executor = _executor(ui, tools)

        results = await executor.execute(
            [
                ToolCall(id="1", name="tool_a", arguments={"target": "a"}),
                ToolCall(id="2", name="tool_b", arguments={"target": "b"}),
            ]
        )

        assert [result.success for result in results] == [True, True]
        assert len(ui.batch_items) == 1, "两条高危调用只允许一张卡"
        assert [raw["tool_name"] for raw in ui.batch_items[0]] == ["tool_a", "tool_b"]
        assert ui.single_asks == 0, "批量决策后不得再逐条询问"
        assert [tool.calls for tool in tools] == [1, 1]

    async def test_once_scope_grants_each_call_digest(self) -> None:
        tools = [_HighRiskTool("tool_a"), _HighRiskTool("tool_b")]
        ui = _UiChannel({"granted": True, "scope": "once", "feedback": ""})
        executor = _executor(ui, tools)

        await executor.execute(
            [
                ToolCall(id="1", name="tool_a", arguments={"target": "a"}),
                ToolCall(id="2", name="tool_b", arguments={"target": "b"}),
            ]
        )

        approvals = get_approval_store()
        assert approvals.is_granted("s-batch", "tool_a", params_digest({"target": "a"}))
        assert approvals.is_granted("s-batch", "tool_b", params_digest({"target": "b"}))
        assert not approvals.is_granted("s-batch", "tool_a", params_digest({"target": "other"}))

    async def test_session_scope_grants_tool_wildcard(self) -> None:
        tools = [_HighRiskTool("tool_a"), _HighRiskTool("tool_b")]
        ui = _UiChannel({"granted": True, "scope": "session", "feedback": ""})
        executor = _executor(ui, tools)

        await executor.execute(
            [
                ToolCall(id="1", name="tool_a", arguments={"target": "a"}),
                ToolCall(id="2", name="tool_b", arguments={"target": "b"}),
            ]
        )

        approvals = get_approval_store()
        assert approvals.is_granted("s-batch", "tool_a", "")
        assert approvals.is_granted("s-batch", "tool_a", params_digest({"target": "different"}))

    async def test_deny_blocks_every_item_in_card(self) -> None:
        tools = [_HighRiskTool("tool_a"), _HighRiskTool("tool_b")]
        ui = _UiChannel({"granted": False, "scope": "once", "feedback": ""})
        executor = _executor(ui, tools)

        results = await executor.execute(
            [
                ToolCall(id="1", name="tool_a", arguments={"target": "a"}),
                ToolCall(id="2", name="tool_b", arguments={"target": "b"}),
            ]
        )

        assert [result.success for result in results] == [False, False]
        assert all(result.meta.get("error_class") == "permission_denied" for result in results)
        assert [tool.calls for tool in tools] == [0, 0], "拒绝后不得执行任何一条"
        assert len(ui.batch_items) == 1

    async def test_single_call_keeps_per_call_prompt(self) -> None:
        tools = [_HighRiskTool("tool_a")]
        ui = _UiChannel({"granted": True, "scope": "once", "feedback": ""}, single_granted=True)
        executor = _executor(ui, tools)

        results = await executor.execute(
            [ToolCall(id="1", name="tool_a", arguments={"target": "a"})]
        )

        assert [result.success for result in results] == [True]
        assert ui.batch_items == [], "单条调用不得走批量卡"
        assert ui.single_asks == 1

    async def test_repeated_calls_do_not_reopen_card(self) -> None:
        tools = [_HighRiskTool("tool_a"), _HighRiskTool("tool_b")]
        ui = _UiChannel({"granted": True, "scope": "once", "feedback": ""})
        executor = _executor(ui, tools)

        await executor.execute(
            [
                ToolCall(id="1", name="tool_a", arguments={"target": "a"}),
                ToolCall(id="2", name="tool_b", arguments={"target": "b"}),
            ]
        )
        results = await executor.execute(
            [
                ToolCall(id="3", name="tool_a", arguments={"target": "a"}),
                ToolCall(id="4", name="tool_b", arguments={"target": "b"}),
            ]
        )

        assert [result.success for result in results] == [True, True]
        assert len(ui.batch_items) == 1, "已授权调用重发不得再弹卡"

    async def test_internal_confirmation_command_is_detected(self) -> None:
        from bandi.tools.bash import BashTool

        pipeline = PermissionsPipeline()
        bash = BashTool()
        risky = ToolCall(id="1", name="bash", arguments={"command": "echo hi > out.txt"})
        safe = ToolCall(id="2", name="bash", arguments={"command": "echo ok"})
        assert pipeline.needs_internal_confirmation(risky, bash) is True
        assert pipeline.needs_internal_confirmation(safe, bash) is False

    async def test_session_grant_skips_internal_confirmation(self) -> None:
        class _NoAskUi:
            async def ask_permission(self, **kwargs: Any) -> bool:
                raise AssertionError("已授权命令不得再逐条询问")

        pipeline = PermissionsPipeline(user_interaction=_NoAskUi())
        pipeline.bind_session_id("s-internal")
        get_approval_store().grant("s-internal", "bash", scope="session", params_digest="")

        granted = await pipeline.confirm_tool(
            tool_name="bash",
            arguments={"command": "echo hi > out.txt"},
            reason="control operators",
        )
        assert granted is True

    async def test_once_grant_only_covers_same_command(self) -> None:
        class _DenyUi:
            async def ask_permission(self, **kwargs: Any) -> bool:
                return False

        pipeline = PermissionsPipeline(user_interaction=_DenyUi())
        pipeline.bind_session_id("s-once")
        get_approval_store().grant(
            "s-once",
            "bash",
            scope="once",
            params_digest=params_digest({"command": "echo hi > out.txt"}),
        )

        assert await pipeline.confirm_tool(
            tool_name="bash",
            arguments={"command": "echo hi > out.txt"},
            reason="control operators",
        )
        assert not await pipeline.confirm_tool(
            tool_name="bash",
            arguments={"command": "echo bye > out.txt"},
            reason="control operators",
        )


@pytest.fixture()
def _isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with (
        patch("bandi.api.app._interaction_store", None),
        patch("bandi.api.app._API_KEY", "test"),
    ):
        yield


def _seed_batch(session_id: str = "s1") -> tuple[InteractionStore, Any, list[dict[str, Any]]]:
    store = app_module._get_interaction_store()
    items = [
        {
            "tool_call_id": "c1",
            "tool_name": "bash",
            "args": {"command": "rm -rf build"},
            "risk": "high",
        },
        {"tool_call_id": "c2", "tool_name": "fs_write", "args": {"path": "a.txt"}, "risk": "high"},
    ]
    item = store.register(
        session_id=session_id,
        kind="permission",
        payload={
            "ui_variant": "permission",
            "batch": True,
            "items": items,
            "tool_name": "bash",
            "args": items[0]["args"],
            "risk": "high",
        },
    )
    return store, item, items


class TestBatchConfirmEndpoint:
    def test_allow_session_applies_to_every_item(self, _isolated_app: Any) -> None:
        store, item, items = _seed_batch()
        resp = client.post(
            "/api/sessions/s1/confirm",
            json={"interaction_id": item.interaction_id, "decision": True, "scope": "session"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"] == "approved"
        assert sorted(body["session_granted"]) == ["bash", "fs_write"]
        approvals = get_approval_store()
        for raw in items:
            assert approvals.is_granted("s1", raw["tool_name"], ""), "会话级=工具级通配"
        assert store.get(item.interaction_id).status == "resolved"

    def test_allow_once_applies_to_every_item_digest(self, _isolated_app: Any) -> None:
        _, item, items = _seed_batch()
        resp = client.post(
            "/api/sessions/s1/confirm",
            json={"interaction_id": item.interaction_id, "decision": True, "scope": "once"},
        )

        assert resp.status_code == 200
        approvals = get_approval_store()
        for raw in items:
            assert approvals.is_granted("s1", raw["tool_name"], params_digest(raw["args"]))
            assert not approvals.is_granted("s1", raw["tool_name"], params_digest({"other": 1}))

    def test_deny_rejects_card_without_grants(self, _isolated_app: Any) -> None:
        store, item, items = _seed_batch()
        resp = client.post(
            "/api/sessions/s1/confirm",
            json={"interaction_id": item.interaction_id, "decision": False},
        )

        assert resp.status_code == 200
        assert resp.json()["outcome"] == "denied"
        assert store.get(item.interaction_id).status == "rejected"
        approvals = get_approval_store()
        for raw in items:
            assert not approvals.is_granted("s1", raw["tool_name"], "")

    def test_settled_batch_card_refuses_late_decision(self, _isolated_app: Any) -> None:
        store, item, _ = _seed_batch()
        assert store.resolve(item.interaction_id, {"granted": True, "scope": "session"})

        resp = client.post(
            "/api/sessions/s1/confirm",
            json={"interaction_id": item.interaction_id, "decision": True, "scope": "session"},
        )
        assert resp.status_code == 409, "已结算卡不得再接受决策（防重复授权）"
