# 功能：会话边界契约——project_root 不得控制面/家目录；保留 session_id 不得创建
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from bandi.api.app import _is_reserved_session_id, _validated_session_project_root


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".bandi").mkdir(parents=True)
    (home / "work").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_project_root_rejects_control_plane(fake_home: Path) -> None:
    with pytest.raises(HTTPException) as error:
        _validated_session_project_root(str(fake_home / ".bandi"), "")
    assert error.value.status_code == 400


def test_project_root_rejects_home_root(fake_home: Path) -> None:
    with pytest.raises(HTTPException) as error:
        _validated_session_project_root(str(fake_home), "")
    assert error.value.status_code == 400


def test_project_root_accepts_existing_directory(fake_home: Path) -> None:
    assert _validated_session_project_root(str(fake_home / "work"), "") == str(
        (fake_home / "work").resolve()
    )


@pytest.mark.parametrize(
    "session_id",
    ["role-session::p::t::r", "role-runtime::r::x", "skill_distill:abc", "team-pending-1"],
)
def test_reserved_session_ids_detected(session_id: str) -> None:
    assert _is_reserved_session_id(session_id) is True


def test_normal_session_id_allowed() -> None:
    assert _is_reserved_session_id("s-normal-1") is False
