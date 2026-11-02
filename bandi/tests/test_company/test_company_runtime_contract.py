from __future__ import annotations

from bandi.company.company_runtime_contract import (
    build_company_work_item_contract,
    build_external_company_work_item_contract,
)


def _make_work_item(
    turn_type: str = "execute",
    title: str = "Test work item",
    metadata: dict | None = None,
) -> dict:
    return {
        "id": "wi-test-001",
        "title": title,
        "turn_type": turn_type,
        "metadata": metadata or {},
        "runtime_metadata": {},
    }


class TestBuildCompanyRuntimeContract:
    def test_execute_turn_type(self) -> None:
        work_item = _make_work_item("execute")
        result = build_company_work_item_contract(work_item)
        assert "## Company Work Item Contract" in result
        assert "Current work item:" in result
        assert "Execute" in result

    def test_review_turn_type(self) -> None:
        work_item = _make_work_item("review")
        result = build_company_work_item_contract(work_item)
        assert "Review" in result
        assert "review_verdict" in result

    def test_plan_turn_type(self) -> None:
        work_item = _make_work_item("plan")
        result = build_company_work_item_contract(work_item)
        assert "Plan / Discussion" in result

    def test_intake_turn_type_without_dispatch_surface(self) -> None:
        """无派发面的 intake（单角色接单）走讨论契约——不硬套分工格式。"""
        work_item = _make_work_item("intake")
        result = build_company_work_item_contract(work_item)
        assert "Plan / Discussion" in result

    def test_intake_turn_type_with_dispatch_surface(self) -> None:
        work_item = _make_work_item("intake", metadata={"allowed_delegate_role_ids": ["visual"]})
        result = build_company_work_item_contract(work_item)
        assert "Turn: Dispatch" in result

    def test_dispatch_turn_type(self) -> None:
        work_item = _make_work_item("dispatch")
        result = build_company_work_item_contract(work_item)
        assert "Turn: Dispatch" in result

    def test_aggregate_turn_type(self) -> None:
        work_item = _make_work_item("aggregate")
        result = build_company_work_item_contract(work_item)
        assert "Aggregate / Deliver" in result

    def test_deliver_turn_type(self) -> None:
        work_item = _make_work_item("deliver")
        result = build_company_work_item_contract(work_item)
        assert "Aggregate / Deliver" in result

    def test_default_turn_type_is_execute(self) -> None:
        work_item = _make_work_item("")
        result = build_company_work_item_contract(work_item)
        assert "Execute" in result

    def test_multi_team_org_injected(self) -> None:
        work_item = _make_work_item(
            "execute",
            metadata={"runtime_model": "multi_team_org"},
        )
        result = build_company_work_item_contract(work_item)
        assert "Organization Runtime Contract" in result

    def test_manager_contract_injected(self) -> None:
        work_item = _make_work_item(
            "execute",
            metadata={"runtime_model": "multi_team_org", "direct_report_seat_ids": ["seat_1"]},
        )
        result = build_company_work_item_contract(work_item)
        assert "Manager Runtime Contract" in result

    def test_title_in_output(self) -> None:
        work_item = _make_work_item(title="My task")
        result = build_company_work_item_contract(work_item)
        assert "My task" in result

    def test_review_block_with_target_work_item(self) -> None:
        work_item = _make_work_item(
            "review",
            metadata={
                "runtime_model": "multi_team_org",
                "review_execution_work_item": True,
                "review_target_work_item_id": "wi-child-001",
                "review_target_title": "Child task",
                "review_completion_report": "Done",
            },
        )
        result = build_company_work_item_contract(work_item)
        assert "Review" in result
        assert "wi-child-001" in result
        assert "Child task" in result

    def test_report_block_with_evidence(self) -> None:
        work_item = _make_work_item(
            "execute",
            metadata={
                "runtime_model": "multi_team_org",
                "report_execution_work_item": True,
                "report_source_summary": "Summary text",
            },
        )
        result = build_company_work_item_contract(work_item)
        assert "Report" in result or "Summary" in result or "summary" in result.lower()

    def test_dispatch_block_with_followup(self) -> None:
        work_item = _make_work_item(
            "dispatch",
            metadata={
                "runtime_model": "multi_team_org",
                "followup_routed_to_final_decider": True,
                "user_supplied_input": "Continue working",
                "direct_report_seat_ids": ["seat_1"],
            },
        )
        result = build_company_work_item_contract(work_item)
        assert "Follow-up" in result or "follow-up" in result.lower()

    def test_audience_native_no_replacement(self) -> None:
        work_item = _make_work_item("review")
        result_native = build_company_work_item_contract(work_item, audience="native")
        result_external = build_company_work_item_contract(work_item, audience="external")
        # native should contain original tool-specific wording
        assert "file_read, bash, git_*" in result_native
        # external should have replaced it with agent-agnostic wording
        assert "external agent's available inspection" in result_external
        assert result_native != result_external

    def test_audience_external_applies_replacements(self) -> None:
        work_item = _make_work_item("review")
        result_external = build_company_work_item_contract(work_item, audience="external")
        # Should not contain original tool-specific wording
        assert "file_read, bash, git_*" not in result_external
        # Should contain replacement text
        assert "external agent's available inspection" in result_external


class TestBuildExternalCompanyWorkItemContract:
    def test_delegates_to_native_with_external_audience(self) -> None:
        work_item = _make_work_item("review")
        result = build_external_company_work_item_contract(work_item)
        assert result != ""
        # external version should have replacement text
        assert "external agent's available inspection" in result
        # should not have original tool-specific wording
        assert "file_read, bash, git_*" not in result


class TestTurnModeGuards:
    """turn_mode 守卫测试：确保 review_execute / report_execute block
    仅在 infer_turn_mode 匹配对应模式时注入，避免非对应轮次被污染。"""

    def test_review_block_blocked_when_phase_is_running(self) -> None:
        """phase=RUNNING 且无 review 标志 → infer 为 → 守卫拦截。"""
        from bandi.company.company_runtime_contract import (
            _review_execute_block,
            _WorkItemLike,
        )
        from bandi.company.phase import Phase

        class FakeWorkItem:
            metadata: dict = {"runtime_model": "multi_team_org"}
            turn_type: str = "execute"
            phase: str | None = Phase.RUNNING.value

        assert _review_execute_block(_WorkItemLike(FakeWorkItem.__dict__)) == ""

    def test_report_block_blocked_when_phase_is_running(self) -> None:
        """phase=RUNNING 且无 report 标志 → infer 为 → 守卫拦截。"""
        from bandi.company.company_runtime_contract import (
            _report_execute_block,
        )
        from bandi.company.phase import Phase

        class FakeWorkItem:
            metadata: dict = {"runtime_model": "multi_team_org"}
            turn_type: str = "execute"
            phase: str | None = Phase.RUNNING.value

        assert _report_execute_block(FakeWorkItem.__dict__) == ""

    def test_review_block_active_with_review_phase(self) -> None:
        """phase=AWAITING_MANAGER_REVIEW + review 标志 → infer=→ 守卫放行。"""
        from bandi.company.company_runtime_contract import (
            _review_execute_block,
        )
        from bandi.company.phase import Phase

        class FakeWorkItem:
            metadata: dict = {
                "runtime_model": "multi_team_org",
                "review_execution_work_item": True,
            }
            turn_type: str = "execute"
            phase: str | None = Phase.AWAITING_MANAGER_REVIEW.value

        result = _review_execute_block(FakeWorkItem.__dict__)
        assert result != ""
        assert "Review" in result or "review" in result.lower()

    def test_report_block_active_with_report_metadata(self) -> None:
        """metadata 含 report_execution_work_item → infer=REPORT → 守卫放行。"""
        from bandi.company.company_runtime_contract import (
            _report_execute_block,
        )

        work_item = _make_work_item(
            "execute",
            metadata={
                "runtime_model": "multi_team_org",
                "report_execution_work_item": True,
                "report_source_summary": "executed summary",
            },
        )
        result = _report_execute_block(work_item)
        assert result != ""
        assert "Report" in result or "summary" in result.lower()


class TestContractToolNameAlignment:
    """契约文本引用的工具名必须与实际注册工具一致（禁出现未注册工具名）。"""

    def test_manager_contract_references_spawn_thread_task(self) -> None:
        work_item = _make_work_item(
            "execute",
            metadata={"runtime_model": "multi_team_org", "direct_report_seat_ids": ["seat_1"]},
        )
        result = build_company_work_item_contract(work_item)
        assert "`spawn_thread_task`" in result
        assert "`delegate_work`" not in result

    def test_dispatch_block_references_registered_names_only(self) -> None:
        work_item = _make_work_item(
            "execute",
            metadata={"runtime_model": "multi_team_org", "direct_report_seat_ids": ["seat_1"]},
        )
        result = build_company_work_item_contract(work_item)
        assert "`spawn_thread_task`" in result
        assert "`delegate_work`" not in result
        for name in ("`manager_board_read`", "`modify_work_item`", "`delete_work_item`"):
            assert name in result
