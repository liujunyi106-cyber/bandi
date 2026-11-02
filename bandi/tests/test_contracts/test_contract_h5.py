from __future__ import annotations


class TestCollabDispatchWorkspaceFallback:
    def test_empty_workspace_falls_back_to_company_dir(self, tmp_path, monkeypatch):
        """
        workspace 回退 ~/.bandi/company——空串直通 CommunicationManager
        会把邮箱数据写到进程 CWD（泄漏）。"""
        import asyncio

        from bandi.tools.collaboration_dispatch import (
            build_collaboration_runtime_from_env,
        )

        env = {
            "BANDI_COMMS_PROJECT": "company",
            "BANDI_COMMS_SESSION": "default",
            "BANDI_COMMS_FROM": "worker",
            # 故意不设 BANDI_WORKSPACE_ROOT
        }
        monkeypatch.delenv("BANDI_WORKSPACE_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)

        _, ctx, _, comms, _org = asyncio.run(build_collaboration_runtime_from_env(env))
        ws = comms.workspace
        assert str(ws).strip() != "", "workspace 为空串"
        # 行为断言：回退目录 = home 派生的 company 目录（隔离环境下 home=tmp_path；
        from pathlib import Path

        assert Path(str(ws)) == Path.home() / ".bandi" / "company", f"非预期回退目录: {ws}"


class TestColleaguesUsesPersistedOrg:
    def test_colleagues_lists_custom_roles(self, tmp_path, monkeypatch):
        """
        空 OrgEngine 只有 builtin 九角色，用户 add-role 创建的角色不可见。"""
        import json as _json
        from unittest.mock import patch

        from typer.testing import CliRunner

        from bandi.cli.collab import collab_app

        home = tmp_path / "home"
        (home / ".bandi").mkdir(parents=True)
        # 直接用 org_persistence 落一份含自定义角色的 org.json
        from bandi.company.org_engine import OrgEngine
        from bandi.company.org_persistence import save_org
        from bandi.company.recruiter import Recruiter

        engine = OrgEngine()
        engine.register_role("my-custom-role", "Custom", "user-defined")
        with patch("pathlib.Path.home", return_value=home):
            save_org(engine, Recruiter())
            monkeypatch.setenv("BANDI_COMMS_FROM", "worker")
            runner = CliRunner()
            result = runner.invoke(collab_app, ["colleagues"])
        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output.strip().splitlines()[-1])
        assert "my-custom-role" in payload["roles"], f"持久化自定义角色不可见: {payload['roles']}"


def test_contract_pending_injection_sub_flushed_on_register():
    """
    _register_active_context 后被补挂到真实 bus——连接先于会话不漏首事件。"""
    import asyncio

    from bandi.core.agent_runner import (
        flush_pending_injection_subs,
        register_pending_injection_sub,
    )
    from bandi.core.event_bus import EventBus
    from bandi.core.events import Event, EventType

    received: list[Event] = []

    async def cb(event: Event) -> None:
        received.append(event)

    bus = EventBus()

    async def main():
        # 模拟 SSE 流先挂（ctx 未注册）
        register_pending_injection_sub("sess-x", cb)
        await flush_pending_injection_subs("sess-x", bus)
        # 注入事件到达 → 回调触发
        await bus.emit(
            Event(
                type=EventType.USER_INPUT_INJECTED, data={"session_id": "sess-x", "turn_index": 3}
            )
        )
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert len(received) == 1 and received[0].data["turn_index"] == 3


def test_contract_checkpoint_interval_default_is_one():
    """契约：run_agent_turn 的 checkpoint_interval 默认必须为 1
    （每步持久化口径）——交付文档声称「默认 5→1」但主链路实际走 5。"""
    import inspect

    from bandi.core.agent_runner import run_agent_turn

    sig = inspect.signature(run_agent_turn)
    default = sig.parameters["checkpoint_interval"].default
    assert int(default) == 1, f"checkpoint_interval 默认值 {default!r} 与文档口径不一致"


def test_contract_api_abort_endpoint_triggers_controller():
    """契约：/api/sessions/{id}/abort 必须置位该会话的
    AbortController（中断的 API 面，不再仅 CLI 可中断）。"""
    import sys

    from fastapi.testclient import TestClient

    from bandi.api.app import _abort_controllers_lock, _active_abort_controllers, app
    from bandi.core.abort_controller import AbortController

    app_module = sys.modules["bandi.api.app"]

    controller = AbortController()
    with _abort_controllers_lock:
        _active_abort_controllers["sess-abort-test"] = controller

    client = TestClient(app)
    # _API_KEY 模块级固化——patch 模块变量对齐 Bearer 校验
    orig_key = app_module._API_KEY
    app_module._API_KEY = "test-key"
    try:
        resp = client.post(
            "/api/sessions/sess-abort-test/abort",
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp.status_code == 200, resp.text
        assert controller.is_aborted()[0], "abort 端点未置位控制器"

        with _abort_controllers_lock:
            _active_abort_controllers.pop("sess-abort-test", None)

        # 无活跃控制器 → 404
        resp2 = client.post(
            "/api/sessions/no-such-session/abort",
            headers={"Authorization": "Bearer test-key"},
        )
        assert resp2.status_code == 404
    finally:
        app_module._API_KEY = orig_key
