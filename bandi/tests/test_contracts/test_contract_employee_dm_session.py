# 功能：契约——员工单聊会话元数据（target_role/employee_id 落库与列表投影）
from __future__ import annotations

from pathlib import Path

from bandi.api.app import (
    _session_employee_id,
    _session_employee_name,
    _session_origin,
    _session_target_role,
)
from bandi.memory.session.store import SessionStore


def test_employee_dm_session_metadata_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "sessions.db"))
    sid = store.create_session(
        title="Leader",
        origin="employee_dm",
        target_role="rc_lead",
        employee_id="emp_rc_lead",
        employee_name="Leader",
    )
    session = store.get_session(sid)
    assert session is not None
    assert _session_origin(session) == "employee_dm"
    assert _session_target_role(session) == "rc_lead"
    assert _session_employee_id(session) == "emp_rc_lead"
    assert _session_employee_name(session) == "Leader"


def test_plain_session_has_no_employee_dm_metadata(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "sessions.db"))
    sid = store.create_session(title="普通会话")
    session = store.get_session(sid)
    assert session is not None
    assert _session_target_role(session) == ""
    assert _session_employee_id(session) == ""
    assert _session_employee_name(session) == ""
