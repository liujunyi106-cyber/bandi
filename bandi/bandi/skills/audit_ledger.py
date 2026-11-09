# 功能：技能内容变更审计账本——每次技能落盘记 before/after sha256（JSONL 追加）
# 供无人值守/半自动技能改写的事后审计与回滚定位。
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _default_path() -> Path:
    # 功能：默认账本路径惰性求值（测试可 patch Path.home 隔离）
    return Path.home() / ".bandi" / "skills_audit.jsonl"


def content_sha256(text: str) -> str:
    """功能：utf-8 文本内容的 sha256（规范化文本口径）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class SkillAuditLedger:
    """功能：技能变更账本（before/after sha256 + action/skill/来源/时间）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path).expanduser() if path is not None else None
        self._lock = threading.Lock()

    def _resolved_path(self) -> Path:
        return self._path if self._path is not None else _default_path()

    def record(
        self,
        *,
        action: str,
        skill_id: str,
        before: str,
        after: str,
        source: str = "",
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """功能：追加一条技能变更记录（before/after 内容级 sha256）。"""
        before_hash = content_sha256(before)
        after_hash = content_sha256(after)
        entry: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "skill_id": skill_id,
            "before_sha256": before_hash,
            "after_sha256": after_hash,
            "changed": before_hash != after_hash,
            "source": source,
            "detail": detail or {},
        }
        path = self._resolved_path()
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def list_entries(self, limit: int = 100) -> list[dict[str, Any]]:
        """功能：读回最近 limit 条记录（坏行跳过，不阻断读取）。"""
        path = self._resolved_path()
        if not path.is_file():
            return []
        entries: list[dict[str, Any]] = []
        with self._lock:
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    entries.append(parsed)
        return entries[-max(1, int(limit)) :]


def record_skill_change(
    *,
    action: str,
    skill_id: str,
    before: str,
    after: str,
    source: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """功能：best-effort 记账入口（账本失败仅告警，不阻断技能落盘主链）。"""
    try:
        SkillAuditLedger().record(
            action=action,
            skill_id=skill_id,
            before=before,
            after=after,
            source=source,
            detail=detail,
        )
    except OSError as exc:
        logger.warning("skill audit ledger write failed: %s", exc)
