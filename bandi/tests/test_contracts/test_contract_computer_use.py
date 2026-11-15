# 功能：契约——Computer Use（工具面按开关注册 / 敏感窗口拒绝 / 运行时未就绪 fail-closed /
# 开启必须确认且需运行时就绪 / 安装状态机与失败原因可查）
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest


def _stub_config(
    monkeypatch: pytest.MonkeyPatch, *, enabled: bool, allow_sensitive: bool = False
) -> Any:
    """功能：把配置加载替换成内存配置（不写真实 config.yaml）。"""
    from bandi.core.config import BandiConfig

    cfg = BandiConfig()
    cfg.tools.computer_use.enabled = enabled
    cfg.tools.computer_use.allow_sensitive_windows = allow_sensitive
    monkeypatch.setattr(BandiConfig, "load", classmethod(lambda cls: cfg))
    return cfg


@pytest.fixture()
def api_key(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """功能：注入固定 API key，配合 Bearer 头调用真实端点。"""
    module = sys.modules["bandi.api.app"]
    monkeypatch.setattr(module, "_API_KEY", "test-key", raising=False)
    return {"Authorization": "Bearer test-key"}


def _client() -> Any:
    from fastapi.testclient import TestClient

    from bandi.api.app import app

    return TestClient(app)


def test_tools_registered_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具面契约：关闭时零出现，开启后恰好四个。"""
    from bandi.agent.factory import build_tool_registry

    _stub_config(monkeypatch, enabled=False)
    disabled_names = [tool.name for tool in build_tool_registry().list_tools()]
    assert not [name for name in disabled_names if name.startswith("computer_")], (
        "关闭状态下不得注册任何桌面工具"
    )

    _stub_config(monkeypatch, enabled=True)
    enabled_names = [
        tool.name
        for tool in build_tool_registry().list_tools()
        if tool.name.startswith("computer_")
    ]
    assert sorted(enabled_names) == [
        "computer_click",
        "computer_screenshot",
        "computer_scroll",
        "computer_type",
    ]


def test_exactly_one_tool_per_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具面契约：开启后总工具数 = 关闭时 + 4（不得因注册改动连带增删）。"""
    from bandi.agent.factory import build_tool_registry

    _stub_config(monkeypatch, enabled=False)
    base = len(build_tool_registry().list_tools())
    _stub_config(monkeypatch, enabled=True)
    assert len(build_tool_registry().list_tools()) == base + 4


def test_disabled_tool_returns_error_not_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """执行契约：关闭时调用必须显式失败（不得静默返回空结果）。"""
    from bandi.tools.computer_use.desktop import ComputerClickTool

    _stub_config(monkeypatch, enabled=False)
    result = asyncio.run(ComputerClickTool().execute({"x": 10, "y": 10}))
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["error"] == "COMPUTER_USE_DISABLED"


def test_sensitive_window_blocks_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安全契约：前台窗口命中敏感词 → 拒绝且**不执行**桌面动作（破坏者：换窗口标题绕过）。"""
    from bandi.tools.computer_use import desktop

    _stub_config(monkeypatch, enabled=True)
    called: list[str] = []

    def _boom(action: str, params: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        called.append(action)
        return {"ok": True}

    monkeypatch.setattr(desktop, "run_action", _boom)
    monkeypatch.setattr(desktop, "foreground_window_title", lambda: "1Password 8 — 密码库")

    result = asyncio.run(desktop.ComputerClickTool().execute({"x": 5, "y": 5}))
    assert result["ok"] is False
    assert result["error"] == "SENSITIVE_WINDOW"
    assert called == [], "命中敏感窗口时不得调用桌面动作"

    # 豁免开关打开后才允许（配置可审计的破例路径）
    _stub_config(monkeypatch, enabled=True, allow_sensitive=True)
    ok = asyncio.run(desktop.ComputerClickTool().execute({"x": 5, "y": 5}))
    assert ok["ok"] is True and called == ["click"]


def test_runtime_missing_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行时契约：缺 venv/依赖时返回 RUNTIME_NOT_READY，且不误报成功。"""
    from bandi.tools.computer_use import desktop
    from bandi.tools.computer_use.runtime import ComputerRuntimeError

    _stub_config(monkeypatch, enabled=True)
    monkeypatch.setattr(desktop, "foreground_window_title", lambda: "记事本")

    def _fail(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ComputerRuntimeError("运行时未就绪：venv_exists=False")

    monkeypatch.setattr(desktop, "run_action", _fail)
    result = asyncio.run(desktop.ComputerScreenshotTool().execute({}))
    assert result["ok"] is False
    assert result["error"] == "RUNTIME_NOT_READY"
    assert "运行时未就绪" in result["detail"]


def test_screenshot_does_not_prompt_but_writes_do() -> None:
    """权限契约：截图是读操作不弹卡；点击/输入/滚动必须弹卡（对齐 Claude 的读写成对）。

    破坏者：截图也设 high ⇒ 模型每看一次屏幕都要点一次卡，实际不可用；
    反向把 click 降到 medium ⇒ 高危动作静默放行。两条都钉死。
    """
    from bandi.core.config import PermissionsConfig
    from bandi.tools.computer_use import COMPUTER_TOOLS

    ask_levels = set(PermissionsConfig().ask_risk_levels)
    by_name = {tool_class.name: tool_class for tool_class in COMPUTER_TOOLS}

    assert by_name["computer_screenshot"].risk_level == "medium"
    assert by_name["computer_screenshot"].risk_level not in ask_levels, "截图不得每次弹权限卡"

    for name in ("computer_click", "computer_type", "computer_scroll"):
        assert by_name[name].risk_level in ask_levels, f"{name} 必须过权限卡"


def test_status_exposes_coordinate_space_and_scale() -> None:
    """坐标系契约：状态面必须给出物理像素口径与缩放比（逻辑坐标 × scale = 物理像素）。"""
    from bandi.tools.computer_use import runtime_status

    status = runtime_status()
    assert status["coordinate_space"] == "physical"
    assert isinstance(status["scale"], float) and status["scale"] > 0


def test_audit_line_written_for_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计契约：拒绝也必须留证（含原因），写失败不得影响返回值。"""
    from bandi.tools.computer_use import desktop, safety

    _stub_config(monkeypatch, enabled=True)
    monkeypatch.setattr(desktop, "foreground_window_title", lambda: "招商银行个人网银")

    asyncio.run(desktop.ComputerTypeTool().execute({"text": "hello"}))
    path = safety.audit_path()
    assert path.exists(), "拒绝也要落审计"
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert rows[-1]["action"] == "computer_type"
    assert rows[-1]["outcome"] == "denied"
    assert rows[-1]["detail"]["reason"] == "sensitive_window"


def _prepare_fake_venv(monkeypatch: pytest.MonkeyPatch) -> Any:
    """功能：伪造「venv 已创建 + 依赖齐备」，只测状态机流转。"""
    from bandi.tools.computer_use import runtime

    runtime.reset_install_state()
    python = runtime.venv_python()
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"stub")
    monkeypatch.setattr(
        runtime,
        "detect_interpreter",
        lambda: {"path": sys.executable, "version": "3.12.10", "source": "auto"},
    )
    return runtime


def test_install_state_machine_reaches_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安装契约：成功后状态 ready + 进度 100；日志与耗时字段可查。"""
    runtime = _prepare_fake_venv(monkeypatch)

    async def _ok(cmd: list[str], timeout: float, lines: list[str]) -> tuple[int, str]:
        lines.append(f"# fake run {cmd[0]}")
        return 0, ""

    monkeypatch.setattr(runtime, "_run_stream", _ok)
    monkeypatch.setattr(
        runtime,
        "_probe",
        lambda python: {
            "ok": True,
            "version": "3.12.10",
            "modules": {"mss": True, "pyautogui": True, "PIL": True, "pyperclip": True},
            "screen": {"width": 1920, "height": 1080},
        },
    )

    async def _run() -> dict[str, Any]:
        await runtime.start_install("")
        assert runtime._install_task is not None
        await runtime._install_task
        return runtime._read_state()

    state = asyncio.run(_run())
    assert state["phase"] == "ready"
    assert state["progress"] == 100
    assert state["error"] == ""
    assert state["started_at"] and state["finished_at"]
    assert any("fake run" in line for line in state["log"])

    status = runtime.runtime_status()
    assert status["venv_exists"] is True
    assert status["deps_installed"] is True
    assert status["missing_packages"] == []


def test_install_failure_records_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安装契约：pip 失败必须落 failed + 原因（不许假装就绪）。"""
    runtime = _prepare_fake_venv(monkeypatch)

    async def _fail(cmd: list[str], timeout: float, lines: list[str]) -> tuple[int, str]:
        lines.append("# fake fail")
        return 1, "ERROR: No matching distribution found for pyautogui"

    monkeypatch.setattr(runtime, "_run_stream", _fail)

    async def _run() -> dict[str, Any]:
        await runtime.start_install("")
        assert runtime._install_task is not None
        await runtime._install_task
        return runtime._read_state()

    state = asyncio.run(_run())
    assert state["phase"] == "failed"
    assert "No matching distribution" in state["error"]


def test_run_stream_returns_when_child_exits_even_if_pipe_held() -> None:
    """子进程契约：孙进程占住管道时，必须以「主进程退出」为准返回（不得挂死）。"""
    import time

    from bandi.tools.computer_use import runtime

    lines: list[str] = []
    cmd = [
        sys.executable,
        "-c",
        "import subprocess,sys;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(8)']);"
        "print('done', flush=True)",
    ]
    started = time.monotonic()
    code, _error = asyncio.run(runtime._run_stream(cmd, 20, lines))
    elapsed = time.monotonic() - started

    assert code == 0
    assert elapsed < 5, f"不得等孙进程退出（实际 {elapsed:.1f}s）"
    assert any("done" in line for line in lines)


def test_install_retries_direct_when_proxy_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安装契约：环境代理不可用（ProxyError）时改直连重试一次，并留日志。"""
    runtime = _prepare_fake_venv(monkeypatch)
    calls: list[list[str]] = []

    async def _run(
        cmd: list[str], timeout: float, lines: list[str], env: dict[str, str] | None = None
    ) -> tuple[int, str]:
        calls.append(list(cmd))
        if len(calls) == 1:
            lines.append(
                "WARNING: Retrying after connection broken by 'ProxyError('Cannot connect to proxy.')'"
            )
            return 1, ""
        assert env is not None, "直连重试必须清掉代理环境变量"
        assert not [key for key in env if key.lower().endswith("_proxy")], "重试环境不得含代理变量"
        return 0, ""

    monkeypatch.setattr(runtime, "_run_stream", _run)
    monkeypatch.setattr(
        runtime,
        "_probe",
        lambda python: {
            "ok": True,
            "version": "3.12.10",
            "modules": {"mss": True, "pyautogui": True, "PIL": True, "pyperclip": True},
        },
    )

    async def _go() -> dict[str, Any]:
        await runtime.start_install("")
        assert runtime._install_task is not None
        await runtime._install_task
        return runtime._read_state()

    state = asyncio.run(_go())
    assert state["phase"] == "ready", state
    assert len(calls) == 2
    assert "--proxy" in calls[1]
    assert any("直连重试" in line for line in state["log"])


def test_endpoint_enable_requires_confirm_and_runtime(
    monkeypatch: pytest.MonkeyPatch, api_key: dict[str, str]
) -> None:
    """端点契约：缺确认 400；运行时未就绪 409；就绪后才写配置。"""
    import bandi.tools.computer_use as package

    ready = {"enabled": False, "deps_installed": True, "venv_exists": True}
    monkeypatch.setattr(package, "runtime_status", lambda: dict(ready))
    client = _client()

    r1 = client.put("/api/computer/enabled", json={"enabled": True}, headers=api_key)
    assert r1.status_code == 400
    assert "CONFIRM_REQUIRED" in r1.text

    not_ready = {"enabled": False, "deps_installed": False, "venv_exists": False}
    monkeypatch.setattr(package, "runtime_status", lambda: dict(not_ready))
    r2 = client.put(
        "/api/computer/enabled", json={"enabled": True, "confirm": True}, headers=api_key
    )
    assert r2.status_code == 409
    assert "RUNTIME_NOT_READY" in r2.text

    monkeypatch.setattr(package, "runtime_status", lambda: dict(ready))
    r3 = client.put(
        "/api/computer/enabled", json={"enabled": True, "confirm": True}, headers=api_key
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["enabled"] is True

    config_path = Path.home() / ".bandi" / "config.yaml"
    assert config_path.exists()
    assert "enabled: true" in config_path.read_text(encoding="utf-8")


def test_endpoint_status_reports_runtime(
    monkeypatch: pytest.MonkeyPatch, api_key: dict[str, str]
) -> None:
    """端点契约：status 透出运行时事实（解释器/依赖/敏感窗口模式）。"""
    import bandi.tools.computer_use as package

    monkeypatch.setattr(
        package,
        "runtime_status",
        lambda: {
            "enabled": True,
            "venv_exists": True,
            "deps_installed": True,
            "missing_packages": [],
            "venv_dir": "C:/tmp/venv",
            "python": "C:/tmp/venv/python.exe",
            "interpreter": {"path": "C:/Python/python.exe", "version": "3.12.10"},
            "safe_mode": True,
            "install": {"phase": "ready", "progress": 100},
        },
    )
    response = _client().get("/api/computer/status", headers=api_key)
    assert response.status_code == 200
    body = response.json()
    assert body["deps_installed"] is True
    assert body["safe_mode"] is True
    assert body["install"]["phase"] == "ready"
