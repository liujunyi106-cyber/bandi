# 功能：tool_argument_blob — 大参数分段暂存（文件 BLOB；create/append/finalize/discard；resolve 单次消费）
from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from pathlib import Path
from typing import Any

from bandi.tools.base import BaseTool, ToolCapability, ToolCategory, ToolPermissionContext

_BLOB_ID_RE = re.compile(r"^[a-f0-9]{8,64}$")
_MAX_BLOB_BYTES = 4 * 1024 * 1024
_MAX_CHUNK_BYTES = 1024 * 1024
_MAX_CHUNK_B64_BYTES = 1_400_000
_MAX_BLOBS_PER_SESSION = 2000


def _default_blob_dir() -> Path:
    return Path.home() / ".bandi" / "data" / "blobs"


class ToolArgumentBlobTool(BaseTool):
    """功能：可执行参数分段暂存 BLOB（按会话分目录，文件持久化 + finalize 锁定）。

    行为不变式：append 追加到 open 态；finalize 后 append 拒绝（ok=False）；
    resolve_blob_text 读后将文件删除（单次消费，二次读取抛 ValueError）。
    """

    name = "tool_argument_blob"
    description = (
        "大参数暂存：create/append/finalize/discard/create_and_finalize（chunk 或 chunk_base64）"
    )
    category = ToolCategory.UTIL
    feature_group = "util"
    is_read_only = False
    is_concurrency_safe = False
    capability_tags: list[str] = [ToolCapability.FILE_WRITE.value]
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "append", "finalize", "discard", "create_and_finalize"],
                "description": "BLOB 操作",
            },
            "blob_id": {"type": "string", "description": "目标 BLOB id"},
            "chunk": {"type": "string", "description": "文本片段"},
            "chunk_base64": {"type": "string", "description": "Base64 编码片段"},
        },
        "required": ["action"],
    }

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._data_dir = Path(data_dir) if data_dir else _default_blob_dir()

    def _session_key(self, context: ToolPermissionContext | None) -> str:
        extra: dict[str, Any] = {}
        user_id = ""
        if context is not None:
            extra = getattr(context, "extra", None) or {}
            user_id = getattr(context, "user_id", "") or ""
        return str(extra.get("session_id") or user_id or "default")

    def _session_dir(self, context: ToolPermissionContext | None) -> Path:
        extra: dict[str, Any] = {}
        if context is not None:
            extra = getattr(context, "extra", None) or {}
        base = Path(str(extra["blob_data_dir"])) if extra.get("blob_data_dir") else self._data_dir
        return base / self._session_key(context)

    def _paths(self, blob_id: str, session_dir: Path) -> tuple[Path, Path]:
        return session_dir / f"{blob_id}.data", session_dir / f"{blob_id}.json"

    def _meta(self, data_path: Path, meta_path: Path) -> dict[str, Any] | None:
        if not meta_path.is_file():
            return None
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        action = params.get("action")
        session_dir = self._session_dir(context)
        blob_id = params.get("blob_id")
        if action in ("append", "finalize", "discard") and not blob_id:
            return {"ok": False, "error": "blob_id is required"}
        if blob_id and not _BLOB_ID_RE.match(str(blob_id)):
            return {"ok": False, "error": f"invalid blob_id: {blob_id}"}
        try:
            if action == "create":
                return await self._create(session_dir)
            if action == "create_and_finalize":
                return await self._create_and_finalize(params, session_dir)
            if action == "append":
                data_path, meta_path = self._paths(blob_id, session_dir)
                return await self._append(params, data_path, meta_path)
            if action == "finalize":
                data_path, meta_path = self._paths(blob_id, session_dir)
                return await self._finalize(data_path, meta_path)
            if action == "discard":
                data_path, meta_path = self._paths(blob_id, session_dir)
                return await self._discard(data_path, meta_path)
            return {"ok": False, "error": f"unknown action: {action}"}
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _decode_chunk(params: dict[str, Any]) -> tuple[str | None, str | None]:
        if params.get("chunk") is not None:
            chunk = str(params["chunk"])
            if len(chunk.encode("utf-8")) > _MAX_CHUNK_BYTES:
                return None, f"chunk exceeds {_MAX_CHUNK_BYTES} bytes"
            return chunk, None
        raw = params.get("chunk_base64")
        if raw is not None:
            if len(str(raw)) > _MAX_CHUNK_B64_BYTES:
                return None, f"chunk_base64 exceeds {_MAX_CHUNK_B64_BYTES} bytes"
            try:
                return base64.b64decode(str(raw), validate=True).decode("utf-8"), None
            except (ValueError, UnicodeDecodeError, base64.binascii.Error):
                return None, "invalid base64 chunk"
        return None, "chunk or chunk_base64 is required"

    async def _create(self, session_dir: Path) -> dict[str, Any]:
        blob_id = uuid.uuid4().hex
        data_path, meta_path = self._paths(blob_id, session_dir)

        def _write() -> bool:
            session_dir.mkdir(parents=True, exist_ok=True)
            if len(list(session_dir.glob("*.data"))) >= _MAX_BLOBS_PER_SESSION:
                return False
            data_path.write_bytes(b"")
            meta_path.write_text(json.dumps({"status": "open", "total_bytes": 0}), encoding="utf-8")
            return True

        if not await asyncio.to_thread(_write):
            return {
                "ok": False,
                "error": f"too many blobs in session (limit {_MAX_BLOBS_PER_SESSION})",
            }
        return {"ok": True, "blob_id": blob_id, "status": "open"}

    async def _append(
        self, params: dict[str, Any], data_path: Path, meta_path: Path
    ) -> dict[str, Any]:
        chunk, error = self._decode_chunk(params)
        if error:
            return {"ok": False, "error": error}

        def _write() -> bool:
            meta = self._meta(data_path, meta_path)
            if meta is None:
                return False
            if meta.get("status") != "open":
                return False
            chunk_bytes = chunk.encode("utf-8")
            current = data_path.stat().st_size if data_path.exists() else 0
            if current + len(chunk_bytes) > _MAX_BLOB_BYTES:
                raise ValueError(f"blob exceeds {_MAX_BLOB_BYTES} bytes")
            data_path.parent.mkdir(parents=True, exist_ok=True)
            with data_path.open("ab") as fh:
                fh.write(chunk_bytes)
            meta["total_bytes"] = data_path.stat().st_size
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            return True

        ok = await asyncio.to_thread(_write)
        if not ok:
            meta = self._meta(data_path, meta_path)
            if meta is None:
                return {"ok": False, "error": "blob not found"}
            return {"ok": False, "error": "blob already finalized", "blob_id": meta.get("blob_id")}
        return {"ok": True}

    async def _finalize(self, data_path: Path, meta_path: Path) -> dict[str, Any]:
        def _write() -> dict[str, Any]:
            meta = self._meta(data_path, meta_path)
            if meta is None:
                return {"ok": False, "error": "blob not found"}
            meta["status"] = "finalized"
            meta["total_bytes"] = data_path.stat().st_size if data_path.is_file() else 0
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            return {"ok": True, "total_bytes": meta["total_bytes"], "status": "finalized"}

        return await asyncio.to_thread(_write)

    async def _discard(self, data_path: Path, meta_path: Path) -> dict[str, Any]:
        def _remove() -> bool:
            removed = False
            for p in (data_path, meta_path):
                if p.is_file():
                    p.unlink()
                    removed = True
            return removed

        removed = await asyncio.to_thread(_remove)
        return {"ok": removed, "removed": removed}

    async def _create_and_finalize(
        self, params: dict[str, Any], session_dir: Path
    ) -> dict[str, Any]:
        chunk, error = self._decode_chunk(params)
        if error:
            return {"ok": False, "error": error}
        created = await self._create(session_dir)
        if not created["ok"]:
            return created
        blob_id = created["blob_id"]
        data_path, meta_path = self._paths(blob_id, session_dir)
        ap = await self._append({"chunk": chunk}, data_path, meta_path)
        if not ap["ok"]:
            return ap
        fin = await self._finalize(data_path, meta_path)
        return {**fin, "blob_id": blob_id}


async def resolve_blob_text(context: ToolPermissionContext, blob_id: str) -> str:
    """功能：读取已 finalize 的 BLOB 内容并消费（文件删除）；二次读取抛 ValueError。"""
    if not _BLOB_ID_RE.match(str(blob_id)):
        raise ValueError(f"invalid blob_id: {blob_id}")
    extra = getattr(context, "extra", None) or {}
    base = Path(str(extra["blob_data_dir"])) if extra.get("blob_data_dir") else _default_blob_dir()
    user_id = getattr(context, "user_id", "") or ""
    session_id = str(extra.get("session_id") or user_id or "default")
    data_path = base / session_id / f"{blob_id}.data"
    meta_path = base / session_id / f"{blob_id}.json"

    def _take() -> str | None:
        if not data_path.is_file():
            return None
        text = data_path.read_text(encoding="utf-8")
        for p in (data_path, meta_path):
            if p.is_file():
                p.unlink()
        return text

    text = await asyncio.to_thread(_take)
    if text is None:
        raise ValueError(f"blob not found or already consumed: {blob_id}")
    return text
