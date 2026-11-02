# 功能：契约——审批反馈与 always 级联（，opencode CorrectedError 等价物）。
# 钉行为：拒绝理由经 approval_reply_content 回灌（审批面）/ _check_ui 反馈通道
# （同步 UI 面）/ HTTPUserInteraction resolve dict / CLI 拒绝理由输入；
# allow_session 后同 session 同工具 pending 级联授权（各自 digest 精确授予
# 不同工具不越权）。
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from bandi.core.message import ToolCall
from bandi.core.user_interaction import HTTPUserInteraction, InteractionStore
from bandi.tools.approval import ApprovalStore, PendingApproval, approval_reply_content
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.permissions_pipeline import PermissionsPipeline


def _mk_store(tmp_path: Path) -> ApprovalStore:
    return ApprovalStore(str(tmp_path / "approvals.json"))


def _pending(call_id: str, tool: str, digest: str) -> PendingApproval:
    return PendingApproval(
        tool_call_id=call_id, tool_name=tool, params_digest=digest, reason="requires approval"
    )


# ──────────────────────────────────────────────────────────────────────
# A. 拒绝理由回灌族
# ──────────────────────────────────────────────────────────────────────


class TestDenyFeedback:
    def test_feedback_in_reply_content(self) -> None:
        reply = approval_reply_content("deny", "bash", feedback="请改用 python 脚本")
        payload = json.loads(reply)
        assert "请改用 python 脚本" in payload["note"], f"拒绝理由必须回灌模型可见文本: {payload}"

    def test_deny_without_feedback_unchanged(self) -> None:
        reply = approval_reply_content("deny", "bash")
        payload = json.loads(reply)
        assert "DENIED" in payload["note"]
        assert "feedback" not in payload["note"], "无理由时不出现空反馈段"

    def test_allow_reply_ignores_feedback(self) -> None:
        reply = approval_reply_content("allow_once", "bash", feedback="不应出现")
        assert "不应出现" not in reply


class _Tool(BaseTool):
    name = "bash"
    description = ""
    category = ToolCategory.UTIL

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        return "ok"


class _UIWithFeedback:
    def __init__(self, granted: bool, feedback: str) -> None:
        self._granted = granted
        self._feedback = feedback
        self.calls = 0

    async def ask_permission(self, tool_name: str, args: dict[str, Any], risk: str) -> bool:
        self.calls += 1
        return self._granted

    async def ask_permission_with_feedback(
        self, tool_name: str, args: dict[str, Any], risk: str
    ) -> tuple[bool, str]:
        self.calls += 1
        return self._granted, self._feedback


class _UIBoolOnly:
    async def ask_permission(self, tool_name: str, args: dict[str, Any], risk: str) -> bool:
        return False


class TestUiFeedbackChannel:
    def test_feedback_channel_preferred(self) -> None:
        ui = _UIWithFeedback(False, "别用 rm，改用 python 删除")
        pp = PermissionsPipeline(ask_tools={"bash"}, user_interaction=ui)
        tool = _Tool()
        verdict = asyncio.run(
            pp.check(ToolCall(id="c", name="bash", arguments={"command": "rm x"}), tool, "execute")
        )
        assert not verdict.allow
        assert "别用 rm" in verdict.reason, f"拒绝理由必须进 verdict: {verdict.reason}"
        assert ui.calls == 1, "优先走 ask_permission_with_feedback"

    def test_bool_only_ui_falls_back(self) -> None:
        pp = PermissionsPipeline(ask_tools={"bash"}, user_interaction=_UIBoolOnly())
        tool = _Tool()
        verdict = asyncio.run(
            pp.check(ToolCall(id="c", name="bash", arguments={"command": "ls"}), tool, "execute")
        )
        assert not verdict.allow
        assert "user denied" in verdict.reason

    def test_http_user_interaction_feedback_via_resolve(self) -> None:
        store = InteractionStore()
        ui = HTTPUserInteraction(store, "s-http")

        async def scenario() -> tuple[bool, str]:
            task = asyncio.create_task(ui.ask_permission_with_feedback("bash", {}, "high"))
            await asyncio.sleep(0.05)
            pendings = store.list_pending("s-http", kind="permission")
            assert pendings, "挂起必须登记 pending"
            store.resolve(
                pendings[0].interaction_id,
                {"granted": False, "feedback": "请解释你要做什么"},
            )
            return await task

        granted, feedback = asyncio.run(scenario())
        assert granted is False
        assert feedback == "请解释你要做什么"

    def test_cli_user_interaction_feedback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bandi.cli.user_interaction as cli_ui
        from bandi.cli.user_interaction import CLIUserInteraction

        answers = iter(["n", "请改用 python"])
        monkeypatch.setattr(cli_ui.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
        granted, feedback = asyncio.run(
            CLIUserInteraction().ask_permission_with_feedback("bash", {}, "high")
        )
        assert granted is False and feedback == "请改用 python"

    def test_cli_approve_returns_empty_feedback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bandi.cli.user_interaction as cli_ui
        from bandi.cli.user_interaction import CLIUserInteraction

        answers = iter(["y"])
        monkeypatch.setattr(cli_ui.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
        granted, feedback = asyncio.run(
            CLIUserInteraction().ask_permission_with_feedback("bash", {}, "high")
        )
        assert granted is True and feedback == ""


# ──────────────────────────────────────────────────────────────────────
# B. always 级联放行族
# ──────────────────────────────────────────────────────────────────────


class TestCascadeGrants:
    def test_cascade_same_tool_pending(self, tmp_path: Path) -> None:
        store = _mk_store(tmp_path)
        store.set_pending("s1", _pending("c1", "bash", "d1"))
        store.set_pending("s1", _pending("c2", "bash", "d2"))
        store.set_pending("s1", _pending("c3", "write", "d3"))
        # 调用链模拟：apply_decision 先 pop 队头（被决策条目），级联处理其余
        store.pop_pending("s1")

        cascaded = store.cascade_pending_grants("s1", "bash", scope="session")

        assert [p.tool_call_id for p in cascaded] == ["c2"], "级联其余同工具 pending"
        assert store.is_granted("s1", "bash", "d2"), "级联条目各自 digest 精确授予"
        assert not store.is_granted("s1", "write", "d3"), "不同工具不得越权"
        remaining = store.list_pending("s1")
        assert [p.tool_call_id for p in remaining] == ["c3"]

    def test_cascade_no_pending_returns_empty(self, tmp_path: Path) -> None:
        store = _mk_store(tmp_path)
        assert store.cascade_pending_grants("s-none", "bash", scope="session") == []

    def test_cascade_does_not_loop_forever(self, tmp_path: Path) -> None:
        store = _mk_store(tmp_path)
        for i in range(4):
            store.set_pending("s2", _pending(f"c{i}", "bash", f"d{i}"))
        store.pop_pending("s2")
        first = store.cascade_pending_grants("s2", "bash", scope="session")
        second = store.cascade_pending_grants("s2", "bash", scope="session")
        assert [p.tool_call_id for p in first] == ["c1", "c2", "c3"]
        assert second == [], "二次级联必须为空（幂等）"


# ──────────────────────────────────────────────────────────────────────
# C. API 端点族
# ──────────────────────────────────────────────────────────────────────


class TestApprovalApi:
    def _client(self, monkeypatch: pytest.MonkeyPatch, store: ApprovalStore) -> Any:
        import importlib

        from fastapi.testclient import TestClient

        app_mod = importlib.import_module("bandi.api.app")
        monkeypatch.setattr(app_mod, "_API_KEY", "test")
        monkeypatch.setattr("bandi.tools.approval._STORE", store)
        client = TestClient(app_mod.app)
        client.headers["Authorization"] = "Bearer test"
        return client

    def test_api_deny_with_feedback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _mk_store(tmp_path)
        store.set_pending("s-api", _pending("c1", "bash", "d1"))
        client = self._client(monkeypatch, store)

        resp = client.post(
            "/api/sessions/s-api/approval-decision",
            json={"decision": "deny", "feedback": "危险命令，请换一种写法"},
        )

        assert resp.status_code == 200, resp.text
        note = json.loads(resp.json()["reply_content"])["note"]
        assert "危险命令，请换一种写法" in note, "API 拒绝理由必须回灌模型可见文本"

    def test_api_allow_session_cascades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _mk_store(tmp_path)
        store.set_pending("s-api", _pending("c1", "bash", "d1"))
        store.set_pending("s-api", _pending("c2", "bash", "d2"))
        client = self._client(monkeypatch, store)

        resp = client.post(
            "/api/sessions/s-api/approval-decision", json={"decision": "allow_session"}
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cascaded"] == ["c2"], f"级联结果必须在响应中可见: {body}"
        assert store.is_granted("s-api", "bash", "d2")
        assert store.list_pending("s-api") == [], "级联后 pending 必须清空"

    def test_api_allow_once_no_cascade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _mk_store(tmp_path)
        store.set_pending("s-api", _pending("c1", "bash", "d1"))
        store.set_pending("s-api", _pending("c2", "bash", "d2"))
        client = self._client(monkeypatch, store)

        resp = client.post("/api/sessions/s-api/approval-decision", json={"decision": "allow_once"})

        assert resp.status_code == 200
        assert resp.json()["cascaded"] == [], "allow_once 不得级联"
        assert store.list_pending("s-api")[0].tool_call_id == "c2"
