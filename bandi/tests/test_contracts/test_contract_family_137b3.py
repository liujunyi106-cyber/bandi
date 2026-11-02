from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.company.seat_progress import EmployeeProgressHandler
from bandi.core.agent_runner import _emit_terminal_reply_frame, _HandlerBindings
from bandi.core.events import Event, EventType
from bandi.core.message import Message, MessageRole, ToolCall
from bandi.core.process_recorder import ProcessRecorder
from bandi.core.stream_handler import StreamHandler
from bandi.core.user_interaction import HTTPUserInteraction, InteractionStore
from bandi.memory.session.store import SessionStore
from bandi.tools.base import BaseTool, ToolPermissionContext
from bandi.tools.executor import ToolExecutor
from bandi.tools.permissions_pipeline import PermissionsPipeline
from bandi.tools.registry import ToolRegistry
from bandi.tools.tool_retry import locate_tool_call, retry_tool_call
from bandi.tools.user_input import UserInputTool

# 功能：契约——-3 破坏性/决策端点畸形输入 fail-closed
# 钉行为（不钉实现）：


# bandi.api.__init__ 执行 from .app import app 后包属性遮蔽子模块——
# 须经 sys.modules 取模块真身（import bandi.api.app as m 会绑到 FastAPI 实例）
app_module = sys.modules["bandi.api.app"]

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重置 interaction store 单例 + 鉴权（每测试独立 store）。"""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    with (
        patch("bandi.api.app._interaction_store", None),
        patch("bandi.api.app._API_KEY", "test"),
    ):
        yield


def _register(kind: str = "confirm", session_id: str = "s1") -> str:
    store = app_module._get_interaction_store()
    item = store.register(session_id=session_id, kind=kind, payload={"probe": True})
    return item.interaction_id


class TestConfirmDecisionRequired:
    def test_missing_decision_400(self) -> None:
        """缺 decision 不得默认批准（fail-closed——原 bool(body.get("decision", True))）。"""
        iid = _register()
        resp = client.post("/api/sessions/s1/confirm", json={"interaction_id": iid})
        assert resp.status_code == 400

    def test_non_bool_decision_400(self) -> None:
        """非 bool decision 不得被 bool() 强转（字符串 'false' 曾静默=批准）。"""
        iid = _register()
        resp = client.post(
            "/api/sessions/s1/confirm", json={"interaction_id": iid, "decision": "false"}
        )
        assert resp.status_code == 400

    def test_int_decision_400(self) -> None:
        """int（1/0）是 isinstance-bool 最现实的绕过路径——必须 400。"""
        iid = _register()
        resp = client.post("/api/sessions/s1/confirm", json={"interaction_id": iid, "decision": 1})
        assert resp.status_code == 400


@pytest.fixture()
def api_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真实 tmp SessionStore 注入。"""
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(tmp_path / "sess.db"))
    sid = store.create_session(mode="execute")
    monkeypatch.setattr(app_module, "_store", store)
    yield store, sid
    store.close()


class TestRevertStageEntryId:
    def test_null_entry_id_400(self, api_store) -> None:
        """null（及数字等非字符串）不得归一为空串触发全截断。"""
        _, sid = api_store
        resp = client.post(f"/api/sessions/{sid}/revert/stage", json={"entry_id": None})
        assert resp.status_code == 400

    def test_empty_string_entry_id_allowed(self, api_store) -> None:
        """显式空串 = 编辑首条「从此处重开」全截断（-3 语义）——必须保持可用。"""
        _, sid = api_store
        resp = client.post(f"/api/sessions/{sid}/revert/stage", json={"entry_id": ""})
        assert resp.status_code == 200
        assert resp.json()["entry_id"] == ""

    def test_invalid_entry_id_400_before_session_lookup(self) -> None:
        """参数校验先于 session 存在性（未知会话 + null entry_id → 400 而非 404）——入口 fail-closed。"""
        resp = client.post("/api/sessions/no-such/revert/stage", json={"entry_id": None})
        assert resp.status_code == 400


class TestRevertFilesParams:
    def test_bool_turn_400(self, api_store) -> None:
        """bool 是 int 子类——true 不得当 turn=1 触发真实文件回滚。"""
        _, sid = api_store
        resp = client.post(f"/api/sessions/{sid}/revert-files", json={"turn": True})
        assert resp.status_code == 400

    def test_non_bool_dry_run_400(self, api_store) -> None:
        """dry_run 非 bool 不得强转（'false'→True 语义漂移）。"""
        _, sid = api_store
        resp = client.post(
            f"/api/sessions/{sid}/revert-files", json={"turn": 1, "dry_run": "false"}
        )
        assert resp.status_code == 400

    def test_int_dry_run_400(self, api_store) -> None:
        """int（1）不得当 bool dry_run——与 turn 的 bool 收窄同族。"""
        _, sid = api_store
        resp = client.post(f"/api/sessions/{sid}/revert-files", json={"turn": 1, "dry_run": 1})
        assert resp.status_code == 400


# ==================== 137b3_artifacts ====================


def r1x137b3__run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Inner:
    async def on_tool_result(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def on_tool_call_start(self, *args: Any) -> None:
        return None


def _make(tmp_path: Any) -> tuple[ProcessRecorder, SessionStore, str]:
    store = SessionStore(str(tmp_path / "s.db"))
    sid = store.create_session()
    return ProcessRecorder(_Inner(), store, sid), store, sid


def test_write_tool_success_registers_artifact(tmp_path: Any) -> None:
    target = tmp_path / "out.txt"
    target.write_text("产物内容", encoding="utf-8")
    recorder, store, sid = _make(tmp_path)
    r1x137b3__run(recorder.on_tool_call_start(sid, "write", {"path": str(target)}, "call_1"))
    r1x137b3__run(recorder.on_tool_result(sid, "write", True, "ok", 5, "call_1"))
    artifacts = store.list_artifacts(sid)
    assert len(artifacts) == 1, f"write 成功必须登记产物：{artifacts}"
    assert artifacts[0]["tool_name"] == "write"
    assert artifacts[0]["path"] == str(target)
    assert artifacts[0]["artifact_type"] == "file"
    assert len(artifacts[0]["hash"]) == 64, "存在的文件必须带 sha256"


def test_failed_or_non_write_tool_not_registered(tmp_path: Any) -> None:
    """破坏者：失败 write 与 bash 成功都不得登记（防产物清单膨胀/虚报）。"""
    recorder, store, sid = _make(tmp_path)
    r1x137b3__run(
        recorder.on_tool_call_start(sid, "write", {"path": str(tmp_path / "x.txt")}, "c1")
    )
    r1x137b3__run(recorder.on_tool_result(sid, "write", False, "boom", 1, "c1"))
    r1x137b3__run(recorder.on_tool_call_start(sid, "bash", {"cmd": "ls"}, "c2"))
    r1x137b3__run(recorder.on_tool_result(sid, "bash", True, "done", 1, "c2"))
    assert store.list_artifacts(sid) == []


def test_unreachable_path_registers_metadata_without_hash(tmp_path: Any) -> None:
    """相对路径/不可达路径 → hash 空、元数据仍登记（清单完整性优先）。"""
    recorder, store, sid = _make(tmp_path)
    r1x137b3__run(recorder.on_tool_call_start(sid, "edit", {"path": "relative/dir/f.txt"}, "c1"))
    r1x137b3__run(recorder.on_tool_result(sid, "edit", True, "ok", 1, "c1"))
    artifacts = store.list_artifacts(sid)
    assert len(artifacts) == 1
    assert artifacts[0]["path"] == "relative/dir/f.txt"
    assert artifacts[0]["hash"] == ""


# ==================== 137b3_multi_questions ====================


def r2x137b3__run(coro: Any) -> Any:
    return asyncio.run(coro)


class _LegacyUI:
    # 功能：仅实现单问通道的旧交互实现（无 ask_questions）——退化路径对照
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def ask_input(self, prompt: str, *, secret: bool = False) -> str:
        self.prompts.append(prompt)
        return "ok"

    async def ask_permission(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def ask_confirmation(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def show_notification(self, *args: Any, **kwargs: Any) -> None:
        return None


def test_multi_questions_single_suspend_and_json_answers() -> None:
    store = InteractionStore()
    frames: list[Any] = []

    async def on_interaction(item: Any) -> None:
        frames.append(item)

    ui = HTTPUserInteraction(store, "s1", on_interaction=on_interaction)
    tool = UserInputTool(ui=ui)

    async def scenario() -> dict[str, Any]:
        task = asyncio.create_task(
            tool.execute(
                {
                    "reason": "配置",
                    "questions": [
                        {"id": "q1", "question": "部署到哪个环境？"},
                        {"id": "q2", "question": "回滚策略？"},
                    ],
                }
            )
        )
        await asyncio.sleep(0.05)
        # 破坏者视角：必须只挂起一次（两个问题不得产生两次挂起）
        assert len(frames) == 1
        payload = frames[0].payload
        assert len(payload["questions"]) == 2
        assert "prompt" not in payload
        store.resolve(frames[0].interaction_id, json.dumps({"q1": "prod", "q2": "手动"}))
        return await task

    result = r2x137b3__run(scenario())
    assert result["answers"] == {"q1": "prod", "q2": "手动"}


def test_single_question_keeps_prompt_channel() -> None:
    store = InteractionStore()
    frames: list[Any] = []

    async def on_interaction(item: Any) -> None:
        frames.append(item)

    ui = HTTPUserInteraction(store, "s1", on_interaction=on_interaction)
    tool = UserInputTool(ui=ui)

    async def scenario() -> dict[str, Any]:
        task = asyncio.create_task(
            tool.execute({"reason": "x", "questions": [{"id": "q1", "question": "环境？"}]})
        )
        await asyncio.sleep(0.05)
        assert len(frames) == 1
        assert "prompt" in frames[0].payload  # 单问仍走原 prompt 通道（兼容）
        assert "questions" not in frames[0].payload
        store.resolve(frames[0].interaction_id, "prod")
        return await task

    result = r2x137b3__run(scenario())
    assert result["answers"] == {"q1": "prod"}


def test_bad_json_answers_fail_closed() -> None:
    store = InteractionStore()
    frames: list[Any] = []

    async def on_interaction(item: Any) -> None:
        frames.append(item)

    ui = HTTPUserInteraction(store, "s1", on_interaction=on_interaction)

    async def scenario() -> dict[str, str]:
        task = asyncio.create_task(ui.ask_questions([{"id": "q1", "question": "?"}]))
        await asyncio.sleep(0.05)
        store.resolve(frames[0].interaction_id, "not-json")
        return await task

    assert r2x137b3__run(scenario()) == {}


def test_multi_questions_without_paged_channel_falls_back() -> None:
    ui = _LegacyUI()
    tool = UserInputTool(ui=ui)
    result = r2x137b3__run(
        tool.execute(
            {
                "reason": "x",
                "questions": [
                    {"id": "q1", "question": "A？"},
                    {"id": "q2", "question": "B？"},
                ],
            }
        )
    )
    assert ui.prompts == ["A？: ", "B？: "]
    assert result["answers"] == {"q1": "ok", "q2": "ok"}


# ==================== 137b3_process_persist ====================


def r3x137b3__run(coro: Any) -> Any:
    return asyncio.run(coro)


class _InnerHandler:
    # 功能：内层 handler 桩——记录转发调用（转发一致性证据）
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def on_content_delta(self, *args: Any) -> None:
        self.calls.append(("content_delta", args))

    async def on_thinking(self, *args: Any) -> None:
        self.calls.append(("thinking", args))

    async def on_tool_call_start(self, *args: Any) -> None:
        self.calls.append(("tool_call_start", args))

    async def on_tool_result(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("tool_result", args))

    async def on_tool_progress(self, *args: Any) -> None:
        self.calls.append(("tool_progress", args))

    async def on_skill_injected(self, *args: Any) -> None:
        self.calls.append(("skill_injected", args))

    async def on_note(self, *args: Any) -> None:
        self.calls.append(("note", args))

    async def on_memory_written(self, *args: Any) -> None:
        self.calls.append(("memory_written", args))

    async def on_task_ledger(self, *args: Any) -> None:
        self.calls.append(("task_ledger", args))

    async def on_done(self, *args: Any) -> None:
        self.calls.append(("done", args))

    async def on_error(self, *args: Any) -> None:
        self.calls.append(("error", args))


def _recorder_with_store(tmp_path: Any) -> tuple[ProcessRecorder, _InnerHandler, SessionStore, str]:
    store = SessionStore(str(tmp_path / "s.db"))
    sid = store.create_session()
    inner = _InnerHandler()
    recorder = ProcessRecorder(inner, store, sid)
    return recorder, inner, store, sid


def test_recorder_forwards_and_persists_on_done(tmp_path: Any) -> None:
    recorder, inner, store, sid = _recorder_with_store(tmp_path)
    r3x137b3__run(recorder.on_thinking("s", "想一想"))
    r3x137b3__run(recorder.on_tool_call_start("s", "bash", {"cmd": "ls"}, "call_1"))
    r3x137b3__run(recorder.on_tool_result("s", "bash", True, "ok", 12, "call_1"))
    r3x137b3__run(recorder.on_skill_injected("s", ["git-workflow"]))
    r3x137b3__run(recorder.on_memory_written("s", ["long_term"]))
    r3x137b3__run(recorder.on_done(sid, "COMPLETED", 10, 1))
    # 转发一致性：内层收到全部调用（含 done）
    assert ("tool_call_start", ("s", "bash", {"cmd": "ls"}, "call_1")) in inner.calls
    assert inner.calls[-1][0] == "done"
    entries = store.get_branch(sid)
    proc = [e for e in entries if e["entry_type"] == "process"]
    assert len(proc) == 1, f"轮末必须落一条 process 条目：{[e['entry_type'] for e in entries]}"
    nodes = json.loads(proc[0]["metadata"])["process"]
    kinds = [n["kind"] for n in nodes]
    assert kinds == ["thought", "tool", "skill", "note"], f"节点序还原：{kinds}"
    tool = nodes[1]
    assert tool["toolName"] == "bash" and tool["status"] == "success"
    assert tool["toolCallId"] == "call_1" and tool["detail"] == "ok" and tool["durationMs"] == 12
    assert nodes[0]["text"] == "想一想"
    assert nodes[3] == {
        "id": nodes[3]["id"],
        "kind": "note",
        "noteType": "memory_written",
        "count": 1,
        "status": "success",
        # 记忆写入带通道：前端据此分层显示长期/情景/草稿
        "channels": ["long_term"],
    }


def test_recorder_no_events_no_entry(tmp_path: Any) -> None:
    """破坏者：纯文本回复不落空 process 条目（防空条目膨胀）。"""
    recorder, _inner, store, sid = _recorder_with_store(tmp_path)
    r3x137b3__run(recorder.on_content_delta("s", "你好"))
    r3x137b3__run(recorder.on_done(sid, "COMPLETED", 1, 1))
    entries = store.get_branch(sid)
    assert all(e["entry_type"] != "process" for e in entries)


def test_recorder_running_tool_converges_at_done(tmp_path: Any) -> None:
    """tool_result 缺失（轮被打断）→ on_done 时 running 工具收敛为 error。"""
    recorder, _inner, store, sid = _recorder_with_store(tmp_path)
    r3x137b3__run(recorder.on_tool_call_start("s", "bash", {"cmd": "ls"}, "call_2"))
    r3x137b3__run(recorder.on_done(sid, "INTERRUPT", 1, 1))
    entries = store.get_branch(sid)
    proc = [e for e in entries if e["entry_type"] == "process"]
    nodes = json.loads(proc[0]["metadata"])["process"]
    assert nodes[0]["status"] == "error"


def test_skip_tool_node_in_persisted_entry(tmp_path: Any) -> None:
    """跳过态随存：按 tool_call_id 翻转 process 条目内节点；未知 id 显式失败。"""
    recorder, _inner, store, sid = _recorder_with_store(tmp_path)
    r3x137b3__run(recorder.on_tool_call_start("s", "bash", {"cmd": "ls"}, "call_3"))
    r3x137b3__run(recorder.on_tool_result("s", "bash", False, "boom", 5, "call_3"))
    r3x137b3__run(recorder.on_done(sid, "COMPLETED", 1, 1))
    assert store.skip_process_tool_node(sid, "call_3") is True
    entries = store.get_branch(sid)
    proc = [e for e in entries if e["entry_type"] == "process"]
    nodes = json.loads(proc[0]["metadata"])["process"]
    assert nodes[0]["status"] == "skipped"
    # 破坏者：未知 tool_call_id → False（不静默成功）
    assert store.skip_process_tool_node(sid, "ghost") is False


# ==================== 137b3_retry_tool ====================


def r4x137b3__run(coro: Any) -> Any:
    return asyncio.run(coro)


class _CountingTool(BaseTool):
    # 功能：计数桩——记录每次真实执行（force 真跑的观测证据）
    name = "counting_probe"
    description = "test counting tool"
    is_concurrency_safe = True

    def __init__(self) -> None:
        self.executions: list[dict[str, Any]] = []

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        self.executions.append(dict(params))
        return f"ran with {params.get('path', '')}"


class _AskTool(BaseTool):
    # 功能：ask 类桩——risk_level=high 触发审批门（headless fail-closed 对照）
    name = "ask_probe"
    description = "ask-class tool"
    risk_level = "high"

    def __init__(self) -> None:
        self.executions: list[dict[str, Any]] = []

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        self.executions.append(dict(params))
        return "should not run"


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """API 隔离：Path.home → tmp_path + 测试 API key（同 test_ws_sse_linkage 口径）。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import importlib

    api_module = importlib.import_module("bandi.api.app")
    monkeypatch.setattr(api_module, "_API_KEY", "test-key")
    return tmp_path


def _make_store_with_call(tmp_path: Any, call: dict[str, Any]) -> SessionStore:
    store = SessionStore(str(tmp_path / "sessions.db"))
    sid = store.create_session()
    msg = Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id=call["id"], name=call["name"], arguments=call["arguments"])],
    )
    store.append_message(sid, msg)
    return store


def test_locate_tool_call_by_id(tmp_path: Any) -> None:
    call = {"id": "call_x", "name": "counting_probe", "arguments": {"path": "a.txt"}}
    store = _make_store_with_call(tmp_path, call)
    found = locate_tool_call(store, store.list_sessions()[0]["id"], "call_x")
    assert found == call, f"必须按 id 定位到完整调用：{found}"


def test_locate_unknown_id_returns_none(tmp_path: Any) -> None:
    """破坏者：未知 tool_call_id（伪造/他会的）→ None，不得瞎猜执行。"""
    store = SessionStore(str(tmp_path / "sessions.db"))
    assert locate_tool_call(store, "any-session", "ghost_id") is None


def test_retry_force_reruns_every_time(tmp_path: Any) -> None:
    """破坏者：结果缓存/幂等命中不得吞掉重试——两次重试 = 两次真实执行。"""
    call = {"id": "call_1", "name": "counting_probe", "arguments": {"path": "x.txt"}}
    store = _make_store_with_call(tmp_path, call)
    sid = store.list_sessions()[0]["id"]
    tool = _CountingTool()
    registry = ToolRegistry()
    registry.register_instance(tool)
    first = r4x137b3__run(retry_tool_call(sid, "call_1", store=store, registry=registry))
    second = r4x137b3__run(retry_tool_call(sid, "call_1", store=store, registry=registry))
    assert first["success"] is True and second["success"] is True
    assert len(tool.executions) == 2, f"每次重试必须真跑：实际执行 {len(tool.executions)} 次"
    assert first["output_preview"] == "ran with x.txt"
    assert isinstance(first["duration_ms"], int) and first["duration_ms"] >= 0
    assert first["tool_name"] == "counting_probe"


def test_retry_passes_stored_arguments(tmp_path: Any) -> None:
    call = {"id": "call_2", "name": "counting_probe", "arguments": {"path": "real.txt"}}
    store = _make_store_with_call(tmp_path, call)
    sid = store.list_sessions()[0]["id"]
    tool = _CountingTool()
    registry = ToolRegistry()
    registry.register_instance(tool)
    r4x137b3__run(retry_tool_call(sid, "call_2", store=store, registry=registry))
    assert tool.executions == [{"path": "real.txt"}], "重试必须还原存储的原始参数"


def test_retry_ask_tool_fail_closed_without_ui(tmp_path: Any) -> None:
    """零提权面：ask 类工具 headless 重试 → APPROVAL_REQUIRED 提示，工具不执行。"""
    call = {"id": "call_3", "name": "ask_probe", "arguments": {}}
    store = _make_store_with_call(tmp_path, call)
    sid = store.list_sessions()[0]["id"]
    tool = _AskTool()
    registry = ToolRegistry()
    registry.register_instance(tool)
    executor = ToolExecutor(
        registry,
        permissions_pipeline=PermissionsPipeline(ask_tools={"ask_probe"}),
    )
    result = r4x137b3__run(
        retry_tool_call(sid, "call_3", store=store, registry=registry, executor=executor)
    )
    assert result["success"] is False, "ask 类工具无交互通道必须失败"
    assert "APPROVAL_REQUIRED" in (result["error"] or ""), f"须返回审批提示：{result['error']}"
    assert tool.executions == [], "fail-closed 路径不得真实执行工具"


@pytest.mark.asyncio
async def test_retry_endpoint_production_assembly(home: Any) -> None:
    """生产主链路：端点走 build_tool_registry 全装配——真实 read 工具真跑不存在路径。"""
    # 端点读 _get_store 单例——测试数据必须写进同一实例（重置防早前测试遗留路径）
    import sys

    from fastapi.testclient import TestClient

    from bandi.api.app import _get_store, app

    api_module = sys.modules["bandi.api.app"]

    with api_module._store_lock:
        api_module._store = None
    store = _get_store()
    sid = store.create_session()
    store.append_message(
        sid,
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(id="call_prod", name="read", arguments={"path": "no_such_file.txt"})
            ],
        ),
    )
    with TestClient(app) as r4x137b3_client:
        headers = {"Authorization": "Bearer test-key"}
        resp = r4x137b3_client.post(
            f"/api/sessions/{sid}/retry-tool",
            json={"tool_call_id": "call_prod"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tool_name"] == "read"
        assert body["success"] is False, "读取不存在的文件必须真跑并失败"
        assert body["error"], "失败必须带错误信息"
        assert "duration_ms" in body and isinstance(body["duration_ms"], int)

        missing = r4x137b3_client.post(
            f"/api/sessions/{sid}/retry-tool",
            json={"tool_call_id": "ghost"},
            headers=headers,
        )
        assert missing.status_code == 404, "未知 tool_call_id 必须 404"

        bad = r4x137b3_client.post(f"/api/sessions/{sid}/retry-tool", json={}, headers=headers)
        assert bad.status_code in (400, 422), "缺 tool_call_id 必须拒绝"


# ==================== 137b3_review_fixes ====================
# 功能：契约——-3 第二轮修复批（终止补帧 / 工具失败回喂语义 / plan 只读白名单）
# 钉行为（不钉实现）：
# 正常路径（无 final_message 或已流式发出）不补帧；帧异常不阻断收尾。
# 连续失败达阈值后暂停工具（拦截不执行 + 指引文案）；暂停后仍被反复调用
# ≥ _BLOCKED_RETRY_LIMIT 次 → should_abort 硬终止（防死循环）；成功后计数复位。


class _FlakyTool(BaseTool):
    """可控失败工具：前 fail_times 次执行失败，之后成功（用于复位验证）。"""

    name: str = "flaky_probe"
    description: str = "probe"
    is_read_only: bool = True
    parameters: dict = {"type": "object", "properties": {}}
    fail_times: int = 3
    calls: int = 0

    async def execute(self, params, context=None):  # type: ignore[override]
        type(self).calls += 1
        if type(self).calls <= self.fail_times:
            raise RuntimeError("boom")
        return {"ok": True}


def _make_executor(threshold: int = 3) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register_instance(_FlakyTool())
    return ToolExecutor(registry=registry, consecutive_failure_threshold=threshold)


def _call() -> ToolCall:
    return ToolCall(id="c1", name="flaky_probe", arguments={})


class TestFailureFeedsBack:
    def setup_method(self) -> None:
        _FlakyTool.calls = 0
        _FlakyTool.fail_times = 3

    def test_failures_do_not_abort_turn(self) -> None:
        async def scenario() -> list[object]:
            executor = _make_executor()
            return [await executor._execute_one(_call()) for _ in range(3)]

        results = asyncio.run(scenario())
        assert all(r.success is False for r in results)
        assert all(r.should_abort is False for r in results), "失败必须回喂 LLM（不熔断整轮）"

    def test_blocked_tool_pauses_with_guidance(self) -> None:
        async def scenario() -> tuple[object, dict[str, int]]:
            executor = _make_executor()
            for _ in range(3):
                await executor._execute_one(_call())
            blocked = await executor._execute_one(_call())  # 第 4 次：前置拦截（暂停）
            return blocked, executor._block_count

        blocked, block_count = asyncio.run(scenario())
        assert blocked.success is False and blocked.should_abort is False
        assert "暂停" in blocked.error, f"暂停文案缺失：{blocked.error}"
        assert block_count["flaky_probe"] == 1

    def test_hard_stop_after_repeated_blocked_calls(self) -> None:
        async def scenario() -> object:
            executor = _make_executor()
            for _ in range(3):
                await executor._execute_one(_call())
            last = None
            for _ in range(3):  # 暂停后仍反复调用 → 第 3 次拦截硬终止
                last = await executor._execute_one(_call())
            return last

        last = asyncio.run(scenario())
        assert last.should_abort is True, "暂停后硬撞 3 次必须终止（防死循环烧钱）"

    def test_success_resets_counters(self) -> None:
        _FlakyTool.fail_times = 2

        async def scenario() -> tuple[object, dict[str, int], dict[str, int]]:
            executor = _make_executor()
            await executor._execute_one(_call())
            await executor._execute_one(_call())
            ok = await executor._execute_one(_call())  # 第 3 次成功
            return ok, executor._failure_count, executor._block_count

        ok, failure_count, block_count = asyncio.run(scenario())
        assert ok.success is True
        assert failure_count.get("flaky_probe") is None
        assert block_count.get("flaky_probe") is None


class _FrameHandler:
    def __init__(self) -> None:
        self.frames: list[tuple[str, str]] = []

    async def on_content_delta(self, session_id: str, delta: str) -> None:
        self.frames.append((session_id, delta))


class TestTerminalReplyFrame:
    def test_emits_when_final_message_differs(self) -> None:
        handler = _FrameHandler()
        asyncio.run(
            _emit_terminal_reply_frame(handler, "s1", "工具执行被中止", "", "工具执行被中止")
        )
        assert handler.frames == [("s1", "工具执行被中止")]

    def test_skips_when_no_final_message_or_already_streamed(self) -> None:
        handler = _FrameHandler()
        asyncio.run(_emit_terminal_reply_frame(handler, "s1", None, "hi", "hi"))
        asyncio.run(_emit_terminal_reply_frame(handler, "s1", "hi", "hi", "hi"))
        assert handler.frames == []

    def test_frame_failure_does_not_raise(self) -> None:
        class _BoomHandler:
            async def on_content_delta(self, session_id: str, delta: str) -> None:
                raise RuntimeError("disconnected")

        asyncio.run(_emit_terminal_reply_frame(_BoomHandler(), "s1", "x", "", "x"))


class TestPlanModeReadonlyWhitelist:
    def test_readonly_tools_allowed(self) -> None:
        names = set(ToolRegistry.PLAN_MODE_TOOLS)
        for required in (
            "read",
            "list_dir",
            "glob",
            "grep",
            "memory_search",
            "memory_read",
            "knowledge_search",
            "ontology_query",
            "ontology_query",
            "web_search",
            "web_fetch",
            "read_document",
            "conversation_history",
            "tool_search",
            "git_status",
            "git_diff",
            "db_inspect",
            "data_aggregate",
        ):
            assert required in names, f"只读工具 {required} 必须在 plan 白名单"

    def test_write_tools_excluded(self) -> None:
        names = set(ToolRegistry.PLAN_MODE_TOOLS)
        for denied in (
            "write",
            "edit",
            "apply_patch",
            "bash",
            "python_exec",
            "git_commit",
            "memory_forget",
            "knowledge_ingest",
            "data_clean",
            "db_execute",
            "canvas_publish",
        ):
            assert denied not in names, f"写类工具 {denied} 不得在 plan 白名单"

    def test_mode_tools_included(self) -> None:
        names = set(ToolRegistry.PLAN_MODE_TOOLS)
        assert "exit_plan_mode" in names and "enter_plan_mode" in names


def test_recovery_set_single_source() -> None:
    """单一来源：恢复安全只读集合 == registry 只读全量（同源派生）。"""
    from bandi.agent.states import RECOVERY_SAFE_READONLY_TOOLS

    assert set(RECOVERY_SAFE_READONLY_TOOLS) == set(ToolRegistry.READONLY_TOOL_NAMES)


# ==================== 137b3_right_panel_api ====================
# 功能：契约——-3 右栏 v5 后端数据面（文件浏览 / 技能列表 / SOP 进度）
# 钉行为（不钉实现）：
# 越界 403 / 敏感路径 403 / 不存在 404 —— fail-closed（与制品预览同口径）


# bandi.api.__init__ 执行 from .app import app 后包属性遮蔽子模块——须经 sys.modules 取模块真身
r6x137b3_app_module = sys.modules["bandi.api.app"]
r6x137b3_client = TestClient(app)
r6x137b3_client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def r6x137b3__isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with (
        patch("bandi.api.app._interaction_store", None),
        patch("bandi.api.app._API_KEY", "test"),
    ):
        yield


@pytest.fixture()
def panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真实 tmp SessionStore + 项目目录（文本/二进制/超大/敏感/忽略名单样本）。"""
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(tmp_path / "sess.db"))
    project = tmp_path / "proj"
    (project / "notes" / "sub").mkdir(parents=True)
    (project / "notes" / "readme.md").write_text("# 标题\n正文", encoding="utf-8")
    (project / "bin.dat").write_bytes(b"\x00\x01\x02\xff\xfe")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("[core]", encoding="utf-8")
    (project / "node_modules").mkdir()
    (project / "big.txt").write_text("x" * 5000, encoding="utf-8")
    sid = store.create_session(mode="execute", project_root=str(project))
    monkeypatch.setattr(r6x137b3_app_module, "_store", store)
    yield store, sid, project
    store.close()


class TestFileTree:
    def test_list_root_hides_ignored_and_sorts_dirs_first(self, panel) -> None:
        _, sid, _ = panel
        resp = r6x137b3_client.get(f"/api/sessions/{sid}/files")
        assert resp.status_code == 200
        body = resp.json()
        names = [e["name"] for e in body["entries"]]
        assert ".git" not in names and "node_modules" not in names, "忽略名单不得出现在文件树"
        assert names.index("notes") < names.index("bin.dat"), "目录排前"
        assert any(e["name"] == "bin.dat" and e["type"] == "file" for e in body["entries"])

    def test_list_subdir_by_path(self, panel) -> None:
        _, sid, _ = panel
        resp = r6x137b3_client.get(f"/api/sessions/{sid}/files", params={"path": "notes"})
        assert resp.status_code == 200
        # 目录在前（sub 是目录、readme.md 是文件）
        assert [e["name"] for e in resp.json()["entries"]] == ["sub", "readme.md"]
        assert resp.json()["path"] == "notes"

    def test_outside_root_403(self, panel) -> None:
        _, sid, _ = panel
        resp = r6x137b3_client.get(f"/api/sessions/{sid}/files", params={"path": "../"})
        assert resp.status_code == 403

    def test_unknown_session_404(self) -> None:
        resp = r6x137b3_client.get("/api/sessions/no-such/files")
        assert resp.status_code == 404


class TestFileContent:
    def test_text_file_returns_content(self, panel) -> None:
        _, sid, _ = panel
        resp = r6x137b3_client.get(
            f"/api/sessions/{sid}/files/content", params={"path": "notes/readme.md"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "text" and "标题" in body["content"]
        assert body["path"] == "notes/readme.md"

    def test_binary_returns_metadata_only(self, panel) -> None:
        _, sid, _ = panel
        resp = r6x137b3_client.get(f"/api/sessions/{sid}/files/content", params={"path": "bin.dat"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "binary" and body["content"] == ""

    def test_too_large_fail_closed_no_partial_content(self, panel) -> None:
        _, sid, _ = panel
        resp = r6x137b3_client.get(
            f"/api/sessions/{sid}/files/content", params={"path": "big.txt", "max_bytes": 1024}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "too_large" and body["content"] == "", "超大不返回半截内容"

    def test_sensitive_segment_403(self, panel) -> None:
        _, sid, _ = panel
        resp = r6x137b3_client.get(
            f"/api/sessions/{sid}/files/content", params={"path": ".git/config"}
        )
        assert resp.status_code == 403, "敏感目录（.git）必须 fail-closed"

    def test_file_not_found_404(self, panel) -> None:
        _, sid, _ = panel
        resp = r6x137b3_client.get(
            f"/api/sessions/{sid}/files/content", params={"path": "nope.txt"}
        )
        assert resp.status_code == 404


class TestSkillsList:
    def test_returns_list_payload_shape(self) -> None:
        resp = r6x137b3_client.get("/api/skills")
        assert resp.status_code == 200
        skills = resp.json()["skills"]
        assert isinstance(skills, list), "技能列表必须是数组（内置技能可存在，不假设空）"
        for item in skills:
            assert {"name", "description", "tags", "team_id", "always"} <= set(item)


class TestSopProgress:
    def test_no_frames_returns_null(self, panel) -> None:
        _, sid, _ = panel
        resp = r6x137b3_client.get(f"/api/sessions/{sid}/sop")
        assert resp.status_code == 200
        assert resp.json()["sop"] is None

    def test_frames_projection_readonly(self, panel) -> None:
        _, sid, _ = panel
        from bandi.skills.frame_store import save_frames
        from bandi.skills.progress import frame_scope_for_session
        from bandi.skills.runtime import SkillFrame, SkillFrameStack, SkillFrameStatus

        scope = frame_scope_for_session(sid)
        frame = SkillFrame(
            task_id="t1",
            skill_id="skill_a",
            node_id="n1",
            status=SkillFrameStatus.ACTIVE,
            slots={"secret_field": "不应外泄的槽位值"},
            created_at="2026-09-25T00:00:00",
            updated_at="2026-09-25T00:00:00",
        )
        save_frames(SkillFrameStack(scope_id=scope, active=frame, pending=[], completed=[]))
        resp = r6x137b3_client.get(f"/api/sessions/{sid}/sop")
        assert resp.status_code == 200
        sop = resp.json()["sop"]
        assert sop and sop["active"]["task_id"] == "t1"
        assert "不应外泄" not in json.dumps(sop), "只读投影不得含槽位值"

    def test_unknown_session_404(self) -> None:
        resp = r6x137b3_client.get("/api/sessions/no-such/sop")
        assert resp.status_code == 404


# ==================== 137b3_tool_call_id ====================


def r7x137b3__run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── 协议层：签名向后兼容（默认 ""）─────────────────────────────────────────


def test_protocol_signature_has_tool_call_id_with_empty_default() -> None:
    """StreamHandler Protocol 两个工具方法必须接受 tool_call_id 且默认空串（旧实现不破坏）。"""
    for method_name in ("on_tool_call_start", "on_tool_result"):
        method = getattr(StreamHandler, method_name)
        params = inspect.signature(method).parameters
        assert "tool_call_id" in params, f"{method_name} 缺 tool_call_id 形参"
        assert params["tool_call_id"].default == "", f"{method_name} tool_call_id 须默认空串"


# ── bindings 层：事件 payload tool_call_id → handler 形参 ─────────────────


def _bindings_with_handler() -> tuple[_HandlerBindings, Any]:
    from unittest.mock import AsyncMock

    handler = AsyncMock()
    bindings = _HandlerBindings(handler, "s-1")
    bindings.bind()
    return bindings, handler


def test_bindings_tool_invoked_passes_tool_call_id() -> None:
    bindings, handler = _bindings_with_handler()
    event = Event(
        type=EventType.TOOL_INVOKED,
        data={"tool_name": "bash", "tool_input": {"cmd": "ls"}, "tool_call_id": "call_1"},
    )
    r7x137b3__run(bindings._on_tool_invoked(event))
    handler.on_tool_call_start.assert_awaited_once_with("s-1", "bash", {"cmd": "ls"}, "call_1")


def test_bindings_tool_completed_passes_tool_call_id() -> None:
    bindings, handler = _bindings_with_handler()
    event = Event(
        type=EventType.TOOL_COMPLETED,
        data={
            "tool_name": "bash",
            "tool_call_id": "call_2",
            "success": True,
            "output_preview": "ok",
            "duration_ms": 10,
        },
    )
    r7x137b3__run(bindings._on_tool_completed(event))
    handler.on_tool_result.assert_awaited_once_with(
        "s-1", "bash", True, "ok", 10, "call_2", citations=[]
    )


def test_bindings_tool_error_passes_tool_call_id() -> None:
    bindings, handler = _bindings_with_handler()
    event = Event(
        type=EventType.TOOL_ERROR,
        data={
            "tool_name": "bash",
            "tool_call_id": "call_3",
            "error_message": "boom",
            "duration_ms": 5,
        },
    )
    r7x137b3__run(bindings._on_tool_error(event))
    handler.on_tool_result.assert_awaited_once_with("s-1", "bash", False, "boom", 5, "call_3")


def test_bindings_legacy_event_without_tool_call_id_yields_empty_string() -> None:
    """破坏者：旧事件源无 tool_call_id 字段——传空串，不崩、不传 None。"""
    bindings, handler = _bindings_with_handler()
    event = Event(
        type=EventType.TOOL_COMPLETED,
        data={"tool_name": "bash", "success": True, "output_preview": "ok", "duration_ms": 1},
    )
    r7x137b3__run(bindings._on_tool_completed(event))
    handler.on_tool_result.assert_awaited_once_with("s-1", "bash", True, "ok", 1, "", citations=[])


# ── 帧族层：WS / SSE / 员工进度三实现 data 均带 tool_call_id（空串也带）────


def test_ws_frame_carries_tool_call_id() -> None:
    from bandi.api.app import _WSStreamHandler

    sent: list[dict[str, Any]] = []

    class _FakeWS:
        async def send_json(self, data: dict[str, Any]) -> None:
            sent.append(data)

    handler = _WSStreamHandler(_FakeWS())
    r7x137b3__run(handler.on_tool_call_start("s1", "read", {"path": "x"}, "call_a"))
    r7x137b3__run(handler.on_tool_result("s1", "read", False, "boom", 10, "call_a"))
    start_data = sent[0]["data"]
    result_data = sent[1]["data"]
    assert start_data["tool_call_id"] == "call_a", "WS tool_call_start 帧必须带 tool_call_id"
    assert result_data["tool_call_id"] == "call_a", "WS tool_result 帧必须带 tool_call_id"


def test_ws_frame_carries_empty_tool_call_id_for_legacy_callers() -> None:
    """破坏者：按旧签名（不传 tool_call_id）调用——帧仍带空串字段（前端键存在性稳定）。"""
    from bandi.api.app import _WSStreamHandler

    sent: list[dict[str, Any]] = []

    class _FakeWS:
        async def send_json(self, data: dict[str, Any]) -> None:
            sent.append(data)

    handler = _WSStreamHandler(_FakeWS())
    r7x137b3__run(handler.on_tool_call_start("s1", "read", {"path": "x"}))
    assert sent[0]["data"]["tool_call_id"] == ""


def test_sse_frame_carries_tool_call_id() -> None:
    from bandi.api.app import _SSEStreamHandler

    handler = _SSEStreamHandler()
    r7x137b3__run(handler.on_tool_call_start("s1", "read", {"path": "x"}, "call_b"))
    r7x137b3__run(handler.on_tool_result("s1", "read", True, "ok", 10, "call_b"))
    start_frame = r7x137b3__run(handler._queue.get())
    result_frame = r7x137b3__run(handler._queue.get())
    assert '"tool_call_id": "call_b"' in start_frame, (
        f"SSE tool_call_start 帧须带 id：{start_frame}"
    )
    assert '"tool_call_id": "call_b"' in result_frame, f"SSE tool_result 帧须带 id：{result_frame}"


def test_employee_progress_frame_carries_tool_call_id() -> None:
    """员工进度帧（thread_progress 内层 event.data）同族——须带 tool_call_id。"""
    frames: list[dict[str, Any]] = []
    handler = EmployeeProgressHandler(frames.append, project_id="p", thread_id="t", role_id="r")
    r7x137b3__run(handler.on_tool_call_start("s1", "read", {"path": "x"}, "call_c"))
    r7x137b3__run(handler.on_tool_result("s1", "read", False, "boom", 10, "call_c"))
    inner_start = frames[0]["data"]["event"]
    inner_result = frames[1]["data"]["event"]
    assert inner_start["type"] == "tool_call_start"
    assert inner_start["data"]["tool_call_id"] == "call_c", "员工进度 tool_call_start 须带 id"
    assert inner_result["type"] == "tool_result"
    assert inner_result["data"]["tool_call_id"] == "call_c", "员工进度 tool_result 须带 id"
