# 功能：工具/生命週期 hooks——cc-haha hooks 事件 + 四类动作同构。
# 8 事件：PreToolUse / PostToolUse / PostToolUseFailure / UserPromptSubmit /
# SessionStart / SessionEnd / PreCompact / PostCompact；动作：command / prompt /
# additionalContext），hook allow 不绕过 settings deny（裁决在权限链后段继续执行）。
# 安全（③⁻）：command 动作默认按 exec 拆分执行（无 shell 展开，注入面收敛）；
# 仅条目显式 `shell: true` 才走系统 shell。
from __future__ import annotations

import asyncio
import json
import logging
import shlex
import sys
from dataclasses import dataclass, field
from typing import Any

from bandi.tools.subprocess_tree import kill_process_tree

logger = logging.getLogger(__name__)

HOOK_PRE_TOOL_USE = "PreToolUse"
HOOK_POST_TOOL_USE = "PostToolUse"
HOOK_POST_TOOL_USE_FAILURE = "PostToolUseFailure"
HOOK_USER_PROMPT_SUBMIT = "UserPromptSubmit"
HOOK_SESSION_START = "SessionStart"
HOOK_SESSION_END = "SessionEnd"
HOOK_PRE_COMPACT = "PreCompact"
HOOK_POST_COMPACT = "PostCompact"

HOOK_EVENTS: tuple[str, ...] = (
    HOOK_PRE_TOOL_USE,
    HOOK_POST_TOOL_USE,
    HOOK_POST_TOOL_USE_FAILURE,
    HOOK_USER_PROMPT_SUBMIT,
    HOOK_SESSION_START,
    HOOK_SESSION_END,
    HOOK_PRE_COMPACT,
    HOOK_POST_COMPACT,
)

_TOOL_EVENTS = frozenset({HOOK_PRE_TOOL_USE, HOOK_POST_TOOL_USE, HOOK_POST_TOOL_USE_FAILURE})
_ACTION_TYPES = ("command", "prompt", "agent", "http")
_DEFAULT_TIMEOUT_SEC = 30.0
_MAX_TIMEOUT_SEC = 600.0
_MAX_HOOK_OUTPUT_CHARS = 60_000
_MAX_HTTP_RESPONSE_BYTES = 1_000_000
_BLOCKING_EXIT_CODE = 2


@dataclass(frozen=True)
class HookRule:
    event: str
    action_type: str
    matcher: str = ""
    command: str = ""
    prompt: str = ""
    url: str = ""
    timeout: float = _DEFAULT_TIMEOUT_SEC
    source: str = ""
    # 安全：command 动作的 shell 语义开关——默认 False（按 exec 拆分执行，分号/
    # 重定向等元字符是字面参数）；显式 True 才走系统 shell（用户自担注入面）
    shell: bool = False

    def matches(self, tool_name: str) -> bool:
        # 功能：事件匹配——工具事件按工具名（空 matcher=全部）；其余事件恒匹配
        if self.event not in _TOOL_EVENTS:
            return True
        if not self.matcher:
            return True
        return self.matcher == tool_name


@dataclass
class HookOutcome:
    decision: str = ""  # "deny" | "allow" | ""
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    additional_context: str = ""
    hook_errors: list[str] = field(default_factory=list)


def parse_hook_output(text: str) -> HookOutcome:
    # 功能：hook 输出 JSON 宽容解析——hookSpecificOutput 嵌套优先，兼容扁平键
    raw = (text or "").strip()
    if not raw:
        return HookOutcome()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return HookOutcome(hook_errors=[f"hook output is not JSON: {raw[:200]}"])
    if not isinstance(data, dict):
        return HookOutcome(hook_errors=["hook output must be a JSON object"])
    specific = data.get("hookSpecificOutput")
    body = specific if isinstance(specific, dict) else data
    decision = str(body.get("permissionDecision") or body.get("decision") or "").strip().lower()
    if decision not in ("", "allow", "deny"):
        decision = ""
    updated = body.get("updatedInput") or body.get("updated_input")
    if not isinstance(updated, dict):
        updated = None
    return HookOutcome(
        decision=decision,
        reason=str(body.get("permissionDecisionReason") or body.get("reason") or ""),
        updated_input=updated,
        additional_context=str(
            body.get("additionalContext") or body.get("additional_context") or ""
        ),
    )


def load_hook_rules(hooks_config: dict[str, Any] | None) -> list[HookRule]:
    # 功能：把合并后的 hooks 配置（event -> 条目列表）转为 HookRule 列表——非法条目跳过
    if not isinstance(hooks_config, dict):
        return []
    rules: list[HookRule] = []
    for event, entries in hooks_config.items():
        if event not in HOOK_EVENTS or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            action_type = str(entry.get("type") or "").strip().lower()
            if action_type not in _ACTION_TYPES:
                logger.warning("hook entry for %s skipped: unknown type %r", event, action_type)
                continue
            try:
                timeout = float(entry.get("timeout") or _DEFAULT_TIMEOUT_SEC)
            except (TypeError, ValueError):
                timeout = _DEFAULT_TIMEOUT_SEC
            rules.append(
                HookRule(
                    event=event,
                    action_type=action_type,
                    matcher=str(entry.get("matcher") or "").strip(),
                    command=str(entry.get("command") or ""),
                    prompt=str(entry.get("prompt") or ""),
                    url=str(entry.get("url") or ""),
                    timeout=max(1.0, min(timeout, _MAX_TIMEOUT_SEC)),
                    source=str(entry.get("source") or ""),
                    shell=_truthy_flag(entry.get("shell")),
                )
            )
    return rules


def _truthy_flag(value: Any) -> bool:
    # 功能：shell 开关解析——bool 原样；字符串只认 1/true/yes/on（"false" 不得变 True）
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _quotes_balanced(command: str) -> bool:
    # 功能：Windows 命令行引号配平预检——反斜杠转义的引号（\"）不计入配对
    # 未闭合引号拒绝执行
    count = 0
    i = 0
    while i < len(command):
        if command[i] == "\\":
            i += 2
            continue
        if command[i] == '"':
            count += 1
        i += 1
    return count % 2 == 0


def _split_command_line_windows(command: str) -> list[str] | None:
    # 功能：Windows 走 CommandLineToArgvW（ctypes，与 CRT/CreateProcess 参数解析
    # 同源）——`--flag="a b"` 引号内空格不拆、包裹引号剥离；无法解析返回 None
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    argc = ctypes.c_int(0)
    argv_ptr = shell32.CommandLineToArgvW(ctypes.c_wchar_p(command), ctypes.byref(argc))
    if not argv_ptr:
        return None
    try:
        tokens = [argv_ptr[i] for i in range(argc.value)]
    finally:
        kernel32.LocalFree(argv_ptr)
    argv = [token for token in tokens if token != ""]
    return argv or None


def _split_command_line(command: str) -> list[str] | None:
    """功能：exec 模式命令行拆分——POSIX 走 shlex.split；Windows 走
    CommandLineToArgvW（引号内空格不拆、包裹引号剥离）；未闭合引号/解析失败
    返回 None，调用方记失败不执行，绝不回落 shell。

    受限语法：Windows 下仅支持 CreateProcess 风格命令行（`--flag="a b"`
    `"C:\\Program Files\\x.exe"`）。`.bat/.cmd` 不能作为程序名直呼执行
    （create_subprocess_exec 限制），需经 `cmd /c` 或显式 `shell: true`。
    """
    if sys.platform == "win32":
        if not _quotes_balanced(command):
            return None
        return _split_command_line_windows(command)
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    argv = [token for token in tokens if token != ""]
    return argv or None


class HookRunner:
    """hooks 执行器：按事件顺序执行匹配规则；deny 短路，其余输出合并。

    依赖注入（provider/agent 动作需要）：provider=None 时 prompt/agent 动作
    记 hook_errors 跳过（不阻断主链）；所有 hook 异常 fail-safe。
    """

    def __init__(
        self,
        rules: list[HookRule] | None = None,
        *,
        provider: Any | None = None,
        project_root: str | None = None,
        session_id: str = "",
        bus: Any | None = None,
        user_id: str = "",
        agent_id: str = "",
    ) -> None:
        self._rules = list(rules or [])
        self._provider = provider
        self._project_root = project_root
        self._session_id = session_id
        self._bus = bus
        self._user_id = user_id
        # （factory 中 id 生成晚于 HookRunner 构造，经 set_agent_id 后绑定）
        self._agent_id = str(agent_id or "")

    def set_agent_id(self, agent_id: str) -> None:
        # 功能：构建后绑定真实 agent_id（与 ToolExecutor.set_agent_id 同口径）
        self._agent_id = str(agent_id or "")

    @property
    def rules(self) -> list[HookRule]:
        return list(self._rules)

    @property
    def has_rules(self) -> bool:
        return bool(self._rules)

    def rules_for(self, event: str) -> list[HookRule]:
        return [rule for rule in self._rules if rule.event == event]

    async def run_event(self, event: str, payload: dict[str, Any]) -> HookOutcome:
        # 功能：执行某事件的全部匹配 hook——deny 短路，其余合并（ctx/output 累积）
        payload = {**payload, "hook_event_name": event}
        matched = [r for r in self.rules_for(event) if r.matches(str(payload.get("tool_name", "")))]
        outcome = HookOutcome()
        for rule in matched:
            try:
                one = await self._run_one(rule, payload)
            except Exception as exc:  # noqa: BLE001 — hook 异常 fail-safe 不阻断主链
                logger.warning("hook %s/%s failed: %s", rule.event, rule.action_type, exc)
                outcome.hook_errors.append(f"{rule.action_type}: {exc}")
                continue
            if one.decision == "deny":
                one.reason = one.reason or outcome.reason
                one.additional_context = outcome.additional_context + one.additional_context
                one.hook_errors = outcome.hook_errors + one.hook_errors
                return one
            if one.decision == "allow":
                outcome.decision = "allow"
            if one.updated_input is not None:
                outcome.updated_input = one.updated_input
            if one.additional_context:
                outcome.additional_context += (
                    "\n" if outcome.additional_context else ""
                ) + one.additional_context
            outcome.hook_errors.extend(one.hook_errors)
        return outcome

    async def _run_one(self, rule: HookRule, payload: dict[str, Any]) -> HookOutcome:
        if rule.action_type == "command":
            return await self._run_command(rule, payload)
        if rule.action_type == "prompt":
            return await self._run_llm(rule, payload, as_agent=False)
        if rule.action_type == "agent":
            return await self._run_llm(rule, payload, as_agent=True)
        if rule.action_type == "http":
            return await self._run_http(rule, payload)
        return HookOutcome(hook_errors=[f"unknown action type: {rule.action_type}"])

    async def _run_command(self, rule: HookRule, payload: dict[str, Any]) -> HookOutcome:
        # 功能：command 动作——stdin JSON 输入；exit 2=阻断（cc-haha 语义）
        # 其余非 0 记错误不阻断；stdout JSON 解析决策。默认 exec 拆分执行（无
        if not rule.command.strip():
            return HookOutcome(hook_errors=["command hook missing command"])
        input_bytes = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        if rule.shell:
            proc = await asyncio.create_subprocess_shell(
                rule.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            argv = _split_command_line(rule.command)
            if argv is None:
                # 拆分失败绝不回落 shell——按失败返回，命令不执行
                return HookOutcome(
                    hook_errors=[
                        f"command hook parse failed (unbalanced quote?): {rule.command[:200]}"
                    ]
                )
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input_bytes), timeout=rule.timeout
            )
        except TimeoutError:
            # proc.kill() 会留孤儿后代进程）
            kill_process_tree(proc)
            await proc.wait()
            return HookOutcome(hook_errors=[f"command hook timed out ({rule.timeout}s)"])
        code = proc.returncode or 0
        stdout = stdout_b.decode("utf-8", errors="replace")[:_MAX_HOOK_OUTPUT_CHARS]
        stderr = stderr_b.decode("utf-8", errors="replace")[:_MAX_HOOK_OUTPUT_CHARS]
        if code == _BLOCKING_EXIT_CODE:
            reason = (stderr or stdout).strip()[:500]
            return HookOutcome(decision="deny", reason=reason or "blocked by hook command")
        if code != 0:
            return HookOutcome(hook_errors=[f"command hook exit {code}: {stderr.strip()[:200]}"])
        return parse_hook_output(stdout)

    async def _run_llm(
        self, rule: HookRule, payload: dict[str, Any], *, as_agent: bool
    ) -> HookOutcome:
        # 功能：prompt（主 provider 单轮）/ agent（子代理）动作——输出 JSON 决策
        text = rule.prompt.strip()
        if not text:
            return HookOutcome(hook_errors=[f"{rule.action_type} hook missing prompt"])
        if self._provider is None:
            return HookOutcome(
                hook_errors=[f"{rule.action_type} hook skipped: no provider available"]
            )
        prompt_text = text.replace(
            "$ARGUMENTS", json.dumps(payload, ensure_ascii=False, default=str)
        )
        if as_agent:
            from bandi.agent.subagent import SubagentConfig, SubagentManager

            manager = SubagentManager(
                provider=self._provider,
                project_root=self._project_root,
                user_id=getattr(self, "_user_id", ""),
                parent_agent_id=getattr(self, "_agent_id", ""),
            )
            try:
                result = await manager.spawn(
                    prompt_text, SubagentConfig(max_turns=5, timeout_sec=int(rule.timeout))
                )
            finally:
                manager.close()
            if not result.success:
                return HookOutcome(hook_errors=[f"agent hook failed: {result.summary}"])
            return parse_hook_output(result.message)
        from bandi.llm.accounted import accounted_complete

        response = await accounted_complete(
            self._provider,
            [{"role": "user", "content": prompt_text}],
            label="hook_prompt",
            session_id=self._session_id or "hook",
            agent_id="hook_prompt",
            bus=self._bus,
        )
        return parse_hook_output(response.content or "")

    async def _run_http(self, rule: HookRule, payload: dict[str, Any]) -> HookOutcome:
        # 功能：http 动作——POST payload JSON 到用户配置的地址，响应 JSON 决策
        if not rule.url.strip():
            return HookOutcome(hook_errors=["http hook missing url"])
        import httpx

        async with httpx.AsyncClient(timeout=rule.timeout) as client:
            response = await client.post(
                rule.url, json=payload, headers={"Content-Type": "application/json"}
            )
        if response.status_code >= 400:
            return HookOutcome(hook_errors=[f"http hook status {response.status_code}"])
        body = response.text[:_MAX_HTTP_RESPONSE_BYTES]
        return parse_hook_output(body)
