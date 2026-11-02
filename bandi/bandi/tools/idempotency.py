# 功能：幂等结果持久化存储（IdempotencyStore 文件实现——跨进程恢复重试 TMC-收尾）
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from bandi.core.message import ToolResult

logger = logging.getLogger(__name__)


class IdempotencyStore:
    """# 功能：按 idempotency_key 持久化工具结果（JSON 文件，带 TTL）——
    进程 A 执行成功写入，进程 B（同确定性 key）恢复时命中直接返回
    不重新执行（防跨进程恢复的副作用重放）。"""

    def __init__(self, base_dir: str | Path, default_ttl: float = 3600.0) -> None:
        self._dir = Path(base_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._default_ttl = default_ttl

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def get(self, key: str) -> ToolResult | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() > float(data.get("expires_at", 0)):
            p.unlink(missing_ok=True)
            return None
        return ToolResult(
            tool_call_id="",
            tool_name=data.get("tool_name", ""),
            success=bool(data.get("success")),
            output=data.get("output", ""),
            error=data.get("error"),
            duration_ms=0.0,
        )

    def set(self, key: str, result: ToolResult, ttl: float | None = None) -> None:
        data: dict[str, Any] = {
            "tool_name": result.tool_name,
            "success": result.success,
            "output": result.output,
            "error": result.error,
            "expires_at": time.time() + (ttl if ttl is not None else self._default_ttl),
        }
        path = self._path(key)
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(str(tmp_path), str(path))
        except OSError as e:
            logger.warning("idempotency store write failed for %s: %s", key, e)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def cleanup(self) -> int:
        # 功能：清理过期条目，返回清理数（TTL 防御）
        removed = 0
        now = time.time()
        for p in self._dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if now > float(data.get("expires_at", 0)):
                    p.unlink(missing_ok=True)
                    removed += 1
            except (OSError, json.JSONDecodeError):
                continue
        return removed
