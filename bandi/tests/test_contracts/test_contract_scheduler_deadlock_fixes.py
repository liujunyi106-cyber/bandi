"""功能：契约——调度状态机的两个一次性标记。

综合轮标记只在本次等待周期内有效：父卡第二次委派子卡、子卡全部完成后仍要进入
综合轮，上一轮综合口径（来源子卡/工作类型）不得在新一轮等待里重放；槽位门在本轮
尝试取答复后即摘除，用户不答复（空答复/等待异常）时同一张卡的同一个门只阻塞一次。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bandi.company.phase import Phase
from bandi.company.seat_executor import EngineSeatExecutor
from bandi.company.store import WorkItemStore
from bandi.company.work_item import (
    REACTIVATION_REASON,
    WorkItemTurnType,
    create_work_item,
    transition_work_item,
)
from bandi.company.work_item_transition import refresh_dependents
from bandi.tools.thread_task import SpawnThreadTaskTool


def _store(tmp_path: Path) -> WorkItemStore:
    return WorkItemStore(str(tmp_path / "work_items.db"))


def _item(
    item_id: str,
    *,
    phase: Phase = Phase.QUEUED,
    metadata: dict[str, Any] | None = None,
    title: str = "任务",
) -> Any:
    item = create_work_item(
        title=title,
        turn_type=WorkItemTurnType.EXECUTE,
        metadata=dict(metadata or {}),
        phase=phase,
    )
    item.id = item_id
    return item


def _delegate_child(store: WorkItemStore, parent_id: str, child_id: str) -> None:
    """功能：走生产派发工具的父卡等待门登记——父卡追加依赖子卡并置等待挂起。"""
    SpawnThreadTaskTool(store=store)._register_single_barrier(parent_id, child_id)


def _approved(store: WorkItemStore, item_id: str) -> None:
    child = store.get(item_id)
    assert child is not None
    child.phase = Phase.APPROVED
    store.save(child)


class _AnswerProvider:
    """功能：可计数假答复源——按序返回答复，{} 表示用户未作答，raise_on 触发等待异常。"""

    def __init__(self, *responses: Any, raise_on: int | None = None) -> None:
        self.calls: list[tuple[str, float]] = []
        self._responses = list(responses)
        self._raise_on = raise_on

    async def __call__(self, interaction_id: str, timeout: float) -> Any:
        self.calls.append((interaction_id, timeout))
        if self._raise_on is not None and len(self.calls) == self._raise_on:
            raise RuntimeError("interaction store unavailable")
        return self._responses.pop(0) if self._responses else {}


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        role_id="planner",
        employee_id="emp-1",
        project_id="p1",
        member_session_id=lambda: "sess-deadlock",
        set_metadata=lambda key, value: None,
    )


def _fake_run_agent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """功能：把执行链换成本地假实现——本轮只验证门与状态机，不调真实模型。"""
    import bandi.company.seat_executor as se_mod

    prompts: list[str] = []

    async def fake_run_agent(**kwargs: Any) -> Any:
        prompts.append(str(kwargs.get("user_input") or ""))
        return ("## 已完成\n\n本轮交付正文。", "COMPLETED", 7, 1)

    monkeypatch.setattr(se_mod, "run_agent", fake_run_agent)
    return prompts


def test_second_delegation_enters_synthesis_turn_again(tmp_path: Path) -> None:
    """父卡第二次等待子卡、子卡全部完成后必须重新进入综合轮。"""
    store = _store(tmp_path)
    store.save(
        _item(
            "parent",
            phase=Phase.RUNNING,
            metadata={
                "dependency_work_item_ids": ["child-1"],
                "delegated_children_pending": True,
                "frontier": "waiting_for_children",
                "work_kind": "execute",
            },
        )
    )
    store.save(_item("child-1", phase=Phase.QUEUED))

    # 第一轮委派：子卡未完成 → 父卡入等待；子卡完成 → 综合轮释放
    assert refresh_dependents(store) is True
    assert store.get("parent").phase == Phase.WAITING_FOR_CHILDREN
    _approved(store, "child-1")
    assert refresh_dependents(store) is True
    first = store.get("parent")
    assert first.phase == Phase.READY, f"第一轮必须进入综合轮，实际 {first.phase}"
    assert first.metadata["synthesis_turn_started"] is True
    assert first.metadata["work_kind"] == "synthesize"

    # 综合轮被认领执行，执行中又派出第二张子卡
    transition_work_item(first, Phase.RUNNING, store, reason="claim")
    store.save(_item("child-2", phase=Phase.QUEUED))
    _delegate_child(store, "parent", "child-2")

    assert refresh_dependents(store) is True
    second_wait = store.get("parent")
    assert second_wait.phase == Phase.WAITING_FOR_CHILDREN
    assert "synthesis_turn_started" not in second_wait.metadata, (
        "上一轮综合标记必须随新等待周期作废"
    )
    assert second_wait.metadata["delegated_children_pending"] is True

    _approved(store, "child-2")
    assert refresh_dependents(store) is True
    second = store.get("parent")
    assert second.phase == Phase.READY, f"第二轮子卡全部完成必须再次进入综合轮，实际 {second.phase}"
    assert second.metadata["synthesis_turn_started"] is True
    assert second.metadata["work_kind"] == "synthesize"
    assert set(second.metadata["synthesis_source_work_item_ids"]) == {"child-1", "child-2"}


def test_completed_synthesis_cycle_is_not_replayed_in_next_wait(tmp_path: Path) -> None:
    """综合轮执行完后再进入等待：旧综合口径清除，连续 tick 不得拿旧产出放行。"""
    store = _store(tmp_path)
    parent = _item(
        "parent",
        phase=Phase.READY,
        metadata={
            "dependency_work_item_ids": ["child-1"],
            "synthesis_turn_started": True,
            "synthesis_source_work_item_ids": ["child-1"],
            "pre_synthesis_work_kind": "execute",
            "work_kind": "synthesize",
            "delegation_turn_kind": "synthesize",
            "current_turn_mode": "synthesize_required",
            "frontier": "synthesis_ready",
        },
    )
    parent.runtime_metadata["_last_output"] = "上一轮综合正文"
    store.save(parent)
    store.save(_item("child-1", phase=Phase.APPROVED))

    # 综合轮待执行：标记必须保留，否则同一轮会被反复释放
    refresh_dependents(store)
    pending = store.get("parent")
    assert pending.phase == Phase.READY
    assert pending.metadata["synthesis_turn_started"] is True

    # 综合轮执行完并交付，随后被外部唤醒复活并认领，再挂一张新子卡
    done = transition_work_item(pending, Phase.RUNNING, store, reason="claim")
    done = transition_work_item(done, Phase.APPROVED, store, reason="synthesis_turn_completed")
    revived = transition_work_item(done, Phase.READY, store, reason=REACTIVATION_REASON)
    revived = transition_work_item(revived, Phase.RUNNING, store, reason="claim")
    assert revived.phase == Phase.RUNNING
    store.save(_item("child-2", phase=Phase.QUEUED))
    _delegate_child(store, "parent", "child-2")

    # 回到等待子卡：上一轮综合口径整组作废
    assert refresh_dependents(store) is True
    waiting = store.get("parent")
    assert waiting.phase == Phase.WAITING_FOR_CHILDREN
    assert "synthesis_turn_started" not in waiting.metadata
    assert "synthesis_source_work_item_ids" not in waiting.metadata
    assert waiting.metadata.get("work_kind") == "execute", (
        "工作类型必须还原，否则综合轮类型跳过规则挡住下一轮"
    )
    assert waiting.metadata.get("current_turn_mode") is None

    # 连续 tick 不得把旧综合当成已完成而释放（旧产出仍在卡上也不重放）
    refresh_dependents(store)
    still_waiting = store.get("parent")
    assert still_waiting.phase == Phase.WAITING_FOR_CHILDREN, "旧综合正文不得被当成新一轮交付"
    assert still_waiting.runtime_metadata.get("_last_output") == "上一轮综合正文"
    assert still_waiting.metadata.get("delegated_children_pending") is True

    _approved(store, "child-2")
    assert refresh_dependents(store) is True
    fresh = store.get("parent")
    assert fresh.phase == Phase.READY
    assert "child-2" in set(fresh.metadata["synthesis_source_work_item_ids"])


@pytest.mark.asyncio
async def test_slot_gate_timeout_wait_happens_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户不答复（返回空）：本轮摘门落超时标记，同卡第二次执行不再等待。"""
    store = _store(tmp_path)
    store.save(_item("card-1", phase=Phase.RUNNING, metadata={"slot_gate": "gate-1"}))
    provider = _AnswerProvider({})
    prompts = _fake_run_agent(monkeypatch)
    executor = EngineSeatExecutor(store=store, slot_answer_provider=provider)

    first = await executor.run_turn(_runtime(), store.get("card-1"))
    assert first.success is True
    assert [call[0] for call in provider.calls] == ["gate-1"]
    assert provider.calls[0][1] > 0
    assert len(prompts) == 1

    saved = store.get("card-1")
    assert saved.metadata.get("slot_gate") is None, "超时也必须消费门标记"
    assert saved.metadata.get("slot_answer_timeout") is True
    assert saved.metadata.get("slot_answered") is None, "未作答不得记成已答复"
    assert "【已确认口径】" not in (saved.description or "")

    second = await executor.run_turn(_runtime(), store.get("card-1"))
    assert second.success is True
    assert len(provider.calls) == 1, f"同一个门只允许阻塞一次，实际等待 {len(provider.calls)} 次"
    assert len(prompts) == 2


@pytest.mark.parametrize("failure_mode", ["empty_answers", "provider_raises"])
@pytest.mark.asyncio
async def test_repeated_timeouts_never_double_consume_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    """破坏者视角：连续超时不得重复消费/误判已答复，新门仍只等一次并写回口径。"""
    store = _store(tmp_path)
    store.save(_item("card-2", phase=Phase.RUNNING, metadata={"slot_gate": "gate-2"}))
    if failure_mode == "provider_raises":
        provider = _AnswerProvider({"market": "美国"}, raise_on=1)
    else:
        provider = _AnswerProvider({}, {"market": "美国"})
    _fake_run_agent(monkeypatch)
    executor = EngineSeatExecutor(store=store, slot_answer_provider=provider)

    await executor.run_turn(_runtime(), store.get("card-2"))
    await executor.run_turn(_runtime(), store.get("card-2"))

    assert len(provider.calls) == 1, "连续两次执行只能等待一次"
    saved = store.get("card-2")
    assert saved.metadata.get("slot_gate") is None
    assert saved.metadata.get("slot_answered") is None, "超时不得被误判为已答复"
    assert saved.metadata.get("slot_answer_timeout") is True
    assert (saved.description or "").count("【已确认口径】") == 0

    # 新一轮多问卡（另一个门）：照常只等一次，答复写回口径且只写一次
    refreshed = store.get("card-2")
    refreshed.metadata = {**refreshed.metadata, "slot_gate": "gate-3"}
    store.save(refreshed)
    await executor.run_turn(_runtime(), store.get("card-2"))
    assert [call[0] for call in provider.calls] == ["gate-2", "gate-3"]

    answered = store.get("card-2")
    assert answered.metadata.get("slot_answered") is True
    assert answered.metadata.get("slot_answer_timeout") is None
    assert (answered.description or "").count("【已确认口径】") == 1

    await executor.run_turn(_runtime(), store.get("card-2"))
    assert len(provider.calls) == 2, "已答复的门不得再次等待"
    assert (store.get("card-2").description or "").count("【已确认口径】") == 1
