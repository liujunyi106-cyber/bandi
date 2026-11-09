from __future__ import annotations

from bandi.company.collaboration_policy import (
    CollaborationAction,
    CollaborationRule,
    build_contact_policy,
    collaboration_allowed,
    effective_contact_roles,
    render_collaboration_context,
)
from bandi.company.org_engine import OrgEngine


class TestCollaborationRule:
    def test_allows_action(self):
        rule = CollaborationRule(
            from_role="worker",
            to_role="manager",
            allowed_actions=frozenset({CollaborationAction.SEND_DM}),
        )
        assert rule.allows(CollaborationAction.SEND_DM)
        assert not rule.allows(CollaborationAction.BROADCAST)

    def test_auto_approve_default_false(self):
        rule = CollaborationRule(from_role="a", to_role="b")
        assert not rule.auto_approve


class TestBuildContactPolicy:
    def test_hierarchical_rules(self):
        org = OrgEngine()
        org.register_role("manager", "Manager")
        org.register_role("worker", "Worker", reports_to="manager")
        org.register_role("reviewer", "Reviewer", reports_to="manager")
        rules = build_contact_policy(org)
        assert len(rules) >= 4
        manager_to_worker = [r for r in rules if r.from_role == "manager" and r.to_role == "worker"]
        assert len(manager_to_worker) == 1
        assert manager_to_worker[0].auto_approve

    def test_peer_rules_not_auto_approve(self):
        org = OrgEngine()
        org.register_role("manager", "Manager")
        org.register_role("worker_a", "Worker A", reports_to="manager")
        org.register_role("worker_b", "Worker B", reports_to="manager")
        rules = build_contact_policy(org)
        peer_rules = [r for r in rules if r.from_role == "worker_a" and r.to_role == "worker_b"]
        assert len(peer_rules) == 1
        assert not peer_rules[0].auto_approve

    def test_list_agents_called_once(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        agents = [
            SimpleNamespace(role_id="manager", reports_to=""),
            SimpleNamespace(role_id="worker_a", reports_to="manager"),
            SimpleNamespace(role_id="worker_b", reports_to="manager"),
        ]
        org = MagicMock()
        org.list_agents.return_value = agents
        rules = build_contact_policy(org)
        assert org.list_agents.call_count == 1
        assert any(r.from_role == "manager" and r.to_role == "worker_a" for r in rules)
        assert any(
            r.from_role == "worker_a" and r.to_role == "worker_b" and not r.auto_approve
            for r in rules
        )


class TestEffectiveContactRoles:
    def test_from_org_engine(self):
        org = OrgEngine()
        org.register_role("manager", "Manager")
        org.register_role("worker", "Worker", reports_to="manager")
        contacts = effective_contact_roles("worker", org=org)
        assert "manager" in contacts

    def test_from_rules(self):
        rules = [
            CollaborationRule(from_role="alice", to_role="bob"),
            CollaborationRule(from_role="bob", to_role="alice"),
        ]
        contacts = effective_contact_roles("alice", rules=rules)
        assert "bob" in contacts
        assert "charlie" not in contacts

    def test_empty_when_no_org_and_no_rules(self):
        contacts = effective_contact_roles("anyone")
        assert contacts == []


class TestCollaborationAllowed:
    def test_allowed_by_rule(self):
        rules = [
            CollaborationRule(
                from_role="worker",
                to_role="manager",
                allowed_actions=frozenset(CollaborationAction),
            )
        ]
        assert collaboration_allowed("worker", "manager", rules=rules)

    def test_blocked_by_rule(self):
        rules = [CollaborationRule(from_role="worker", to_role="peer")]
        assert not collaboration_allowed("worker", "stranger", rules=rules)

    def test_default_blocked_without_org_or_rules(self):
        assert not collaboration_allowed("a", "b")

    def test_empty_rules_short_circuits_org_fallback(self):
        org = OrgEngine()
        org.register_role("manager", "Manager")
        org.register_role("worker", "Worker", reports_to="manager")
        assert not collaboration_allowed("worker", "manager", org=org, rules=[])


class TestRenderCollaborationContext:
    def test_renders_contacts(self):
        org = OrgEngine()
        org.register_role("manager", "Manager")
        org.register_role("worker", "Worker", reports_to="manager")
        result = render_collaboration_context("worker", org=org)
        assert "worker" in result
        assert "manager" in result

    def test_empty_when_no_contacts(self):
        org = OrgEngine()
        result = render_collaboration_context("alone", org=org)
        assert result == ""


def _work_item(**kwargs):
    from types import SimpleNamespace

    metadata = kwargs.pop("metadata", {})
    return SimpleNamespace(
        id=kwargs.pop("id", "wi-1"),
        title=kwargs.pop("title", "Task title"),
        description=kwargs.pop("description", "Task description"),
        assignee_role=kwargs.pop("assignee_role", "worker"),
        metadata=metadata,
    )


def _contract(**kwargs):
    contract = {
        "write_scope": kwargs.pop("write_scope", "assigned_workspace"),
        "allowed_collaboration_targets": kwargs.pop("allowed_collaboration_targets", ["manager"]),
        "downstream_consumer": kwargs.pop("downstream_consumer", ["reviewer"]),
    }
    return {k: v for k, v in contract.items() if v is not None}


class TestCollectDynamicContactRoles:
    def test_none_returns_empty(self):
        from bandi.company.collaboration_policy import collect_dynamic_contact_roles

        assert collect_dynamic_contact_roles(None) == []

    def test_empty_metadata(self):
        from bandi.company.collaboration_policy import collect_dynamic_contact_roles

        assert collect_dynamic_contact_roles(_work_item()) == []

    def test_allowed_collaboration_targets(self):
        from bandi.company.collaboration_policy import collect_dynamic_contact_roles

        wi = _work_item(metadata={"ownership_contract": _contract()})
        roles = collect_dynamic_contact_roles(wi)
        assert "manager" in roles

    def test_downstream_consumer(self):
        from bandi.company.collaboration_policy import collect_dynamic_contact_roles

        wi = _work_item(
            metadata={"ownership_contract": _contract(downstream_consumer=["reviewer", "auditor"])}
        )
        roles = collect_dynamic_contact_roles(wi)
        assert "reviewer" in roles
        assert "auditor" in roles

    def test_dynamic_allowed_contact_roles(self):
        from bandi.company.collaboration_policy import collect_dynamic_contact_roles

        wi = _work_item(metadata={"dynamic_allowed_contact_roles": ["temp_role"]})
        roles = collect_dynamic_contact_roles(wi)
        assert "temp_role" in roles

    def test_work_item_gate_reviewer_role(self):
        from bandi.company.collaboration_policy import collect_dynamic_contact_roles

        wi = _work_item(metadata={"work_item_gate": {"reviewer_role": "reviewer"}})
        roles = collect_dynamic_contact_roles(wi)
        assert "reviewer" in roles

    def test_manager_role_id_fallback(self):
        from bandi.company.collaboration_policy import collect_dynamic_contact_roles

        wi = _work_item(metadata={"manager_role_id": "manager"})
        roles = collect_dynamic_contact_roles(wi)
        assert "manager" in roles

    def test_handoff_log_roles(self):
        from bandi.company.collaboration_policy import collect_dynamic_contact_roles

        wi = _work_item(
            metadata={
                "handoff_log": [{"from": "alice", "to": "bob"}, {"from": "bob", "to": "carol"}]
            }
        )
        roles = collect_dynamic_contact_roles(wi)
        assert "alice" in roles
        assert "bob" in roles
        assert "carol" in roles

    def test_deduplicates_and_strips_blanks(self):
        from bandi.company.collaboration_policy import collect_dynamic_contact_roles

        wi = _work_item(
            metadata={
                "ownership_contract": _contract(
                    allowed_collaboration_targets=["manager", "  ", "manager"]
                ),
                "handoff_log": [{"from": "", "to": "manager"}],
            }
        )
        roles = collect_dynamic_contact_roles(wi)
        assert roles.count("manager") == 1


class TestEffectiveContactRolesDynamic:
    def test_static_and_dynamic_merged(self):
        from bandi.company.collaboration_policy import effective_contact_roles

        org = OrgEngine()
        org.register_role("manager", "Manager")
        org.register_role("worker", "Worker", reports_to="manager")
        wi = _work_item(
            metadata={"ownership_contract": _contract(downstream_consumer=["reviewer"])}
        )
        roles = effective_contact_roles("worker", work_item=wi, org_engine=org)
        assert "manager" in roles
        assert "reviewer" in roles

    def test_dynamic_only_without_org(self):
        from bandi.company.collaboration_policy import effective_contact_roles

        wi = _work_item(metadata={"dynamic_allowed_contact_roles": ["peer"]})
        roles = effective_contact_roles("worker", work_item=wi)
        assert roles == ["peer"]

    def test_org_engine_without_task_support(self):
        from bandi.company.collaboration_policy import effective_contact_roles

        org = OrgEngine()
        org.register_role("manager", "Manager")
        org.register_role("worker", "Worker", reports_to="manager")
        roles = effective_contact_roles("worker", org_engine=org)
        assert "manager" in roles

    def test_empty_without_org_and_work_item(self):
        from bandi.company.collaboration_policy import effective_contact_roles

        assert effective_contact_roles("nobody") == []


class TestOwnershipGuardViolation:
    def test_none_work_item_allowed(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        assert (
            ownership_guard_violation(work_item=None, tool_name="file_edit", arguments={}) is None
        )

    def test_non_company_mode_allowed(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(metadata={"ownership_contract": _contract(write_scope="read_only")})
        assert (
            ownership_guard_violation(
                work_item=wi, tool_name="file_edit", arguments={"path": "/tmp/x.txt"}
            )
            is None
        )

    def test_no_contract_allowed(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(metadata={"execution_mode": "company_mode"})
        assert ownership_guard_violation(work_item=wi, tool_name="file_edit", arguments={}) is None

    def test_read_only_blocks_write_tool(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="read_only"),
            }
        )
        result = ownership_guard_violation(
            work_item=wi, tool_name="file_edit", arguments={"path": "/tmp/x.txt"}
        )
        assert result is not None
        assert "read_only" in result

    def test_out_of_scope_write_blocked(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="assigned_workspace"),
                "target_output_dir": "/workspace/wi-1",
            }
        )
        result = ownership_guard_violation(
            work_item=wi, tool_name="file_edit", arguments={"path": "/outside/x.txt"}
        )
        assert result is not None
        assert "outside the assigned workspace" in result

    def test_in_scope_write_allowed(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="assigned_workspace"),
                "target_output_dir": "/workspace/wi-1",
            }
        )
        assert (
            ownership_guard_violation(
                work_item=wi, tool_name="file_edit", arguments={"path": "/workspace/wi-1/a.txt"}
            )
            is None
        )

    def test_collaboration_blocked_for_unknown_recipient(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(allowed_collaboration_targets=["manager"]),
            }
        )
        result = ownership_guard_violation(
            work_item=wi, tool_name="send_dm", arguments={"to_agent": "stranger"}
        )
        assert result is not None
        assert "not allowed" in result

    def test_collaboration_allowed_for_known_recipient(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(allowed_collaboration_targets=["manager"]),
            }
        )
        assert (
            ownership_guard_violation(
                work_item=wi, tool_name="send_dm", arguments={"to_agent": "manager"}
            )
            is None
        )

    def test_non_write_non_collab_tool_allowed(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="read_only"),
            }
        )
        assert ownership_guard_violation(work_item=wi, tool_name="web_search", arguments={}) is None

    def test_bash_cwd_outside_scope_blocked(self):
        """
        working_directory 恒空导致漏检）。"""
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="assigned_workspace"),
                "target_output_dir": "/workspace/wi-1",
            }
        )
        result = ownership_guard_violation(
            work_item=wi,
            tool_name="bash",
            arguments={"command": "ls -la", "cwd": "/outside"},
        )
        assert result is not None
        assert "outside the assigned workspace" in result

    def test_bash_cwd_in_scope_allowed(self):
        """
        缺失而误拦）。"""
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="assigned_workspace"),
                "target_output_dir": "/workspace/wi-1",
            }
        )
        result = ownership_guard_violation(
            work_item=wi,
            tool_name="bash",
            arguments={"command": "ls -la", "cwd": "/workspace/wi-1"},
        )
        assert result is None

    def test_bash_relative_redirect_resolved_against_cwd(self):
        """
        恒空，相对目标回退为裸路径导致越界判定失效）。"""
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="assigned_workspace"),
                "target_output_dir": "/workspace/wi-1",
            }
        )
        result = ownership_guard_violation(
            work_item=wi,
            tool_name="bash",
            arguments={"command": "cat a.txt > ../escape.txt", "cwd": "/workspace/wi-1"},
        )
        assert result is not None, "相对路径 .. 越界必须拦截"


class TestRenderOwnershipContract:
    def test_none_returns_empty(self):
        from bandi.company.collaboration_policy import render_ownership_contract

        assert render_ownership_contract(None) == ""

    def test_no_contract_returns_empty(self):
        from bandi.company.collaboration_policy import render_ownership_contract

        assert render_ownership_contract(_work_item()) == ""

    def test_renders_write_scope(self):
        from bandi.company.collaboration_policy import render_ownership_contract

        wi = _work_item(metadata={"ownership_contract": _contract()})
        result = render_ownership_contract(wi)
        assert "## Ownership Contract" in result
        assert "Write scope: assigned_workspace" in result

    def test_renders_targets_and_downstream(self):
        from bandi.company.collaboration_policy import render_ownership_contract

        wi = _work_item(
            metadata={
                "ownership_contract": _contract(
                    allowed_collaboration_targets=["manager", "reviewer"],
                    downstream_consumer=["consumer_a", "consumer_b"],
                )
            }
        )
        result = render_ownership_contract(wi)
        assert "- manager" in result
        assert "- reviewer" in result
        assert "- consumer_a" in result

    def test_empty_contract_returns_empty(self):
        from bandi.company.collaboration_policy import render_ownership_contract

        wi = _work_item(metadata={"ownership_contract": {}})
        assert render_ownership_contract(wi) == ""


class TestOwnershipGuardP1ProductionTools:
    def test_write_tool_production_name_out_of_scope(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="assigned_workspace"),
                "target_output_dir": "/workspace/wi-1",
            }
        )
        result = ownership_guard_violation(
            work_item=wi, tool_name="write", arguments={"path": "/outside/x.txt"}
        )
        assert result is not None
        assert "outside the assigned workspace" in result

    def test_edit_tool_production_name_out_of_scope(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="assigned_workspace"),
                "target_output_dir": "/workspace/wi-1",
            }
        )
        result = ownership_guard_violation(
            work_item=wi, tool_name="edit", arguments={"path": "/outside/x.txt"}
        )
        assert result is not None
        assert "outside the assigned workspace" in result

    def test_bash_redirect_outside_scope_blocked(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="assigned_workspace"),
                "target_output_dir": "/workspace/wi-1",
            }
        )
        result = ownership_guard_violation(
            work_item=wi,
            tool_name="bash",
            arguments={
                "command": "echo x > /etc/evil.txt",
                "working_directory": "/workspace/wi-1",
            },
        )
        assert result is not None
        assert "outside the assigned workspace" in result

    def test_bash_in_scope_redirect_allowed(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="assigned_workspace"),
                "target_output_dir": "/workspace/wi-1",
            }
        )
        assert (
            ownership_guard_violation(
                work_item=wi,
                tool_name="bash",
                arguments={
                    "command": "echo hi > note.txt",
                    "working_directory": "/workspace/wi-1",
                },
            )
            is None
        )


class TestOwnershipGuardCP11:
    def test_assigned_workspace_without_root_blocked_fail_closed(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="assigned_workspace"),
            }
        )
        result = ownership_guard_violation(
            work_item=wi, tool_name="write", arguments={"path": "/etc/evil.sh"}
        )
        assert result is not None
        assert "workspace" in result

    def test_assigned_workspace_with_manifest_root_in_scope_allowed(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="assigned_workspace"),
                "workspace_manifest": {"root_path": "/workspace/wi-1"},
            }
        )
        assert (
            ownership_guard_violation(
                work_item=wi,
                tool_name="write",
                arguments={"path": "/workspace/wi-1/out.txt"},
            )
            is None
        )

    def test_assigned_workspace_with_manifest_root_out_of_scope_blocked(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="assigned_workspace"),
                "workspace_manifest": {"root_path": "/workspace/wi-1"},
            }
        )
        result = ownership_guard_violation(
            work_item=wi, tool_name="write", arguments={"path": "/etc/evil.sh"}
        )
        assert result is not None
        assert "outside the assigned workspace" in result

    def test_contract_without_write_scope_still_guarded_for_candidate_paths(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        wi = _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": {"allowed_collaboration_targets": ["manager"]},
            }
        )
        result = ownership_guard_violation(
            work_item=wi, tool_name="write", arguments={"path": "/etc/evil.sh"}
        )
        assert result is not None


class TestP2Batch1CP:
    def _bash_wi(self):
        return _work_item(
            metadata={
                "execution_mode": "company_mode",
                "ownership_contract": _contract(write_scope="assigned_workspace"),
                "target_output_dir": "/workspace/wi-1",
            }
        )

    def test_bash_fd_redirect_outside_scope_blocked(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        result = ownership_guard_violation(
            work_item=self._bash_wi(),
            tool_name="bash",
            arguments={
                "command": "echo x 2> /etc/evil.log",
                "working_directory": "/workspace/wi-1",
            },
        )
        assert result is not None
        assert "outside the assigned workspace" in result

    def test_bash_amp_redirect_outside_scope_blocked(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        result = ownership_guard_violation(
            work_item=self._bash_wi(),
            tool_name="bash",
            arguments={
                "command": "echo x &> /etc/evil.log",
                "working_directory": "/workspace/wi-1",
            },
        )
        assert result is not None

    def test_bash_fd_copy_to_file_outside_scope_blocked(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        result = ownership_guard_violation(
            work_item=self._bash_wi(),
            tool_name="bash",
            arguments={
                "command": "echo x >& /etc/evil.log",
                "working_directory": "/workspace/wi-1",
            },
        )
        assert result is not None

    def test_bash_fd_duplication_not_blocked(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        assert (
            ownership_guard_violation(
                work_item=self._bash_wi(),
                tool_name="bash",
                arguments={
                    "command": "echo x 2>&1",
                    "working_directory": "/workspace/wi-1",
                },
            )
            is None
        )

    def test_bash_fd_copy_to_fd_not_blocked(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        assert (
            ownership_guard_violation(
                work_item=self._bash_wi(),
                tool_name="bash",
                arguments={
                    "command": "echo x >&1",
                    "working_directory": "/workspace/wi-1",
                },
            )
            is None
        )

    def test_bash_fd_redirect_in_scope_allowed(self):
        from bandi.company.collaboration_policy import ownership_guard_violation

        assert (
            ownership_guard_violation(
                work_item=self._bash_wi(),
                tool_name="bash",
                arguments={
                    "command": "echo x 2> note.log",
                    "working_directory": "/workspace/wi-1",
                },
            )
            is None
        )
