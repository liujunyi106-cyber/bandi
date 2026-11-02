"""端到端（Mock）：组织构建全链闭环——草案→确认落地→立即可派单。

生产主链路：LLM 产 register_role 草案（自动度 B 不落地）→ confirm_org_build 落地
到 org_engine → 同一 org 实例直接派卡调度执行（无需重启/重建装配）。

断言「最终产物」：确认后角色/员工落地；落地角色当天即可派卡并完成执行
（run_work_item 成功 + _task_status=completed）。
"""

from __future__ import annotations

import asyncio
import json

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.org_engine import OrgEngine
from bandi.company.seat_executor import SeatResult
from bandi.company.seat_recruiter import Recruiter
from bandi.company.secretary import SecretaryService
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


class _StubExecutor:
    async def prepare_seat(self, runtime, item) -> None:
        return None

    async def run_turn(self, runtime, item) -> SeatResult:
        return SeatResult(success=True, output="done", metadata={"terminal_reason": "VERIFIED"})

    async def interrupt(self, runtime) -> None:
        return None


def _confirm_build(tmp_path, org: OrgEngine) -> None:
    llm = MockProvider()
    draft = json.dumps(
        {
            "response": "草案：新增 dev 角色",
            "actions": [
                {
                    "kind": "register_role",
                    "role_id": "dev",
                    "name": "Developer",
                    "responsibility": "Writes code",
                    "team_id": "team-a",
                }
            ],
        },
        ensure_ascii=False,
    )
    confirm = json.dumps(
        {
            "response": "已确认",
            "actions": [{"kind": "confirm_org_build", "draft_id": "latest"}],
        },
        ensure_ascii=False,
    )
    for resp in (draft, confirm):
        llm.add_response(LLMResponse(content=resp))
    secretary = SecretaryService(base_dir=str(tmp_path / "secretary"), llm=llm, org_engine=org)
    asyncio.run(secretary.handle_message("建团队", session_id="s1"))
    asyncio.run(secretary.handle_message("确认", session_id="s1"))


def test_org_build_chain_landed_role_dispatchable_immediately(tmp_path) -> None:
    org = OrgEngine()
    _confirm_build(tmp_path, org)
    role = org.get_role("dev")
    assert role is not None and role.team_id == "team-a", "确认后角色必须带 team 落地"

    store = WorkItemStore(str(tmp_path / "wi.db"))
    recruiter = Recruiter()
    recruiter.register_seat("dev")
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        seat_executor=_StubExecutor(),
        config=DispatcherConfig(max_concurrent=3),
    )
    assert dispatcher.claim("dev"), "落地角色席位可认领（生产主链路 claim→run）"

    card = create_work_item(title="落地后立刻派", turn_type=WorkItemTurnType.EXECUTE)
    card.assignee_role = "dev"
    store.save(card)
    result = asyncio.run(dispatcher.run_work_item(card.id))

    assert result.success, f"落地后立即可派单应成功：{result.output}"
    loaded = store.get(card.id)
    assert loaded is not None and loaded.runtime_metadata.get("_task_status") == "completed"


def test_org_build_chain_real_llm_draft_prompt_contract() -> None:
    """冒烟剧本契约：秘书系统提示词必须产出 register_role/confirm_org_build 动作
    并示范 team_id（供批次 A 真 LLM 双团队场景复用，防剧本与制品脱节）。"""
    system_prompt = SecretaryService._system_prompt()
    assert "register_role" in system_prompt
    assert "confirm_org_build" in system_prompt
    assert "team_id" in system_prompt
