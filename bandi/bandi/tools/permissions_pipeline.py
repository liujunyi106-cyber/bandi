# 功能：权限管道 — 7 层路径拦截（实际生效 5 层 + 预留 2 层）
#
# （当前无 UI 与 webhook/policy-as-code 接入，不参与拦截）。
# 层清单以下方 ACTIVE_LAYERS / RESERVED_LAYERS 为准——勿再让 docstring 与实际漂移。

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bandi.core.message import ToolCall
from bandi.core.permission.rules import PermissionRule, resolve_rules, rule_matches
from bandi.core.user_interaction import UserInteraction
from bandi.tools.base import BaseTool

logger = logging.getLogger(__name__)

# 实际参与拦截的层（按代码执行顺序）
ACTIVE_LAYERS: tuple[str, ...] = ("deny", "ask", "mode", "shell", "sensitive")
# 预留层：已占位、当前不参与拦截
RESERVED_LAYERS: tuple[str, ...] = ("ui", "external")
# 管线层总数——契约据此防「宣称 7 层实为 5 层」的静默漂移
TOTAL_LAYERS: int = len(ACTIVE_LAYERS) + len(RESERVED_LAYERS)

# 此处缺省必须与其保持一致（契约 test_contract_ask_tools_single_source 钉死相等）。
# memory_assert 是结构化三元组直写长期记忆——与本体写入同走权限卡
_ASK_TOOLS: set[str] = {"bash", "write", "ontology_define", "ontology_assert", "memory_assert"}

# 触发询问的风险级默认值——唯一真实默认源：core/config.py PermissionsConfig.ask_risk_levels。
_DEFAULT_ASK_RISK_LEVELS: tuple[str, ...] = ("high", "critical")


@dataclass
class PermissionVerdict:
    allow: bool = True
    ask_user: bool = False
    reason: str = ""
    feedback: str = ""
    rule_pattern: str = ""
    rule_source: str = ""
    rule_action: str = ""


_SENSITIVE_PATH_ARG_NAMES: tuple[str, ...] = (
    "path",
    "file_path",
    "file",
    "cwd",
    "target",
    "dest",
    "output_dir",
    "output_path",
    "destination",
    "src",
    "source",
)

_DENY_TOOLS: set[str] = set()
_BYPASS_TOOLS: set[str] = {
    "read",
    "web_search",
    "web_fetch",
    "glob",
    "grep",
    "ontology_query",
    "request_user_input",
    "todo_read",
    "todo_write",
}
_DENY_COMMANDS: list[str] = ["rm -rf /", "format *", "del /f /s", "rd /s /q"]
# PowerShell 破坏命令形态（大小写不敏感）——bash AST 层已拦
# 权限管道 Layer 1 deny 列表独立防线同步覆盖
_PS_DENY_COMMANDS: list[str] = [
    "remove-item -recurse",
    "remove-item -r ",
    "remove-item -force",
    "remove-item -f ",
    "format-volume",
    "clear-content",
    "icacls",
    "takeown",
    "invoke-expression",
    "iex ",
    "stop-computer",
    "restart-computer",
]


def _tool_command_fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    """确认缓存指纹——有 command 的工具（bash）用命令串；其余工具回退参数摘要
    指纹（此前无 command 恒返回空串 → write 等 ask 工具的审批授权
    cache_approved 永不生效，headless 下 granted re-issue 仍被 _check_ui 拒）。"""
    command = str(arguments.get("command", "")) if arguments else ""
    if command:
        return f"{tool_name}:{command}"
    if not arguments:
        return ""
    digest = hashlib.sha256(
        json.dumps(arguments, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"{tool_name}:{digest}"


# 目标（原正则排除引号致 `> "C:/越界"` 完全不提取而绕过沙箱）
_REDIRECT_TARGET_RE = re.compile(
    r"""(?:[0-9]?>>?|&>>?)\s*(?:"([^"]+)"|'([^']+)'|([^\s|;&<>'\"]+))"""
)
_WRITE_CMD_TARGET: dict[str, int] = {
    "cp": -1,
    "mv": -1,
    "install": -1,
}
# 全部位置参数均为写目标（rm a b c → 三个都是）——修复前只检末位致越界目标漏检
_WRITE_CMD_ALL_OPERANDS: frozenset[str] = frozenset(
    {"rm", "rmdir", "mkdir", "touch", "tee", "truncate"}
)
_REDIRECT_TARGET_EXEMPT: frozenset[str] = frozenset(
    {"/dev/null", "nul", "/dev/stdout", "/dev/stderr"}
)
# 中间参数（-c k=v / --git-dir= 等）且 of= 支持引号目标
_DD_OF_RE = re.compile(r"""\bof=(?:"([^"]+)"|'([^']+)'|([^\s|;&<>'\"]+))""")
_GIT_CONFIG_GLOBAL_RE = re.compile(r"\bgit\b[^|;&<>]*?\bconfig\b[^|;&<>]*?--(?:global|system)\b")


def _expand_home_token(token: str) -> str:
    """功能：——写目标 token 的 home 展开（~ / $HOME 前缀）；含其他变量/
    命令替换的目标原样返回（由 _check_shell_safety fail-closed 拒绝）。"""
    t = token.strip()
    home = str(Path.home())
    if t == "~":
        return home
    if t.startswith("~") and len(t) > 1 and t[1] in ("/", "\\"):
        return str(Path(home) / t[2:])
    for prefix in ("${HOME}", "$HOME"):
        if t == prefix:
            return home
        if t.startswith(prefix + "/"):
            return str(Path(home) / t[len(prefix) + 1 :])
    return t


def extract_shell_write_targets(command: str) -> list[str]:
    """功能：提取 shell 命令的落盘写目标——重定向目标
    cp/mv/tee 等命令的目标位置参数、sed -i 文件参数、dd of= 目标、git 全局配置；
    去重保序（沙箱校验消费）；：引号包裹目标提取 + 波浪号/$HOME 展开归一。"""
    if not command:
        return []
    targets: list[str] = []

    def _collect(match: re.Match[str]) -> None:
        token = (match.group(1) or match.group(2) or match.group(3) or "").strip()
        if token and token not in _REDIRECT_TARGET_EXEMPT:
            targets.append(token)

    for m in _REDIRECT_TARGET_RE.finditer(command):
        _collect(m)
    for m in _DD_OF_RE.finditer(command):
        _collect(m)
    if _GIT_CONFIG_GLOBAL_RE.search(command):
        # 全局配置落 home（workspace 外）→ 由沙箱判定 deny（绝对化防 ~ 字面量绕过）
        targets.append(str(Path.home() / ".gitconfig"))
    from bandi.company.shell_safety import split_shell_segments

    for segment in split_shell_segments(command):
        if len(segment) < 2:
            continue
        head = segment[0].lower()
        if head == "sed" and any(
            item.startswith("-i") or item.startswith("--in-place") for item in segment[1:]
        ):
            operands = [t.strip("'\"") for t in segment[1:] if not t.startswith("-")]
            for token in operands[1:]:
                if token and token not in _REDIRECT_TARGET_EXEMPT:
                    targets.append(token)
            continue
        if head in _WRITE_CMD_ALL_OPERANDS:
            for token in (t.strip("'\"") for t in segment[1:] if not t.startswith("-")):
                if token and token not in _REDIRECT_TARGET_EXEMPT:
                    targets.append(token)
            continue
        index = _WRITE_CMD_TARGET.get(head)
        if index is None:
            continue
        token = segment[index].strip("'\"")
        if token and not token.startswith("-") and token not in _REDIRECT_TARGET_EXEMPT:
            targets.append(token)
    out: list[str] = []
    seen: set[str] = set()
    for t in targets:
        expanded = _expand_home_token(t)
        if expanded not in seen:
            seen.add(expanded)
            out.append(expanded)
    return out


def plan_mode_tool_violations(registry: Any) -> list[str]:
    # 功能：plan 白名单只读性守卫——plan 静默放行的成员不得有未确认副作用。
    # ask 名单成员不构成违规：_check_ask_list 对其在 plan 下同样返回 ask_user
    # （弹权限卡，副作用决定权交还用户）；仍违规的是破坏性/override=ask/高危成员
    # （这三类在 plan 下会被静默放行）。
    from bandi.tools.registry import ToolRegistry

    violations: list[str] = []
    # 只遍历 PLAN_MODE_TOOLS（只读集），**不含** ALWAYS_AVAILABLE_TOOLS 的交互通道：
    # 提问工具在 plan 下放行是显式产品决策（plan 模式也允许向用户澄清），非腐化；
    # 本守卫的语义是「只读集内不得混入有副作用成员」。
    for name in ToolRegistry.PLAN_MODE_TOOLS:
        tool = registry.get(name)
        if tool is None:
            continue
        reasons = []
        if tool.is_destructive:
            reasons.append("destructive")
        if tool.permissions_override == "ask":
            reasons.append("override=ask requires confirmation channel")
        if tool.risk_level in _DEFAULT_ASK_RISK_LEVELS:
            reasons.append(f"risk={tool.risk_level}")
        if reasons:
            violations.append(f"{name}: {', '.join(reasons)}")
    return violations


class PermissionsPipeline:
    def __init__(
        self,
        deny_tools: set[str] | None = None,
        ask_tools: set[str] | None = None,
        bypass_tools: set[str] | None = None,
        deny_commands: list[str] | None = None,
        user_interaction: UserInteraction | None = None,
        project_root: str | None = None,
        ask_risk_levels: list[str] | None = None,
        rules: list[PermissionRule] | None = None,
    ):
        self._deny_tools = _DENY_TOOLS if deny_tools is None else deny_tools
        # ask_tools 缺省回落——值与 config.ask_tools 默认一致（双源统一，见模块常量注释）
        self._ask_tools = _ASK_TOOLS if ask_tools is None else ask_tools
        # ask_risk_levels 缺省回落——值与 config.ask_risk_levels 默认一致；None 才回退
        # 显式空列表 = 显式关闭风险级询问（与 deny/ask/bypass 三名单同语义）
        self._ask_risk_levels: tuple[str, ...] = (
            _DEFAULT_ASK_RISK_LEVELS if ask_risk_levels is None else tuple(ask_risk_levels)
        )
        self._bypass_tools = _BYPASS_TOOLS if bypass_tools is None else bypass_tools
        self._deny_commands = _DENY_COMMANDS if deny_commands is None else deny_commands
        self._rules: list[PermissionRule] = list(rules) if rules else []
        self._user_interaction = user_interaction
        # 确认缓存（tool_name+command 指纹）——WARN 命令确认后
        # 同会话同命令不再重复询问
        self._confirm_cache: dict[str, float] = {}
        # 模式判据只取决于 (工具名, 模式)，与本次调用的参数无关——同一次调用里
        # 「参数校验前的模式闸门」与「权限流水线内的模式检查」判的是同一件事，
        # 各算一次等于重复评估。缓存判据让二者复用同一结果（单点评估）。
        self._mode_verdict_cache: dict[tuple[str, str], PermissionVerdict] = {}
        self._project_root = project_root
        # .2 修复：拒绝追踪的会话维度——由执行器经 bind_session_id 注入
        self._session_id = ""

    def set_user_interaction(self, ui: UserInteraction | None) -> None:
        self._user_interaction = ui

    def bind_session_id(self, session_id: str) -> None:
        """.2 修复：交互式审批（CLI/SSE）的记账会话键——此前只有 headless
        审批流计入拒绝追踪，交互式通道的拒绝不记账也不短路（在主要
        入口失效）。执行器绑定 session_meta 时同步注入。"""
        self._session_id = str(session_id or "")

    def _record_decision(self, tool_name: str, *, approved: bool) -> None:
        """交互式决策记账（无会话键=不可归属时跳过，不抛）。"""
        sid = getattr(self, "_session_id", "")
        if not sid:
            return
        from bandi.safety.denial_tracker import get_denial_tracker

        tracker = get_denial_tracker()
        if approved:
            tracker.record_approval(sid, tool_name)
        else:
            tracker.record_denial(sid, tool_name)

    @property
    def user_interaction_available(self) -> bool:
        # headless/API 场景走审批暂停）
        return self._user_interaction is not None

    @property
    def user_interaction(self) -> Any:
        # 功能：交互通道只读访问（executor 批量预检据此发起单卡聚合授权）
        return self._user_interaction

    def is_confirm_cached(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        # 功能：该调用是否已在确认缓存内（批量预检据此跳过重复询问）
        fingerprint = _tool_command_fingerprint(tool_name, arguments)
        return bool(fingerprint and self._confirm_cache_hit(fingerprint))

    def needs_internal_confirmation(self, tc: ToolCall, tool: BaseTool | None) -> bool:
        # 功能：该调用执行时是否会触发工具内部二次确认（bash 的 WARN/CAUTION 命令）
        if tool is None or tc.name != "bash":
            return False
        command = str((tc.arguments or {}).get("command") or "").strip()
        if not command:
            return False
        from bandi.tools.bash import BashTool

        return BashTool.evaluate_command(command)[0] == "confirm"

    def needs_approval(self, tc: ToolCall, tool: BaseTool | None, mode: str) -> bool:
        """功能：是否需要人工审批——委托 _check_ask_list 的单一实现。

        此前是同一语义的第二份手写实现（plan / bypass / ask 名单 / 风险级四处条件
        与 _check_ask_list 完全重复），改一处忘另一处会造成 CLI 放行 / headless
        拦截的分裂。委托后两通道共用同一判定。
        """
        return self._check_ask_list(tc, tool, mode).ask_user

    def explain_approval(self, tc: ToolCall, tool: BaseTool | None, mode: str) -> PermissionVerdict:
        """功能：返回审批命中的规则/来源，供审批卡解释「为什么弹卡」"""
        return self._check_ask_list(tc, tool, mode)

    async def check(
        self,
        tc: ToolCall,
        tool: BaseTool | None,
        mode: str,
        agent_id: str = "default",
        role: str = "user",
        scope: str = "default",
    ) -> PermissionVerdict:
        verdict = self._check_rule_deny(tc)
        if not verdict.allow:
            return verdict

        if tool is not None:
            override = tool.permissions_override
            if override == "deny":
                return PermissionVerdict(
                    allow=False, reason=f"tool {tc.name} denied by permissions_override"
                )
            if override == "ask":
                return await self._check_override_ask(tc, tool, mode)

        verdict = self._check_deny_list(tc, tool)  # Layer 1: Deny
        if not verdict.allow:
            return verdict

        verdict = self._check_ask_list(tc, tool, mode)  # Layer 2: Ask
        if verdict.ask_user:
            user_verdict = await self._check_ui(tc, tool)
            if not user_verdict.allow:
                return user_verdict

        # 工具可 override check_permissions 实现 subcommand 规则/文件安全路径等
        if tool is not None:
            tool_verdict = await self._check_tool_permissions(tc, tool)
            if not tool_verdict.allow:
                return tool_verdict

        verdict = self._check_mode(tc, tool, mode)  # Layer 3: Mode
        if not verdict.allow:
            return verdict

        if tc.name == "bash":  # Layer 4: Shell
            verdict = self._check_shell_safety(tc)
            if not verdict.allow:
                return verdict

        # 参数元组过滤——无匹配参数的工具自然跳过）
        verdict = self._check_sensitive_path(tc, tool)
        if not verdict.allow:
            return verdict

        return PermissionVerdict(allow=True)

    def _check_rule_deny(self, tc: ToolCall) -> PermissionVerdict:
        # 功能：规则 deny 判定（三态裁决最高优先级，覆盖工具 override）
        for rule in self._rules:
            if rule.action == "deny" and rule_matches(rule, tc.name, tc.arguments):
                return PermissionVerdict(
                    allow=False,
                    reason=f"denied by permission rule {rule.pattern!r}",
                    rule_pattern=rule.pattern,
                    rule_source=rule.source,
                    rule_action="deny",
                )
        return PermissionVerdict(allow=True)

    def _check_deny_list(self, tc: ToolCall, tool: BaseTool | None) -> PermissionVerdict:
        if tc.name in self._deny_tools:
            return PermissionVerdict(allow=False, reason=f"tool {tc.name} is denied")
        if tc.name == "bash":
            command = (tc.arguments.get("command") or "").strip()
            lowered = command.lower()
            for denied in self._deny_commands:
                if command.startswith(denied):
                    return PermissionVerdict(allow=False, reason=f"command denied: {command}")
            for denied in _PS_DENY_COMMANDS:
                if lowered.startswith(denied):
                    return PermissionVerdict(allow=False, reason=f"command denied: {command}")
        return PermissionVerdict(allow=True)

    async def _check_tool_permissions(
        self, tc: ToolCall, tool: BaseTool | None
    ) -> PermissionVerdict:
        """工具实现级权限委派：调用 tool.check_permissions 若 override。"""
        if tool is None:
            return PermissionVerdict(allow=True)
        check = getattr(tool, "check_permissions", None)
        # 仅当工具显式实现了 check_permissions（非 BaseTool 默认/非 mock 自动属性）
        if check is None or not callable(check):
            return PermissionVerdict(allow=True)
        impl = getattr(type(tool), "check_permissions", None)
        if impl is None:
            return PermissionVerdict(allow=True)
        try:
            result = await check(tc.arguments, None)
        except Exception as e:  # noqa: BLE001 — 工具权限检查异常按拒绝处理
            logger.warning("tool %s check_permissions failed: %s", tc.name, e)
            return PermissionVerdict(allow=False, reason=f"tool permission check error: {e}")
        if isinstance(result, tuple):
            allowed, reason = result
        elif isinstance(result, bool):
            # 不得 getattr(False,'allow',True) fail-open
            allowed, reason = result, ""
        else:
            allowed = getattr(result, "allow", None)
            if allowed is None:
                return PermissionVerdict(
                    allow=False,
                    reason=f"tool {tc.name} check_permissions returned no verdict",
                )
            reason = getattr(result, "reason", "")
        if not allowed:
            return PermissionVerdict(allow=False, reason=str(reason) or f"denied by {tc.name}")
        return PermissionVerdict(allow=True)

    def _check_ask_list(self, tc: ToolCall, tool: BaseTool | None, mode: str) -> PermissionVerdict:
        in_ask_list = tc.name in self._ask_tools
        if mode == "plan":
            # plan 白名单内的 ask 名单成员（memory_assert，计划记依赖的既有放行）
            # 在 plan 下仍要求确认——弹权限卡把写入决定交还用户，不是静默旁路。
            # 白名单外的 ask 成员（bash/write 等）不在此触发：交给 Layer3 mode
            # 拒绝（报「plan mode 不允许」，语义准确于「headless 无确认通道」）。
            from bandi.tools.registry import ToolRegistry

            if in_ask_list and tc.name in ToolRegistry.PLAN_MODE_TOOLS:
                return PermissionVerdict(allow=True, ask_user=True)
            return PermissionVerdict(allow=True)
        rule_action = resolve_rules(self._rules, tc.name, tc.arguments)
        if rule_action == "allow":
            return PermissionVerdict(allow=True)
        if rule_action == "ask":
            matched = next(
                (
                    rule
                    for rule in self._rules
                    if rule.action == "ask" and rule_matches(rule, tc.name, tc.arguments)
                ),
                None,
            )
            pattern = matched.pattern if matched is not None else ""
            source = matched.source if matched is not None else ""
            return PermissionVerdict(
                allow=True,
                ask_user=True,
                reason=f"requires approval by permission rule {pattern!r}",
                rule_pattern=pattern,
                rule_source=source,
                rule_action="ask",
            )
        if tc.name in self._bypass_tools:
            return PermissionVerdict(allow=True)
        if in_ask_list:
            return PermissionVerdict(
                allow=True,
                ask_user=True,
                reason=f"tool {tc.name} is in ask_tools",
                rule_source="ask_tools",
                rule_action="ask",
            )
        # 工具自声明的「必须询问」意图——`check()` 在 Layer 1 之前即把它路由到
        # `_check_override_ask()` → `_check_ui()`，故本判定必须同口径，否则
        # `needs_approval` 为 False 会让审批暂停门（executor._approval_gate）永不触发
        # headless/API 下这些工具落到 `_check_ui()` 被硬拒（"requires approval but no
        if tool is not None and tool.permissions_override == "ask":
            return PermissionVerdict(
                allow=True,
                ask_user=True,
                reason=f"tool {tc.name} declares permissions_override=ask",
                rule_source="tool_override",
                rule_action="ask",
            )
        if tool and tool.risk_level in self._ask_risk_levels:
            return PermissionVerdict(
                allow=True,
                ask_user=True,
                reason=f"tool {tc.name} risk_level={tool.risk_level}",
                rule_source="risk_level",
                rule_action="ask",
            )
        return PermissionVerdict(allow=True)

    def _check_mode(self, tc: ToolCall, tool: BaseTool | None, mode: str) -> PermissionVerdict:
        cached = self._mode_verdict_cache.get((tc.name, mode))
        if cached is not None:
            return cached
        verdict = self._compute_mode_verdict(tc, mode)
        self._mode_verdict_cache[(tc.name, mode)] = verdict
        return verdict

    def _compute_mode_verdict(self, tc: ToolCall, mode: str) -> PermissionVerdict:
        if mode == "plan":
            from bandi.tools.registry import ToolRegistry

            # + 全域交互通道）——消除双处定义漂移。
            # → LLM 已正确调用提问工具（参数含 choices）仍被拒「not allowed in plan
            # mode」，多选项卡/多问分页卡生产链路恒不触发。
            allowed = set(ToolRegistry.PLAN_MODE_ALLOWED)
            if tc.name not in allowed:
                return PermissionVerdict(
                    allow=False, reason=f"tool {tc.name} not allowed in plan mode"
                )
        return PermissionVerdict(allow=True)

    def check_mode_available(
        self, tc: ToolCall, tool: BaseTool | None, mode: str
    ) -> PermissionVerdict:
        """功能：模式可用性前置校验（先于参数校验调用——模式不可用的报错优先于参数错误）。"""
        return self._check_mode(tc, tool, mode)

    def _check_shell_safety(self, tc: ToolCall) -> PermissionVerdict:
        command = (tc.arguments.get("command") or "").strip()
        if not command:
            return PermissionVerdict(allow=False, reason="empty command")
        # ① 注入形态（管道执行解释器/反引号/命令替换）硬拒，不给确认机会；
        # ② classifier DENIED 级（危险命令形态）deny；
        # DANGEROUS/CAUTION 交给 bash 工具确认层（合法重定向不受影响）
        from bandi.company.shell_safety import (
            ShellSafetyLevel,
            classify_shell_command,
            pipeline_deny_reason,
        )

        injection = pipeline_deny_reason(command)
        if injection:
            return PermissionVerdict(allow=False, reason=f"dangerous shell pattern: {injection}")
        result = classify_shell_command(command)
        if result.level == ShellSafetyLevel.DENIED:
            return PermissionVerdict(allow=False, reason=f"shell safety denied: {result.reason}")
        for target in extract_shell_write_targets(command):
            if "$" in target or "`" in target:
                return PermissionVerdict(allow=False, reason=f"unresolvable write target: {target}")
            if not self._path_within_workspace(target):
                return PermissionVerdict(
                    allow=False,
                    reason=(
                        f"bash command write target outside workspace: {target} "
                        "(redirect/write targets must stay inside the project or workspace root)"
                    ),
                )
        return PermissionVerdict(allow=True)

    def _path_within_workspace(self, path_str: str) -> bool:
        # 功能：6.7 写目标沙箱判定——复用 fs_tool 单一沙箱语义（含符号链接防御）
        from bandi.tools.fs_tool import resolve_within_workspace

        try:
            resolve_within_workspace(
                path_str,
                extra_roots=[self._project_root] if self._project_root else None,
                base=self._project_root,
            )
            return True
        except (ValueError, OSError):
            return False

    def _check_sensitive_path(self, tc: ToolCall, tool: BaseTool | None) -> PermissionVerdict:
        # 固定元组全参数逐一校验（原 set 取首参，敏感值可被安全参数掩盖）
        from bandi.tools.fs_tool import sensitive_path_reason

        for arg_name in _SENSITIVE_PATH_ARG_NAMES:
            raw = tc.arguments.get(arg_name)
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                p = Path(raw)
                if not p.is_absolute() and self._project_root:
                    p = Path(self._project_root) / p
                target = p.resolve()
            except (OSError, ValueError):
                return PermissionVerdict(allow=False, reason=f"invalid path: {raw}")
            reason = sensitive_path_reason(target)
            if reason:
                return PermissionVerdict(allow=False, reason=reason)
        return PermissionVerdict(allow=True)

    # 确认缓存 TTL：一次性授权（once/WARN 确认）在有效期内穿透 re-issue
    # 不得永久放行——用户「Allow once」不应被放大为进程级永久免问
    _CONFIRM_CACHE_TTL_SECS = 300.0

    def _confirm_cache_hit(self, fingerprint: str) -> bool:
        # 功能：确认缓存命中判定（过期即清）
        expiry = self._confirm_cache.get(fingerprint)
        if expiry is None:
            return False
        if time.time() > expiry:
            self._confirm_cache.pop(fingerprint, None)
            return False
        return True

    def _confirm_cache_put(self, fingerprint: str) -> None:
        # 功能：写入确认缓存（TTL 限时——一次性授权不得被放大为永久免问）
        self._confirm_cache[fingerprint] = time.time() + self._CONFIRM_CACHE_TTL_SECS

    def cache_approved(self, tool_name: str, arguments: dict[str, Any]) -> None:
        """功能：记录一次性确认缓存——审批授权（人工/机审）放行后 re-issue 时
        穿透 UI 层检查（与 confirm_tool 的确认缓存同源，防 headless 二次拒绝）。"""
        fingerprint = _tool_command_fingerprint(tool_name, arguments)
        if fingerprint:
            self._confirm_cache_put(fingerprint)

    async def _check_ui(self, tc: ToolCall, tool: BaseTool | None) -> PermissionVerdict:
        # 缓存判定必须先于 headless 判空：已授权的调用不得被无通道判定二次拒绝
        fingerprint = _tool_command_fingerprint(tc.name, tc.arguments)
        if fingerprint and self._confirm_cache_hit(fingerprint):
            return PermissionVerdict(allow=True)
        if self._user_interaction is None:
            return PermissionVerdict(
                allow=False,
                reason=(
                    f"tool {tc.name} requires approval but no confirmation channel "
                    "is available (headless mode denies by default)"
                ),
            )
        try:
            risk = tool.risk_level if tool else "low"
            with_feedback = getattr(self._user_interaction, "ask_permission_with_feedback", None)
            feedback = ""
            if callable(with_feedback):
                granted, raw_feedback = await with_feedback(
                    tool_name=tc.name,
                    args=tc.arguments,
                    risk=risk,
                )
                feedback = str(raw_feedback or "")
            else:
                granted = await self._user_interaction.ask_permission(
                    tool_name=tc.name,
                    args=tc.arguments,
                    risk=risk,
                )
            if granted:
                if fingerprint:
                    self._confirm_cache_put(fingerprint)
                self._record_decision(tc.name, approved=True)
                return PermissionVerdict(allow=True)
            reason = f"user denied: {tc.name}"
            if feedback:
                reason += f" | user feedback: {feedback}"
            self._record_decision(tc.name, approved=False)
            return PermissionVerdict(allow=False, reason=reason, feedback=feedback)
        except Exception:  # noqa: BLE001
            logger.warning("Permission prompt failed for tool %s, denying", tc.name)
            return PermissionVerdict(allow=False, reason="permission prompt failed")

    async def confirm_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        risk: str = "medium",
        reason: str = "",
    ) -> bool:
        """工具内部 WARN 级命令的确认通道（bash WARN 使用）——
        无确认通道时 fail-closed 拒绝。

        同一命令指纹（tool_name+command）确认后缓存；已授权（会话级/本次参数）
        直接放行，避免批量授权卡决策后逐条二次确认。
        """
        fingerprint = _tool_command_fingerprint(tool_name, arguments)
        if fingerprint and self._confirm_cache_hit(fingerprint):
            return True
        sid = getattr(self, "_session_id", "")
        if sid:
            from bandi.tools.approval import get_approval_store, params_digest

            if get_approval_store().is_granted(sid, tool_name, params_digest(arguments)):
                if fingerprint:
                    self._confirm_cache_put(fingerprint)
                return True
        if self._user_interaction is None:
            return False
        try:
            granted = await self._user_interaction.ask_permission(
                tool_name=tool_name,
                args=arguments,
                risk=risk,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Permission prompt failed for %s, denying", tool_name)
            granted = False
        if granted:
            if fingerprint:
                self._confirm_cache_put(fingerprint)
        else:
            logger.info("user denied WARN command %s: %s", tool_name, reason)
        return granted

    # 不参与拦截

    async def _check_override_ask(
        self, tc: ToolCall, tool: BaseTool | None, mode: str
    ) -> PermissionVerdict:
        v = self._check_deny_list(tc, tool)
        if not v.allow:
            return v
        v = self._check_mode(tc, tool, mode)
        if not v.allow:
            return v
        if tc.name == "bash":
            v = self._check_shell_safety(tc)
            if not v.allow:
                return v
        v = self._check_sensitive_path(tc, tool)
        if not v.allow:
            return v
        return await self._check_ui(tc, tool)
