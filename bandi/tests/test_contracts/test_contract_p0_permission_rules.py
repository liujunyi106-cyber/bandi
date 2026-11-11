# 功能：P0-3 权限规则契约——规则 CRUD / 审批来源解释 / always-allow 撤销
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.api import settings_ext
from bandi.core.config import BandiConfig
from bandi.core.message import ToolCall
from bandi.core.permission.rules import PermissionRule
from bandi.tools.approval import ApprovalStore
from bandi.tools.permissions_pipeline import PermissionsPipeline


def test_permission_rules_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = settings_ext.apply_permission_rules(
        [
            {"pattern": "bash:echo:*", "action": "ask"},
            {"pattern": "read", "action": "allow"},
            {"pattern": "bash:rm -rf:*", "action": "deny"},
        ]
    )
    assert [rule["action"] for rule in result["rules"]] == ["ask", "allow", "deny"]
    loaded = settings_ext.list_permission_rules(BandiConfig.load())
    assert any(rule["pattern"] == "bash:echo:*" for rule in loaded["rules"])


def test_permission_rules_reject_invalid_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(settings_ext.PreferenceError):
        settings_ext.apply_permission_rules([{"pattern": "bash:echo", "action": "maybe"}])


def test_permission_pipeline_explains_matched_rule() -> None:
    pipeline = PermissionsPipeline(
        rules=[
            PermissionRule(
                pattern="bash:echo:*",
                tool="bash",
                content="echo",
                kind="prefix",
                action="ask",
                source="user",
            )
        ]
    )
    verdict = pipeline.explain_approval(
        ToolCall(id="c1", name="bash", arguments={"command": "echo hi"}), None, "execute"
    )
    assert verdict.ask_user is True
    assert verdict.rule_pattern == "bash:echo:*"
    assert verdict.rule_source == "user"
    assert "permission rule" in verdict.reason


def test_approval_store_lists_and_revokes_grants(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "approvals.json")
    store.grant("sess-1", "bash", scope="session", params_digest="digest-1")
    grants = store.list_grants()
    assert len(grants) == 1
    assert grants[0]["tool_name"] == "bash"
    assert grants[0]["params_digest"] == "digest-1"
    assert grants[0]["scope"] == "session"
    assert grants[0]["expires_at"] > grants[0]["granted_at"]

    assert store.revoke_grant("sess-1", "bash", "digest-1", "session") is True
    assert store.list_grants() == []
    assert store.is_granted("sess-1", "bash", "digest-1") is False
    assert store.revoke_grant("sess-1", "bash", "digest-1", "session") is False
