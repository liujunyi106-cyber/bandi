"""功能：peer parking + route_work 真排队契约。

钉行为不钉实现：公司绑定时 ask_peer_and_wait 写 peer_wait 并挂起（waiting_for_peer），
回复到达经 resolve_task_peer_wait 唤醒（注入 latest_peer_reply）；未绑定回退轮询。
route_work.spawn_task 建真实排队卡；未注册角色拒绝；未绑定降级 DM。"""

from __future__ import annotations

import pytest

from bandi.company.communication import CommunicationManager
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.tools.base import ToolPermissionContext
from bandi.tools.collaboration import AskPeerAndWaitTool, RouteWorkTool


def _org() -> OrgEngine:
    engine = OrgEngine()
    engine.register_role("manager", "经理")
    engine.register_role("worker", "员工", reports_to="manager")
    return engine


def _fixtures(tmp_path):
    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = _org()
    comms = CommunicationManager(str(tmp_path / "ws"), "p1", "s1", org_engine=org)
    item = create_work_item(
        title="待问询任务",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="worker",
        phase=Phase.RUNNING,
    )
    store.save(item)
    ctx = ToolPermissionContext(role="worker", communication=comms)
    return store, org, comms, item, ctx


@pytest.mark.asyncio
async def test_ask_peer_parks_card_in_company_mode(tmp_path) -> None:
    store, _org_engine, _comms, item, ctx = _fixtures(tmp_path)
    tool = AskPeerAndWaitTool()
    tool.bind_company(store=store, work_item=item)
    result = await tool.execute({"to_agent": "manager", "subject": "问一件事", "body": "内容"}, ctx)
    assert result.get("requires_peer_wait") is True
    assert "waiting_for_peer" in str(result.get("note", ""))
    fresh = store.get(item.id)
    assert fresh is not None
    assert fresh.phase == Phase.WAITING_FOR_PEER
    wait = fresh.metadata["peer_wait"]
    assert wait["kind"] == "peer_message"
    assert wait["message_id"] == result["msg_id"]
    assert wait["waiting_on_agents"] == ["manager"]
    assert wait["timeout_seconds"] >= 300


@pytest.mark.asyncio
async def test_reply_wakes_parked_card_with_content(tmp_path) -> None:
    store, _org_engine, comms, item, ctx = _fixtures(tmp_path)
    tool = AskPeerAndWaitTool()
    tool.bind_company(store=store, work_item=item)
    result = await tool.execute({"to_agent": "manager", "subject": "问一件事", "body": "内容"}, ctx)
    comms.reply_to_message(result["msg_id"], "manager", "回复内容")
    fresh = store.get(item.id)
    assert fresh is not None
    assert comms.resolve_task_peer_wait(fresh) is True
    store.save(fresh)
    reloaded = store.get(item.id)
    assert reloaded is not None
    assert reloaded.phase == Phase.READY
    assert reloaded.metadata["latest_peer_reply"] == "回复内容"
    assert reloaded.metadata.get("peer_wait") is None


@pytest.mark.asyncio
async def test_unbound_tool_falls_back_to_polling(tmp_path) -> None:
    store, _org_engine, _comms, item, ctx = _fixtures(tmp_path)
    tool = AskPeerAndWaitTool()
    result = await tool.execute(
        {"to_agent": "manager", "subject": "s", "body": "b", "timeout_seconds": 1}, ctx
    )
    assert result.get("reply") is None
    assert "requires_peer_wait" not in result
    fresh = store.get(item.id)
    assert fresh is not None
    assert fresh.phase == Phase.RUNNING


@pytest.mark.asyncio
async def test_route_work_spawns_real_queued_card(tmp_path) -> None:
    store, org, _comms, item, ctx = _fixtures(tmp_path)
    tool = RouteWorkTool()
    tool.bind_company(store=store, org_engine=org, work_item=item)
    result = await tool.execute(
        {"action": "spawn_task", "target_role": "manager", "prompt": "做竞品调研"}, ctx
    )
    assert result.get("work_item_id")
    created = store.get(result["work_item_id"])
    assert created is not None
    assert created.assignee_role == "manager"
    assert created.phase == Phase.QUEUED
    assert created.metadata["source"] == "route_work"


@pytest.mark.asyncio
async def test_route_work_spawn_rejects_unregistered_role(tmp_path) -> None:
    store, org, _comms, item, ctx = _fixtures(tmp_path)
    tool = RouteWorkTool()
    tool.bind_company(store=store, org_engine=org, work_item=item)
    result = await tool.execute(
        {"action": "spawn_task", "target_role": "ghost", "prompt": "x"}, ctx
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_route_work_unbound_degrades_to_dm(tmp_path) -> None:
    store, _org_engine, _comms, _item, ctx = _fixtures(tmp_path)
    tool = RouteWorkTool()
    result = await tool.execute(
        {"action": "spawn_task", "target_role": "manager", "prompt": "x"}, ctx
    )
    assert result.get("msg_id")
    assert "work_item_id" not in result


class TestPeerReplyContractRender:
    def _work_item(self, metadata: dict) -> dict:
        return {
            "id": "wi-peer",
            "title": "peer 卡",
            "turn_type": "execute",
            "metadata": {"runtime_model": "multi_team_org", **metadata},
            "runtime_metadata": {},
        }

    def test_reply_block_rendered(self) -> None:
        from bandi.company.company_runtime_contract import build_company_work_item_contract

        text = build_company_work_item_contract(
            self._work_item({"latest_peer_reply": "预算已批 200 元"})
        )
        assert "Peer Reply Context" in text
        assert "预算已批 200 元" in text

    def test_timeout_block_rendered(self) -> None:
        from bandi.company.company_runtime_contract import build_company_work_item_contract

        text = build_company_work_item_contract(
            self._work_item({"peer_timeout_action": "continue"})
        )
        assert "Peer Reply Context" in text
        assert "timed out" in text

    def test_absent_without_keys(self) -> None:
        from bandi.company.company_runtime_contract import build_company_work_item_contract

        text = build_company_work_item_contract(self._work_item({}))
        assert "Peer Reply Context" not in text
