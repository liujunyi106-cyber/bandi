# 功能：契约 槽位门（群聊等答案）挂起与恢复的端到端行为
#
# 行为不变式：
#   ① 卡上登记 slot_gate 后，执行前必须真挂起等用户答复，答复内容要写进任务描述顶部
#      （模型看得到口径，而不是系统自己猜）；
#   ② 挂起期间用户提交答复 → 本轮被唤醒并继续执行，不丢这一轮；
#   ③ 不答复/超时 → 门被消费一次并留 slot_answer_timeout，同一张卡的重试不得二次等满
#      等待上限（否则座位被白白占住半小时，同角色其它卡全部排队）。

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bandi.company.company_runtime import CompanyRuntime
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.core.user_interaction import InteractionStore


def _runtime() -> CompanyRuntime:
    return CompanyRuntime(
        project_id="p1",
        session_scope_id="s1",
        role_id="worker",
        employee_id="e1",
    )


@pytest.fixture()
def answered_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """功能：把引擎替换成捕获输入，避免本用例依赖真实 LLM。"""
    import bandi.company.seat_executor as seat_mod

    captured: dict[str, Any] = {}

    async def fake_run_agent(user_input, mode="execute", session_id=None, **kwargs):
        captured.setdefault("inputs", []).append(user_input)
        return "ok", "COMPLETED", 10, 1

    monkeypatch.setattr(seat_mod, "run_agent", fake_run_agent)
    return captured


def _slot_item(store: WorkItemStore, gate_id: str) -> Any:
    item = create_work_item(
        title="做一份季度经营分析",
        turn_type=WorkItemTurnType.EXECUTE,
        description="目标行业与预算待确认",
        assignee_role="worker",
    )
    item.metadata["slot_gate"] = gate_id
    store.save(item)
    return item


def test_answered_slot_gate_is_written_into_task(tmp_path: Any, answered_capture) -> None:
    """契约①：答复取到后必须写进任务描述，且卡上的门被摘除。"""
    from bandi.company.seat_executor import EngineSeatExecutor

    store = WorkItemStore(str(tmp_path / "company.db"))
    interactions = InteractionStore()
    extension = interactions.register(
        session_id="sess-slot",
        kind="input",
        payload={"questions": [{"id": "industry", "question": "目标行业？"}]},
        ttl_seconds=30.0,
    )
    item = _slot_item(store, extension.interaction_id)
    interactions.resolve(extension.interaction_id, {"industry": "消费电子"})

    async def provider(interaction_id: str, timeout: float = 30.0) -> dict[str, Any] | None:
        return await interactions.get_decision(interaction_id, timeout=timeout)

    executor = EngineSeatExecutor(store=store, slot_answer_provider=provider)
    asyncio.run(executor.run_turn(_runtime(), item))

    sent = answered_capture["inputs"][0]
    assert "已确认口径" in sent, f"答复必须回注任务描述，实际：{sent[:160]!r}"
    assert "消费电子" in sent, "答复原文必须出现在模型输入里"
    refreshed = store.get(item.id)
    assert "slot_gate" not in (refreshed.metadata or {}), "门必须被消费掉"
    assert refreshed.metadata.get("slot_answered") is True


def test_pending_answer_resumes_suspended_turn(tmp_path: Any, answered_capture) -> None:
    """契约②：先挂起，用户随后答复，本轮被唤醒并带着口径继续跑完。"""
    from bandi.company.seat_executor import EngineSeatExecutor

    store = WorkItemStore(str(tmp_path / "company.db"))
    interactions = InteractionStore()
    extension = interactions.register(
        session_id="sess-slot",
        kind="input",
        payload={"questions": [{"id": "budget", "question": "预算上限？"}]},
        ttl_seconds=30.0,
    )
    item = _slot_item(store, extension.interaction_id)

    async def provider(interaction_id: str, timeout: float = 30.0) -> dict[str, Any] | None:
        return await interactions.get_decision(interaction_id, timeout=timeout)

    executor = EngineSeatExecutor(store=store, slot_answer_provider=provider)

    async def scenario() -> None:
        turn = asyncio.ensure_future(executor.run_turn(_runtime(), item))
        await asyncio.sleep(0.05)
        assert not turn.done(), "未答复前本轮必须真挂起，不得空跑"
        interactions.resolve(extension.interaction_id, {"budget": "八十万以内"})
        await asyncio.wait_for(turn, timeout=10)

    asyncio.run(scenario())

    sent = answered_capture["inputs"][0]
    assert "八十万以内" in sent, f"唤醒后必须带着答复继续，实际：{sent[:160]!r}"
    assert store.get(item.id).metadata.get("slot_answered") is True


def test_timeout_consumes_gate_only_once(tmp_path: Any, answered_capture) -> None:
    """契约③（破坏者视角）：不答复 → 门消费一次并留超时标记；同卡重试不得二次等待。"""
    from bandi.company.seat_executor import EngineSeatExecutor

    store = WorkItemStore(str(tmp_path / "company.db"))
    item = _slot_item(store, "gate-never-answered")
    waits: list[str] = []

    async def provider(interaction_id: str, timeout: float = 30.0) -> dict[str, Any] | None:
        waits.append(interaction_id)
        return None

    executor = EngineSeatExecutor(store=store, slot_answer_provider=provider)
    asyncio.run(executor.run_turn(_runtime(), item))

    first = store.get(item.id)
    assert "slot_gate" not in (first.metadata or {}), "超时也必须摘门，否则重试会再等满上限"
    assert first.metadata.get("slot_answer_timeout") is True

    asyncio.run(executor.run_turn(_runtime(), first))
    assert len(waits) == 1, f"同一个门只允许阻塞一次，实际等待 {len(waits)} 次"
    assert "已确认口径" not in answered_capture["inputs"][-1], "无答复时不得伪造已确认口径"
