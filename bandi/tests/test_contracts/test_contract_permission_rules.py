# 功能：契约——权限规则匹配。
# 钉行为：规则解析（tool / tool:content 三类判别 + 字符串简写/dict 双形态）/
# exact·prefix·wildcard 匹配（词边界、compound 防护、env 前缀剥离）/
# deny>ask>allow 裁决 / 管道集成（allow 不绕过 deny 名单与 shell 安全；
from __future__ import annotations

import asyncio

from bandi.core.message import ToolCall
from bandi.core.permission.rules import parse_permission_rule, resolve_rules, rule_matches
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.permissions_pipeline import PermissionsPipeline

# ──────────────────────────────────────────────────────────────────────
# A. 规则解析族
# ──────────────────────────────────────────────────────────────────────


class TestParseRule:
    def test_tool_only_rule(self) -> None:
        r = parse_permission_rule("bash")
        assert r is not None
        assert r.tool == "bash" and r.kind == "tool"
        assert r.action == "allow", "字符串简写默认放行"

    def test_exact_content(self) -> None:
        r = parse_permission_rule({"pattern": "bash:git status", "action": "deny"})
        assert r is not None
        assert r.kind == "exact" and r.content == "git status" and r.action == "deny"

    def test_prefix_content(self) -> None:
        r = parse_permission_rule("bash:git:*")
        assert r is not None
        assert r.kind == "prefix" and r.content == "git"

    def test_wildcard_content(self) -> None:
        r = parse_permission_rule("bash:npm run *")
        assert r is not None and r.kind == "wildcard"

    def test_source_label_preserved(self) -> None:
        r = parse_permission_rule({"pattern": "read", "source": "project"})
        assert r is not None and r.source == "project"

    def test_invalid_entries_skipped(self) -> None:
        assert parse_permission_rule("") is None
        assert parse_permission_rule({"action": "deny"}) is None, "缺 pattern 拒绝"
        assert parse_permission_rule(123) is None
        assert parse_permission_rule({"pattern": "bash", "action": "nonsense"}) is None
        assert parse_permission_rule({"pattern": ":git"}) is None, "空工具名拒绝"


# ──────────────────────────────────────────────────────────────────────
# B. 规则匹配族
# ──────────────────────────────────────────────────────────────────────


class TestMatchRule:
    def test_tool_rule_matches_any_content(self) -> None:
        r = parse_permission_rule("bash")
        assert r is not None
        assert rule_matches(r, "bash", {"command": "anything && x"})
        assert not rule_matches(r, "read", {})
        assert rule_matches(r, "bash", None), "无参数工具级规则也命中"

    def test_exact_content(self) -> None:
        r = parse_permission_rule({"pattern": "bash:git status", "action": "allow"})
        assert r is not None
        assert rule_matches(r, "bash", {"command": "git status"})
        assert not rule_matches(r, "bash", {"command": "git status --short"})

    def test_prefix_word_boundary(self) -> None:
        r = parse_permission_rule("bash:git:*")
        assert r is not None
        assert rule_matches(r, "bash", {"command": "git status"})
        assert rule_matches(r, "bash", {"command": "git"})
        assert not rule_matches(r, "bash", {"command": "gitk --all"}), "前缀需词边界"

    def test_wildcard_full_anchor(self) -> None:
        r = parse_permission_rule("bash:git *")
        assert r is not None
        assert rule_matches(r, "bash", {"command": "git push origin main"})
        assert rule_matches(r, "bash", {"command": "git"}), "尾 ' *' 可省略匹配裸命令"
        assert not rule_matches(r, "bash", {"command": "gitk"})

    def test_compound_blocks_allow_but_not_deny(self) -> None:
        allow = parse_permission_rule("bash:git:*")
        deny = parse_permission_rule({"pattern": "bash:git:*", "action": "deny"})
        assert allow is not None and deny is not None
        cmd = "git status && rm -rf /"
        assert not rule_matches(allow, "bash", {"command": cmd}), "allow 规则不得命中复合命令"
        assert rule_matches(deny, "bash", {"command": cmd}), "deny 规则不受复合限制"

    def test_env_prefix_stripped(self) -> None:
        deny = parse_permission_rule({"pattern": "bash:git:*", "action": "deny"})
        assert deny is not None
        assert rule_matches(deny, "bash", {"command": "FOO=bar git push"}), (
            "env 前缀不得用于绕过规则"
        )

    def test_non_bash_content_rule_no_match(self) -> None:
        r = parse_permission_rule("read:somefile")
        assert r is not None
        assert not rule_matches(r, "read", {"path": "somefile"}), "content 规则仅对 bash 生效"


class TestResolveRules:
    def _rules(self) -> list:
        return [
            parse_permission_rule("bash:git:*"),
            parse_permission_rule({"pattern": "bash:git *", "action": "ask"}),
            parse_permission_rule({"pattern": "bash:git:*", "action": "deny"}),
        ]

    def test_deny_beats_ask_and_allow(self) -> None:
        assert resolve_rules(self._rules(), "bash", {"command": "git status"}) == "deny"

    def test_ask_beats_allow(self) -> None:
        rules = [
            parse_permission_rule("bash:git:*"),
            parse_permission_rule({"pattern": "bash:git push*", "action": "ask"}),
        ]
        assert resolve_rules(rules, "bash", {"command": "git push"}) == "ask"

    def test_no_match_returns_none(self) -> None:
        assert resolve_rules(self._rules(), "read", {}) is None


# ──────────────────────────────────────────────────────────────────────
# C. 管道集成族
# ──────────────────────────────────────────────────────────────────────


class _Tool(BaseTool):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.description = ""
        self.category = ToolCategory.UTIL

    async def execute(self, params, context=None):
        return "ok"


def _bash_call(command: str) -> ToolCall:
    return ToolCall(id="c1", name="bash", arguments={"command": command})


def _rules(*entries: object) -> list:
    out = []
    for e in entries:
        r = parse_permission_rule(e)
        assert r is not None
        out.append(r)
    return out


class TestPipelineIntegration:
    def test_allow_rule_skips_ask_list(self) -> None:
        pp = PermissionsPipeline(rules=_rules("bash:git:*"), ask_tools={"bash"})
        verdict = asyncio.run(pp.check(_bash_call("git status"), _Tool("bash"), "execute"))
        assert verdict.allow and not verdict.ask_user
        assert pp.needs_approval(_bash_call("git status"), _Tool("bash"), "execute") is False

    def test_allow_rule_does_not_bypass_deny_tools(self) -> None:
        pp = PermissionsPipeline(
            rules=_rules({"pattern": "bash", "action": "allow"}), deny_tools={"bash"}
        )
        verdict = asyncio.run(pp.check(_bash_call("git status"), _Tool("bash"), "execute"))
        assert not verdict.allow, "allow 规则不得绕过 deny 名单"

    def test_allow_rule_does_not_bypass_shell_safety(self) -> None:
        pp = PermissionsPipeline(rules=_rules("bash:*"), ask_tools={"bash"})
        verdict = asyncio.run(pp.check(_bash_call("rm -rf /"), _Tool("bash"), "execute"))
        assert not verdict.allow, "allow 规则不得绕过 shell 安全检查"

    def test_ask_rule_forces_prompt_even_for_bypass_tool(self) -> None:
        pp = PermissionsPipeline(
            rules=_rules({"pattern": "read", "action": "ask"}), bypass_tools={"read"}
        )
        assert pp.needs_approval(
            ToolCall(id="c", name="read", arguments={}), _Tool("read"), "execute"
        )

    def test_deny_rule_verdict(self) -> None:
        pp = PermissionsPipeline(rules=_rules({"pattern": "bash:rm *", "action": "deny"}))
        verdict = asyncio.run(pp.check(_bash_call("rm -rf build"), _Tool("bash"), "execute"))
        assert not verdict.allow and "rule" in verdict.reason

    def test_compound_command_falls_back_to_ask(self) -> None:
        pp = PermissionsPipeline(rules=_rules("bash:git:*"), ask_tools={"bash"})
        assert pp.needs_approval(_bash_call("git status && echo done"), _Tool("bash"), "execute"), (
            "allow 规则不命中复合命令时必须回落到询问（不得静默放行）"
        )

    def test_plan_mode_whitelist_still_enforced(self) -> None:
        pp = PermissionsPipeline(rules=_rules({"pattern": "bash", "action": "allow"}))
        verdict = asyncio.run(pp.check(_bash_call("git status"), _Tool("bash"), "plan"))
        assert not verdict.allow, "plan 模式白名单不因 allow 规则松动"
