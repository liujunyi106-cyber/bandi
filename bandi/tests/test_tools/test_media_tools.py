# 功能：媒体工具单测——schema/默认落盘/provider 透出/错误处理
from __future__ import annotations

import os
from typing import Any

import pytest

from bandi.tools.base import ToolPermissionContext
from bandi.tools.media.audio_generate import AudioGenerateTool
from bandi.tools.media.base import build_media_filename
from bandi.tools.media.image_generate import ImageGenerateTool
from bandi.tools.media.video_generate import VideoGenerateTool


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: Any) -> None:
    for key in list(os.environ):
        if key.startswith("BANDI_") and key.endswith("GEN_URL") or key.endswith("GEN_API_KEY"):
            monkeypatch.delenv(key, raising=False)
    from bandi.generation.service import reset_generation_service

    reset_generation_service()


def _ctx(tmp_path: Any) -> ToolPermissionContext:
    return ToolPermissionContext(
        user_id="unit-test", role="tester", mode="execute", extra={"project_root": str(tmp_path)}
    )


def test_build_media_filename_stems_prompt() -> None:
    name = build_media_filename("一只橘猫在窗台", "image")
    assert name.endswith(".png")
    assert "一只橘猫" in name
    assert build_media_filename("同一提示词", "image") == build_media_filename(
        "同一提示词", "image"
    )
    assert build_media_filename("图片 生成", "image") != build_media_filename("视频 生成", "video")


@pytest.mark.asyncio
async def test_image_generate_default_output_path(tmp_path: Any) -> None:
    tool = ImageGenerateTool()
    result = await tool.execute({"prompt": "海边日落"}, _ctx(tmp_path))
    assert result["success"] is True
    assert result["placeholder"] is True
    assert result["provider"] == "offline"
    assert result["file"].endswith(".png")
    assert result["width"] == 512 and result["height"] == 512
    import os

    assert os.path.exists(result["file"])


@pytest.mark.asyncio
async def test_image_generate_custom_size_and_path(tmp_path: Any) -> None:
    tool = ImageGenerateTool()
    out_path = str(tmp_path / "custom.png")
    result = await tool.execute(
        {"prompt": "抽象画", "size": "1024x1024", "style": "vivid", "output_path": out_path},
        _ctx(tmp_path),
    )
    assert result["success"] is True
    assert result["width"] == 1024
    assert result["file"] == out_path
    import struct

    raw = (tmp_path / "custom.png").read_bytes()
    width, height = struct.unpack(">II", raw[16:24])
    assert (width, height) == (1024, 1024)


@pytest.mark.asyncio
async def test_audio_generate_duration_respected(tmp_path: Any) -> None:
    tool = AudioGenerateTool()
    result = await tool.execute({"prompt": "早安", "duration": 1.0}, _ctx(tmp_path))
    assert result["success"] is True
    assert result["duration_sec"] == 1.0
    import struct

    data = open(result["file"], "rb").read()
    assert data[:4] == b"RIFF"
    sample_rate = struct.unpack("<I", data[24:28])[0]
    assert sample_rate == 16000


@pytest.mark.asyncio
async def test_video_generate_placeholder(tmp_path: Any) -> None:
    tool = VideoGenerateTool()
    result = await tool.execute({"prompt": "城市夜景", "duration": 2.0}, _ctx(tmp_path))
    assert result["success"] is True
    assert result["placeholder"] is True
    data = open(result["file"], "rb").read()
    assert b"ftyp" in data[:32] and b"mdat" in data


@pytest.mark.asyncio
async def test_schema_rejects_bad_params() -> None:
    assert ImageGenerateTool().validate_params({"prompt": "x", "size": "999x999"}) != []
    assert AudioGenerateTool().validate_params({"prompt": "", "duration": 100}) != []
    assert VideoGenerateTool().validate_params({"duration": -1}) != []


@pytest.mark.asyncio
async def test_missing_prompt_fails_gracefully(tmp_path: Any) -> None:
    result = await ImageGenerateTool().execute({"prompt": "  "}, _ctx(tmp_path))
    assert result["success"] is False
    assert "prompt" in result["error"]
