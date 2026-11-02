"""契约族：final_decider 显式化+ 输出契约阻断。

生产调用路径：
1. OrgEngine.register_role/get_final_decider_role_id——组织角色注册与决策者解析
2. OrgEngine._role_to_dict/restore——序列化往返（is_final_decider 保留）
3. seat_executor.EngineSeatExecutor.run_turn——输出契约校验阻断 SeatResult
4. company_mode.run_work_item——违规卡转 NEEDS_ATTENTION

断言「最终产物」：显式决策者优先 / 序列化往返保留 / 契约违规 success=False /
仅 warnings 放行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.seat_executor import EngineSeatExecutor, SeatResult
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


def test_final_decider_explicit_preferred() -> None:
    """显式 is_final_decider 角色优先于插入序 top-level。"""
    org = OrgEngine()
    # 先注册 A（top-level），再注册显式 B
    org.register_role("role-a", "A", responsibility="first")
    org.register_role("role-b", "B", responsibility="decider", is_final_decider=True)
    assert org.get_final_decider_role_id() == "role-b", "显式决策者必须优先"


def test_final_decider_fallback_top0() -> None:
    """无显式标记时回退插入序第一个 top-level（向后兼容，builtin ceo 优先）。"""
    org = OrgEngine()
    org.register_role("role-a", "A")
    org.register_role("role-b", "B")
    assert org.get_final_decider_role_id() == "ceo", "无显式时回退 builtin 首个 top-level"


def test_final_decider_serialization_roundtrip() -> None:
    """is_final_decider 经 _role_to_dict/from_dict 往返保留。"""
    org = OrgEngine()
    org.register_role("role-x", "X", responsibility="r", is_final_decider=True)
    role = org.get_role("role-x")
    assert role is not None
    payload = OrgEngine._role_to_dict(role)
    restored = OrgEngine._role_from_dict(payload)
    assert restored.is_final_decider is True, "序列化往返必须保留 is_final_decider"


def test_final_decider_restore_from_store(tmp_path: Path) -> None:
    """restore（store 加载）保留 is_final_decider。"""
    store = WorkItemStore(str(tmp_path / "org.db"))
    org = OrgEngine()
    org.register_role("role-y", "Y", is_final_decider=True)
    org.persist(store)
    org2 = OrgEngine(persist_store=store)
    assert org2.get_final_decider_role_id() == "role-y", "store 加载后决策者必须保留"


def test_final_decider_org_json_roundtrip(tmp_path: Path, monkeypatch: Any) -> None:
    """org.json（CLI/API 主装配路径）持久化保留 is_final_decider + tool_tags。"""
    from bandi.company.org_persistence import load_org, save_org
    from bandi.company.recruiter import Recruiter

    org = OrgEngine()
    org.register_role("role-z", "Z", is_final_decider=True, tool_tags=["admin"])
    recruiter = Recruiter()

    json_path = tmp_path / "org.json"
    save_org(org, recruiter, json_path)
    # 加载到新 engine——is_final_decider/tool_tags 必须保留
    org2 = OrgEngine()
    load_org(org2, Recruiter(), json_path)
    role = org2.get_role("role-z")
    assert role is not None
    assert role.is_final_decider is True, "org.json 往返必须保留 is_final_decider"
    assert role.tool_tags == ["admin"], "org.json 往返必须保留 tool_tags"
    assert org2.get_final_decider_role_id() == "role-z", "加载后决策者必须保留"


def _mk_work_item(**kw: Any) -> Any:
    item = create_work_item(
        title=kw.get("title", "t"),
        turn_type=kw.get("turn_type", WorkItemTurnType.EXECUTE),
        description=kw.get("description", ""),
    )
    item.runtime_metadata["_last_output"] = kw.get("output", "output text")
    return item


def _mk_executor() -> EngineSeatExecutor:
    return EngineSeatExecutor(communication=None, org_engine=None)


def _mk_runtime() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        member_session_id=lambda: "sess-1",
        project_id="",
        employee_id="",
        role_id="worker",
    )


@pytest.mark.asyncio
async def test_output_contract_violation_blocks_success(monkeypatch: Any) -> None:
    """硬约束失败（required_keys 缺失）→ SeatResult.success=False + violation 元数据。"""
    executor = _mk_executor()
    # turn_type=report → 契约 required_keys=("summary")，输出不含 summary 即违规
    item = _mk_work_item(output="no required content at all", turn_type=WorkItemTurnType.REPORT)
    from bandi.company.output_contract import validate_work_item_output

    report = validate_work_item_output(item)
    assert not report.valid, "缺失 required_key 必须判定不合法"
    assert "summary" in report.missing_keys

    # 用 mock 让 run_agent 直接返回（验证 run_turn 的契约阻断逻辑）
    async def fake_run_agent(**kw: Any) -> tuple[str, str, int, int]:
        return "no required content at all", "COMPLETED", 10, 1

    monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)
    result = await executor.run_turn(runtime=_mk_runtime(), work_item=item)
    assert result.success is False, "契约违规必须阻断 success"
    assert "output_contract_violation" in (result.metadata or {}), "必须携带违规元数据"
    assert "summary" in str(result.output)


@pytest.mark.asyncio
async def test_output_contract_warnings_only_pass(monkeypatch: Any) -> None:
    """仅 warnings（如格式提示）不阻断——success 保持 True。"""
    executor = _mk_executor()
    # turn_type=deliver → 契约 format_spec=markdown，无 required_keys；
    # markdown 无标题只是 warning，不阻断
    item = _mk_work_item(output="plain text no heading", turn_type=WorkItemTurnType.DELIVER)
    from bandi.company.output_contract import validate_work_item_output

    report = validate_work_item_output(item)
    assert report.valid is True, "格式提示是 warning 不应阻断"
    assert report.warnings, "markdown 无标题应进 warnings"

    async def fake_run_agent(**kw: Any) -> tuple[str, str, int, int]:
        return "plain text no heading", "COMPLETED", 10, 1

    monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)
    result = await executor.run_turn(runtime=_mk_runtime(), work_item=item)
    assert result.success is True, "仅 warnings 不得阻断"
    assert "output_contract_warnings" in (result.metadata or {}), "warnings 应入 metadata"


def test_dispatcher_moves_violation_card_to_needs_attention(tmp_path: Path) -> None:
    """run_work_item 对 output_contract_violation 卡转 NEEDS_ATTENTION（真实路径）。"""
    import asyncio

    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.seat_executor import SeatResult

    class ViolatingSeat:
        async def prepare_seat(self, runtime: Any, work_item: Any) -> None:
            return None

        async def run_turn(self, runtime: Any, work_item: Any) -> SeatResult:
            # 产出不含 report 契约 required_key "summary" → 违规
            work_item.runtime_metadata["_last_output"] = "no summary at all"
            return SeatResult(
                success=False,
                output="输出契约校验失败: ...",
                metadata={
                    "output_contract_violation": {
                        "missing_keys": ["summary"],
                        "format_errors": [],
                        "warnings": [],
                    },
                    "_original_output": "no summary at all",
                },
            )

        async def interrupt(self, runtime: Any) -> None:
            return None

        async def checkpoint(self, runtime: Any) -> dict:
            return {}

    store = WorkItemStore(str(tmp_path / "wi.db"))
    item = create_work_item(title="t", turn_type=WorkItemTurnType.REPORT, assignee_role="worker")
    item.phase = Phase.READY
    store.save(item)

    disp = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=OrgEngine(),
        communication=None,
        seat_executor=ViolatingSeat(),
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    # 跳过 task_preparer（无 recruiter seat 时 prepare 会查 get_seat 失败）
    disp.task_preparer = None  # type: ignore[assignment]
    result = asyncio.run(disp.run_work_item(item.id))
    assert result.success is False, "违规执行必须失败"
    final = store.get(item.id)
    assert final is not None
    assert final.phase == Phase.NEEDS_ATTENTION, "违规卡必须转 NEEDS_ATTENTION"
    assert final.runtime_metadata.get("_last_output") == "no summary at all", (
        "违规卡必须保留原始产物（非错误文案）"
    )


def test_retry_work_item_recovers_needs_attention(tmp_path: Path) -> None:
    """NEEDS_ATTENTION 卡经 retry_work_item 转 READY（恢复路径，防死卡）。"""
    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig

    store = WorkItemStore(str(tmp_path / "wi.db"))
    item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker")
    item.phase = Phase.NEEDS_ATTENTION
    store.save(item)

    disp = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=OrgEngine(),
        communication=None,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    retried = disp.retry_work_item(item.id)
    assert retried is not None
    assert retried.phase == Phase.READY, "retry 必须把 NEEDS_ATTENTION 转 READY"

    # 非 NEEDS_ATTENTION 卡 retry 拒绝
    item2 = create_work_item(title="t2", turn_type=WorkItemTurnType.EXECUTE)
    item2.phase = Phase.RUNNING
    store.save(item2)
    try:
        disp.retry_work_item(item2.id)
        assert False, "非 NEEDS_ATTENTION 卡 retry 必须拒绝"
    except ValueError:
        pass


@pytest.mark.asyncio
async def test_output_contract_violation_emits_event_when_bus(monkeypatch: Any) -> None:
    """seat_executor 注入 bus 时，违规 emit OUTPUT_CONTRACT_VIOLATION 事件。"""
    from bandi.core.event_bus import EventBus
    from bandi.core.events import Event, EventType

    bus = EventBus()
    emitted: list[Event] = []

    async def collect(e: Event) -> None:
        emitted.append(e)

    await bus.subscribe(EventType.OUTPUT_CONTRACT_VIOLATION, collect)

    executor = EngineSeatExecutor(communication=None, org_engine=None, event_bus=bus)
    item = _mk_work_item(output="no required content at all", turn_type=WorkItemTurnType.REPORT)

    async def fake_run_agent(**kw: Any) -> tuple[str, str, int, int]:
        return "no required content at all", "COMPLETED", 10, 1

    monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)
    result = await executor.run_turn(runtime=_mk_runtime(), work_item=item)
    assert result.success is False
    assert len(emitted) == 1, "违规必须 emit OUTPUT_CONTRACT_VIOLATION 事件"
    assert emitted[0].type == EventType.OUTPUT_CONTRACT_VIOLATION
    assert emitted[0].data["work_item_id"] == item.id
    await bus.close()


def test_runtime_cache_hit_recovers_stale_claims(tmp_path: Path) -> None:
    """缓存命中 build_org_runtime 也跑 recover_stale_claims（跨进程 stale 卡恢复）。"""
    from bandi.company.company_runtime_profiles import RoleProfile
    from bandi.company.custom_runtime import CustomRuntimeRunner, RuntimeHost
    from bandi.company.store import WorkItemStore

    store = WorkItemStore(str(tmp_path / "wi.db"))
    runner = CustomRuntimeRunner(RuntimeHost(store=store, project_id="p", session_id="s"))
    roles = [
        RoleProfile(id="manager", name="Manager", reports_to=None, can_spawn=["worker"]),
        RoleProfile(id="worker", name="Worker", reports_to="manager"),
    ]
    b1 = runner.build_org_runtime(roles=roles)
    # 第二次调用命中缓存——recover_stale_claims 幂等执行
    b2 = runner.build_org_runtime(roles=roles)
    assert b1.dispatcher is b2.dispatcher, "同 key 必须复用缓存实例"
    runner.invalidate()


@pytest.mark.asyncio
async def test_output_contract_validator_crash_blocks_success(monkeypatch: Any) -> None:
    """破坏者：validate_work_item_output 抛异常（校验器崩溃）→ fail-closed 阻断
    不得静默放行成 success=True（注释承诺「校验失败按违规处理」必须成立）。"""
    executor = _mk_executor()
    item = _mk_work_item(output="任何内容", turn_type=WorkItemTurnType.REPORT)

    async def fake_run_agent(**kw: Any) -> tuple[str, str, int, int]:
        return "任何内容", "COMPLETED", 10, 1

    def crasher(_item: Any) -> Any:
        raise RuntimeError("validator exploded")

    monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)
    monkeypatch.setattr("bandi.company.seat_executor.validate_work_item_output", crasher)
    result = await executor.run_turn(runtime=_mk_runtime(), work_item=item)

    assert result.success is False, "校验器异常必须 fail-closed 阻断"
    violation = (result.metadata or {}).get("output_contract_violation")
    assert violation is not None, "必须携带违规元数据供调度方处置"
    assert any("校验器" in e for e in violation["format_errors"]), violation
