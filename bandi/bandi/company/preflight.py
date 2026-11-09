from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bandi.company.recruiter import RECRUITMENT_AGENT_CHOICES

_EXTERNAL_AGENT_CHOICES: frozenset[str] = RECRUITMENT_AGENT_CHOICES - {"native"}
_VERSION_COMMANDS: dict[str, tuple[str, ...]] = {
    "codex": ("--version",),
    "claude_code": ("--version",),
    "cursor": ("--version",),
    "opencode": ("--version",),
}
_RPC_TIMEOUT_SECONDS = 30.0


@dataclass
class PathProbeResult:
    name: str
    path: str
    ok: bool
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "ok": self.ok, "error": self.error}


@dataclass
class ExternalAgentPreflightResult:
    agent: str
    enabled: bool
    command: str
    available: bool
    binary: str = ""
    version: str = ""
    launch_command: str = ""
    stdin_policy: str = ""
    collaboration_rpc_transport: str = ""
    isolated_home: str = ""
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    write_checks: list[PathProbeResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.enabled and self.available and not self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "enabled": self.enabled,
            "command": self.command,
            "available": self.available,
            "binary": self.binary,
            "version": self.version,
            "launch_command": self.launch_command,
            "stdin_policy": self.stdin_policy,
            "collaboration_rpc_transport": self.collaboration_rpc_transport,
            "isolated_home": self.isolated_home,
            "issues": list(self.issues),
            "warnings": list(self.warnings),
            "write_checks": [c.as_dict() for c in self.write_checks],
            "ok": self.ok,
        }


def ensure_external_agent_surfaces(
    home: Path | str,
    *,
    agents: frozenset[str] | None = None,
) -> dict[str, dict[str, str]]:
    """为每个已知外部 Agent 创建隔离 home 目录。幂等安全，可重复调用。"""
    base = Path(home).expanduser()
    if agents is None:
        agents = _EXTERNAL_AGENT_CHOICES
    surfaces: dict[str, dict[str, str]] = {}
    for agent_name in sorted(agents):
        slug = _agent_slug(agent_name)
        agent_home = base / "external-agents" / slug
        agent_home.mkdir(parents=True, exist_ok=True)
        surfaces[agent_name] = {"home": str(agent_home), "slug": slug}
    return surfaces


def _agent_slug(agent_name: str) -> str:
    return agent_name


def probe_external_agent_write_contract(
    *,
    workspace_path: str | Path,
    opc_home: Path | None = None,
    metadata: Mapping[str, Any] | None = None,
    project_db_path: str | Path | None = None,
) -> list[PathProbeResult]:
    """检查外部 Agent 启动前必须可写的全部路径。"""
    base_home = Path(opc_home) if opc_home else _get_oponica_home()
    meta = dict(metadata or {})
    probes: list[tuple[str, Path, bool]] = [
        ("workspace", Path(workspace_path).expanduser(), False),
        ("opc_home", base_home.expanduser(), False),
        ("opc_memory", (base_home / "memory").expanduser(), False),
    ]
    for name, key in (
        ("collab_workspace", "comms_workspace_root"),
        ("collab_root", "comms_root"),
        ("output_root", "output_root"),
        ("target_output_dir", "target_output_dir"),
    ):
        raw = str(meta.get(key) or "").strip()
        if raw:
            probes.append((name, Path(raw).expanduser(), False))
    if project_db_path:
        probes.append(("project_db", Path(project_db_path).expanduser(), True))
    results: list[PathProbeResult] = []
    seen: set[tuple[str, str]] = set()
    for name, path, is_file in probes:
        key = (name, str(path))
        if key in seen:
            continue
        seen.add(key)
        results.append(_probe_writable_path(name, path, is_file=is_file))
    return results


def run_external_agent_preflight(
    home: str | Path | None = None,
    *,
    project_id: str = "default",
    workspace_path: str | Path | None = None,
    agents: frozenset[str] | None = None,
    probe_commands: bool = True,
    prepare_surfaces: bool = True,
) -> list[ExternalAgentPreflightResult]:
    """运行外部 Agent 启动预检，返回每个 Agent 的可用性报告。"""
    base_home = Path(home).expanduser() if home else _get_oponica_home()
    if workspace_path is None:
        workspace = base_home / "projects" / project_id / "workspace"
    else:
        workspace = Path(workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)
    surfaces: dict[str, dict[str, str]] = (
        ensure_external_agent_surfaces(base_home, agents=agents) if prepare_surfaces else {}
    )
    project_db = base_home / "projects" / project_id / "tasks.db"
    write_checks = probe_external_agent_write_contract(
        workspace_path=workspace,
        opc_home=base_home,
        project_db_path=project_db,
    )
    rpc_transport, rpc_issue = _describe_collaboration_rpc_transport()
    agent_list = sorted(agents) if agents else sorted(_EXTERNAL_AGENT_CHOICES)
    results: list[ExternalAgentPreflightResult] = []
    for agent_name in agent_list:
        binary = shutil.which(agent_name)
        result = ExternalAgentPreflightResult(
            agent=agent_name,
            enabled=True,
            command=agent_name,
            available=bool(binary),
            binary=str(binary or ""),
            write_checks=list(write_checks),
            collaboration_rpc_transport=rpc_transport,
        )
        surface = surfaces.get(agent_name, {})
        result.isolated_home = surface.get("home", "")
        if not result.available:
            result.issues.append(_missing_agent_issue(agent_name))
            if rpc_issue:
                result.issues.append(rpc_issue)
            results.append(result)
            continue
        _add_windows_wrapper_warnings(agent_name, str(binary), result.warnings)
        if rpc_issue:
            result.issues.append(rpc_issue)
        if probe_commands:
            result.version = _probe_version(agent_name, str(binary), result.warnings)
        _probe_isolated_home(agent_name, result)
        for check in write_checks:
            if not check.ok:
                result.issues.append(f"{check.name} is not writable: {check.path} ({check.error})")
        results.append(result)
    return results


def _get_oponica_home() -> Path:
    return Path.home() / ".bandi"


def _describe_collaboration_rpc_transport() -> tuple[str, str]:
    from bandi.tools.collaboration_rpc import rpc_env_available

    if rpc_env_available():
        return ("tcp(externally-configured)", "")
    return ("tcp(loopback)", "")


def _missing_agent_issue(agent_name: str) -> str:
    if agent_name == "cursor":
        editor = shutil.which("cursor")
        agent = shutil.which("cursor-agent")
        if editor and not agent:
            msg = "Cursor editor found, cursor-agent missing; "
            msg += "install Agent CLI for headless execution"
            return msg
    return f"command not found on PATH: {agent_name}"


def _add_windows_wrapper_warnings(agent_name: str, binary: str, warnings: list[str]) -> None:
    if os.name != "nt":
        return
    path = Path(binary)
    if path.suffix.lower() == ".ps1":
        warnings.append(
            "PowerShell execution policy may block this .ps1 wrapper; "
            "use the matching .cmd command manually"
        )
        return
    if path.suffix.lower() == ".cmd":
        ps1 = path.with_suffix(".ps1")
        if ps1.exists():
            warnings.append(
                f"PowerShell may prefer {ps1.name}; "
                f"use `{path.name}` manually if script execution is blocked"
            )


def _probe_writable_path(name: str, path: Path, *, is_file: bool = False) -> PathProbeResult:
    try:
        if is_file:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                with path.open("ab"):
                    pass
            else:
                _write_delete_probe(path.parent)
        else:
            path.mkdir(parents=True, exist_ok=True)
            _write_delete_probe(path)
        resolved = path.resolve(strict=False)
        return PathProbeResult(name=name, path=str(resolved), ok=True)
    except Exception as exc:  # noqa: BLE001
        return PathProbeResult(name=name, path=str(path), ok=False, error=str(exc))


def _write_delete_probe(directory: Path) -> None:
    probe_path = directory / f".bandi-probe-{uuid.uuid4().hex[:8]}.tmp"
    probe_path.write_text("ok", encoding="utf-8")
    probe_path.unlink(missing_ok=True)


def _probe_version(agent_name: str, binary: str, warnings: list[str]) -> str:
    args = _VERSION_COMMANDS.get(agent_name)
    if not args:
        return ""
    try:
        proc = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"version probe failed: {exc}")
        return ""
    text = (proc.stdout or proc.stderr or "").strip().splitlines()
    if not text and proc.returncode != 0:
        warnings.append(f"version probe exited {proc.returncode}")
    return text[0][:200] if text else ""


def _probe_isolated_home(agent_name: str, result: ExternalAgentPreflightResult) -> None:
    if not result.isolated_home:
        return
    home = Path(result.isolated_home)
    if not home.exists():
        result.issues.append(f"isolated agent home was not created: {home}")
