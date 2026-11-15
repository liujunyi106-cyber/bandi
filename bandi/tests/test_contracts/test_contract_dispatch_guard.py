"""功能：dispatch guard 契约——判定矩阵 + 契约文本渲染。

钉行为不钉实现：非 DELEGATE 轮/有 board 变更/有 justification 一律放行；
无变更时限次退回重试（RETRY），超限标记未解决（UNRESOLVED）放行不得死循环。
"""

from __future__ import annotations

from bandi.company.dispatch_guard import (
    DISPATCH_GUARD_MAX_ATTEMPTS,
    DispatchGuardVerdict,
    evaluate_dispatch_guard,
)


def _verdict(
    *,
    is_delegate_turn: bool = True,
    board_changed: bool = False,
    has_justification: bool = False,
    attempts: int = 0,
) -> DispatchGuardVerdict:
    return evaluate_dispatch_guard(
        is_delegate_turn=is_delegate_turn,
        board_changed=board_changed,
        has_justification=has_justification,
        attempts=attempts,
    )


class TestDispatchGuardVerdictMatrix:
    def test_non_delegate_turn_passes(self) -> None:
        assert _verdict(is_delegate_turn=False) == DispatchGuardVerdict.OK

    def test_board_changed_passes(self) -> None:
        assert _verdict(board_changed=True) == DispatchGuardVerdict.OK

    def test_justification_passes(self) -> None:
        assert _verdict(has_justification=True, attempts=5) == DispatchGuardVerdict.OK

    def test_no_board_change_retries_then_unresolves(self) -> None:
        assert _verdict(attempts=0) == DispatchGuardVerdict.RETRY
        assert _verdict(attempts=1) == DispatchGuardVerdict.RETRY
        assert _verdict(attempts=DISPATCH_GUARD_MAX_ATTEMPTS) == DispatchGuardVerdict.UNRESOLVED
        assert _verdict(attempts=DISPATCH_GUARD_MAX_ATTEMPTS + 3) == DispatchGuardVerdict.UNRESOLVED


class TestDispatchGuardContractRender:
    def test_violation_rendered_in_contract(self) -> None:
        from bandi.company.company_runtime_contract import build_company_work_item_contract

        work_item = {
            "id": "wi-1",
            "title": "派单卡",
            "turn_type": "execute",
            "metadata": {
                "runtime_model": "multi_team_org",
                "direct_report_seat_ids": ["seat_1"],
                "manager_dispatch_guard_violation": "no delegation done",
            },
            "runtime_metadata": {},
        }
        text = build_company_work_item_contract(work_item)
        assert "Dispatch Guard Rejection" in text
        assert "no delegation done" in text
        assert "NO_DELEGATION_JUSTIFICATION" in text

    def test_no_violation_no_render(self) -> None:
        from bandi.company.company_runtime_contract import build_company_work_item_contract

        work_item = {
            "id": "wi-2",
            "title": "派单卡",
            "turn_type": "execute",
            "metadata": {
                "runtime_model": "multi_team_org",
                "direct_report_seat_ids": ["seat_1"],
            },
            "runtime_metadata": {},
        }
        text = build_company_work_item_contract(work_item)
        assert "Dispatch Guard Rejection" not in text
