"""契约：审批决策只恢复「那一张卡」，不再按会话全量回填。

旧行为 `resume_after_approval(session_id)` 把同会话所有 awaiting_approval 卡一起
re-queue（连带重跑）；现行为优先用 tool_call_id 精确命中，其次 tool_name，
旧数据（卡上无审批锚点）才退回会话级全量恢复。
"""

from __future__ import annotations

from pathlib import Path

from bandi.company.company_mode import CompanyDispatcher
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItem, WorkItemTurnType


def _item(item_id: str, *, session_id: str, call_id: str = "", tool: str = "") -> WorkItem:
    runtime_metadata: dict[str, object] = {
        "_approval_session_id": session_id,
        "_task_status": "awaiting_approval",
    }
    if call_id:
        runtime_metadata["_approval_tool_call_id"] = call_id
    if tool:
        runtime_metadata["_approval_tool_name"] = tool
    return WorkItem(
        id=item_id,
        title=item_id,
        description="",
        phase=Phase.READY,
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="alice",
        parent_id=None,
        children_ids=[],
        links={},
        metadata={},
        runtime_metadata=runtime_metadata,
    )


def _dispatcher(tmp_path: Path) -> CompanyDispatcher:
    dispatcher = object.__new__(CompanyDispatcher)
    dispatcher.store = WorkItemStore(str(tmp_path / "wi.db"))
    dispatcher.signal_dispatcher_wake = lambda: None  # type: ignore[method-assign]
    return dispatcher


def _resume_meta(dispatcher: CompanyDispatcher, item_id: str) -> dict:
    item = dispatcher.store.get(item_id)
    assert item is not None
    return dict((item.metadata or {}).get("approval_resume") or {})


def test_resume_matches_only_the_decided_call(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    dispatcher.store.save(_item("card-a", session_id="s1", call_id="call-a", tool="bash"))
    dispatcher.store.save(_item("card-b", session_id="s1", call_id="call-b", tool="bash"))

    resumed = dispatcher.resume_after_approval(
        "s1", decision="allow_once", tool_name="bash", tool_call_id="call-a"
    )

    assert resumed == 1
    assert _resume_meta(dispatcher, "card-a").get("decision") == "allow_once"
    assert _resume_meta(dispatcher, "card-b") == {}, "未被决策的卡不得被连带 re-queue"


def test_resume_falls_back_to_tool_name_for_legacy_cards(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    dispatcher.store.save(_item("card-a", session_id="s1", call_id="call-a", tool="bash"))
    dispatcher.store.save(_item("card-legacy", session_id="s1", tool="write"))

    # 决策的 tool_call_id 在任何卡上都找不到（例如卡侧未记录）→ 退回 tool_name 匹配
    resumed = dispatcher.resume_after_approval(
        "s1", decision="deny", tool_name="write", tool_call_id="call-missing"
    )

    assert resumed == 1, "精确 id 未命中时应退回 tool_name 匹配"
    assert _resume_meta(dispatcher, "card-legacy").get("decision") == "deny"
    assert _resume_meta(dispatcher, "card-a") == {}


def test_resume_falls_back_to_session_for_unlabelled_cards(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    dispatcher.store.save(_item("card-1", session_id="s1"))
    dispatcher.store.save(_item("card-2", session_id="s1"))

    resumed = dispatcher.resume_after_approval("s1", decision="allow_once", tool_name="bash")

    assert resumed == 2, "旧数据（无 tool_call/工具名锚点）保持会话级恢复"
    assert _resume_meta(dispatcher, "card-1")
    assert _resume_meta(dispatcher, "card-2")
