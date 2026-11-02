from __future__ import annotations

from pathlib import Path

import pytest

from bandi.tools.fs_tool import resolve_within_workspace


class TestResolveWithExtraRoots:
    def test_path_inside_extra_root_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        project = tmp_path / "project"
        memory = project / ".bandi" / "memory" / "MEMORY.md"
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", ws)
        target = resolve_within_workspace(str(memory), extra_roots=[str(project)])
        assert target == memory.resolve()

    def test_path_outside_extra_root_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        project = tmp_path / "project"
        other = tmp_path / "other" / "secret.txt"
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", ws)
        with pytest.raises(ValueError, match="outside workspace"):
            resolve_within_workspace(str(other), extra_roots=[str(project)])

    def test_workspace_path_allowed_without_extra_roots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        inside = ws / "a.txt"
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", ws)
        assert resolve_within_workspace(str(inside)) == inside.resolve()

    def test_symlink_in_extra_root_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        target_outside = tmp_path / "real.txt"
        target_outside.write_text("x", encoding="utf-8")
        link = project / "link.md"
        try:
            link.symlink_to(target_outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlink not supported")
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", ws)
        with pytest.raises(ValueError, match="symlink"):
            resolve_within_workspace(str(link), extra_roots=[str(project)])


class TestFsToolMemoryPathWritable:
    def test_memory_write_allowed_via_extra_roots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        project = tmp_path / "project"
        memory = project / ".bandi" / "memory"
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", ws)
        target = resolve_within_workspace(str(memory / "MEMORY.md"), extra_roots=[str(project)])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# 项目记忆\n", encoding="utf-8")
        assert (memory / "MEMORY.md").read_text(encoding="utf-8") == "# 项目记忆\n"
