# 功能：执行隔离契约——环境清洗 / 工作区 fail-closed / Docker 缺失 fail-closed
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bandi.tools import isolation


def _ctx(workspace: Path, **extra: object) -> SimpleNamespace:
    return SimpleNamespace(extra={"employee_workspace": str(workspace), **extra})


def test_sanitize_env_strips_secrets_and_redirects_home(tmp_path: Path) -> None:
    workspace = tmp_path / "employee"
    env = isolation.sanitize_env(
        {
            "PATH": "C:/Windows/System32",
            "BANDI_API_KEY": "secret",
            "OPENAI_API_KEY": "sk-xxx",
            "SOME_TOKEN": "tok",
            "KEEP_ME": "1",
        },
        workspace,
    )
    assert "bandi-isolation" in env["HOME"]
    assert env["USERPROFILE"] == env["HOME"]
    assert "bandi-isolation" in env["TMP"]
    assert "BANDI_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "SOME_TOKEN" not in env
    assert env["KEEP_ME"] == "1"
    assert env["PATH"]


def test_employee_without_workspace_fails_closed(tmp_path: Path) -> None:
    context = SimpleNamespace(extra={"employee_id": "emp-1"})
    with pytest.raises(isolation.IsolationError):
        isolation.workspace_for(context)


def test_local_hardened_mode_uses_sanitized_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BANDI_EXECUTION_ISOLATION_MODE", "local_hardened")
    monkeypatch.setenv("BANDI_API_KEY", "secret")
    env = isolation.tool_env(_ctx(tmp_path))
    assert "BANDI_API_KEY" not in env
    assert "bandi-isolation" in env["HOME"]


def test_docker_prefix_has_no_network_and_workspace_mount(tmp_path: Path) -> None:
    settings = isolation.IsolationSettings(mode="docker", memory_mb=512, cpu_seconds=60)
    prefix = isolation.docker_prefix(tmp_path, settings)
    joined = " ".join(prefix)
    assert "--network none" in joined
    assert "--memory 512m" in joined
    assert f"-v {tmp_path}:/workspace" in joined


def test_docker_mode_without_docker_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BANDI_EXECUTION_ISOLATION_MODE", "docker")
    monkeypatch.setattr(isolation.shutil, "which", lambda _: None)
    with pytest.raises(isolation.IsolationError):
        isolation.bash_command("echo hi", _ctx(tmp_path))
    with pytest.raises(isolation.IsolationError):
        isolation.python_command("print('hi')", _ctx(tmp_path))
