"""契约族：多团队互 route 同时跑（单 org 内多 team 隔离）。

生产调用路径（DoD-10 族级覆盖）：
1. session_scoping.scoped_member_session_id——公司运行时成员会话 key（team 维度）
2. session_scoping.scoped_queue_key——role 队列 key（team 维度）
3. CompanyRuntime.enqueue_runnable_work_items / pop_next_work_item_id——调度队列入/出
4. CompanyRuntime.claim_work_item——认领后成员会话聚焦（team 维度）
5. company_runtime.canonical_role_session_id / parse_role_session_id——角色会话 id
6. OrgEngine.register_role + persist/restore——组织重建后 home team 不丢
7. CompanyDispatcher._build_runtime——公司运行时装配点（team 身份透传）

断言「行为/最终产物」而非实现：跨 team 同 role 的会话/队列/认领互不串
（fail-closed），组织重建后 team 归属保留，旧 payload 兼容。
"""

from __future__ import annotations

from pathlib import Path

from bandi.company.company_mode import CompanyDispatcher
from bandi.company.company_runtime import (
    CompanyRuntime,
    canonical_role_session_id,
    parse_role_session_id,
)
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.session_scoping import scoped_member_session_id, scoped_queue_key
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _mk_store(tmp_path: Path) -> WorkItemStore:
    return WorkItemStore(str(tmp_path / "wi.db"))


def test_scoped_keys_isolate_by_team() -> None:
    """跨 team 同 role 的成员会话/队列 key 必须互异（互 route 不串的 key 空间保证）。"""
    session_a = scoped_member_session_id(
        project_id="proj",
        session_scope_id="scope1",
        role_id="engineer",
        employee_id="alice",
        team_instance_id="team-a",
    )
    session_b = scoped_member_session_id(
        project_id="proj",
        session_scope_id="scope1",
        role_id="engineer",
        employee_id="alice",
        team_instance_id="team-b",
    )
    assert session_a != session_b
    assert "team-a" in session_a
    assert "team-b" in session_b

    queue_a = scoped_queue_key(
        session_scope_id="scope1", role_id="engineer", team_instance_id="team-a"
    )
    queue_b = scoped_queue_key(
        session_scope_id="scope1", role_id="engineer", team_instance_id="team-b"
    )
    assert queue_a != queue_b
    assert queue_a == "scope1::team-a::engineer"
    assert queue_b == "scope1::team-b::engineer"


def test_org_role_team_id_roundtrip_after_rebuild(tmp_path: Path) -> None:
    """register_role(team_id) 后经 persist/restore 组织重建，home team 归属不丢；
    旧 payload 无 team_id 键时兼容为空串（不破坏既有 org.json）。"""
    store = _mk_store(tmp_path)
    org = OrgEngine()
    org.register_role(role_id="engineer", name="工程师", team_id="team-a", reports_to=None)
    org.register_role(role_id="qa", name="QA", team_id="team-b", reports_to=None)

    org.persist(store)
    rebuilt = OrgEngine()
    rebuilt.restore(store)
    by_id = {r.id: r for r in rebuilt.list_roles()}
    assert by_id["engineer"].team_id == "team-a"
    assert by_id["qa"].team_id == "team-b"

    legacy_payload = OrgEngine._role_to_dict(by_id["engineer"])  # type: ignore[attr-defined]
    legacy_payload.pop("team_id", None)
    loaded = OrgEngine._role_from_dict(legacy_payload)  # type: ignore[attr-defined]
    assert loaded.team_id == ""


def _mk_team_runtime(team_id: str) -> CompanyRuntime:
    return CompanyRuntime(
        project_id="proj",
        session_scope_id="company-runtime",
        role_id="",
        employee_id="",
        team_instance_id=team_id,
    )


def test_runtime_queues_isolate_by_home_team(tmp_path: Path) -> None:
    """同 runtime 内两队同层角色：各自队列互不可见（A 的 engineer 只 pop A 队卡）。"""
    store = _mk_store(tmp_path)
    runtime = _mk_team_runtime("")  # 容器 runtime，拥有双 team 归属表
    runtime.set_home_team("engineer", "team-a")
    runtime.set_home_team("qa", "team-b")

    card_a = create_work_item(title="team-a card", turn_type=WorkItemTurnType.EXECUTE)
    card_a.assignee_role = "engineer"
    card_a.phase = Phase.READY
    store.save(card_a)
    card_b = create_work_item(title="team-b card", turn_type=WorkItemTurnType.EXECUTE)
    card_b.assignee_role = "qa"
    card_b.phase = Phase.READY
    store.save(card_b)

    runtime.enqueue_runnable_work_items([card_a, card_b])
    assert runtime.pop_next_work_item_id("engineer") == card_a.id
    assert runtime.pop_next_work_item_id("engineer") is None, (
        "engineer 归属 team-a，不得 pop 到 team-b 的 qa 卡"
    )
    assert runtime.pop_next_work_item_id("qa") == card_b.id
    assert runtime.pop_next_work_item_id("qa") is None


def test_role_session_id_is_team_scoped_and_parse_compatible() -> None:
    """同 run 同 role 不同 team 的角色会话 id 必须互异；parse 兼容新（3段）/旧（2段）。"""
    run_id = "run-1"
    sid_a = canonical_role_session_id(run_id=run_id, role_id="engineer", team_instance_id="team-a")
    sid_b = canonical_role_session_id(run_id=run_id, role_id="engineer", team_instance_id="team-b")
    assert sid_a != sid_b
    assert parse_role_session_id(sid_a) == (run_id, "team-a::engineer")
    assert parse_role_session_id(sid_b) == (run_id, "team-b::engineer")
    legacy = canonical_role_session_id(run_id=run_id, role_id="engineer")
    assert parse_role_session_id(legacy) == (run_id, "engineer")
    assert parse_role_session_id("not-a-role-session") is None


def test_member_session_and_claim_isolated_by_team() -> None:
    """CompanyRuntime 带 team 身份时：成员会话 key 含 team；两队同 role 各认领各卡，聚焦互不串。"""
    runtime_a = _mk_team_runtime("team-a")
    runtime_b = _mk_team_runtime("team-b")
    sid_a = runtime_a.member_session_id()
    sid_b = runtime_b.member_session_id()
    assert sid_a != sid_b
    assert "team-a" in sid_a and "team-b" in sid_b

    card_a = create_work_item(title="a", turn_type=WorkItemTurnType.EXECUTE)
    card_a.assignee_role = "engineer"
    card_a.phase = Phase.RUNNING
    card_b = create_work_item(title="b", turn_type=WorkItemTurnType.EXECUTE)
    card_b.assignee_role = "engineer"
    card_b.phase = Phase.RUNNING
    runtime_a.claim_work_item(card_a.id, role_id="engineer")
    runtime_b.claim_work_item(card_b.id, role_id="engineer")
    session_a = runtime_a.ensure_role_member_session("engineer")
    session_b = runtime_b.ensure_role_member_session("engineer")
    assert session_a.member_session_id != session_b.member_session_id
    assert session_a.focused_work_item_id == card_a.id
    assert session_b.focused_work_item_id == card_b.id
    assert session_a.focused_work_item_id != session_b.focused_work_item_id


def test_dispatcher_runtime_build_embeds_team(tmp_path: Path) -> None:
    """CompanyDispatcher._build_runtime 装配点支持 team 身份透传（互 route 同时跑→
    每 team 运行时身份独立）。"""
    store = _mk_store(tmp_path)
    org = OrgEngine()
    dispatcher = CompanyDispatcher(store=store, recruiter=None, org_engine=org)  # type: ignore[arg-type]
    runtime = dispatcher._build_runtime(  # type: ignore[attr-defined]
        session_scope_id="s1",
        role_id="engineer",
        employee_id="engineer",
        team_instance_id="team-a",
    )
    assert isinstance(runtime, CompanyRuntime)
    assert "team-a" in runtime.member_session_id()
