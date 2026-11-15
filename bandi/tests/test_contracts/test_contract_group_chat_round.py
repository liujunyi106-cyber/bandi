"""契约：群聊多 Agent 发言顺序、@ 规则与 PASS 语义。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.api.company_chat import handle_chat_message, resolve_mentions, unresolved_mentions
from bandi.company.group_chat_protocol import (
    GROUP_PASS_HINT,
    MENTION_DEPRECATED,
    MENTION_NOT_IN_PROJECT,
    MentionError,
    is_pass_output,
)
from bandi.company.group_round import SlotStatus
from bandi.company.group_round_store import GroupRoundStore
from bandi.company.org_engine import OrgEngine
from bandi.company.store import WorkItemStore
from bandi.company.work_item import Phase


class _Comms:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_dm(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


def _org() -> OrgEngine:
    org = OrgEngine()
    org.register_role("lead", "负责人", team_id="t1")
    org.register_role("worker", "执行", team_id="t1")
    org.register_role("reviewer", "评审", team_id="t1")
    return org


def _store(tmp_path: Path) -> WorkItemStore:
    return WorkItemStore(str(tmp_path / "group-chat.db"))


def _run(tmp_path: Path, content: str, default_role: str = "") -> list[Any]:
    return handle_chat_message(
        content=content,
        sender_role="user",
        communication=_Comms(),
        store=_store(tmp_path),
        org_engine=_org(),
        default_role=default_role,
        member_role_ids={"lead", "worker", "reviewer"},
    )


def _run_capture(
    tmp_path: Path,
    content: str,
    *,
    default_role: str = "",
    member_role_ids: set[str] | None = None,
) -> tuple[list[Any], _Comms, WorkItemStore]:
    comms = _Comms()
    store = _store(tmp_path)
    created = handle_chat_message(
        content=content,
        sender_role="user",
        communication=comms,
        store=store,
        org_engine=_org(),
        default_role=default_role,
        member_role_ids=member_role_ids
        if member_role_ids is not None
        else {"lead", "worker", "reviewer"},
    )
    return created, comms, store


def test_no_mention_routes_to_first_active_member(tmp_path: Path) -> None:
    created = _run(tmp_path, "开始做一版方案", default_role="lead")
    assert [item.assignee_role for item in created] == ["lead"]
    assert created[0].phase == Phase.QUEUED
    assert created[0].metadata["context_hint"] == GROUP_PASS_HINT


def test_multiple_mentions_are_serialized(tmp_path: Path) -> None:
    created = _run(tmp_path, "@worker 做实现，@reviewer 做评审")
    assert [item.assignee_role for item in created] == ["worker"]
    assert created[0].phase == Phase.QUEUED
    assert created[0].metadata["group_speaker_index"] == 0
    assert created[0].metadata["context_hint"] == GROUP_PASS_HINT
    round_store = GroupRoundStore(str(tmp_path / "group-chat.db"))
    try:
        round_ = round_store.get_active_by_session("")
        assert round_ is not None
        assert [slot.role_id for slot in round_.slots] == ["worker", "reviewer"]
        assert [slot.status for slot in round_.slots] == [
            SlotStatus.SPEAKING,
            SlotStatus.WAITING,
        ]
    finally:
        round_store.close()


def test_at_all_is_rejected(tmp_path: Path) -> None:
    comms = _Comms()
    store = _store(tmp_path)
    with pytest.raises(MentionError) as exc:
        handle_chat_message(
            content="@all 全体注意",
            sender_role="user",
            communication=comms,
            store=store,
            org_engine=_org(),
            member_role_ids={"lead", "worker", "reviewer"},
        )
    assert exc.value.code == MENTION_DEPRECATED
    assert comms.sent == []
    assert store.list_all() == []
    assert resolve_mentions("@all 全体注意", ["lead", "worker"]).target_agents == []
    assert unresolved_mentions("@all 全体注意", _org()) == ["all"]


def test_non_project_registered_mention_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(MentionError) as exc:
        _run_capture(
            tmp_path,
            "@reviewer 做评审",
            member_role_ids={"lead", "worker"},
        )
    assert exc.value.code == MENTION_NOT_IN_PROJECT
    assert exc.value.mentions == ["reviewer"]


def test_mixed_project_and_non_project_mention_rejects_whole_message(tmp_path: Path) -> None:
    with pytest.raises(MentionError) as exc:
        _run_capture(
            tmp_path,
            "@worker @reviewer 一起看一下",
            member_role_ids={"lead", "worker"},
        )
    assert exc.value.code == MENTION_NOT_IN_PROJECT
    assert exc.value.mentions == ["reviewer"]


def test_non_project_mention_does_not_persist_board_or_cards(tmp_path: Path) -> None:
    comms = _Comms()
    store = _store(tmp_path)
    with pytest.raises(MentionError):
        handle_chat_message(
            content="@reviewer 做评审",
            sender_role="user",
            communication=comms,
            store=store,
            org_engine=_org(),
            member_role_ids={"lead", "worker"},
        )
    assert comms.sent == [], "非项目成员 @ 不得落 board 广播"
    assert store.list_all() == [], "非项目成员 @ 不得建卡"


def test_pass_output_is_detected() -> None:
    assert is_pass_output("[PASS]")
    assert is_pass_output("pass")
    assert is_pass_output("（PASS）")
    assert is_pass_output("【PASS】")
    assert is_pass_output("**PASS**")
    assert is_pass_output("`PASS`")
    assert is_pass_output("[PASS]。")
    assert not is_pass_output("这里不能 pass 掉")
    assert not is_pass_output("PASS，我补充一点")
    assert not is_pass_output("[PASS] 补充")


@pytest.mark.parametrize("text", ["@worker", "@worker @reviewer"])
def test_specific_mentions_never_fall_back_to_broadcast(tmp_path: Path, text: str) -> None:
    created = _run(tmp_path, f"{text} 处理一下")
    assert created
    assert all(item.assignee_role in {"worker", "reviewer"} for item in created)
