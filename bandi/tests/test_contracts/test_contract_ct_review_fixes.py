# 功能：契约——CT 块修复批次。
# 钉行为：规则 env 引号/制表符/复合段绕过防护；来源自报伪造拒绝；投影真实 TOOL
# 包裹剥离与 USER 前缀碰撞安全 / 空源清旧段 / 非可迭代守卫；cancel 真实返回值；
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from bandi.core.message import Message, MessageRole, ToolCall
from bandi.core.permission.rules import parse_permission_rule, rule_matches
from bandi.memory.context_projection import (
    PROJECTION_PREFIX,
    ProjectionSources,
    build_context_projection,
    projection_message_index,
    upsert_projection_message,
)


def _deny(pattern: str) -> Any:
    rule = parse_permission_rule({"pattern": pattern, "action": "deny"})
    assert rule is not None
    return rule


# ──────────────────────────────────────────────────────────────────────
# A. 规则绕过防护族
# ──────────────────────────────────────────────────────────────────────


class TestRuleBypassFixes:
    def test_env_prefix_quoted_values_stripped(self) -> None:
        rule = _deny("bash:git:*")
        assert rule_matches(rule, "bash", {"command": 'MSG="a b" git push'}), (
            "引号内空格的 env 前缀必须完整剥离"
        )

    def test_env_prefix_multiple_assignments(self) -> None:
        rule = _deny("bash:git:*")
        assert rule_matches(rule, "bash", {"command": 'A=1 B="x y" git push'})

    def test_prefix_tab_boundary(self) -> None:
        rule = _deny("bash:git:*")
        assert rule_matches(rule, "bash", {"command": "git\tpush"}), "制表符也是词边界"

    def test_deny_matches_compound_segment(self) -> None:
        rule = _deny("bash:git:*")
        assert rule_matches(rule, "bash", {"command": "echo hi && git push"}), (
            "deny 必须拆复合命令逐段匹配"
        )

    def test_deny_matches_late_segment_with_env(self) -> None:
        rule = _deny("bash:git:*")
        assert rule_matches(rule, "bash", {"command": "echo hi && FOO=bar git push"})

    def test_allow_still_blocked_by_compound(self) -> None:
        allow = parse_permission_rule("bash:git:*")
        assert allow is not None
        assert not rule_matches(allow, "bash", {"command": "git status && evil"}), (
            "allow 的 compound 防护不得回归"
        )


# ──────────────────────────────────────────────────────────────────────
# B. 来源自报伪造族
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    import os

    home = tmp_path / "home"
    (home / ".bandi").mkdir(parents=True)
    proj = tmp_path / "proj"
    (proj / ".bandi").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(proj)
    for key in list(os.environ):
        if key.startswith("BANDI_"):
            monkeypatch.delenv(key, raising=False)
    return home, proj


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestSourceSpoofFixes:
    def test_hook_source_spoof_rejected(self, sandbox: tuple[Path, Path]) -> None:
        from bandi.core.config import BandiConfig

        _, proj = sandbox
        _write(
            proj / ".bandi" / "config.yaml",
            "hooks:\n  PreToolUse:\n    - type: command\n      command: echo hi\n"
            "      source: user\n",
        )
        cfg = BandiConfig.load()
        assert cfg.hooks == {}, "项目 hooks 自报 source: user 必须被拒绝（无条件覆写来源）"

    def test_rules_source_spoof_overwritten(self, sandbox: tuple[Path, Path]) -> None:
        from bandi.core.config import BandiConfig

        home, _ = sandbox
        _write(
            home / ".bandi" / "config.yaml",
            "env: dev\npermissions:\n  rules:\n    - pattern: read\n      source: evil\n",
        )
        cfg = BandiConfig.load()
        assert cfg.permissions.rules[0]["source"] == "user", "规则来源必须由加载器裁定"


# ──────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────


def _mk_projection_ctx() -> Any:
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.llm.mock import MockProvider

    return ExecutionContext(
        messages=[Message(role=MessageRole.SYSTEM, content="sys")],
        provider=MockProvider(),
        config=AgentConfig(),
        bus=EventBus(),
    )


class TestProjectionFixes:
    def test_tool_output_wrapper_stripped(self) -> None:
        # 剥离发生在消息读取层（states._projection_tool_lines）——生产 TOOL 消息
        # 首行恒为 `<tool_output tool="...">` XML 包裹（states.py:406-409）
        from bandi.agent.states import _projection_tool_lines

        ctx = _mk_projection_ctx()
        ctx.messages.append(
            Message(
                role=MessageRole.TOOL,
                content='<tool_output tool="bash">\n实际输出内容\n</tool_output>',
                tool_call_id="c1",
                name="bash",
            )
        )
        lines = _projection_tool_lines(ctx)
        assert lines == ["实际输出内容"], f"必须剥 XML 包裹取真实输出: {lines}"

    def test_user_prefix_collision_safe(self) -> None:
        messages = [
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content=f"{PROJECTION_PREFIX}\n用户引用的文档"),
        ]
        upsert_projection_message(messages, f"{PROJECTION_PREFIX}\n## 新投影")
        user_msgs = [m for m in messages if m.role == MessageRole.USER]
        assert any("用户引用的文档" in str(m.content) for m in user_msgs), (
            "USER 前缀碰撞消息不得被替换"
        )
        assert len([m for m in messages if str(m.content).startswith(PROJECTION_PREFIX)]) == 2

    def test_task_projection_non_iterable_guarded(self) -> None:
        text = build_context_projection(ProjectionSources(tasks=lambda: 42))  # type: ignore[arg-type,return-value]
        assert text == "", "非列表任务源必须降级不崩"

    def test_stale_projection_cleared_on_empty_sources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bandi.agent import states as states_mod
        from bandi.agent.base import ExecutionContext
        from bandi.agent.states import _refresh_projection_if_changed
        from bandi.core.config import AgentConfig
        from bandi.core.event_bus import EventBus
        from bandi.llm.mock import MockProvider

        ctx = ExecutionContext(
            messages=[Message(role=MessageRole.SYSTEM, content="sys")],
            provider=MockProvider(),
            config=AgentConfig(),
            bus=EventBus(),
        )
        upsert_projection_message(ctx.messages, f"{PROJECTION_PREFIX}\n## 旧投影")
        monkeypatch.setattr(states_mod, "_projection_sources_for", lambda ctx: ProjectionSources())
        ctx.meta["projection_fingerprint"] = "stale"

        _refresh_projection_if_changed(ctx)

        assert projection_message_index(ctx.messages) < 0, "源全空时必须清除过期投影段"


# ──────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────


class TestSubagentFixes:
    def test_cancel_returns_bool_semantics(self) -> None:
        from bandi.agent.subagent import SubagentManager

        manager = SubagentManager()
        assert manager.cancel("subagent-missing") is False, "未命中必须返回 False"

    def test_cancel_active_task_noop_returns_false(self) -> None:
        from bandi.agent import subagent as sub_mod
        from bandi.agent.subagent import SubagentManager, cancel_active_task

        manager = SubagentManager()
        with sub_mod._ACTIVE_TASKS_LOCK:
            sub_mod._ACTIVE_TASKS["subagent-noop"] = manager
        try:
            assert cancel_active_task("subagent-noop") is False, (
                "注册表有键但无活跃 session 时取消是 no-op——不得虚报成功"
            )
        finally:
            with sub_mod._ACTIVE_TASKS_LOCK:
                sub_mod._ACTIVE_TASKS.pop("subagent-noop", None)

    def test_request_cancel_closed_loop_no_crash(self) -> None:
        from bandi.agent.subagent import SubagentManager

        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(asyncio.sleep(0))
            loop.run_until_complete(task)
        finally:
            loop.close()
        SubagentManager._request_cancel(task)  # loop 已关闭——不得抛 RuntimeError

    def test_tasks_string_normalized(self) -> None:
        from bandi.agent.subagent import SubagentResult
        from bandi.agent.subagent_tool import SubAgentTool

        captured: dict[str, Any] = {}

        class _Spy:
            def __init__(self, **_kw: Any) -> None:
                pass

            async def spawn_many(self, tasks, config, **kwargs):
                captured["tasks"] = list(tasks)
                return [SubagentResult(success=True, summary="ok", task_id="subagent-x")]

            def cancel_all(self) -> None:
                pass

            def close(self) -> None:
                pass

        from bandi.agent import subagent_tool as st_mod

        original = st_mod.SubagentManager
        st_mod.SubagentManager = _Spy  # type: ignore[assignment]
        try:
            asyncio.run(SubAgentTool(provider=object()).execute({"tasks": "do X"}))
        finally:
            st_mod.SubagentManager = original  # type: ignore[assignment]
        assert captured["tasks"] == ["do X"], "字符串 tasks 必须归一为单元素"

    def test_crash_snapshot_not_labelled_cancelled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bandi.agent import subagent as sub_mod
        from bandi.agent.subagent import (
            SubagentConfig,
            SubagentManager,
            list_completed_tasks,
        )

        async def crashy(
            sub_self: Any, task, cfg, session, tool_registry=None, parent_trace_id="", depth=0
        ) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(sub_mod.SubagentManager, "_run_subagent", crashy)
        manager = SubagentManager()
        manager._loop_checkpoint_store = None
        with pytest.raises(RuntimeError):
            asyncio.run(manager.spawn("c1", SubagentConfig(timeout_sec=5)))
        snapshots = list_completed_tasks(limit=1)
        assert snapshots, "崩溃必须落档（finally 快照）"
        assert "cancel" not in snapshots[0].summary.lower(), (
            f"崩溃快照不得标为 cancelled: {snapshots[0].summary!r}"
        )


# ──────────────────────────────────────────────────────────────────────
# E. hooks / API 修复族
# ──────────────────────────────────────────────────────────────────────


class TestHooksAndApiFixes:
    def test_hook_timeout_clamped(self) -> None:
        from bandi.tools.hooks import load_hook_rules

        rules = load_hook_rules(
            {"PreToolUse": [{"type": "command", "command": "x", "timeout": 99999}]}
        )
        assert rules and rules[0].timeout <= 600.0, "hook 超时必须钳制上界"

    def test_http_ask_permission_dict_granted_false(self) -> None:
        from bandi.core.user_interaction import HTTPUserInteraction, InteractionStore

        store = InteractionStore()
        ui = HTTPUserInteraction(store, "s-dict")

        async def scenario() -> bool:
            task = asyncio.create_task(ui.ask_permission("bash", {}, "high"))
            await asyncio.sleep(0.05)
            pending = store.list_pending("s-dict", kind="permission")
            store.resolve(pending[0].interaction_id, {"granted": False, "feedback": "no"})
            return await task

        assert asyncio.run(scenario()) is False, (
            "dict 形态 granted=False 不得被 bool(dict)=True 误当批准"
        )

    def test_confirm_endpoint_feedback_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib

        from fastapi.testclient import TestClient

        from bandi.core.user_interaction import (
            INTERACTION_KIND_PERMISSION,
            InteractionStore,
        )

        app_mod = importlib.import_module("bandi.api.app")
        store = InteractionStore()
        item = store.register(session_id="s-conf", kind=INTERACTION_KIND_PERMISSION, payload={})
        monkeypatch.setattr(app_mod, "_API_KEY", "test")
        monkeypatch.setattr(app_mod, "_interaction_store", store)
        client = TestClient(app_mod.app)
        client.headers["Authorization"] = "Bearer test"

        resp = client.post(
            "/api/sessions/s-conf/confirm",
            json={
                "interaction_id": item.interaction_id,
                "decision": False,
                "feedback": "改用 python",
            },
        )

        assert resp.status_code == 200, resp.text
        resolved = store.get(item.interaction_id)
        assert resolved is not None and resolved.status == "resolved"
        value = resolved._early_value
        assert isinstance(value, dict) and value.get("feedback") == "改用 python", (
            f"/confirm feedback 必须进决策值: {value!r}"
        )


# ──────────────────────────────────────────────────────────────────────
# F. 子代理 hooks 继承通道
# ──────────────────────────────────────────────────────────────────────


class TestSubagentHookChannel:
    def test_tool_forwards_hook_runner_to_manager(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bandi.agent.subagent import SubagentResult
        from bandi.agent.subagent_tool import SubAgentTool
        from bandi.tools.base import ToolPermissionContext

        captured: dict[str, Any] = {}
        sentinel_runner = object()

        class _Spy:
            def __init__(self, **kw: Any) -> None:
                captured.update(kw)

            async def spawn_many(self, tasks, config, **kwargs):
                return [SubagentResult(success=True, summary="ok", task_id="subagent-x")]

            def cancel_all(self) -> None:
                pass

            def close(self) -> None:
                pass

        from bandi.agent import subagent_tool as st_mod

        original = st_mod.SubagentManager
        st_mod.SubagentManager = _Spy  # type: ignore[assignment]
        try:
            context = ToolPermissionContext(
                permissions_pipeline=None, extra={"hook_runner": sentinel_runner}
            )
            asyncio.run(SubAgentTool(provider=object()).execute({"task": "t"}, context))
        finally:
            st_mod.SubagentManager = original  # type: ignore[assignment]
        assert captured.get("hook_runner") is sentinel_runner, (
            "hook_runner 必须经 context.extra 透传到子代理 manager"
        )

    def test_subagent_executor_gets_hook_pipeline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 真实 _run_subagent 链路（MockProvider）：ToolExecutor 构造参数经 spy 捕获
        from bandi.agent.subagent import SubagentConfig, SubagentManager
        from bandi.tools import executor as executor_mod
        from bandi.tools.hooks import HOOK_PRE_TOOL_USE, HookRule, HookRunner

        captured: dict[str, Any] = {}
        original_init = executor_mod.ToolExecutor.__init__

        def spy_init(self: Any, *args: Any, **kwargs: Any) -> None:
            captured["pipeline"] = kwargs.get("tool_pipeline")
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(executor_mod.ToolExecutor, "__init__", spy_init)

        class _Registry:
            def list_tools(self) -> list[Any]:
                return []

            def list_schemas(self, allowed_tools: Any = None) -> list[Any]:
                return []

        runner = HookRunner([HookRule(event=HOOK_PRE_TOOL_USE, action_type="command", command="x")])
        manager = SubagentManager(hook_runner=runner)
        manager._loop_checkpoint_store = None
        result = asyncio.run(
            manager.spawn("t", SubagentConfig(timeout_sec=10), tool_registry=_Registry())
        )
        assert result.success
        assert captured.get("pipeline") is not None, (
            "子代理 ToolExecutor 必须挂 hooks 中间件管道（hook_runner 存在时）"
        )


# ──────────────────────────────────────────────────────────────────────
# G. bug 检查批次（confirm-kind feedback 隔离 + 子代理 hook 真实 deny E2E）
# ──────────────────────────────────────────────────────────────────────


class TestConfirmFeedbackIsolation:
    def test_confirm_kind_feedback_stays_reject(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ask_confirmation 的 bool(dict)=True 会把拒绝变批准
        import importlib

        from fastapi.testclient import TestClient

        from bandi.core.user_interaction import (
            INTERACTION_KIND_CONFIRM,
            InteractionStore,
        )

        app_mod = importlib.import_module("bandi.api.app")
        store = InteractionStore()
        item = store.register(session_id="s-fb", kind=INTERACTION_KIND_CONFIRM, payload={})
        monkeypatch.setattr(app_mod, "_API_KEY", "test")
        monkeypatch.setattr(app_mod, "_interaction_store", store)
        client = TestClient(app_mod.app)
        client.headers["Authorization"] = "Bearer test"

        async def scenario() -> Any:
            task = asyncio.create_task(store.get_decision(item.interaction_id, timeout=5))
            await asyncio.sleep(0.05)
            resp = client.post(
                "/api/sessions/s-fb/confirm",
                json={
                    "interaction_id": item.interaction_id,
                    "decision": False,
                    "feedback": "带理由的拒绝",
                },
            )
            assert resp.status_code == 200, resp.text
            return await task

        decision = asyncio.run(scenario())
        assert decision is None, (
            f"confirm 类交互的拒绝必须保持 reject 语义（None），不得变成 dict/True: {decision!r}"
        )

    def test_ask_confirmation_dict_defense(self) -> None:
        from bandi.core.user_interaction import (
            INTERACTION_KIND_CONFIRM,
            HTTPUserInteraction,
            InteractionStore,
        )

        store = InteractionStore()
        ui = HTTPUserInteraction(store, "s-def")

        async def scenario() -> bool:
            task = asyncio.create_task(ui.ask_confirmation("继续？"))
            await asyncio.sleep(0.05)
            pending = store.list_pending("s-def", kind=INTERACTION_KIND_CONFIRM)
            store.resolve(pending[0].interaction_id, {"granted": False, "feedback": "no"})
            return await task

        assert asyncio.run(scenario()) is False, "dict 形态 grant=False 不得被 bool(dict) 当批准"


class TestSubagentHookDenyE2E:
    def test_subagent_bash_denied_by_hook(self, tmp_path: Path) -> None:
        # E2E：真实子代理调用链（MockProvider 工具轮）→ 子代理 ToolExecutor 的
        # HookBefore deny → bash 工具真实未执行
        from bandi.agent.subagent import SubagentConfig, SubagentManager
        from bandi.core.message import LLMResponse, TokenUsage
        from bandi.llm.mock import MockProvider
        from bandi.tools.base import BaseTool, ToolCategory
        from bandi.tools.hooks import HOOK_PRE_TOOL_USE, HookRule, HookRunner

        executed: list[dict[str, Any]] = []

        class _Bash(BaseTool):
            name = "bash"
            description = ""
            category = ToolCategory.UTIL

            async def execute(self, params: dict[str, Any], context: Any = None) -> str:
                executed.append(dict(params))
                return "executed!"

        class _Registry:
            def list_tools(self) -> list[Any]:
                return [_Bash()]

            def list_schemas(self, allowed_tools: Any = None) -> list[Any]:
                return [
                    {
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "description": "",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ]

            def get(self, name: str) -> Any:
                return _Bash() if name == "bash" else None

        provider = MockProvider()
        provider.add_response(
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "echo hi"})],
                usage=TokenUsage(prompt_tokens=5, completion_tokens=5),
                finish_reason="tool_calls",
            )
        )
        provider.add_response(
            LLMResponse(
                content="工具被拦截，改用其他方式",
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=5, completion_tokens=5),
                finish_reason="stop",
            )
        )

        code = "import sys\nsys.stderr.write('bash denied by hook e2e')\nsys.exit(2)\n"
        script = tmp_path / "deny.py"
        script.write_text(code, encoding="utf-8")
        command = f'"{sys.executable}" "{script}"'
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_PRE_TOOL_USE,
                    action_type="command",
                    matcher="bash",
                    command=command,
                )
            ]
        )

        manager = SubagentManager(provider=provider, hook_runner=runner)
        manager._loop_checkpoint_store = None
        # 审批门（executor 层）先于 pipeline：bash 在默认 ask 名单时会先挂审批
        from bandi.tools.permissions_pipeline import PermissionsPipeline

        manager._permissions_pipeline = PermissionsPipeline(ask_tools=set(), bypass_tools={"bash"})
        result = asyncio.run(
            manager.spawn("跑一个 bash", SubagentConfig(timeout_sec=30), tool_registry=_Registry())
        )
        assert result.success, result.summary
        assert executed == [], "hook deny 后 bash 工具不得真实执行"
