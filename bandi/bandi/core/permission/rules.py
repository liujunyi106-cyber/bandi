# 功能：权限规则匹配——支持 exact / prefix / wildcard 三类规则。
# 语法：`tool`（工具名 exact）/ `tool:content`（content 尾 `:*`→prefix；含 `*`→
# wildcard 全串锚定；否则 exact）；action ∈ allow/ask/deny，裁决 deny>ask>allow。
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

_RULE_ACTIONS: tuple[str, ...] = ("allow", "ask", "deny")
_TOOL_ONLY = "tool"
_EXACT = "exact"
_PREFIX = "prefix"
_WILDCARD = "wildcard"
# compound 命令防护（allow 规则不得命中复合命令——防 `git status && evil` 借道）
_COMPOUND_RE = re.compile(r"&&|;|\|")
# `MSG="a b" git push` 必须整体消费）
_ENV_ASSIGN_RE = re.compile(r"""^\s*[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|\S*)\s+""")


@dataclass(frozen=True)
class PermissionRule:
    pattern: str
    tool: str
    content: str
    kind: str
    action: str
    source: str = ""


def parse_permission_rule(entry: Any, *, source: str = "") -> PermissionRule | None:
    # 功能：解析规则条目（字符串简写默认 allow / dict 显式 action）——非法条目返回 None
    if isinstance(entry, str):
        pattern = entry.strip()
        action = "allow"
    elif isinstance(entry, dict):
        pattern = str(entry.get("pattern") or "").strip()
        action = str(entry.get("action") or "allow").strip().lower()
        source = str(entry.get("source") or source)
    else:
        return None
    if not pattern or action not in _RULE_ACTIONS:
        return None
    tool, sep, content = pattern.partition(":")
    tool = tool.strip()
    content = content.strip()
    if not tool:
        return None
    if not sep or not content:
        return PermissionRule(pattern, tool, "", _TOOL_ONLY, action, source)
    if content.endswith(":*"):
        base = content[:-2].strip()
        if not base:
            return None
        return PermissionRule(pattern, tool, base, _PREFIX, action, source)
    if "*" in content:
        return PermissionRule(pattern, tool, content, _WILDCARD, action, source)
    return PermissionRule(pattern, tool, content, _EXACT, action, source)


def _strip_env_prefix(command: str) -> str:
    # 功能：剥离前导 env 赋值（`FOO=bar cmd` / `MSG="a b" cmd`）——双向防绕过
    text = command.lstrip()
    while True:
        match = _ENV_ASSIGN_RE.match(text)
        if match is None:
            return text
        text = text[match.end() :]


def _wildcard_regex(pattern: str) -> re.Pattern[str]:
    # 功能：`*`→`.*` 全串锚定；尾 ` *` 可省略（`git *` 匹配裸 `git`，但不吞 `gitk`）
    if pattern.endswith(" *"):
        base = pattern[:-2].strip()
        return re.compile("^" + re.escape(base) + r"(?:\s.*)?$")
    body = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.compile("^" + body + "$")


def _match_single(rule: PermissionRule, command: str) -> bool:
    # 功能：单段匹配（exact/prefix/wildcard）——env 剥离后判定；词边界含制表符
    text = _strip_env_prefix(command.strip())
    if not text:
        return False
    if rule.kind == _EXACT:
        return text == rule.content
    if rule.kind == _PREFIX:
        return (
            text == rule.content
            or re.match(re.escape(rule.content) + r"(?=\s|$)", text) is not None
        )
    if rule.kind == _WILDCARD:
        return _wildcard_regex(rule.content).match(text) is not None
    return False


def rule_matches(rule: PermissionRule, tool_name: str, arguments: dict[str, Any] | None) -> bool:
    # 功能：规则命中判定——tool-only 任意参数命中；content 规则仅对 bash 命令生效；
    if rule.tool != tool_name:
        return False
    if rule.kind == _TOOL_ONLY:
        return True
    if tool_name != "bash":
        return False
    command = str((arguments or {}).get("command") or "").strip()
    if not command:
        return False
    if rule.action == "allow" and _COMPOUND_RE.search(command):
        return False
    if rule.action in ("deny", "ask") and _COMPOUND_RE.search(command):
        from bandi.company.shell_safety import split_shell_segments

        try:
            segments = [" ".join(seg) for seg in split_shell_segments(command)]
        except Exception:  # noqa: BLE001 — 拆段失败回退整串匹配
            segments = []
        if segments and any(_match_single(rule, seg) for seg in segments):
            return True
    return _match_single(rule, command)


def resolve_rules(
    rules: Sequence[PermissionRule], tool_name: str, arguments: dict[str, Any] | None
) -> str | None:
    # 功能：裁决——多命中取最高优先级（deny > ask > allow）；无命中返回 None
    matched = {rule.action for rule in rules if rule_matches(rule, tool_name, arguments)}
    for action in ("deny", "ask", "allow"):
        if action in matched:
            return action
    return None
