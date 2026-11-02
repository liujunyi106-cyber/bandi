"""功能：spawn_thread_task 生产主链路端到端契约（DoD-12/§6a）。

重建根因：registry 裸注册 spawn 工具，公司链路（EngineSeatExecutor→run_agent→
run_agent_turn）必须在 registry 建成后 bind_deps，否则工具对 LLM 可见但每次调用
必 fail-closed。本契约走真实装配链 + MockProvider 注入 spawn tool_call，断言最终
产物（子卡真实落 WorkItemStore + 线程真实收到产出回写消息 + 事件广播触发）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.llm.base import LLMResponse
from bandi.llm.mock import MockProvider


def _mock_provider_with_spawn() -> tuple[MockProvider, LLMResponse]:
    """MockProvider：第 1 响应下发 spawn_thread_task tool_call，第 2 响应终结。"""
    from bandi.core.message import ToolCall

    provider = MockProvider()
    spawn_call = LLMResponse(
        content="我将把任务委派给 tech",
        tool_calls=[
            ToolCall(
                id="call_spawn_1",
                name="spawn_thread_task",
                arguments={
                    "target_role": "tech",
                    "task_prompt": "设计用户画像 schema",
                    "parent_task_id": "parent_1",
                },
            )
        ],
        finish_reason="tool_calls",
    )
    finished = LLMResponse(content="委派完成", finish_reason="stop")
    provider.add_response(spawn_call)
    provider.add_response(finished)
    return provider, spawn_call


def _store_manager_pair(tmp_path: Path) -> tuple[Any, Any]:
    from bandi.company.discussion_thread import ThreadManager
    from bandi.company.store import WorkItemStore

    store = WorkItemStore(str(tmp_path / "e2e.db"))
    thread_mgr = ThreadManager(
        workspace=str(tmp_path / "ws"), project_id="company", session_id="default"
    )
    return store, thread_mgr


def _minimal_config() -> Any:
    from bandi.core.config import BandiConfig

    cfg = BandiConfig()
    cfg.llm.api_key = ""
    cfg.agent.max_iterations = 3
    return cfg


@pytest.mark.asyncio
async def test_run_agent_turn_spawns_work_item(tmp_path: Path) -> None:
    """run_agent_turn 装配链：LLM 发起 spawn 调用 → 子卡真实落库 + 线程 + 广播事件。"""
    from bandi.company.org_engine import OrgEngine
    from bandi.core.agent_runner import AgentTurnResult, run_agent_turn

    org = OrgEngine()
    org.register_role("tech", "技术", "负责技术方案设计与实现")
    provider, _ = _mock_provider_with_spawn()
    store, thread_mgr = _store_manager_pair(tmp_path)

    events: list[dict[str, Any]] = []

    def on_event(event: dict[str, Any]) -> None:
        events.append(event)

    thread_mgr.bind_on_message(on_event)

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path) + "_home")):
        with patch("bandi.core.agent_runner.build_provider", return_value=provider) as mock_build:
            result: AgentTurnResult = await run_agent_turn(
                user_input="给 tech 派个任务：设计用户画像 schema",
                session_id="e2e-session-1",
                bandi_config=_minimal_config(),
                company_store=store,
                company_thread_manager=thread_mgr,
                company_org_engine=org,
            )
    assert result.terminal_reason in {"VERIFIED", "TOOL_COMPLETED", "AGENT_DONE"}
    assert mock_build.call_count == 1

    items = store.list_all()
    assert len(items) == 1, "spawn 子卡必须真实落 store"
    child = items[0]
    assert child.title == "设计用户画像 schema"
    assert child.metadata.get("thread_id"), "子卡必须绑定线程"
    assert child.parent_id == "parent_1"
    tid = child.metadata["thread_id"]

    thread = thread_mgr.get_thread(tid)
    assert thread is not None, "spawn 必须真实创建/更新线程"
    assert thread.messages[0].content == "设计用户画像 schema"

    thread_event = [e for e in events if e["type"] == "thread_message"]
    assert any(e["thread_id"] == tid for e in thread_event), "must emit thread events"
