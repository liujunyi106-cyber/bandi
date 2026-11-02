# 功能：AudioGenerateTool — 文本 → 音频（离线确定性占位 WAV + 远程候选骨架）
from __future__ import annotations

from typing import Any

from bandi.generation.service import get_generation_service
from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool
from bandi.tools.media.base import (
    media_default_output_path,
    media_project_root,
    save_generation_output,
)

_AUDIO_VOICES = ["alloy", "echo", "fable", "onyx", "nova", "shimmer", "female", "male"]


class AudioGenerateTool(FsTool):
    name: str = "audio_generate"
    description: str = (
        "Generate an audio clip from a text prompt. Uses a configured remote "
        "provider when credentials are available (BANDI_AUDIO_GEN_URL/API_KEY); "
        "otherwise produces a deterministic offline placeholder WAV. Writes the "
        "file to output_path and returns file metadata."
    )
    category: ToolCategory = ToolCategory.MEDIA
    feature_group: str = "media"
    capability_tags: list[str] = [ToolCapability.NETWORK.value, ToolCapability.LLM_CALL.value]
    risk_level: str = "low"
    is_concurrency_safe: bool = True
    is_idempotent: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text to speak / audio script."},
            "voice": {
                "type": "string",
                "enum": _AUDIO_VOICES,
                "description": "Voice to use (default female).",
            },
            "duration": {
                "type": "number",
                "minimum": 0.1,
                "maximum": 30.0,
                "description": "Audio duration in seconds (default 1.0).",
            },
            "output_path": {
                "type": "string",
                "description": "Destination .wav path (default workspace/generated/audio/).",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        try:
            prompt = str(params.get("prompt") or "").strip()
            if not prompt:
                return {"success": False, "error": "prompt is required"}
            output_path = params.get("output_path")
            if not output_path:
                output_path = media_default_output_path(prompt, "audio", context)
            params_for_gen = {
                "prompt": prompt,
                "voice": params.get("voice", "female"),
                "duration": float(params.get("duration", 1.0)),
            }
            out = await get_generation_service().generate(kind="audio", **params_for_gen)
            if not out.success:
                return {"success": False, "error": out.error, "provider": out.provider}

            ok, err, saved_path = await save_generation_output(
                out,
                output_path=output_path,
                project_root=str(media_project_root(context)),
            )
            if not ok:
                return {"success": False, "error": err}
            return {
                "success": True,
                "kind": "audio",
                "provider": out.provider,
                "model": out.model,
                "mime": out.mime,
                "file": saved_path,
                "file_size_bytes": len(out.data or b""),
                "placeholder": bool(out.meta.get("placeholder", False)),
                "duration_sec": out.meta.get("duration_sec"),
            }
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}


__all__ = ["AudioGenerateTool"]
