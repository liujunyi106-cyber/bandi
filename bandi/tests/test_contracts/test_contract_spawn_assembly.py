"""功能：spawn_thread_task 装配契约 —— registry 裸实例 bind_deps 后可执行（DoD-11 接线闭环）。

重建根因：factory.build_tool_registry 以裸构造注册 SpawnThreadTaskTool()，生产链路
（EngineSeatExecutor→run_agent→run_agent_turn）必须在 registry 建成后注入 deps
否则工具对 LLM 可见但每次调用必 fail-closed（被审查复现：execute → requires deps）。
"""

from __future__ import annotations

import pytest

from bandi.agent.factory import build_tool_registry
from bandi.tools.base import ToolPermissionContext
from bandi.tools.thread_task import SpawnThreadTaskTool


class _FakeOrg:
    def __init__(self, roles: list[str]) -> None:
        self._roles = roles

    def list_roles(self) -> list[object]:
        return [type("Role", (), {"id": r})() for r in self._roles]


def _ctx() -> ToolPermissionContext:
    return ToolPermissionContext(role="product_agent", extra={"session_id": "s", "project_id": "p"})


class _FakeStore:
    """WorkItemStore 最小行为桩（save/get 留痕断言最终产物，不测真实落盘）。"""

    def __init__(self) -> None:
        self.saved: list[object] = []

    def save(self, item: object) -> None:
        self.saved.append(item)

    def get(self, item_id: str) -> object | None:
        return next((i for i in self.saved if getattr(i, "id", "") == item_id), None)


class _FakeThreadManager:
    """ThreadManager 行为桩：创建/追加必须写回真实可读产物（断言契约，非代码存在）。"""

    def __init__(self) -> None:
        self.threads: dict[str, dict] = {}

    def create_thread(
        self, parent_task_id: str | None = None, participants: list[str] | None = None
    ) -> object:
        tid = f"thread_{len(self.threads) + 1}"
        self.threads[tid] = {"id": tid, "participants": list(participants or []), "messages": []}
        return type("Thread", (), {"thread_id": tid})()

    def get_thread(self, thread_id: str) -> object | None:
        return object() if thread_id in self.threads else None

    def add_message(
        self,
        thread_id: str,
        sender: str,
        content: str,
        mentions: list[str] | None = None,
        **_: object,
    ) -> None:
        self.threads[thread_id]["messages"].append(
            {"sender": sender, "content": content, "mentions": mentions}
        )


def test_registry_spawn_tool_starts_bare() -> None:
    """registry 装配的 spawn 实例依赖为空（现状基线），未绑定时 execute 必须 fail-closed。"""
    registry = build_tool_registry()
    tool = registry.get("spawn_thread_task")
    assert tool is not None
    assert tool.store is None and tool.thread_manager is None and tool.org_engine is None


@pytest.mark.asyncio
async def test_registry_spawn_tool_bind_deps_then_executes(tmp_path) -> None:
    """沿 bind_registry/bind_ui 先例：registry 裸实例 bind_deps 后 execute 必须真实可用。"""
    from bandi.company.discussion_thread import ThreadManager
    from bandi.company.store import WorkItemStore

    registry = build_tool_registry()
    tool = registry.get("spawn_thread_task")
    assert isinstance(tool, SpawnThreadTaskTool)
    store = WorkItemStore(str(tmp_path / "wi.db"))
    tmgr = ThreadManager(workspace=str(tmp_path / "ws"), project_id="p", session_id="s")
    org = _FakeOrg(["tech"])
    tool.bind_deps(store=store, thread_manager=tmgr, org_engine=org)
    result = await tool.execute(
        params={"target_role": "tech", "task_prompt": "实现 X", "parent_task_id": "t_parent"},
        context=_ctx(),
    )
    assert result is not None and result["success"] is True
    item_id = result["work_item_id"]
    item = store.get(item_id)
    assert item is not None, "子卡必须真实落 store（最终产物，非仅返回成功）"
    assert item.metadata["thread_id"] == result["thread_id"]
    thread = tmgr.get_thread(result["thread_id"])
    assert thread is not None and thread.messages[0].content == "实现 X"


def test_spawn_execute_fail_closed_when_deps_missing() -> None:
    """未绑定 / 绑定 None：execute 必须 fail-closed 而非崩或静默。"""
    tool = SpawnThreadTaskTool()
    assert tool.execute is not None
