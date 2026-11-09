"""契约：制品内容预览端点——`GET /api/sessions/{sid}/artifacts/{aid}/content`。

安全面三条硬约束（本契约逐条钉死，全部为破坏者用例）：
① **目录边界**：解析后路径必须落在会话 project_root 内（穿越 → 403 PATH_OUTSIDE_ROOT）；
② **敏感路径**：命中 `fs_tool.sensitive_path_reason`（单源判断）→ 403 SENSITIVE_PATH；
③ **fail-closed 不返内容**：超大（> max_bytes）与二进制只回元数据（kind=too_large / binary）
   绝不把二进制或超长内容灌给前端。
另有正常路径（文本内容 + truncated 标记）与未知 id（404）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from bandi.agent.subagent import build_child_ledger_bundle
from bandi.api.app import _interaction_frame, app
from bandi.core.config import LLMConfig
from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.user_interaction import (
    INTERACTION_KIND_NOTIFY,
    INTERACTION_KIND_PERMISSION,
    HTTPUserInteraction,
    InteractionStore,
)
from bandi.llm.adapters.anthropic import _thinking_body
from bandi.llm.adapters.google import _thinking_config
from bandi.llm.model_catalog import BUILTIN_MODELS
from bandi.llm.providers.moonshot import MoonshotProvider
from bandi.llm.providers.zhipu import ZhipuProvider
from bandi.memory.session.store import SessionStore
from bandi.tools.task_ledger import TaskLedger, TaskLedgerConfig
from bandi.tools.todo import TodoReadTool, TodoWriteTool, _get_shared_ledger

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """隔离：Path.home → tmp；会话库 + 项目根 + 该会话的 store 单例复位。"""
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "bandi.db"
    store = SessionStore(str(db_path))
    session_id = store.create_session(project_root=str(project_root))
    with (
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._db_path", db_path),
        patch("bandi.api.app._store", store),
    ):
        yield {"store": store, "session_id": session_id, "root": project_root}


def _register(env: dict[str, Any], path: str) -> str:
    return env["store"].register_artifact(
        env["session_id"], tool_name="write", path=path, hash_value=""
    )


def _get(env: dict[str, Any], artifact_id: str, **params: Any) -> Any:
    return client.get(
        f"/api/sessions/{env['session_id']}/artifacts/{artifact_id}/content",
        params=params or None,
        headers={"Authorization": "Bearer test"},
    )


def test_text_artifact_returns_content(env: dict[str, Any]) -> None:
    target = env["root"] / "docs" / "out.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes("# 报告\n正文".encode())
    artifact_id = _register(env, str(target))
    resp = _get(env, artifact_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "text" and body["content"] == "# 报告\n正文"
    assert body["truncated"] is False and body["size"] > 0


def test_relative_path_resolved_against_project_root(env: dict[str, Any]) -> None:
    """相对路径按会话 project_root 解析（登记时可能是相对路径）。"""
    target = env["root"] / "rel.txt"
    target.write_bytes(b"rel-ok")
    artifact_id = _register(env, "rel.txt")
    body = _get(env, artifact_id).json()
    assert body["kind"] == "text" and body["content"] == "rel-ok"


def test_breaker_path_traversal_blocked(env: dict[str, Any]) -> None:
    """① 目录边界：`../../` 穿越到项目根之外 → 403（即使文件真实存在也不读）。"""
    outside = env["root"].parent / "secret.txt"
    outside.write_bytes("不该被读到".encode())
    artifact_id = _register(env, "../secret.txt")
    resp = _get(env, artifact_id)
    assert resp.status_code == 403, resp.text
    # 全局错误处理器把 HTTPException.detail 收进 message 字段（此处按文本断言）
    assert "PATH_OUTSIDE_ROOT" in resp.text, resp.text


def test_breaker_absolute_path_outside_root_blocked(env: dict[str, Any]) -> None:
    """① 绝对路径同样受边界约束（不因绝对就放行）。"""
    outside = env["root"].parent / "abs-secret.txt"
    outside.write_bytes(b"x")
    artifact_id = _register(env, str(outside))
    resp = _get(env, artifact_id)
    assert resp.status_code == 403 and "PATH_OUTSIDE_ROOT" in resp.text


def test_breaker_sensitive_path_blocked(env: dict[str, Any]) -> None:
    """② 敏感路径：根内 `.ssh` 目录命中单源判断 → 403 SENSITIVE_PATH（不读内容）。"""
    sensitive_dir = env["root"] / ".ssh"
    sensitive_dir.mkdir(parents=True, exist_ok=True)
    target = sensitive_dir / "id_rsa.txt"
    target.write_bytes(b"PRIVATE")
    artifact_id = _register(env, str(target))
    resp = _get(env, artifact_id)
    assert resp.status_code == 403 and "SENSITIVE_PATH" in resp.text


def test_breaker_oversize_returns_metadata_only(env: dict[str, Any]) -> None:
    """③ 超大：超过 max_bytes 只回元数据（kind=too_large，content 空）。"""
    target = env["root"] / "big.txt"
    target.write_bytes(b"a" * 5000)
    artifact_id = _register(env, str(target))
    body = _get(env, artifact_id, max_bytes=1024).json()
    assert body["kind"] == "too_large" and body["content"] == ""
    assert body["size"] == 5000


def test_breaker_binary_returns_metadata_only(env: dict[str, Any]) -> None:
    """③ 二进制：只回元数据（kind=binary，content 空——不猜编码不灌前端）。"""
    target = env["root"] / "blob.bin"
    target.write_bytes(b"\x00\x01\x02\xff\xfe" * 100)
    artifact_id = _register(env, str(target))
    body = _get(env, artifact_id).json()
    assert body["kind"] == "binary" and body["content"] == ""


def test_unknown_artifact_and_session_404(env: dict[str, Any]) -> None:
    assert _get(env, "ghost").status_code == 404
    missing = client.get(
        "/api/sessions/ghost-session/artifacts/ghost/content",
        headers={"Authorization": "Bearer test"},
    )
    assert missing.status_code == 404


# ==================== 137c_capabilities ====================


r1x137c_client = TestClient(app)
r1x137c_client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with (
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


def _first_frame(ws: Any) -> dict[str, Any]:
    return ws.receive_json()


def test_company_ws_hello_declares_capabilities() -> None:
    with r1x137c_client.websocket_connect(
        "/ws/company", headers={"authorization": "Bearer test"}
    ) as ws:
        frame = _first_frame(ws)
        assert frame["type"] == "hello", f"连接首帧必须为能力握手 hello：{frame}"
        caps = frame["data"]["capabilities"]
        assert isinstance(caps, list) and all(isinstance(item, str) for item in caps), (
            f"capabilities 必须是字符串数组（可机器判定）：{caps!r}"
        )
        # 前端门控依赖的能力名必须在声明中（改名/漏声明会让订阅静默失效）
        assert "progress_scope" in caps, f"必须声明 progress_scope：{caps}"


def test_capabilities_constant_is_source_of_truth() -> None:
    """破坏者：hello 帧内容必须与后端常量同源（不得两处各写一份清单）。"""
    import importlib

    app_module = importlib.import_module("bandi.api.app")
    declared = list(app_module._COMPANY_WS_CAPABILITIES)
    assert declared and all(isinstance(item, str) for item in declared)
    with r1x137c_client.websocket_connect(
        "/ws/company", headers={"authorization": "Bearer test"}
    ) as ws:
        frame = _first_frame(ws)
        assert frame["data"]["capabilities"] == declared, "hello 必须逐字等于常量清单"


def test_subscribe_still_acked_after_hello() -> None:
    """对照组：握手不破坏定向订阅（hello → subscribe → subscribed ack 序位稳定）。"""
    with r1x137c_client.websocket_connect(
        "/ws/company", headers={"authorization": "Bearer test"}
    ) as ws:
        assert _first_frame(ws)["type"] == "hello"
        ws.send_json({"type": "subscribe", "project_ids": ["pA"]})
        ack = ws.receive_json()
        assert ack["type"] == "subscribed" and ack["project_ids"] == ["pA"]


# ==================== 137c_ledger_scope ====================


async def _parent_ledger() -> tuple[TaskLedger, list[Event]]:
    bus = EventBus()
    captured: list[Event] = []

    async def _capture(event: Event) -> None:
        captured.append(event)

    await bus.subscribe(EventType.TASK_LEDGER_UPDATED, _capture)
    ledger = TaskLedger(config=TaskLedgerConfig(emit_runtime_events=True), event_bus=bus)
    return ledger, captured


class _StubRegistry:
    """最小注册表桩：只需 get/list_tools/list_schemas（子视图委托面）。"""

    def __init__(self, tools: list[object]) -> None:
        self._tools = {tool.name: tool for tool in tools}  # type: ignore[attr-defined]

    def get(self, name: str) -> object | None:
        return self._tools.get(name)

    def list_tools(self) -> list[object]:
        return list(self._tools.values())

    def list_schemas(
        self, mode: str | None = None, allowed_tools: list[str] | None = None
    ) -> list[object]:
        return [{"name": tool.name} for tool in self._tools.values()]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_subagent_todo_write_does_not_touch_parent_ledger() -> None:
    parent, captured = await _parent_ledger()
    parent.apply_todos([{"content": "父任务", "status": "in_progress"}])
    await parent.persist()
    assert len(captured) == 1 and captured[0].data["item_count"] == 1  # type: ignore[index]

    parent_registry = _StubRegistry([TodoReadTool(ledger=parent), TodoWriteTool(ledger=parent)])
    child_ledger, child_registry = build_child_ledger_bundle(parent_registry)
    assert child_registry is not None

    child_write = child_registry.get("todo_write")
    await child_write.execute(  # type: ignore[union-attr]
        {"todos": [{"content": "子任务A"}, {"content": "子任务B"}]}
    )

    # ① 子台账收到子任务（子代理自身 todo_read 往返一致）
    child_read = child_registry.get("todo_read")
    snapshot = await child_read.execute({})  # type: ignore[union-attr]
    contents = [item["content"] for item in snapshot["result"]["todos"]]
    assert contents == ["子任务A", "子任务B"], f"子台账应含子任务：{contents}"

    # ② 父台账**未被脏写**：下一次父 persist 不得把子任务写进父会话任务条
    assert [item["content"] for item in parent.items] == ["父任务"], (
        f"父台账被脏写：{[i['content'] for i in parent.items]}"
    )
    await parent.persist()
    last = captured[-1].data  # type: ignore[union-attr]
    assert last["item_count"] == 1 and last["task_ledger"][0]["content"] == "父任务", (
        f"父任务条不得出现子代理项：{last['task_ledger']}"
    )


@pytest.mark.asyncio
async def test_child_ledger_is_silent_and_not_persisted() -> None:
    """子台账不发运行时事件、不落会话（不上父链帧、不进会话 metadata）。"""
    _, child_registry = build_child_ledger_bundle(_StubRegistry([TodoWriteTool(), TodoReadTool()]))
    ledger = child_registry.ledger
    assert ledger._config.emit_runtime_events is False, "子台账不得发运行时事件"
    assert ledger._config.persist_to_session is False, "子台账不得落会话"
    assert ledger._config.persist_to_work_item is False, "子台账不得落工作项"
    ledger.apply_todos([{"content": "子任务"}])
    await ledger.persist()  # 无 store/bus ⇒ 纯 no-op，不抛


def test_shared_ledger_buckets_by_session() -> None:
    """破坏者（CLI 跨会话串账）：两会话的兜底台账必须互相隔离。"""
    s1 = _get_shared_ledger("sess-1")
    s2 = _get_shared_ledger("sess-2")
    assert s1 is not s2, "不同会话不得共享同一台账对象"
    assert _get_shared_ledger("sess-1") is s1, "同会话必须稳定复取（幂等）"
    s1.apply_todos([{"content": "会话一的待办"}])
    assert s2.items == [], f"会话二不得读到会话一的待办：{s2.items}"
    assert [i["content"] for i in s1.items] == ["会话一的待办"]


def test_shared_ledger_default_bucket_preserved() -> None:
    """兼容：无会话 id 的调用（旧签名 `_get_shared_ledger()`）落同一兜底桶。"""
    assert _get_shared_ledger() is _get_shared_ledger("")


@pytest.mark.asyncio
async def test_todo_via_executor_context_buckets_by_session() -> None:
    """-修复（生产入口）：executor 按 needs_context 注入 session_id →
    未注入 ledger 的 todo 工具走会话分桶，不再落进程级兜底桶（CLI 跨会话串账闭环）。"""
    from bandi.core.message import ToolCall
    from bandi.tools import todo as todo_module
    from bandi.tools.executor import ToolExecutor

    todo_module._shared_ledgers.pop("sess-exec-A", None)
    executor = ToolExecutor(registry=_StubRegistry([TodoWriteTool()]))  # type: ignore[arg-type]
    executor._session_meta = {"session_id": "sess-exec-A"}
    results = await executor.execute(
        [ToolCall(id="c1", name="todo_write", arguments={"todos": [{"content": "生产桶任务"}]})]
    )
    assert results[0].success, results[0].error
    written = _get_shared_ledger("sess-exec-A").snapshot()
    assert [i["content"] for i in written["result"]["todos"]] == ["生产桶任务"]
    assert _get_shared_ledger("").snapshot()["result"]["todos"] == [], "不得落入兜底桶"


# ==================== 137c_ledger_session_id ====================


async def _ledger(session_id: str) -> tuple[TaskLedger, list[Event]]:
    bus = EventBus()
    captured: list[Event] = []

    async def _capture(event: Event) -> None:
        captured.append(event)

    await bus.subscribe(EventType.TASK_LEDGER_UPDATED, _capture)
    ledger = TaskLedger(
        config=TaskLedgerConfig(enabled=True, persist_to_session=False, persist_to_work_item=False),
        event_bus=bus,
        session_id=session_id,
    )
    return ledger, captured


@pytest.mark.asyncio
async def test_ledger_event_carries_session_id() -> None:
    ledger, captured = await _ledger("sess-abc")
    ledger.apply_todos([{"content": "步骤一", "status": "in_progress"}])
    await ledger.persist()
    assert len(captured) == 1, f"应恰好发一条台账事件：{captured}"
    data: dict[str, Any] = captured[0].data  # type: ignore[assignment]
    assert data.get("session_id") == "sess-abc", f"data 必须自带 session_id：{data}"
    assert data["item_count"] == 1
    assert data["task_ledger"][0]["content"] == "步骤一"


@pytest.mark.asyncio
async def test_ledger_event_empty_session_id_still_present() -> None:
    """破坏者：无会话台账（CLI/裸路径）→ 键存在且为空串（形态稳定，不塌缩成缺键）。"""
    ledger, captured = await _ledger("")
    ledger.apply_todos([{"content": "步骤一"}])
    await ledger.persist()
    data: dict[str, Any] = captured[0].data  # type: ignore[assignment]
    assert "session_id" in data, "键必须在（消费侧无需 get 兜底）"
    assert data["session_id"] == ""


@pytest.mark.asyncio
async def test_owns_filters_by_data_session_id_when_event_level_missing() -> None:
    """破坏者（归属防线）：event 级 session_id 缺失时，_owns 用 data.session_id 判定——
    他会话台账事件不得进入本会话绑定。"""
    from bandi.core.agent_runner import _HandlerBindings

    bindings = _HandlerBindings.__new__(_HandlerBindings)
    bindings.session_id = "sess-mine"
    other = Event(
        type=EventType.TASK_LEDGER_UPDATED,
        data={"task_ledger": [], "item_count": 0, "session_id": "sess-other"},
    )
    mine = Event(
        type=EventType.TASK_LEDGER_UPDATED,
        data={"task_ledger": [], "item_count": 0, "session_id": "sess-mine"},
    )
    assert bindings._owns(other) is False, "他会话台账事件必须被拒"
    assert bindings._owns(mine) is True


# ==================== 137c_notify_interaction ====================


class _Captured:
    def __init__(self) -> None:
        self.items: list[object] = []

    async def __call__(self, item: object) -> None:
        self.items.append(item)


def test_show_notification_emits_notify_kind_frame() -> None:
    store = InteractionStore()
    captured = _Captured()
    ui = HTTPUserInteraction(store, session_id="s1", on_interaction=captured)
    asyncio.run(ui.show_notification("已自动批准 bash（本会话始终允许）", level="info"))
    assert len(captured.items) == 1, "必须推一条帧"
    frame = _interaction_frame(captured.items[0])
    assert frame["kind"] == INTERACTION_KIND_NOTIFY, f"kind 必须为 notify：{frame['kind']}"
    assert frame["payload"]["notify"] is True, "保留 notify 标记（旧前端兼容）"
    assert frame["payload"]["message"] == "已自动批准 bash（本会话始终允许）"
    # 通知类不得带倒计时（不是待决策项）
    assert "expires_in" not in frame, "notify 帧不得携带 expires_in（会伪装成待审批卡）"


def test_show_notification_does_not_suspend() -> None:
    """不挂起：登记后立即结束 ⇒ 挂起表为空（决策端点不会指向它）。"""
    store = InteractionStore()
    ui = HTTPUserInteraction(store, session_id="s1", on_interaction=_Captured())
    asyncio.run(ui.show_notification("提示"))
    assert store.list_pending("s1") == [], "notify 不得留 pending（否则决策端点可被误点）"


def test_notify_kind_whitelisted_unknown_still_raises() -> None:
    """破坏者（白名单收紧未被放松）：notify 在白名单内；未知 kind 仍显式报错。"""
    store = InteractionStore()
    item = store.register(session_id="s1", kind=INTERACTION_KIND_NOTIFY, payload={"message": "x"})
    assert item.kind == INTERACTION_KIND_NOTIFY
    with pytest.raises(ValueError):
        store.register(session_id="s1", kind="ghost", payload={})


def test_permission_kind_frame_keeps_countdown() -> None:
    """对照组：permission 挂起帧仍带倒计时（notify 的例外不得误伤决策项）。"""
    store = InteractionStore(ttl_seconds=60)
    item = store.register(
        session_id="s1", kind=INTERACTION_KIND_PERMISSION, payload={"prompt": "允许？"}
    )
    frame = _interaction_frame(item)
    assert frame["kind"] == INTERACTION_KIND_PERMISSION
    assert "expires_in" in frame, "待决策项必须保留倒计时"


# ==================== 137c_thinking_modes ====================


def _respond(captured: dict) -> httpx.MockTransport:
    """兼容端点响应桩：必须含 choices（空 choices 会被适配层判为错误响应）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "x",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    return httpx.MockTransport(handler)


def _config(model: str, mode: str = "") -> LLMConfig:
    return LLMConfig(api_key="sk", default_model=model, thinking_mode=mode, max_output_tokens=8192)


# ── Anthropic ────────────────────────────────────────────────


def test_anthropic_adaptive_uses_effort_and_summarized_display() -> None:
    body = _thinking_body("claude-opus-5", "high", 8192)
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}, body
    assert body["output_config"] == {"effort": "high"}, body
    assert "budget_tokens" not in json.dumps(body), "adaptive 系不得出现 budget_tokens"


def test_anthropic_adaptive_off_is_explicit_disabled() -> None:
    """破坏者（关闭失效）：5 系不传 thinking = adaptive 思考开 ⇒ 关闭必须显式 disabled。"""
    assert _thinking_body("claude-sonnet-5", "", 8192) == {"thinking": {"type": "disabled"}}
    assert _thinking_body("claude-opus-4-7", "", 8192) == {"thinking": {"type": "disabled"}}


def test_anthropic_legacy_family_keeps_budget_semantics() -> None:
    body = _thinking_body("claude-sonnet-4-6", "high", 8192)
    assert body["thinking"]["type"] == "enabled" and body["thinking"]["budget_tokens"] >= 1024
    # 旧系默认无思考 ⇒ 空档不下发（`enabled` 仅为显式开启）
    assert _thinking_body("claude-haiku-4-5", "", 8192) == {}
    # budget 下限保护（max_tokens 太小 → 不发送，防必然 400）
    assert _thinking_body("claude-sonnet-4-6", "low", 1024) == {}


def test_anthropic_xhigh_maps_to_max() -> None:
    assert _thinking_body("claude-opus-5", "xhigh", 8192)["output_config"]["effort"] == "max"


# ── Gemini ───────────────────────────────────────────────────


def test_gemini_uses_thinking_level_enum() -> None:
    model = "gemini-3.5-flash"
    assert _thinking_config("high", model) == {"thinkingLevel": "high"}
    assert _thinking_config("low", model) == {"thinkingLevel": "low"}
    assert _thinking_config("medium", model) == {"thinkingLevel": "medium"}


def test_gemini_off_maps_to_minimal_and_deprecated_field_gone() -> None:
    """破坏者：3.x 空档 → minimal（不可关闭的等价关）；非 3.x 不支持该字段 ⇒ 空档不下发（防 400）；
    且任何路径不得再出现整数 thinkingBudget。"""
    assert _thinking_config("", "gemini-3.1-pro-preview") == {"thinkingLevel": "minimal"}
    assert _thinking_config("", "gemini-2.5-flash") == {}, (
        "非 3.x 不支持 thinkingLevel，空档不得下发"
    )
    assert "thinkingBudget" not in json.dumps(_thinking_config("high", "gemini-3.5-flash"))


# ── Kimi / GLM ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kimi_sends_top_level_reasoning_effort_without_thinking() -> None:
    captured: dict[str, Any] = {}
    provider = MoonshotProvider(_config("kimi-k3", "high"))
    provider._client = httpx.AsyncClient(transport=_respond(captured))
    try:
        await provider.complete([{"role": "user", "content": "hi"}])
    finally:
        await provider.close()
    body = captured["body"]
    assert body.get("reasoning_effort") == "high", body
    assert "thinking" not in body, f"K3 不得携带 thinking 字段：{body}"


@pytest.mark.asyncio
async def test_kimi_off_floors_to_low_not_default_max() -> None:
    """破坏者（关闭失效）：不发 = 官方默认 max —— 空档必须显式下发 low。"""
    captured: dict[str, Any] = {}
    provider = MoonshotProvider(_config("kimi-k3", ""))
    provider._client = httpx.AsyncClient(transport=_respond(captured))
    try:
        await provider.complete([{"role": "user", "content": "hi"}])
    finally:
        await provider.close()
    assert captured["body"].get("reasoning_effort") == "low", captured["body"]


def test_glm_53_forces_thinking_and_never_disables() -> None:
    always_on = ZhipuProvider(_config("glm-5.3", ""))._thinking_payload()
    assert always_on == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}, always_on
    assert ZhipuProvider(_config("glm-5.3", "medium"))._thinking_payload() == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }, "官方映射：medium → high"


def test_glm_52_can_disable_thinking() -> None:
    assert ZhipuProvider(_config("glm-5.2", ""))._thinking_payload() == {
        "thinking": {"type": "disabled"}
    }


# ── 目录（挡位与「能否关闭」声明）────────────────────────────────


def test_catalog_declares_efforts_and_off_support() -> None:
    """破坏者（假支持/假关闭）：四家模型必须声明挡位；强制思考族必须标记不可关闭。"""
    expectations = {
        "claude-opus-5": (["low", "medium", "high", "max"], True),
        "claude-sonnet-4-6": (["low", "medium", "high"], True),
        "gemini-3.5-flash": (["low", "medium", "high"], True),
        "kimi-k3": (["low", "high", "max"], False),
        "glm-5.3": (["low", "high", "max"], False),
    }
    for model, (efforts, off_supported) in expectations.items():
        spec = BUILTIN_MODELS[model]
        assert spec.reasoning_efforts == efforts, f"{model} 挡位：{spec.reasoning_efforts}"
        assert spec.supports_reasoning is True
        assert spec.supports_thinking_off is off_supported, f"{model} 不可关闭标记错误"


def test_catalog_off_unsupported_models_have_no_off_effort() -> None:
    """破坏者：不可关闭的模型挡位列表里不得出现 none/minimal（会被读成「可关闭档」）。"""
    for model in ("kimi-k3", "glm-5.3", "glm-5.3-flash"):
        spec = BUILTIN_MODELS[model]
        assert not ({"none", "minimal"} & set(spec.reasoning_efforts)), (
            f"{model} 不可关闭却声明了 none/minimal：{spec.reasoning_efforts}"
        )
