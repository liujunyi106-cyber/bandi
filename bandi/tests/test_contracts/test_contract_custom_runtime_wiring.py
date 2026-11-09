"""
契约族：CustomRuntimeRunner 生产接线。

背景：CustomRuntimeRunner（自定义 org 会话入口，承载 Recruiter/Secretary 链/
org_work_item_planner 三引擎）此前生产零实例化——招募/秘书/规划引擎主链不可达。

本契约钉的行为族（生产调用路径）：
- `api/app.py::/api/company/custom/message`（生产入口——POST 自定义 org 会话消息）
- `company/custom_runtime.py::CustomRuntimeRunner.process_message`（委托链）

断言「最终产物」：
- 端点委托 CustomRuntimeRunner.process_message（接线断言——patch 后端点行为跟随）；
- 缺 content / roles → 400；
- 真实装配冒烟：CustomRuntimeRunner(host).build_org_runtime 可产出可用 dispatcher。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bandi.api.app import app  # noqa: F401 — 触发模块加载

app_module = sys.modules["bandi.api.app"]  # __init__ re-export 遮蔽子模块属性，取真实模块


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """端点路径隔离：Path.home→tmp + HOME/USERPROFILE env + 重置 runner 单例。

    端点直调会触发 `_get_custom_runtime_runner()` 首建（内部 Path.home()
    派生 ~/.bandi/company/company.db）——不隔离会写真实用户目录；env 同步
    因 os.path.expanduser("~") 不走 Path.home patch。
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(app_module, "_custom_runtime_runner", None)
    monkeypatch.setattr(app_module, "_company_dispatcher", None)


# Windows 下 TestClient 对本 app 静默崩（GPF），端点契约直接调 async
# 函数（接线断言等价：patch runner 后端点行为跟随）；HTTP 层由集成测试覆盖


def test_contract_custom_message_endpoint_delegates_to_runner(monkeypatch) -> None:
    """端点必须委托 CustomRuntimeRunner.process_message（接线契约 ）。"""

    captured: dict = {}

    async def fake_process(
        self,
        content,
        *,
        roles,
        profile=None,
        org_id=None,
        session_id=None,
        workspace=None,
        llm=None,
        executor=None,
    ):
        captured["content"] = content
        captured["role_ids"] = [r.id for r in roles]
        return "secretary-reply"

    monkeypatch.setattr(
        "bandi.company.custom_runtime.CustomRuntimeRunner.process_message",
        fake_process,
    )
    import asyncio

    resp = asyncio.run(
        app_module.company_custom_message(
            {
                "content": "帮我调研竞品",
                "roles": [{"id": "ceo", "name": "CEO"}, {"id": "worker", "name": "Worker"}],
                "org_id": "org-t1",
            }
        )
    )
    assert resp["output"] == "secretary-reply"
    assert captured["content"] == "帮我调研竞品"
    assert captured["role_ids"] == ["ceo", "worker"]


def test_contract_custom_message_endpoint_validates_payload() -> None:
    """缺 content / roles 必须 400（fail-closed，不静默空转）。"""
    import asyncio

    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        asyncio.run(app_module.company_custom_message({"roles": [{"id": "ceo"}]}))
    assert ei.value.status_code == 400, "缺 content 必须 400"
    with pytest.raises(HTTPException) as ei:
        asyncio.run(app_module.company_custom_message({"content": "hi"}))
    assert ei.value.status_code == 400, "缺 roles 必须 400"


def test_contract_custom_runner_build_org_runtime_smoke(tmp_path) -> None:
    """真实装配冒烟：RuntimeHost + build_org_runtime 产出可用 dispatcher（引擎可达）。"""

    from bandi.company.company_runtime_profiles import CompanyProfile, RoleProfile
    from bandi.company.custom_runtime import CustomRuntimeRunner, RuntimeHost
    from bandi.company.store import WorkItemStore

    store = WorkItemStore(str(tmp_path / "wi.db"))
    host = RuntimeHost(store=store, project_id="p1", workspace=str(tmp_path), session_id="s1")
    runner = CustomRuntimeRunner(host)
    roles = [
        RoleProfile(id="ceo", name="CEO", responsibility="决策"),
        RoleProfile(id="worker", name="Worker", responsibility="执行", reports_to="ceo"),
    ]
    bundle = runner.build_org_runtime(
        roles=roles, profile=CompanyProfile.CUSTOM, org_id="org-smoke"
    )
    assert bundle.dispatcher is not None and bundle.org_engine is not None
    assert bundle.recruiter_planner is not None, "规划引擎必须可达"
    bundle2 = runner.build_org_runtime(
        roles=roles, profile=CompanyProfile.CUSTOM, org_id="org-smoke"
    )
    assert bundle2 is bundle, "同 key 必须复用缓存实例"
    runner.shutdown()


def test_custom_runner_passes_interaction_dependencies(tmp_path) -> None:
    """custom runtime 的 seat_executor 必须拿到交互/槽位/产物依赖。"""
    from bandi.company.company_runtime_profiles import CompanyProfile, RoleProfile
    from bandi.company.custom_runtime import CustomRuntimeRunner, RuntimeHost
    from bandi.company.store import WorkItemStore

    store = WorkItemStore(str(tmp_path / "wi.db"))
    provider = object()
    slot = object()
    host = RuntimeHost(
        store=store,
        project_id="p1",
        workspace=str(tmp_path),
        session_id="s1",
        interaction_provider=provider,
        slot_answer_provider=slot,
        artifact_store=store,
    )
    runner = CustomRuntimeRunner(host)
    bundle = runner.build_org_runtime(
        roles=[RoleProfile(id="worker", name="Worker", responsibility="执行")],
        profile=CompanyProfile.CUSTOM,
        org_id="org-deps",
    )
    executor = bundle.dispatcher.seat_executor
    assert executor is not None
    assert executor._interaction_provider is provider
    assert executor._slot_answer_provider is slot
    assert executor._artifact_store is store
    runner.shutdown()


def test_custom_runner_routes_approval_resume_and_ticks(tmp_path) -> None:
    """custom 卡审批决策必须由 custom dispatcher 恢复并立即 tick。"""
    import asyncio
    from types import SimpleNamespace

    from bandi.company.custom_runtime import CustomRuntimeRunner, RuntimeHost
    from bandi.company.store import WorkItemStore

    class _Dispatcher:
        def __init__(self) -> None:
            self.resumed = 0
            self.ticked = 0

        def resume_after_approval(self, session_id: str, **kwargs) -> int:
            self.resumed += 1
            return 1

        async def tick(self) -> None:
            self.ticked += 1

    runner = CustomRuntimeRunner(RuntimeHost(store=WorkItemStore(str(tmp_path / "wi.db"))))
    dispatcher = _Dispatcher()
    runner._bundle_cache[("o", "p", "s", "", ())] = SimpleNamespace(dispatcher=dispatcher)

    resumed = asyncio.run(
        runner.resume_after_approval(
            "s",
            decision="allow_once",
            tool_name="write",
            note="ok",
            tool_call_id="call-1",
        )
    )
    assert resumed == 1
    assert dispatcher.resumed == 1
    assert dispatcher.ticked == 1, "恢复后必须立即 tick，否则卡停在 READY"
