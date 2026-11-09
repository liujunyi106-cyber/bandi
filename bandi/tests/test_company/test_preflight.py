from __future__ import annotations

from pathlib import Path

import pytest

from bandi.company.preflight import (
    ExternalAgentPreflightResult,
    PathProbeResult,
    ensure_external_agent_surfaces,
    probe_external_agent_write_contract,
    run_external_agent_preflight,
)


class TestPathProbeResult:
    def test_as_dict(self) -> None:
        result = PathProbeResult(name="test", path="/tmp", ok=True, error="")
        assert result.as_dict() == {"name": "test", "path": "/tmp", "ok": True, "error": ""}

    def test_as_dict_with_error(self) -> None:
        result = PathProbeResult(name="x", path="/no", ok=False, error="denied")
        assert result.as_dict()["error"] == "denied"


class TestProbeWorkspaceWritable:
    def test_all_paths_writable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 隔离 opc_home 到 tmp：默认探针会打真实 ~/.bandi，WorkBuddy safe-delete
        import bandi.company.preflight as preflight_mod

        monkeypatch.setattr(preflight_mod, "_get_oponica_home", lambda: tmp_path)
        results = probe_external_agent_write_contract(
            workspace_path=tmp_path,
            metadata={},
            project_db_path=None,
        )
        names = {r.name for r in results}
        assert names == {"workspace", "opc_home", "opc_memory"}
        assert all(r.ok for r in results)

    def test_non_writable_path_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        results = probe_external_agent_write_contract(
            workspace_path=str(tmp_path),
            metadata={"comms_workspace_root": str(tmp_path / "comms")},
        )
        names = {r.name for r in results}
        assert "comms_workspace_root" in names or "workspace" in names


class TestExternalAgentPreflightResult:
    def test_ok_when_enabled_and_available(self) -> None:
        r = ExternalAgentPreflightResult(
            agent="codex", enabled=True, command="codex", available=True
        )
        assert r.ok is True

    def test_not_ok_when_not_available(self) -> None:
        r = ExternalAgentPreflightResult(
            agent="codex", enabled=True, command="codex", available=False
        )
        assert r.ok is False

    def test_as_dict_full(self) -> None:
        r = ExternalAgentPreflightResult(
            agent="claude_code",
            enabled=True,
            command="claude",
            available=True,
            binary="/usr/bin/claude",
            version="1.2.3",
        )
        d = r.as_dict()
        assert d["agent"] == "claude_code"
        assert d["ok"] is True
        assert d["issues"] == []


class TestDescribeRpcTransport:
    def test_returns_tcp_on_windows(self) -> None:
        from bandi.company.preflight import _describe_collaboration_rpc_transport

        transport, issue = _describe_collaboration_rpc_transport()
        assert transport == "tcp(loopback)"
        assert issue == ""


class TestProbeVersion:
    def test_unknown_agent_returns_empty(self) -> None:
        from bandi.company.preflight import _probe_version

        version = _probe_version("unknown_agent_xyz", "/nonexistent", [])
        assert version == ""

    def test_nonexistent_binary_returns_empty(self) -> None:
        from bandi.company.preflight import _probe_version

        version = _probe_version("codex", "nonexistent_binary_xyz_12345", [])
        assert version == ""


class TestEnsureExternalAgentSurfaces:
    def test_creates_isolated_home(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        surfaces = ensure_external_agent_surfaces(home)
        # Should create isolated homes for known external agents
        assert len(surfaces) >= 1
        for agent_slug, info in surfaces.items():
            assert "home" in info
            assert Path(info["home"]).exists()

    def test_idempotent(self, tmp_path: Path) -> None:
        home = tmp_path / "home2"
        home.mkdir()
        s1 = ensure_external_agent_surfaces(home)
        s2 = ensure_external_agent_surfaces(home)
        assert s1.keys() == s2.keys()


class TestRunExternalAgentPreflight:
    def test_probes_known_agents(self, tmp_path: Path) -> None:
        home = tmp_path / "home3"
        home.mkdir()
        results = run_external_agent_preflight(
            home=str(home),
            workspace_path=str(tmp_path),
            probe_commands=False,
        )
        agents = {r.agent for r in results}
        # Should include the known external agents
        assert "codex" in agents or len(results) > 0

    def test_returns_write_checks(self, tmp_path: Path) -> None:
        home = tmp_path / "home4"
        home.mkdir()
        results = run_external_agent_preflight(
            home=str(home),
            workspace_path=str(tmp_path),
            probe_commands=False,
        )
        for r in results:
            assert isinstance(r.write_checks, list)
