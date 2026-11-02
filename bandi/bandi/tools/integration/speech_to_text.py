# 功能：speech_to_text——语音转写双轨：配置 OPENAI_API_KEY 走 Remote Whale（httpx），否则 Offline 明确失败占位
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from bandi.tools.base import BaseTool, ToolCapability, ToolCategory, ToolPermissionContext

_API_URL = "https://api.openai.com/v1/audio/transcriptions"
_MIME_BY_SUFFIX = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
}


class SpeechToTextTool(BaseTool):
    # 安全（③⁻）：转写文本外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content = True
    name = "speech_to_text"
    description = "把本地音频文件转为文字。优先走 Remote OpenAI Whisper（需设置 OPENAI_API_KEY）；未配置时返回明确的开启指引，不静默失败。"
    category = ToolCategory.INTEGRATION
    feature_group = "integration"
    capability_tags = [ToolCapability.NETWORK.value]
    risk_level = "medium"
    is_read_only = True
    is_idempotent = False
    aliases = ("transcribe_audio", "audio_to_text")
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "本地音频文件的绝对路径。"},
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "model": {"type": "string", "default": "whisper-1", "description": "转写模型名。"},
        },
        "required": ["file_path"],
    }

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> str | dict[str, Any]:
        try:
            return await self._transcribe(params)
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:200] if e.response is not None else ""
            return {
                "success": False,
                "error": f"speech_to_text API error {e.response.status_code}: {detail}",
            }
        except (ValueError, TypeError, KeyError, IndexError, OSError, httpx.HTTPError) as e:
            return {"success": False, "error": str(e)}

    async def _transcribe(self, params: dict[str, Any]) -> dict[str, Any]:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return {
                "success": False,
                "error": (
                    "OpenAI Whisper is not configured: set OPENAI_API_KEY to enable "
                    "Remote transcription; no local (offline) engine is installed yet."
                ),
            }
        file_path = params.get("file_path")
        if not file_path or not isinstance(file_path, str):
            raise ValueError("'file_path' must be a string path")
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise ValueError(f"audio file not found: {path}")
        model = params.get("model") or "whisper-1"
        data = path.read_bytes()
        mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")
        response = httpx.post(
            _API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (path.name, data, mime)},
            data={"model": model},
            timeout=120.0,
        )
        response.raise_for_status()
        text = response.json().get("text", "")
        if not text:
            raise ValueError("transcription returned empty text")
        return {"success": True, "text": text, "model": model}
