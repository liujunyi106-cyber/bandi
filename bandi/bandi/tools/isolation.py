# 功能：执行隔离子系统——本地加固环境与可选 Docker 容器后端
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class IsolationError(RuntimeError):
    """功能：隔离边界无法建立（fail-closed，不回落宿主执行）"""


@dataclass(frozen=True)
class IsolationSettings:
    mode: str = "local_hardened"
    image: str = "python:3.12-slim"
    network: str = "none"
    memory_mb: int = 1024
    cpu_seconds: int = 300


def load_settings() -> IsolationSettings:
    """功能：隔离配置——环境变量优先，其次 BandiConfig，最后安全默认值"""
    mode = os.environ.get("BANDI_EXECUTION_ISOLATION_MODE", "").strip().lower()
    image = os.environ.get("BANDI_EXECUTION_ISOLATION_IMAGE", "").strip()
    network = os.environ.get("BANDI_EXECUTION_ISOLATION_NETWORK", "").strip()
    memory = os.environ.get("BANDI_EXECUTION_ISOLATION_MEMORY_MB", "").strip()
    cpu = os.environ.get("BANDI_EXECUTION_ISOLATION_CPU_SECONDS", "").strip()
    if not any((mode, image, network, memory, cpu)):
        try:
            from bandi.core.config import BandiConfig

            cfg = BandiConfig.load().execution_isolation
            return IsolationSettings(
                mode=str(cfg.mode or "local_hardened").strip().lower(),
                image=str(cfg.image or "python:3.12-slim").strip(),
                network=str(cfg.network or "none").strip(),
                memory_mb=int(cfg.memory_mb or 1024),
                cpu_seconds=int(cfg.cpu_seconds or 300),
            )
        except Exception:  # noqa: BLE001 — 配置不可用时用安全默认
            pass
    return IsolationSettings(
        mode=mode or "local_hardened",
        image=image or "python:3.12-slim",
        network=network or "none",
        memory_mb=int(memory or 1024),
        cpu_seconds=int(cpu or 300),
    )


def _workspace_for(context: Any) -> Path:
    """功能：隔离工作区——员工 workspace 优先，其次项目根，最后全局 workspace"""
    extra = getattr(context, "extra", None) or {}
    raw = (
        str(extra.get("employee_workspace") or "").strip()
        or str(extra.get("project_root") or "").strip()
    )
    if not raw:
        from bandi.tools.fs_tool import workspace_root

        raw = str(workspace_root())
    if extra.get("employee_id") and not str(extra.get("employee_workspace") or "").strip():
        raise IsolationError(
            f"employee {extra.get('employee_id')} has no isolated workspace; "
            "refusing host execution"
        )
    path = Path(raw).expanduser().resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IsolationError(f"isolation workspace unavailable: {path}: {exc}") from exc
    return path


def workspace_for(context: Any) -> Path:
    """功能：隔离工作区公开入口"""
    return _workspace_for(context)


def sanitize_env(base: dict[str, str], workspace: Path) -> dict[str, str]:
    """功能：清洗子进程环境——HOME/TMP 指向隔离区，移除密钥形状变量"""
    env = dict(base)
    digest = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:16]
    sandbox_root = Path(tempfile.gettempdir()) / "bandi-isolation" / digest
    home = sandbox_root / "home"
    tmp = sandbox_root / "tmp"
    try:
        home.mkdir(parents=True, exist_ok=True)
        tmp.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IsolationError(f"isolation temp dirs unavailable: {exc}") from exc
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "TMP": str(tmp),
            "TEMP": str(tmp),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    for key in list(env):
        upper = key.upper()
        if upper == "BANDI_API_KEY" or upper.endswith(
            ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
        ):
            env.pop(key, None)
    return env


def tool_env(context: Any) -> dict[str, str]:
    """功能：本地加固模式下的工具环境（工作区缺失即 fail-closed）"""
    settings = load_settings()
    if settings.mode == "docker":
        raise IsolationError("docker isolation does not use host environment")
    return sanitize_env(os.environ, _workspace_for(context))


def docker_available() -> bool:
    """功能：Docker 能力探测（可执行文件 + daemon 可用）"""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0 and bool((result.stdout or "").strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def docker_prefix(workspace: Path, settings: IsolationSettings) -> list[str]:
    """功能：Docker 隔离命令前缀（无网络/内存/CPU 上限/工作区挂载）"""
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        settings.network,
        "--memory",
        f"{max(1, settings.memory_mb)}m",
        "--cpus",
        "1",
        "-v",
        f"{workspace}:/workspace",
        "-w",
        "/workspace",
        settings.image,
    ]


def bash_command(command: str, context: Any) -> tuple[list[str], dict[str, str], str]:
    """功能：bash 执行命令/环境/cwd——docker 模式 fail-closed 或容器执行"""
    settings = load_settings()
    workspace = _workspace_for(context)
    if settings.mode == "docker":
        if not docker_available():
            raise IsolationError(
                "execution isolation mode=docker but Docker is unavailable; refusing host execution"
            )
        return (
            docker_prefix(workspace, settings) + ["sh", "-lc", command],
            dict(os.environ),
            str(workspace),
        )
    return ["bash", "-lc", command], sanitize_env(os.environ, workspace), str(workspace)


def python_command(code: str, context: Any) -> tuple[list[str], dict[str, str], str]:
    """功能：python_exec 执行命令/环境/cwd——docker 模式容器内 python -c"""
    settings = load_settings()
    workspace = _workspace_for(context)
    if settings.mode == "docker":
        if not docker_available():
            raise IsolationError(
                "execution isolation mode=docker but Docker is unavailable; refusing host execution"
            )
        return (
            docker_prefix(workspace, settings) + ["python", "-c", code],
            dict(os.environ),
            str(workspace),
        )
    import sys

    return [sys.executable, "-c", code], sanitize_env(os.environ, workspace), str(workspace)
