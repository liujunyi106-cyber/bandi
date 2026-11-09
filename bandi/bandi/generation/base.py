# 功能：媒体生成基础类型（GenerationOutput/GenerationBackend/kind 常量）
"""媒体生成基础类型 （⭐1）。

``GenerationBackend`` 是媒体生成策略协议（离线兜底 / 远程 HTTP 骨架）
``GenerationOutput`` 是统一产物描述；``GenerationService`` 借助能力层
（CapabilityRouter）选候选、重试、并以离线兜底为地板。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

GENERATION_KINDS: tuple[str, ...] = ("image", "audio", "video")

# 每个 kind 的默认下载/落盘扩展名
_KIND_EXT: dict[str, str] = {"image": "png", "audio": "wav", "video": "mp4"}

# 每 kind 的默认 mime
_KIND_MIME: dict[str, str] = {
    "image": "image/png",
    "audio": "audio/wav",
    "video": "video/mp4",
}


@dataclass
class GenerationOutput:
    """媒体生成的一次产物描述。"""

    success: bool
    kind: str
    data: bytes | None = None
    mime: str = ""
    filename: str = ""
    provider: str = ""
    model: str = ""
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(cls, kind: str, error: str, *, provider: str = "") -> GenerationOutput:
        return cls(success=False, kind=kind, error=error, provider=provider)


class GenerationBackend(Protocol):
    """媒体生成后端协议——候选由能力层画像描述。"""

    name: str
    kinds: tuple[str, ...]

    def available(self) -> bool: ...
    async def generate(self, *, kind: str, prompt: str, **params: Any) -> GenerationOutput: ...


def ext_for_kind(kind: str) -> str:
    """kind 的默认扩展名（未知 kind 回退 bin）。"""
    return _KIND_EXT.get(kind, "bin")


def mime_for_kind(kind: str) -> str:
    """kind 的默认 mime（未知 kind 回退 octet-stream）。"""
    return _KIND_MIME.get(kind, "application/octet-stream")


__all__ = [
    "GENERATION_KINDS",
    "GenerationBackend",
    "GenerationOutput",
    "ext_for_kind",
    "mime_for_kind",
]
