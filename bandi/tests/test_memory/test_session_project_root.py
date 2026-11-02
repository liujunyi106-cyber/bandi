from __future__ import annotations

import json
from pathlib import Path

from bandi.memory.session.store import SessionStore


class TestCreateSessionProjectRoot:
    def test_project_root_stored_in_metadata(self, tmp_path: Path) -> None:
        store = SessionStore(str(tmp_path / "s.db"))
        try:
            sid = store.create_session(project_root=str(tmp_path / "proj"))
            session = store.get_session(sid)
            assert session is not None
            meta = json.loads(session["metadata"] or "{}")
            assert meta.get("project_root") == str(tmp_path / "proj")
        finally:
            store.close()

    def test_no_project_root_metadata_empty(self, tmp_path: Path) -> None:
        store = SessionStore(str(tmp_path / "s.db"))
        try:
            sid = store.create_session()
            session = store.get_session(sid)
            assert session is not None
            assert session["metadata"] in ("", "{}")
        finally:
            store.close()

    def test_default_signature_backward_compatible(self, tmp_path: Path) -> None:
        store = SessionStore(str(tmp_path / "s.db"))
        try:
            sid = store.create_session(title="t", mode="plan")
            session = store.get_session(sid)
            assert session is not None
            assert session["mode"] == "plan"
            assert session["title"] == "t"
        finally:
            store.close()
