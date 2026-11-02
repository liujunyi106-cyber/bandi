"""功能：团队名称注册表（team_id → 显示名），供自定义团队使用。"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

_REGISTRY_LOCK = threading.RLock()


class TeamRegistry:
    """功能：读写团队显示名（JSON 原子写入）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        default = Path(os.environ.get("BANDI_HOME", "~/.bandi")) / "company" / "team_names.json"
        self.path = Path(path).expanduser() if path is not None else default.expanduser()
        self.status_path = self.path.with_name("team_status.json")
        self.members_path = self.path.with_name("team_members.json")

    def _read(self) -> dict[str, str]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items() if str(value).strip()}

    def get(self, team_id: str) -> str:
        return self._read().get(str(team_id).strip(), "")

    def all(self) -> dict[str, str]:
        return self._read()

    def _read_status(self) -> dict[str, str]:
        try:
            raw = self.status_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items()}

    def _read_members(self) -> dict[str, list[str]]:
        try:
            raw = self.members_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): [str(item) for item in value if str(item).strip()]
            for key, value in data.items()
            if isinstance(value, list)
        }

    def get_members(self, team_id: str) -> list[str]:
        """功能：读取团队解散前成员角色（用于重新拉入恢复）。"""
        return self._read_members().get(str(team_id).strip(), [])

    def set_members(self, team_id: str, role_ids: list[str]) -> None:
        """功能：持久化团队解散前成员角色。"""
        normalized_id = str(team_id).strip()
        if not normalized_id:
            raise ValueError("team_id required")
        with _REGISTRY_LOCK:
            data = self._read_members()
            data[normalized_id] = [str(item).strip() for item in role_ids if str(item).strip()]
            self.members_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.members_path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.members_path)

    def get_status(self, team_id: str) -> str:
        """功能：读取团队状态（缺省 active）。"""
        return self._read_status().get(str(team_id).strip(), "active")

    def set_status(self, team_id: str, status: str) -> None:
        """功能：持久化团队状态（active / dissolved）。"""
        normalized_id = str(team_id).strip()
        normalized_status = str(status).strip()
        if not normalized_id or normalized_status not in {"active", "dissolved"}:
            raise ValueError("team_id/status required")
        with _REGISTRY_LOCK:
            data = self._read_status()
            data[normalized_id] = normalized_status
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.status_path)

    def set(self, team_id: str, name: str) -> None:
        normalized_id = str(team_id).strip()
        normalized_name = str(name).strip()
        if not normalized_id or not normalized_name:
            raise ValueError("team_id/name required")
        with _REGISTRY_LOCK:
            data = self._read()
            data[normalized_id] = normalized_name
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
