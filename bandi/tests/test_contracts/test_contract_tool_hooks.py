# 功能：契约——工具/生命周期 hooks。
# 钉行为：8 事件常量完备；配置解析（四动作类型/非法跳过）；hook 输出协议解析
# （hookSpecificOutput 嵌套/扁平/容错）；四动作真实执行（command stdin JSON /
# 权限链）；PostToolUse 中间件（additionalContext 注入 + 失败事件分派）；
# states 生命周期接线（UserPromptSubmit 注入 / SessionStart 首轮 / 压缩前后事件）。
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.hooks import (
    HOOK_EVENTS,
    HOOK_POST_COMPACT,
    HOOK_POST_TOOL_USE,
    HOOK_POST_TOOL_USE_FAILURE,
    HOOK_PRE_COMPACT,
    HOOK_PRE_TOOL_USE,
    HOOK_SESSION_END,
    HOOK_SESSION_START,
    HOOK_USER_PROMPT_SUBMIT,
    HookRule,
    HookRunner,
    load_hook_rules,
    parse_hook_output,
)
from bandi.tools.middleware import HookAfterMiddleware, HookBeforeMiddleware
from bandi.tools.pipeline import MiddlewareResult, ToolContext, ToolPipeline

_PY = sys.executable


def _script(tmp_path: Path, name: str, code: str) -> str:
    path = tmp_path / name
    path.write_text(code, encoding="utf-8")
    return f'"{_PY}" "{path}"'


# ──────────────────────────────────────────────────────────────────────
# A. 常量与配置解析族
# ──────────────────────────────────────────────────────────────────────


class TestEventsAndParsing:
    def test_eight_events_defined(self) -> None:
        assert len(HOOK_EVENTS) == 8
        assert set(HOOK_EVENTS) == {
            HOOK_PRE_TOOL_USE,
            HOOK_POST_TOOL_USE,
            HOOK_POST_TOOL_USE_FAILURE,
            HOOK_USER_PROMPT_SUBMIT,
            HOOK_SESSION_START,
            HOOK_SESSION_END,
            HOOK_PRE_COMPACT,
            HOOK_POST_COMPACT,
        }

    def test_loads_four_action_types(self) -> None:
        rules = load_hook_rules(
            {
                "PreToolUse": [
                    {"type": "command", "command": "x", "matcher": "bash"},
                    {"type": "prompt", "prompt": "p"},
                    {"type": "agent", "prompt": "p"},
                    {"type": "http", "url": "https://example.test/h"},
                ]
            }
        )
        assert [r.action_type for r in rules] == ["command", "prompt", "agent", "http"]
        assert rules[0].matcher == "bash"

    def test_invalid_entries_skipped(self) -> None:
        rules = load_hook_rules(
            {
                "PreToolUse": [{"type": "shell"}, "not-a-dict", {"type": "command"}],
                "NotARealEvent": [{"type": "command", "command": "x"}],
                "BadShape": "oops",
            }
        )
        assert [r.action_type for r in rules] == ["command"], (
            "非法类型/事件名/形状跳过；缺 command 字段的 command 规则仍加载（执行期报错）"
        )

    def test_matcher_only_for_tool_events(self) -> None:
        rule = HookRule(event=HOOK_SESSION_START, action_type="command", matcher="bash")
        assert rule.matches("") and rule.matches("read"), "非工具事件忽略 matcher"
        tool_rule = HookRule(event=HOOK_PRE_TOOL_USE, action_type="command", matcher="bash")
        assert tool_rule.matches("bash") and not tool_rule.matches("read")


class TestParseOutput:
    def test_nested_hook_specific_output(self) -> None:
        out = parse_hook_output(
            '{"hookSpecificOutput": {"permissionDecision": "deny",'
            ' "permissionDecisionReason": "no", "updatedInput": {"a": 1},'
            ' "additionalContext": "ctx"}}'
        )
        assert out.decision == "deny" and out.reason == "no"
        assert out.updated_input == {"a": 1} and out.additional_context == "ctx"

    def test_flat_keys(self) -> None:
        out = parse_hook_output('{"decision": "allow", "additional_context": "hi"}')
        assert out.decision == "allow" and out.additional_context == "hi"

    def test_invalid_json_records_error(self) -> None:
        out = parse_hook_output("not json at all")
        assert out.hook_errors and out.decision == ""

    def test_empty_output_clean(self) -> None:
        out = parse_hook_output("")
        assert out.hook_errors == [] and out.decision == ""


# ──────────────────────────────────────────────────────────────────────
# B. command 动作族（真实 subprocess）
# ──────────────────────────────────────────────────────────────────────

_ECHO_CODE = """
import json, sys
payload = json.loads(sys.stdin.read())
print(json.dumps({"hookSpecificOutput": {
    "additionalContext": payload.get("tool_name", "") + ":" + payload.get("hook_event_name", "")
}}))
"""

_EXIT2_CODE = """
import json, sys
payload = json.loads(sys.stdin.read())
if payload.get("tool_name") == "bash":
    sys.stderr.write("bash blocked by policy")
    sys.exit(2)
print(json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}}))
"""

_DENY_JSON_CODE = """
import json
print(json.dumps({"hookSpecificOutput": {
    "permissionDecision": "deny", "permissionDecisionReason": "nope"
}}))
"""


class TestCommandAction:
    def test_stdin_payload_and_json_output(self, tmp_path: Path) -> None:
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_PRE_TOOL_USE,
                    action_type="command",
                    command=_script(tmp_path, "h.py", _ECHO_CODE),
                )
            ]
        )
        out = asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))
        assert out.additional_context == "bash:PreToolUse", (
            "stdin 必须收到 payload + hook_event_name"
        )

    def test_exit2_blocks_and_exit0_deny_json(self, tmp_path: Path) -> None:
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_PRE_TOOL_USE,
                    action_type="command",
                    command=_script(tmp_path, "h.py", _EXIT2_CODE),
                )
            ]
        )
        denied = asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))
        assert denied.decision == "deny" and "blocked by policy" in denied.reason
        allowed = asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "read"}))
        assert allowed.decision == "allow" and not allowed.reason

    def test_deny_json_output(self, tmp_path: Path) -> None:
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_PRE_TOOL_USE,
                    action_type="command",
                    command=_script(tmp_path, "h.py", _DENY_JSON_CODE),
                )
            ]
        )
        out = asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))
        assert out.decision == "deny" and out.reason == "nope"

    def test_timeout_isolated(self, tmp_path: Path) -> None:
        code = "import time\ntime.sleep(10)\n"
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_PRE_TOOL_USE,
                    action_type="command",
                    command=_script(tmp_path, "slow.py", code),
                    timeout=1.0,
                )
            ]
        )
        out = asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))
        assert out.decision == "" and any("timed out" in e for e in out.hook_errors)

    def test_nonzero_exit_records_error_without_blocking(self, tmp_path: Path) -> None:
        code = "import sys\nsys.exit(3)\n"
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_PRE_TOOL_USE,
                    action_type="command",
                    command=_script(tmp_path, "fail.py", code),
                )
            ]
        )
        out = asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))
        assert out.decision == "" and any("exit 3" in e for e in out.hook_errors)

    def test_deny_short_circuits_remaining_rules(self, tmp_path: Path) -> None:
        record = tmp_path / "second-ran.txt"
        second = _script(
            tmp_path,
            "second.py",
            "from pathlib import Path\n"
            f"Path(r'{record}').write_text('ran', encoding='utf-8')\n"
            'print("{}")\n',
        )
        first = _script(tmp_path, "first.py", _DENY_JSON_CODE)
        runner = HookRunner(
            [
                HookRule(event=HOOK_PRE_TOOL_USE, action_type="command", command=first),
                HookRule(event=HOOK_PRE_TOOL_USE, action_type="command", command=second),
            ]
        )
        out = asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))
        assert out.decision == "deny"
        assert not record.exists(), "deny 必须短路后续规则"


# ──────────────────────────────────────────────────────────────────────
# C. prompt / agent / http 动作族
# ──────────────────────────────────────────────────────────────────────


class TestLlmAndHttpActions:
    def test_prompt_action_via_provider(self) -> None:
        from bandi.core.message import LLMResponse, TokenUsage
        from bandi.llm.mock import MockProvider

        provider = MockProvider()
        provider.add_response(
            LLMResponse(
                content='{"hookSpecificOutput": {"permissionDecision": "deny",'
                ' "permissionDecisionReason": "prompt says no"}}',
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=5, completion_tokens=5),
                finish_reason="stop",
            )
        )
        runner = HookRunner(
            [HookRule(event=HOOK_PRE_TOOL_USE, action_type="prompt", prompt="check $ARGUMENTS")],
            provider=provider,
        )
        out = asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))
        assert out.decision == "deny" and out.reason == "prompt says no"

    def test_prompt_action_without_provider_skips(self) -> None:
        runner = HookRunner(
            [HookRule(event=HOOK_PRE_TOOL_USE, action_type="prompt", prompt="check")]
        )
        out = asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))
        assert out.decision == "" and any("no provider" in e for e in out.hook_errors)

    def test_agent_action_spawns_subagent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bandi.agent.subagent as sub_mod
        from bandi.agent.subagent import SubagentResult

        captured: dict[str, Any] = {}

        async def fake_spawn(self: Any, task: str, config: Any = None, **kw: Any) -> SubagentResult:
            captured["task"] = task
            return SubagentResult(
                success=True,
                message='{"hookSpecificOutput": {"permissionDecision": "deny"}}',
            )

        monkeypatch.setattr(sub_mod.SubagentManager, "spawn", fake_spawn)
        runner = HookRunner(
            [HookRule(event=HOOK_PRE_TOOL_USE, action_type="agent", prompt="verify $ARGUMENTS")],
            provider=object(),
        )
        out = asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))
        assert out.decision == "deny"
        assert "$ARGUMENTS" not in captured["task"] and "bash" in captured["task"]

    def test_http_action_posts_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        captured: dict[str, Any] = {}

        class _Resp:
            status_code = 200
            text = '{"hookSpecificOutput": {"additionalContext": "from http"}}'

        class _Client:
            def __init__(self, **kw: Any) -> None:
                captured["timeout"] = kw.get("timeout")

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *args: Any) -> bool:
                return False

            async def post(self, url: str, json: Any = None, headers: Any = None) -> _Resp:
                captured["url"] = url
                captured["json"] = json
                return _Resp()

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        runner = HookRunner(
            [HookRule(event=HOOK_PRE_TOOL_USE, action_type="http", url="https://example.test/hook")]
        )
        out = asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))
        assert out.additional_context == "from http"
        assert captured["url"] == "https://example.test/hook"
        assert captured["json"]["tool_name"] == "bash"


# ──────────────────────────────────────────────────────────────────────
# D. 中间件族
# ──────────────────────────────────────────────────────────────────────


def _ctx() -> ToolContext:
    return ToolContext()


def _call(cmd: str = "echo hi") -> ToolCall:
    return ToolCall(id="c1", name="bash", arguments={"command": cmd})


class TestHookMiddleware:
    def test_pre_deny_blocks(self, tmp_path: Path) -> None:
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_PRE_TOOL_USE,
                    action_type="command",
                    command=_script(tmp_path, "h.py", _EXIT2_CODE),
                )
            ]
        )
        result = asyncio.run(HookBeforeMiddleware(runner).before_execute(_ctx(), _call()))
        assert result.allow is False and "hook" in (result.reason or "")

    def test_updated_input_rewrites_arguments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code = (
            "import json\n"
            'print(json.dumps({"hookSpecificOutput": {"updatedInput": {"command": "ls -la"}}}))\n'
        )
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_PRE_TOOL_USE,
                    action_type="command",
                    command=_script(tmp_path, "h.py", code),
                )
            ]
        )
        result = asyncio.run(HookBeforeMiddleware(runner).before_execute(_ctx(), _call("evil")))
        assert result.allow is True and result.modified_params == {"command": "ls -la"}

    def test_allow_hook_does_not_bypass_permission(self, tmp_path: Path) -> None:
        from bandi.tools.middleware import PermissionMiddleware
        from bandi.tools.permissions_pipeline import PermissionsPipeline

        code = 'print(json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}}))\n'
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_PRE_TOOL_USE,
                    action_type="command",
                    command=_script(tmp_path, "h.py", code),
                )
            ]
        )
        pipeline = ToolPipeline()
        pipeline.add(HookBeforeMiddleware(runner))
        pipeline.add(PermissionMiddleware(PermissionsPipeline(deny_tools={"bash"})))

        async def run_tool(_ctx: ToolContext, call: ToolCall) -> ToolResult:
            return ToolResult(tool_call_id=call.id, tool_name=call.name, success=True, output="ran")

        result = asyncio.run(pipeline.execute(_ctx(), _call(), run_tool))
        assert result.success is False and "denied" in (result.error or ""), (
            "hook allow 不得绕过权限链 deny"
        )

    def test_post_adds_context(self, tmp_path: Path) -> None:
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_POST_TOOL_USE,
                    action_type="command",
                    command=_script(tmp_path, "h.py", _ECHO_CODE),
                )
            ]
        )
        result = ToolResult(tool_call_id="c1", tool_name="bash", success=True, output="base")
        out = asyncio.run(HookAfterMiddleware(runner).after_execute(_ctx(), _call(), result))
        assert "[hook context]" in out.output and "bash:PostToolUse" in out.output

    def test_failure_event_dispatched(self, tmp_path: Path) -> None:
        record = tmp_path / "events.txt"
        code = (
            "import sys, json\n"
            "payload = json.loads(sys.stdin.read())\n"
            f"open(r'{record}', 'a', encoding='utf-8').write(payload['hook_event_name'] + chr(10))\n"
        )
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_POST_TOOL_USE,
                    action_type="command",
                    command=_script(tmp_path, "ok.py", code),
                ),
                HookRule(
                    event=HOOK_POST_TOOL_USE_FAILURE,
                    action_type="command",
                    command=_script(tmp_path, "fail.py", code),
                ),
            ]
        )
        failed = ToolResult(
            tool_call_id="c1", tool_name="bash", success=False, output="", error="boom"
        )
        asyncio.run(HookAfterMiddleware(runner).after_execute(_ctx(), _call(), failed))
        assert record.read_text(encoding="utf-8").strip() == "PostToolUseFailure"

    def test_no_runner_is_noop(self) -> None:
        result = asyncio.run(HookBeforeMiddleware(None).before_execute(_ctx(), _call()))
        assert isinstance(result, MiddlewareResult) and result.allow


# ──────────────────────────────────────────────────────────────────────
# E. 信任门禁族（config 层）
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / ".bandi").mkdir(parents=True)
    proj = tmp_path / "proj"
    (proj / ".bandi").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(proj)
    import os

    for key in list(os.environ):
        if key.startswith("BANDI_"):
            monkeypatch.delenv(key, raising=False)
    return home, proj


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_HOOKS_YAML = "hooks:\n  PreToolUse:\n    - type: command\n      command: echo hi\n"


class TestTrustGate:
    def test_user_hooks_always_loaded(self, sandbox: tuple[Path, Path]) -> None:
        from bandi.core.config import BandiConfig

        home, _ = sandbox
        _write(home / ".bandi" / "config.yaml", "env: dev\n" + _HOOKS_YAML)
        cfg = BandiConfig.load()
        assert cfg.hooks.get("PreToolUse"), "user 层 hooks 必须生效"

    def test_project_hooks_need_trust(self, sandbox: tuple[Path, Path]) -> None:
        from bandi.core.config import BandiConfig

        _, proj = sandbox
        _write(proj / ".bandi" / "config.yaml", _HOOKS_YAML)
        cfg = BandiConfig.load()
        assert cfg.hooks == {}, "未信任的项目 hooks 必须被剔除（fail-closed）"

    def test_trusted_project_hooks_loaded(
        self, sandbox: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        from bandi.core.config import BandiConfig

        _, proj = sandbox
        _write(proj / ".bandi" / "config.yaml", _HOOKS_YAML)
        os.environ["BANDI_TRUST_PROJECT_HOOKS"] = "1"
        try:
            cfg = BandiConfig.load()
        finally:
            os.environ.pop("BANDI_TRUST_PROJECT_HOOKS", None)
        assert cfg.hooks.get("PreToolUse"), "显式信任后项目 hooks 必须加载"


# ──────────────────────────────────────────────────────────────────────
# F. states 生命周期接线族
# ──────────────────────────────────────────────────────────────────────


class TestStatesLifecycle:
    def _mk_ctx(self, raw_input: str) -> Any:
        from bandi.agent.base import ExecutionContext
        from bandi.core.config import AgentConfig
        from bandi.core.event_bus import EventBus
        from bandi.core.message import Message, MessageRole
        from bandi.llm.mock import MockProvider

        ctx = ExecutionContext(
            messages=[Message(role=MessageRole.USER, content=raw_input)],
            provider=MockProvider(),
            config=AgentConfig(),
            bus=EventBus(),
        )
        ctx.raw_input = raw_input
        return ctx

    def test_user_prompt_submit_injects_context(self, tmp_path: Path) -> None:
        from bandi.agent.states import on_init

        record = tmp_path / "events.txt"
        code = (
            "import sys, json\n"
            "payload = json.loads(sys.stdin.read())\n"
            f"open(r'{record}', 'a', encoding='utf-8').write(payload['hook_event_name'] + chr(10))\n"
            'print(json.dumps({"hookSpecificOutput": {"additionalContext": "EXTRA"}}))\n'
        )
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_USER_PROMPT_SUBMIT,
                    action_type="command",
                    command=_script(tmp_path, "h.py", code),
                ),
                HookRule(
                    event=HOOK_SESSION_START,
                    action_type="command",
                    command=_script(tmp_path, "h2.py", code),
                ),
            ]
        )
        ctx = self._mk_ctx("帮我查库存")
        ctx.meta["hook_runner"] = runner
        ctx.turn_index = 0

        asyncio.run(on_init(ctx))

        assert ctx.raw_input.startswith("EXTRA"), "additionalContext 必须注入 raw_input"
        events = record.read_text(encoding="utf-8").split()
        assert events == ["SessionStart", "UserPromptSubmit"], f"事件顺序/触发错误: {events}"

    def test_session_start_only_first_turn(self, tmp_path: Path) -> None:
        from bandi.agent.states import on_init

        record = tmp_path / "events.txt"
        code = (
            "import sys, json\n"
            "payload = json.loads(sys.stdin.read())\n"
            f"open(r'{record}', 'a', encoding='utf-8').write(payload['hook_event_name'] + chr(10))\n"
        )
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_SESSION_START,
                    action_type="command",
                    command=_script(tmp_path, "h.py", code),
                ),
                HookRule(
                    event=HOOK_USER_PROMPT_SUBMIT,
                    action_type="command",
                    command=_script(tmp_path, "h2.py", code),
                ),
            ]
        )
        ctx = self._mk_ctx("继续")
        ctx.meta["hook_runner"] = runner
        ctx.turn_index = 3

        asyncio.run(on_init(ctx))

        events = record.read_text(encoding="utf-8").split()
        assert events == ["UserPromptSubmit"], "非首轮不得重复 SessionStart"

    def test_compaction_events_fire(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from bandi.agent import states as states_mod
        from bandi.agent.states import on_compressing

        events: list[str] = []

        class _SpyRunner:
            async def run_event(self, event: str, payload: dict[str, Any]) -> Any:
                from bandi.tools.hooks import HookOutcome

                events.append(event)
                return HookOutcome()

        stats = SimpleNamespace(
            stage="micro",
            input_chars=10,
            output_chars=5,
            input_messages=3,
            output_messages=2,
            micro_deduplicated=0,
            micro_trimmed=0,
            micro_merged=0,
            progressive_chunks=0,
        )
        outcome = SimpleNamespace(success=True, stats=stats, messages=[])

        async def fake_micro(ctx: Any) -> Any:
            return outcome

        async def fake_auto(ctx: Any) -> Any:
            return outcome

        monkeypatch.setattr(
            states_mod,
            "get_query_deps",
            lambda ctx: SimpleNamespace(microcompact=fake_micro, autocompact=fake_auto),
        )

        ctx = self._mk_ctx("压缩场景")
        ctx.meta["hook_runner"] = _SpyRunner()
        ctx.messages = []
        asyncio.run(on_compressing(ctx))

        assert events[0] == "PreCompact" and "PostCompact" in events, f"压缩事件接线缺失: {events}"


# ──────────────────────────────────────────────────────────────────────
# G. command 动作默认 exec（无 shell 注入面）
# ──────────────────────────────────────────────────────────────────────

_DUMP_ARGV_CODE = """
import json, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding="utf-8")
print(json.dumps({"hookSpecificOutput": {"additionalContext": "argv-dumped"}}))
"""


def _argv_command(tmp_path: Path, out: Path, tail: str) -> str:
    return f'{_script(tmp_path, "dump_argv.py", _DUMP_ARGV_CODE)} "{out}" {tail}'


class TestCommandExecDefault:
    """契约：command hook 默认以 exec 拆分执行（分号/重定向是字面参数，无 shell 展开）；
    仅显式 `shell: true` 才走系统 shell；拆分失败返回失败不执行。"""

    def test_load_parses_shell_flag(self) -> None:
        rules = load_hook_rules(
            {
                "PreToolUse": [
                    {"type": "command", "command": "x", "shell": True},
                    {"type": "command", "command": "y"},
                ]
            }
        )
        assert rules[0].shell is True and rules[1].shell is False, (
            "shell 仅显式声明才为 True（默认 exec）"
        )

    def test_semicolon_is_literal_argument(self, tmp_path: Path) -> None:
        """破坏者契约：`echo hi; rm -rf x` 的分号默认不得触发第二条命令。"""
        import json

        out = tmp_path / "argv.json"
        marker = tmp_path / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        command = _argv_command(tmp_path, out, f'echo hi; rm -rf "{marker}"')
        runner = HookRunner(
            [HookRule(event=HOOK_PRE_TOOL_USE, action_type="command", command=command)]
        )
        outcome = asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))

        assert marker.exists(), "默认 exec 下分号后的命令不得执行"
        argv = json.loads(out.read_text(encoding="utf-8"))
        assert argv == ["echo", "hi;", "rm", "-rf", str(marker)], (
            f"分号必须作为字面参数保留在单个 token：{argv}"
        )
        assert outcome.decision == ""

    def test_redirection_is_literal_argument(self, tmp_path: Path) -> None:
        """破坏者契约：默认 exec 下重定向元字符不生效（不写文件）。"""
        import json

        out = tmp_path / "argv.json"
        target = tmp_path / "shell-redirect.txt"
        command = _argv_command(tmp_path, out, f'> "{target}"')
        runner = HookRunner(
            [HookRule(event=HOOK_PRE_TOOL_USE, action_type="command", command=command)]
        )
        asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))

        assert not target.exists(), "默认 exec 下 > 重定向不得写文件"
        argv = json.loads(out.read_text(encoding="utf-8"))
        assert ">" in argv and str(target) in argv, f"重定向必须是字面参数：{argv}"

    def test_explicit_shell_true_executes_shell_semantics(self, tmp_path: Path) -> None:
        """契约：显式 shell: true 才启用系统 shell（重定向真实生效）。"""
        target = tmp_path / "shell-out.txt"
        command = f'echo shell-ran>"{target}"'
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_PRE_TOOL_USE,
                    action_type="command",
                    command=command,
                    shell=True,
                )
            ]
        )
        asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))

        assert target.exists(), "shell: true 时重定向必须生效"
        assert "shell-ran" in target.read_text(encoding="utf-8")

    def test_unparsable_command_fails_without_executing(self, tmp_path: Path) -> None:
        """契约：拆分失败（未闭合引号）→ 记失败不执行，绝不回落 shell。"""
        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_PRE_TOOL_USE,
                    action_type="command",
                    command='echo "unclosed',
                )
            ]
        )
        outcome = asyncio.run(runner.run_event(HOOK_PRE_TOOL_USE, {"tool_name": "bash"}))
        assert outcome.decision == "" and outcome.hook_errors, "拆分失败必须记 hook_errors"
        assert any("parse" in e or "split" in e for e in outcome.hook_errors), (
            f"失败原因需可观测：{outcome.hook_errors}"
        )

    def test_quoted_flag_value_kept_as_single_token(self) -> None:
        """契约（功能面）：`--flag="a b"` 必须解析为单个参数——Windows posix=False
        此前拆成 `--flag="a` 与 `b"`（引号内空格拆错）。"""
        from bandi.tools.hooks import _split_command_line

        argv = _split_command_line('tool --flag="a b" tail')
        assert argv == ["tool", "--flag=a b", "tail"], f"引号内空格不得拆开：{argv}"

    def test_unclosed_quote_rejected_by_splitter(self) -> None:
        """破坏者契约：未闭合引号脚本必须拒绝（返回 None），不得部分后照常执行。"""
        from bandi.tools.hooks import _split_command_line

        assert _split_command_line('tool --flag="unclosed') is None


class TestHookDenyObservable:
    """HookBefore deny 短路后**拒绝事件必须发出**——
    deny 落入 TOOL_ERROR 事件，不得静默拒绝。"""

    class _Bus:
        def __init__(self) -> None:
            from bandi.core.event_bus import EventBus

            self._inner = EventBus()
            self.events: list[Any] = []

        async def emit(self, event: Any) -> None:
            self.events.append(event)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    def _probe_tool(self) -> Any:
        from bandi.tools.base import BaseTool, ToolCategory

        class _ProbeTool(BaseTool):
            name: str = "probe"
            description: str = "hook deny 探针"
            category: ToolCategory = ToolCategory.UTIL

            async def execute(self, params: dict[str, Any], context: Any = None) -> str:
                return "should-not-run"

        return _ProbeTool()

    def test_middleware_deny_emits_tool_error(self, tmp_path: Path) -> None:
        """契约（核心）：中间件 deny（HookBefore exit2）→ TOOL_ERROR 事件
        携带拒绝原因（红：现状 deny 结果静默返回、事件流无痕）。"""
        from bandi.core.events import EventType
        from bandi.tools.executor import ToolExecutor
        from bandi.tools.registry import ToolRegistry

        runner = HookRunner(
            [
                HookRule(
                    event=HOOK_PRE_TOOL_USE,
                    action_type="command",
                    # 无条件 deny（exit 2）——不依赖 _EXIT2_CODE 的工具名匹配逻辑
                    command=_script(tmp_path, "deny2.py", "import sys\nsys.exit(2)\n"),
                )
            ]
        )
        bus = self._Bus()
        reg = ToolRegistry()
        reg.register_instance(self._probe_tool())
        pipeline = ToolPipeline()
        pipeline.add(HookBeforeMiddleware(runner))
        pipeline.add(HookAfterMiddleware(runner))
        ex = ToolExecutor(registry=reg, event_bus=bus, tool_pipeline=pipeline)

        results = asyncio.run(ex.execute([ToolCall(id="h1", name="probe", arguments={})]))

        assert results[0].success is False, "deny 必须失败"
        assert "should-not-run" not in (results[0].output or ""), "工具不得执行"
        errors = [e for e in bus.events if e.type is EventType.TOOL_ERROR]
        assert errors, "HookBefore deny 必须发 TOOL_ERROR 事件（拒绝可观测）"
        assert (
            "deny" in str(errors[0].data.get("error_message", "")).lower()
            or "blocked" in str(errors[0].data.get("error_message", "")).lower()
        )

    def test_no_deny_no_error_event(self, tmp_path: Path) -> None:
        """契约（对照）：无 deny 时正常执行、不发 TOOL_ERROR。"""
        from bandi.core.events import EventType
        from bandi.tools.executor import ToolExecutor
        from bandi.tools.registry import ToolRegistry

        bus = self._Bus()
        reg = ToolRegistry()
        reg.register_instance(self._probe_tool())
        ex = ToolExecutor(registry=reg, event_bus=bus, tool_pipeline=ToolPipeline())

        results = asyncio.run(ex.execute([ToolCall(id="h2", name="probe", arguments={})]))

        assert results[0].success is True
        assert not [e for e in bus.events if e.type is EventType.TOOL_ERROR]
