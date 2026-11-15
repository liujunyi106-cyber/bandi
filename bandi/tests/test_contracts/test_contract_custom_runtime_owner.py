# 功能：多 dispatcher 共享 store 时的 runtime_owner 归属过滤
from __future__ import annotations

from types import SimpleNamespace

from bandi.company.company_mode import META_RUNTIME_OWNER, CompanyDispatcher
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _dispatcher(owner: str, items: list) -> CompanyDispatcher:
    dispatcher = CompanyDispatcher.__new__(CompanyDispatcher)
    dispatcher.runtime_owner = owner
    dispatcher.store = SimpleNamespace(list_all=lambda: items)
    return dispatcher


def test_company_and_custom_cards_do_not_cross_claim() -> None:
    company = create_work_item("company", WorkItemTurnType.EXECUTE)
    custom = create_work_item(
        "custom",
        WorkItemTurnType.EXECUTE,
        metadata={META_RUNTIME_OWNER: "custom:org-a"},
    )
    items = [company, custom]

    company_disp = _dispatcher("company", items)
    custom_disp = _dispatcher("custom:org-a", items)

    assert company_disp._owned_items() == [company]
    assert custom_disp._owned_items() == [custom]
    assert company_disp._owns(company) is True
    assert company_disp._owns(custom) is False
    assert custom_disp._owns(custom) is True


def test_legacy_card_without_owner_belongs_to_company() -> None:
    legacy = create_work_item("legacy", WorkItemTurnType.EXECUTE)
    assert _dispatcher("company", [legacy])._owns(legacy) is True
    assert _dispatcher("custom:org-a", [legacy])._owns(legacy) is False
