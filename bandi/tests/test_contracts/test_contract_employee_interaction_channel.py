# 功能：契约——员工链路交互通道（多问卡/权限卡）装配与广播
# 背景：员工（dispatcher tick）执行中 request_user_input 缺 ui 通道 → RuntimeError headless →
# 多问卡失败、执行中断。修法：seat_executor 经 interaction_provider 工厂为每轮建
# HTTPUserInteraction（登记到发起会话 origin_session_id）→ 帧经 /ws/company 广播 →
# 用户在前端答复 → confirm 端点唤醒 → 同 turn 继续执行（不中断）。
# 钉行为：
# ① run_agent 接受并透传 user_interaction（此前签名缺失 → TypeError，通道到不了工具）；
# ② run_turn 装配 provider 后以 origin_session_id 构建通道并透传（生产调用链）；
# ③ 破坏者：provider 抛异常 → 不阻断执行（user_interaction=None，工具层 fail-closed 自报）；
# ④ 破坏者：无 origin_session_id → 回落员工会话（member_session_id）；
# ⑤ 广播：交互帧经 _broadcast_interaction_frame 到达公司 WS 客户端（最终产物断言）；
# ⑥ 员工通道工厂绑定会话：ask_input 挂起登记在指定会话（confirm 端点可命中）。
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from bandi.company import seat_executor as se_mod
from bandi.company.seat_executor import EngineSeatExecutor
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _fake_runtime() -> Any:
    class _Runtime:
        role_id = "worker"
        employee_id = "e1"
        project_id = "p1"

        def member_session_id(self) -> str:
            return "role-runtime::p1::worker"

    return _Runtime()


def _spy(result: tuple[str, str, int, int] = ("完成输出", "COMPLETED", 0, 1)) -> tuple[dict, Any]:
    seen: dict[str, Any] = {}

    async def _run(**kwargs: Any) -> tuple[str, str, int, int]:
        seen.update(kwargs)
        return result

    return seen, _run


class TestRunAgentForwardsUserInteraction:
    """① 便利函数 run_agent 必须接受并透传 user_interaction（断链点：签名缺失 → TypeError）。"""

    def test_factory_run_agent_forwards_user_interaction(self) -> None:
        import bandi.agent.factory as factory_mod
        import bandi.core.agent_runner as runner_mod

        ui = object()  # 通道实例哨兵（断言同一对象到达 run_agent_turn）
        seen: dict[str, Any] = {}

        async def _spy_turn(**kwargs: Any) -> Any:
            seen.update(kwargs)
            return SimpleNamespace(
                reply="ok", terminal_reason="COMPLETED", total_tokens=0, turn_index=1
            )

        with patch.object(runner_mod, "run_agent_turn", _spy_turn):
            out = asyncio.run(factory_mod.run_agent(user_input="hi", user_interaction=ui))  # type: ignore[arg-type]

        assert seen.get("user_interaction") is ui, (
            "员工链 run_agent 必须把交互通道透传到 run_agent_turn（否则工具 fail-closed）"
        )
        assert out[0] == "ok"


class TestRunTurnInteractionChannel:
    """② run_turn 装配链：provider(origin_session_id) → 透传 run_agent。"""

    def test_run_turn_builds_interaction_from_origin_session(self) -> None:
        built: list[str] = []
        ui = object()

        def _provider(session_id: str) -> object:
            built.append(session_id)
            return ui

        executor = EngineSeatExecutor(interaction_provider=_provider)
        item = create_work_item(title="多问卡用例", turn_type=WorkItemTurnType.EXECUTE)
        item.assignee_role = "worker"
        item.metadata["origin_session_id"] = "sess-origin-9"
        seen, spy = _spy()

        with patch.object(se_mod, "run_agent", spy):
            result = asyncio.run(executor.run_turn(_fake_runtime(), item))

        assert result.success is True
        assert built == ["sess-origin-9"], "通道必须以发起会话构建（卡片落到用户现场）"
        assert seen.get("user_interaction") is ui, "通道必须透传到 run_agent（员工工具可达）"

    def test_missing_origin_falls_back_to_member_session(self) -> None:
        """④ 破坏者：无 origin_session_id（群聊广播派单）→ 回落员工会话。"""
        built: list[str] = []

        def _provider(session_id: str) -> object:
            built.append(session_id)
            return object()

        executor = EngineSeatExecutor(interaction_provider=_provider)
        item = create_work_item(title="回落用例", turn_type=WorkItemTurnType.EXECUTE)
        item.assignee_role = "worker"
        runtime = _fake_runtime()
        _, spy = _spy()

        with patch.object(se_mod, "run_agent", spy):
            result = asyncio.run(executor.run_turn(runtime, item))

        assert result.success is True
        assert built == [runtime.member_session_id()]

    def test_provider_build_failure_does_not_break_execution(self) -> None:
        """③ 破坏者：provider 抛异常 → 执行继续（user_interaction=None，工具层自报）。"""

        def _boom(_session_id: str) -> object:
            raise RuntimeError("boom")

        executor = EngineSeatExecutor(interaction_provider=_boom)
        item = create_work_item(title="容错用例", turn_type=WorkItemTurnType.EXECUTE)
        item.assignee_role = "worker"
        item.metadata["origin_session_id"] = "sess-x"
        seen, spy = _spy()

        with patch.object(se_mod, "run_agent", spy):
            result = asyncio.run(executor.run_turn(_fake_runtime(), item))

        assert result.success is True, "通道构建失败不得阻断执行链"
        assert seen.get("user_interaction") is None, "构建失败回落 None（工具层 fail-closed 自报）"

    def test_without_provider_keeps_headless_semantics(self) -> None:
        """未装配 provider（CLI/旧装配）→ 行为不变：user_interaction 缺失。"""
        executor = EngineSeatExecutor()
        item = create_work_item(title="裸装配用例", turn_type=WorkItemTurnType.EXECUTE)
        item.assignee_role = "worker"
        seen, spy = _spy()

        with patch.object(se_mod, "run_agent", spy):
            result = asyncio.run(executor.run_turn(_fake_runtime(), item))

        assert result.success is True
        assert seen.get("user_interaction") is None


class TestInteractionBroadcast:
    """⑤⑥ 广播出口与通道工厂（app 层装配件）。"""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        app_module = importlib.import_module("bandi.api.app")
        monkeypatch.setattr(app_module, "_interaction_store", None)
        yield

    def test_broadcast_interaction_frame_reaches_company_ws(self) -> None:
        app_module = importlib.import_module("bandi.api.app")

        store = app_module._get_interaction_store()
        item = store.register(
            session_id="sess-1", kind="input", payload={"prompt": "品类？"}, ttl_seconds=30
        )

        sent: list[dict[str, Any]] = []

        class _FakeWS:
            async def send_json(self, data: dict[str, Any]) -> None:
                sent.append(data)

        fake = _FakeWS()
        app_module._company_ws_clients.add(fake)
        try:

            async def _run() -> None:
                app_module._broadcast_interaction_frame(item)
                for _ in range(50):
                    await asyncio.sleep(0.02)
                    if sent:
                        break

            asyncio.run(_run())
        finally:
            app_module._company_ws_clients.discard(fake)

        assert sent, "交互帧必须广播到公司 WS（员工多问卡实时可见）"
        assert sent[0]["type"] == "interaction"
        assert sent[0]["data"]["interaction_id"] == item.interaction_id
        assert sent[0]["data"]["session_id"] == "sess-1"
        assert sent[0]["data"]["kind"] == "input"
        assert sent[0]["data"]["payload"]["prompt"] == "品类？"

    def test_broadcast_without_loop_is_silent(self) -> None:
        """⑥ 破坏者：同步上下文（无运行 loop）广播静默跳过不抛。"""
        app_module = importlib.import_module("bandi.api.app")

        store = app_module._get_interaction_store()
        item = store.register(session_id="sess-2", kind="input", payload={"prompt": "x"})
        app_module._broadcast_interaction_frame(item)

    def test_employee_provider_binds_session_and_suspends(self) -> None:
        """⑥ 工厂：产出的通道把提问登记到指定会话（confirm 端点的归属键）。"""
        app_module = importlib.import_module("bandi.api.app")
        from bandi.core.user_interaction import HTTPUserInteraction

        ui = app_module._employee_interaction_provider("sess-emp-1")
        assert isinstance(ui, HTTPUserInteraction)

        async def _run() -> None:
            task = asyncio.create_task(ui.ask_input("品类？", choices=["A", "B"]))
            await asyncio.sleep(0)
            pending = app_module._get_interaction_store().list_pending(session_id="sess-emp-1")
            assert len(pending) == 1, "提问必须登记到指定会话（用户答复可经 confirm 命中）"
            app_module._get_interaction_store().resolve(pending[0].interaction_id, "A")
            assert await task == "A"

        asyncio.run(_run())
