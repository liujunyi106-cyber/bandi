"""功能：spawn_thread_task 契约 —— Agent 互@ 建子任务真实执行 + 线程绑定 + fail-closed。"""

from __future__ import annotations

import pytest

from bandi.company.discussion_thread import ThreadManager
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.tools.base import RetryPolicy, ToolPermissionContext
from bandi.tools.thread_task import SpawnThreadTaskTool


class _FakeOrg:
    def __init__(self, roles: list[str]) -> None:
        self._roles = roles

    def list_roles(self) -> list[object]:
        return [type("Role", (), {"id": r})() for r in self._roles]


def _ctx() -> ToolPermissionContext:
    return ToolPermissionContext(role="product_agent", extra={"session_id": "s", "project_id": "p"})


@pytest.mark.asyncio
async def test_spawn_thread_task_binds_subtask_and_thread(tmp_path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    tmgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p", session_id="s")
    org = _FakeOrg(["tech"])
    thread = tmgr.create_thread(parent_task_id=None, participants=["product_agent", "tech"])
    tool = SpawnThreadTaskTool(store=store, thread_manager=tmgr, org_engine=org)
    result = await tool.execute(
        params={
            "target_role": "tech",
            "task_prompt": "实现 X",
            "thread_id": thread.thread_id,
            "parent_task_id": "t_parent",
        },
        context=_ctx(),
    )
    assert result is not None and result["success"] is True
    item_id = result["work_item_id"]
    item = store.get(item_id)
    assert item is not None
    assert item.assignee_role == "tech"
    assert item.parent_id == "t_parent"
    assert item.metadata["thread_id"] == thread.thread_id
    thread_obj = tmgr.get_thread(thread.thread_id)
    assert thread_obj is not None and len(thread_obj.messages) == 1
    assert thread_obj.messages[0].sender == "product_agent"
    assert thread_obj.messages[0].content == "实现 X"
    assert thread_obj.messages[0].mentions == ["tech"]


@pytest.mark.asyncio
async def test_spawn_thread_task_writes_prompt_contract(tmp_path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    tmgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p", session_id="s")
    tool = SpawnThreadTaskTool(store=store, thread_manager=tmgr, org_engine=_FakeOrg(["tech"]))
    result = await tool.execute(
        params={
            "target_role": "tech",
            "task_prompt": "实现 X",
            "upstream_intent_summary": "用户要做 Y",
            "deliverables": ["x.py"],
            "acceptance_criteria": ["测试通过"],
            "scope_key": "tech:x",
        },
        context=_ctx(),
    )
    assert result is not None and result["success"] is True
    item = store.get(result["work_item_id"])
    assert item is not None
    contract = item.metadata["prompt_contract"]
    assignment = contract["assignment_context"]
    assert contract["task_brief"] == "实现 X"
    assert assignment["upstream_intent_summary"] == "用户要做 Y"
    assert assignment["deliverables"] == ["x.py"]
    assert assignment["acceptance_criteria"] == ["测试通过"]
    assert assignment["scope_key"] == "tech:x"


@pytest.mark.asyncio
async def test_spawn_thread_task_unregistered_role_rejected(tmp_path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    tmgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p", session_id="s")
    org = _FakeOrg(["tech"])
    tool = SpawnThreadTaskTool(store=store, thread_manager=tmgr, org_engine=org)
    result = await tool.execute(
        params={"target_role": "ghost", "task_prompt": "x"},
        context=_ctx(),
    )
    assert result is not None and result["success"] is False
    assert isinstance(result["error"], str)


@pytest.mark.asyncio
async def test_spawn_thread_task_auto_creates_thread_when_missing(tmp_path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    tmgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p", session_id="s")
    org = _FakeOrg(["tech"])
    tool = SpawnThreadTaskTool(store=store, thread_manager=tmgr, org_engine=org)
    result = await tool.execute(
        params={"target_role": "tech", "task_prompt": "x", "parent_task_id": "t_p"},
        context=_ctx(),
    )
    assert result is not None and result["success"] is True
    thread_id = result["thread_id"]
    assert thread_id.startswith("thread_")
    thread = tmgr.get_thread(thread_id)
    assert thread is not None
    assert "tech" in thread.participants


def test_spawn_thread_task_must_declare_non_retryable() -> None:
    """写工具声明 retryable=False（派发副作用不能重试重放）。"""
    policy = SpawnThreadTaskTool.retry_policy
    assert isinstance(policy, RetryPolicy) and policy.retryable is False


@pytest.mark.asyncio
async def test_spawn_defaults_parent_to_bound_work_item(tmp_path) -> None:
    """公司链路绑定当前卡后未传 parent_task_id：子卡自动挂为当前卡子级。

    评审桥/依赖屏障据 parent_id 挂接——模型漏传父卡时链条不得断开。
    """
    store = WorkItemStore(str(tmp_path / "wi.db"))
    tmgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p", session_id="s")
    org = _FakeOrg(["tech"])
    parent = create_work_item(
        title="当前卡", turn_type=WorkItemTurnType.EXECUTE, assignee_role="product_agent"
    )
    store.save(parent)
    tool = SpawnThreadTaskTool(store=store, thread_manager=tmgr, org_engine=org)
    tool.bind_deps(work_item=parent)
    result = await tool.execute({"target_role": "tech", "task_prompt": "做 X"}, _ctx())
    assert result["success"] is True
    child = store.get(result["work_item_id"])
    assert child is not None
    assert child.parent_id == parent.id

    explicit = await tool.execute(
        {"target_role": "tech", "task_prompt": "做 Y", "parent_task_id": "t_parent"}, _ctx()
    )
    assert explicit["success"] is True
    child_explicit = store.get(explicit["work_item_id"])
    assert child_explicit is not None
    assert child_explicit.parent_id == "t_parent"


@pytest.mark.asyncio
async def test_spawn_message_id_unique_across_similar_prompts(tmp_path) -> None:
    """修复：两次相似 prompt（前 16 字符相同）spawn 的线程消息 message_id
    必须唯一——修复前用 task_prompt[:16] 作 id 会冲突。"""
    store = WorkItemStore(str(tmp_path / "wi.db"))
    tm = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p", session_id="s")
    org = _FakeOrg(["tech"])
    tool = SpawnThreadTaskTool(store=store, thread_manager=tm, org_engine=org)

    r1 = await tool.execute({"target_role": "tech", "task_prompt": "做竞品分析报告"}, None)
    r2 = await tool.execute(
        {"target_role": "tech", "task_prompt": "做竞品分析报告并附对比表"}, None
    )
    assert r1["success"] and r2["success"]
    t1 = tm.get_thread(r1["thread_id"])
    t2 = tm.get_thread(r2["thread_id"])
    id1 = t1.messages[0].message_id
    id2 = t2.messages[0].message_id
    assert id1 != id2, f"相似 prompt 的 message_id 冲突: {id1}"
    assert id1.startswith("m_") and len(id1) == len("m_") + 12


@pytest.mark.asyncio
async def test_spawn_sequence_links_children_in_order(tmp_path) -> None:
    """串行派发：sequence=true 时后一张卡等待前一张完成（chain_step 执行轮）。"""
    store = WorkItemStore(str(tmp_path / "wi.db"))
    tmgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p", session_id="s")
    org = _FakeOrg(["researcher", "analyst", "designer"])
    tool = SpawnThreadTaskTool(store=store, thread_manager=tmgr, org_engine=org)
    result = await tool.execute(
        {
            "tasks": [
                {"target_role": "researcher", "task_prompt": "第一步：调研"},
                {"target_role": "analyst", "task_prompt": "第二步：测算"},
                {"target_role": "designer", "task_prompt": "第三步：出图"},
            ],
            "sequence": True,
        },
        _ctx(),
    )
    assert result["success"] is True
    ids = result["work_item_ids"]
    first, second, third = (store.get(i) for i in ids)
    assert first is not None and second is not None and third is not None
    assert first.phase == Phase.QUEUED
    assert "chain_step" not in first.metadata
    for index, item in enumerate((second, third), start=1):
        assert item.phase == Phase.WAITING_DEPENDENCIES
        assert item.metadata["chain_step"] is True
        assert item.metadata["dependency_work_item_ids"] == [ids[index - 1]]


@pytest.mark.asyncio
async def test_spawn_without_sequence_keeps_parallel(tmp_path) -> None:
    """显式 sequence=false：多卡互不依赖，全部直接排队（并行扇出）。"""
    store = WorkItemStore(str(tmp_path / "wi.db"))
    tmgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p", session_id="s")
    org = _FakeOrg(["researcher", "analyst"])
    tool = SpawnThreadTaskTool(store=store, thread_manager=tmgr, org_engine=org)
    result = await tool.execute(
        {
            "tasks": [
                {"target_role": "researcher", "task_prompt": "A"},
                {"target_role": "analyst", "task_prompt": "B"},
            ],
            "sequence": False,
        },
        _ctx(),
    )
    assert result["success"] is True
    for item_id in result["work_item_ids"]:
        item = store.get(item_id)
        assert item is not None
        assert item.phase == Phase.QUEUED
        assert "chain_step" not in item.metadata
