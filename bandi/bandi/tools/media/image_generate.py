# 功能：ImageGenerateTool — 文本 → 图片（离线确定性占位 + 远程候选骨架）
from __future__ import annotations

from typing import Any

from bandi.generation.service import get_generation_service
from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool
from bandi.tools.media.base import media_default_output_path, save_generation_output

_IMAGE_SIZES = ["256x256", "512x512", "1024x1024"]
_IMAGE_STYLES = ["natural", "vivid"]


class ImageGenerateTool(FsTool):
    name: str = "image_generate"
    description: str = (
        "Generate an image from a text prompt. Uses a configured remote provider "
        "when credentials are available (BANDI_IMAGE_GEN_URL/API_KEY); otherwise "
        "produces a deterministic offline placeholder PNG. Writes the file to "
        "output_path and returns file metadata."
    )
    category: ToolCategory = ToolCategory.IMAGE
    feature_group: str = "image"
    capability_tags: list[str] = [ToolCapability.NETWORK.value, ToolCapability.LLM_CALL.value]
    risk_level: str = "low"
    is_concurrency_safe: bool = True
    is_idempotent: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Image description."},
            "size": {
                "type": "string",
                "enum": _IMAGE_SIZES,
                "description": "Output size (default 512x512).",
            },
            "style": {
                "type": "string",
                "enum": _IMAGE_STYLES,
                "description": "Rendering style (default natural).",
            },
            "output_path": {
                "type": "string",
                "description": "Destination .png path (default workspace/generated/image/).",
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
                output_path = media_default_output_path(prompt, "image", context)
            params_for_gen = {
                "prompt": prompt,
                "size": params.get("size", "512x512"),
                "style": params.get("style", "natural"),
            }
            out = await get_generation_service().generate(kind="image", **params_for_gen)
            if not out.success:
                return {"success": False, "error": out.error, "provider": out.provider}

            from bandi.tools.media.base import media_project_root

            ok, err, saved_path = await save_generation_output(
                out,
                output_path=output_path,
                project_root=str(media_project_root(context)),
            )
            if not ok:
                return {"success": False, "error": err}
            return {
                "success": True,
                "kind": "image",
                "provider": out.provider,
                "model": out.model,
                "mime": out.mime,
                "file": saved_path,
                "file_size_bytes": len(out.data or b""),
                "placeholder": bool(out.meta.get("placeholder", False)),
                "width": out.meta.get("width"),
                "height": out.meta.get("height"),
            }
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}


__all__ = ["ImageGenerateTool"]
