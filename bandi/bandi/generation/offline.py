# 功能：确定性离线媒体兜底后端（全 kind 占位产物）
from __future__ import annotations

from typing import Any

from bandi.generation.base import GenerationOutput
from bandi.generation.placeholders import (
    color_from_prompt,
    placeholder_mp4,
    silent_wav,
    solid_png,
)


def _parse_size(size: Any, default: tuple[int, int] = (512, 512)) -> tuple[int, int]:
    if isinstance(size, str):
        sep = "x" if "x" in size else None
        if sep:
            try:
                w, h = size.split(sep)
                return int(w), int(h)
            except (ValueError, TypeError):
                return default
    return default


class OfflineGenerationBackend:
    """对每个媒体 kind 始终可用的确定性占位后端（无凭证环境的兜底地板）。"""

    name = "offline"
    kinds = ("image", "video", "audio")

    def available(self) -> bool:
        return True

    async def generate(self, *, kind: str, prompt: str, **params: Any) -> GenerationOutput:
        if kind == "image":
            w, h = _parse_size(params.get("size"))
            rgb = color_from_prompt(prompt)
            data = solid_png(w, h, rgb)
            return GenerationOutput(
                success=True,
                kind="image",
                data=data,
                mime="image/png",
                filename="image.png",
                provider=self.name,
                model="offline-solid",
                meta={"placeholder": True, "width": w, "height": h, "rgb": list(rgb)},
            )
        if kind == "video":
            data = placeholder_mp4(prompt)
            return GenerationOutput(
                success=True,
                kind="video",
                data=data,
                mime="video/mp4",
                filename="video.mp4",
                provider=self.name,
                model="offline-clip",
                meta={"placeholder": True, "duration_sec": params.get("duration", 2)},
            )
        if kind == "audio":
            duration = float(params.get("duration") or params.get("duration_sec") or 1.0)
            data = silent_wav(prompt, duration_sec=duration)
            return GenerationOutput(
                success=True,
                kind="audio",
                data=data,
                mime="audio/wav",
                filename="audio.wav",
                provider=self.name,
                model="offline-tts",
                meta={"placeholder": True, "duration_sec": duration},
            )
        return GenerationOutput.failure(kind, f"offline backend cannot produce '{kind}'")


__all__ = ["OfflineGenerationBackend"]
