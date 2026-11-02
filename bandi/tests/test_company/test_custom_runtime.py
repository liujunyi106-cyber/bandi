from __future__ import annotations

import asyncio

import pytest

from bandi.company.company_runtime_profiles import RoleProfile
from bandi.company.custom_runtime import CustomRuntimeRunner, OrgRuntimeBundle, RuntimeHost
from bandi.company.recruiter import CompanyRecruiter, RecruitStatus
from bandi.company.store import WorkItemStore
from bandi.company.talent_market import TalentMarket
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


def _roles() -> list[RoleProfile]:
    return [
        RoleProfile(id="manager", name="Manager", reports_to=None, can_spawn=["worker"]),
        RoleProfile(id="worker", name="Worker", reports_to="manager"),
    ]


@pytest.fixture
def store(tmp_path):
    return WorkItemStore(str(tmp_path / "custom_runtime.db"))


@pytest.fixture
def host(store, tmp_path) -> RuntimeHost:
    return RuntimeHost(
        store=store,
        project_id="parent-project",
        workspace=str(tmp_path),
        session_id="parent-session",
    )


@pytest.fixture
def runner(host) -> CustomRuntimeRunner:
    return CustomRuntimeRunner(host)


class TestConstruction:
    def test_holds_parent(self, runner, host) -> None:
        assert runner.parent is host

    def test_default_org_id(self, runner, host) -> None:
        bundle = runner.build_org_runtime(roles=[])
        assert bundle.org_id == "custom"


class TestBuildOrgRuntime:
    def test_roles_registered(self, runner) -> None:
        bundle = runner.build_org_runtime(roles=_roles())
        assert bundle.org_engine.get_role("manager") is not None
        assert bundle.org_engine.get_role("worker") is not None

    def test_keeps_hierarchy(self, runner) -> None:
        bundle = runner.build_org_runtime(roles=_roles())
        worker = bundle.org_engine.get_role("worker")
        assert worker is not None
        assert worker.reports_to == "manager"
        manager = bundle.org_engine.get_role("manager")
        assert manager is not None
        assert manager.can_spawn == ["worker"]

    def test_seats_registered_for_all_roles(self, runner) -> None:
        bundle = runner.build_org_runtime(roles=_roles())
        for role in bundle.org_engine.list_roles():
            seat = bundle.recruiter.get_seat(role.id)
            assert seat is not None
            assert seat.status == RecruitStatus.AVAILABLE

    def test_communication_uses_parent_project(self, runner) -> None:
        bundle = runner.build_org_runtime(roles=_roles())
        assert bundle.communication.project_id == "parent-project"

    def test_dispatcher_wired(self, runner, store) -> None:
        bundle = runner.build_org_runtime(roles=_roles())
        assert bundle.dispatcher.store is store
        assert bundle.dispatcher.org_engine is bundle.org_engine
        assert bundle.dispatcher.recruiter is bundle.recruiter
        assert bundle.dispatcher.communication is bundle.communication

    def test_dispatcher_has_seat_executor(self, runner) -> None:
        """
        否则 secretary 链创建的 work item 无法真正执行（run_work_item 恒拒绝）。"""
        bundle = runner.build_org_runtime(roles=_roles())
        assert bundle.dispatcher.seat_executor is not None, (
            "自定义运行时 dispatcher 必须配置 seat_executor"
        )

    def test_org_id_recorded(self, runner) -> None:
        bundle = runner.build_org_runtime(roles=_roles(), org_id="acme")
        assert bundle.org_id == "acme"

    def test_build_is_isolated_from_parent(self, runner, host) -> None:
        bundle = runner.build_org_runtime(roles=_roles())
        assert host.store is not None
        assert bundle.dispatcher.store is host.store
        assert not hasattr(host, "org_engine")

    def test_builds_independent_instances(self, runner) -> None:
        first = runner.build_org_runtime(roles=_roles())
        second = runner.build_org_runtime(roles=_roles())
        assert first.org_engine is second.org_engine
        assert first.dispatcher is second.dispatcher
        # 不同 roles 拓扑 → 不同缓存实例
        other = runner.build_org_runtime(roles=[RoleProfile(id="solo", name="Solo")])
        assert other.org_engine is not first.org_engine

    def test_same_role_id_overrides(self, runner) -> None:
        roles = [
            RoleProfile(id="manager", name="Manager"),
            RoleProfile(id="manager", name="New Manager"),
        ]
        bundle = runner.build_org_runtime(roles=roles)
        role = bundle.org_engine.get_role("manager")
        assert role is not None
        assert role.name == "New Manager"

    def test_empty_roles_keeps_builtin_roles(self, runner) -> None:
        bundle = runner.build_org_runtime(roles=[])
        assert bundle.org_engine.get_role("ceo") is not None

    def test_custom_project_override(self, runner) -> None:
        bundle = runner.build_org_runtime(
            roles=_roles(),
            project_id="custom-project",
            session_id="custom-session",
        )
        assert bundle.communication.project_id == "custom-project"

    def test_recruiter_planner_wired_with_org_and_market(self, runner) -> None:
        market = TalentMarket(templates_dir="/nonexistent")
        bundle = runner.build_org_runtime(roles=_roles(), talent_market=market)
        assert isinstance(bundle.recruiter_planner, CompanyRecruiter)
        assert bundle.recruiter_planner is not None
        assert bundle.recruiter_planner.org_engine is bundle.org_engine
        assert bundle.recruiter_planner.talent_market is market

    def test_recruiter_planner_default_market(self, runner) -> None:
        bundle = runner.build_org_runtime(roles=_roles())
        assert bundle.recruiter_planner is not None
        assert bundle.recruiter_planner.talent_market is not None

    def test_talent_market_bound_to_org_engine(self, runner) -> None:
        """默认装配的 TalentMarket 必须绑定 org_engine——否则 list_installed_packages
        恒返回空（DoD-11 装配点接线完整性）。"""
        bundle = runner.build_org_runtime(roles=_roles())
        market = bundle.recruiter_planner.talent_market
        assert market is not None
        assert market._org_engine is bundle.org_engine

    def test_talent_market_custom_instance_bound_to_org_engine(self, runner) -> None:
        """传入的自定义 TalentMarket 同样必须绑定 org_engine。"""
        market = TalentMarket(templates_dir="/nonexistent")
        bundle = runner.build_org_runtime(roles=_roles(), talent_market=market)
        assert market._org_engine is bundle.org_engine


class TestProcessMessage:
    def test_no_executor_default_chain_echoes(self, runner) -> None:
        result = asyncio.run(runner.process_message("hi", roles=_roles()))
        assert result == "hi"

    def test_secretary_relative_dir_constant(self) -> None:
        from bandi.company.custom_runtime import _SECRETARY_RELATIVE_DIR

        assert _SECRETARY_RELATIVE_DIR == (".bandi", "company", "secretary")

    def test_executor_invoked_with_bundle_and_content(self, runner) -> None:
        captured: list[tuple[OrgRuntimeBundle, str]] = []

        async def executor(bundle: OrgRuntimeBundle, content: str) -> str:
            captured.append((bundle, content))
            return f"echo:{content}"

        result = asyncio.run(runner.process_message("hello", roles=_roles(), executor=executor))
        assert result == "echo:hello"
        assert captured[0][1] == "hello"

    def test_executor_receives_isolated_bundle(self, runner) -> None:
        captured: list[OrgRuntimeBundle] = []

        async def executor(bundle: OrgRuntimeBundle, content: str) -> str:
            captured.append(bundle)
            return "ok"

        asyncio.run(runner.process_message("hi", roles=_roles(), executor=executor))
        assert captured[0].dispatcher is not None
        assert captured[0].org_engine is not None

    def test_executor_can_write_to_shared_store(self, runner, store) -> None:
        async def executor(bundle: OrgRuntimeBundle, content: str) -> str:
            item = create_work_item(
                title="from custom runtime",
                turn_type=WorkItemTurnType.EXECUTE,
                assignee_role="worker",
            )
            bundle.dispatcher.store.save(item)
            return item.id

        result = asyncio.run(runner.process_message("hi", roles=_roles(), executor=executor))
        assert store.get(result) is not None

    def test_project_override_reaches_bundle(self, runner) -> None:
        captured: list[OrgRuntimeBundle] = []

        async def executor(bundle: OrgRuntimeBundle, content: str) -> str:
            captured.append(bundle)
            return "ok"

        asyncio.run(
            runner.process_message(
                "hi",
                roles=_roles(),
                project_id="override-project",
                executor=executor,
            )
        )
        assert captured[0].communication.project_id == "override-project"

    def test_releases_seats_after_executor(self, runner) -> None:
        captured: list[OrgRuntimeBundle] = []

        async def executor(bundle: OrgRuntimeBundle, content: str) -> str:
            captured.append(bundle)
            bundle.dispatcher.claim("worker")
            assert bundle.dispatcher.is_claimed("worker")
            return "ok"

        asyncio.run(runner.process_message("hi", roles=_roles(), executor=executor))
        # 显式 invalidate 才释放
        assert captured[0].dispatcher.is_claimed("worker") is True
        runner.invalidate()
        assert captured[0].dispatcher.is_claimed("worker") is False

    def test_releases_seats_even_on_executor_error(self, runner) -> None:
        captured: list[OrgRuntimeBundle] = []

        async def executor(bundle: OrgRuntimeBundle, content: str) -> str:
            captured.append(bundle)
            bundle.dispatcher.claim("worker")
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            asyncio.run(runner.process_message("hi", roles=_roles(), executor=executor))
        assert captured[0].dispatcher.is_claimed("worker") is True
        runner.invalidate()
        assert captured[0].dispatcher.is_claimed("worker") is False


class TestSecretaryChain:
    def test_no_llm_does_not_create_work_item(self, runner, store) -> None:
        result = asyncio.run(runner.process_message("hello", roles=_roles()))
        assert result == "hello"
        assert len(store.list_all()) == 0

    def test_creates_work_item_from_secretary_action(self, runner, store) -> None:
        payload = (
            '{"response": "queued", "actions": [{"kind": "create_work_item", '
            '"title": "write docs", "turn_type": "execute", "assignee_role": "worker"}]}'
        )
        llm = MockProvider()
        llm.add_response(LLMResponse(content=payload))
        result = asyncio.run(runner.process_message("create a task", roles=_roles(), llm=llm))
        assert result.startswith("queued")
        assert "created work item(s):" in result
        assert len(store.list_all()) == 1
        item = store.get(result.rsplit(": ", 1)[-1].strip("]"))
        assert item is not None
        assert item.title == "write docs"
        assert item.assignee_role == "worker"

    def test_creates_work_item_under_project_override(self, runner, store) -> None:
        payload = (
            '{"response": "ok", "actions": [{"kind": "create_work_item", '
            '"title": "task", "turn_type": "review", "assignee_role": "manager"}]}'
        )
        llm = MockProvider()
        llm.add_response(LLMResponse(content=payload))
        result = asyncio.run(
            runner.process_message(
                "create a task",
                roles=_roles(),
                llm=llm,
                project_id="override-project",
            )
        )
        assert len(store.list_all()) == 1
        assert "created work item(s):" in result

    def test_releases_seats_after_default_chain(self, runner, store) -> None:
        payload = (
            '{"response": "ok", "actions": [{"kind": "create_work_item", '
            '"title": "task", "turn_type": "execute", "assignee_role": "worker"}]}'
        )
        llm = MockProvider()
        llm.add_response(LLMResponse(content=payload))
        asyncio.run(runner.process_message("create a task", roles=_roles(), llm=llm))
        assert not runner.build_org_runtime(roles=_roles()).dispatcher.is_claimed("worker")

    def test_created_id_extraction_tolerates_extra_backticks(self, runner, store) -> None:
        from bandi.company.custom_runtime import _extract_created_work_item_ids

        assert _extract_created_work_item_ids(["created work item `id-1` (`extra`)"]) == ["id-1"]
        assert _extract_created_work_item_ids(["created work item `id-2`"]) == ["id-2"]
        assert _extract_created_work_item_ids(["unrelated action text"]) == []

    def test_default_chain_releases_seats_in_finally(self, runner, store) -> None:
        calls: list[int] = []
        original = runner.build_org_runtime

        def build_with_tracked_release(*args, **kwargs):
            bundle = original(*args, **kwargs)
            bundle.dispatcher.release_all = lambda: calls.append(1)  # type: ignore[method-assign]
            return bundle

        runner.build_org_runtime = build_with_tracked_release  # type: ignore[method-assign]
        result = asyncio.run(runner.process_message("hello", roles=_roles()))
        assert result == "hello"
        assert calls == []
        runner.invalidate()
        assert calls == [1]
