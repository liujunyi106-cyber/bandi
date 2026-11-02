"""契约：readiness 存储探针以「可写」为准，删除失败不得假 503。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.api.app import _probe_storage_writable


def test_storage_probe_survives_unlink_failure(monkeypatch: Any, tmp_path: Path) -> None:
    # 口径与实现一致：探测路径走 BANDI_HOME
    monkeypatch.setenv("BANDI_HOME", str(tmp_path / ".bandi"))

    def _deny_unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("locked by scanner")

    monkeypatch.setattr(Path, "unlink", _deny_unlink)
    assert _probe_storage_writable() is True
    assert (tmp_path / ".bandi" / "data").is_dir()
