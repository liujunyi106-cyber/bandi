# 功能：/media 工具族契约族——字节级产物/写盘/越界/gate/remote 双态/装配/确定性
# 行为族级覆盖（DoD-10）：image/audio/video 全部生产调用路径逐条有契约
from __future__ import annotations

import asyncio
import hashlib
import os
import struct
from pathlib import Path
from typing import Any

import pytest

_KINDS = ("image", "audio", "video")


def _ctx(tmp_path: Any, **extra: Any) -> Any:
    from bandi.tools.base import ToolPermissionContext

    merged: dict[str, Any] = {"project_root": str(tmp_path)}
    merged.update(extra)
    return ToolPermissionContext(user_id="unit-test", role="tester", mode="execute", extra=merged)


def _strip_env(monkeypatch: Any) -> None:
    for key in list(os.environ):
        if (
            key.startswith("BANDI_IMAGE_GEN_")
            or key.startswith("BANDI_VIDEO_GEN_")
            or key.startswith("BANDI_AUDIO_GEN_")
        ):
            monkeypatch.delenv(key, raising=False)


def _tool(kind: str):
    from bandi.tools.media.audio_generate import AudioGenerateTool
    from bandi.tools.media.image_generate import ImageGenerateTool
    from bandi.tools.media.video_generate import VideoGenerateTool

    return {"image": ImageGenerateTool, "audio": AudioGenerateTool, "video": VideoGenerateTool}[
        kind
    ]()


def _params(kind: str, tmp_path: Any, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "prompt": "示例提示词",
        "output_path": str(tmp_path / f"gen.{_EXT[kind]}"),
    }
    if kind == "image":
        base.update({"size": "512x512", "style": "vivid"})
    if kind == "audio":
        base.update({"voice": "female", "duration": 1.0})
    if kind == "video":
        base.update({"duration": 2.0})
    base.update(extra)
    return base


_EXT = {"image": "png", "audio": "wav", "video": "mp4"}


# ---------- 字节级产物族（真实可解析的占位媒体，DoD-10 全 kind） ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _KINDS)
async def test_generation_writes_readable_magic_bytes(
    tmp_path: Any, monkeypatch: Any, kind: str
) -> None:
    """离线兜底产出字节级可读回的媒体文件（PNG/WAV/MP4 magic），不依赖任何三方编码器。"""
    _strip_env(monkeypatch)
    tool = _tool(kind)
    out = tmp_path / f"gen.{_EXT[kind]}"
    result = await tool.execute(_params(kind, tmp_path), _ctx(tmp_path))
    assert result["success"] is True, f"{kind} 失败: {result}"
    raw = out.read_bytes()
    assert raw, f"{kind} 产物为空"
    if kind == "image":
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", raw[16:24])
        assert (width, height) == (512, 512)
    elif kind == "audio":
        assert raw.startswith(b"RIFF") and raw[8:12] == b"WAVE"
        sample_rate = struct.unpack("<I", raw[24:28])[0]
        assert sample_rate == 16000
    else:
        assert raw.startswith(b"\x00\x00\x00") and b"ftyp" in raw[:24] and b"mdat" in raw


@pytest.mark.asyncio
async def test_image_png_reuses_provider_hash_width_height(tmp_path: Any, monkeypatch: Any) -> None:
    _strip_env(monkeypatch)
    tool = _tool("image")
    result = await tool.execute(_params("image", tmp_path, size="256x256"), _ctx(tmp_path))
    assert result["success"] is True
    raw = (tmp_path / "gen.png").read_bytes()
    width, height = struct.unpack(">II", raw[16:24])
    assert (width, height) == (256, 256)


# ---------- schema 族（prompt required / enum / additionalProperties False） ----------


def test_schema_prompt_required_and_additional_properties_closed() -> None:
    for kind in _KINDS:
        tool = _tool(kind)
        assert tool.validate_params({}) != []
        assert any("prompt" in e for e in tool.validate_params({}))
        assert tool.validate_params({"prompt": "x", "nope": 1}) != []


def test_schema_enum_fields_reject_unknown() -> None:
    assert _tool("image").validate_params({"prompt": "x", "size": "999x999"}) != []
    assert _tool("audio").validate_params({"prompt": "x", "voice": "robot"}) != []
    assert _tool("video").validate_params({"prompt": "x", "duration": "fast"}) != []


def test_schema_valid_params_pass() -> None:
    assert (
        _tool("image").validate_params({"prompt": "x", "size": "1024x1024", "style": "natural"})
        == []
    )
    assert _tool("audio").validate_params({"prompt": "x", "voice": "male", "duration": 2.0}) == []
    assert _tool("video").validate_params({"prompt": "x", "duration": 1.5}) == []


# ---------- 相对 output_path 族：file 元数据须按 project_root（非 CWD）解析 ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _KINDS)
async def test_relative_output_path_file_field_resolves_under_project_root(
    tmp_path: Any, monkeypatch: Any, kind: str
) -> None:
    """相对 output_path 时，返回的 file 字段与实际落盘（project_root 解析）一致。"""
    _strip_env(monkeypatch)
    tool = _tool(kind)
    relative = f"sub/gen-relative.{_EXT[kind]}"
    result = await tool.execute(_params(kind, tmp_path, output_path=relative), _ctx(tmp_path))
    assert result["success"] is True, f"{kind} 失败: {result}"
    expected = (tmp_path / relative).resolve()
    assert Path(result["file"]).resolve() == expected
    assert expected.is_file()


# ---------- 路径安全族（破坏者：越界 fail-closed，DoD-3） ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _KINDS)
async def test_output_path_traversal_rejected(tmp_path: Any, monkeypatch: Any, kind: str) -> None:
    _strip_env(monkeypatch)
    tool = _tool(kind)
    result = await tool.execute(
        {"prompt": "x", "output_path": "../escape." + _EXT[kind]}, _ctx(tmp_path)
    )
    assert result["success"] is False
    assert "outside workspace" in str(result.get("error", "")).lower()
    assert not (tmp_path.parent / f"escape.{_EXT[kind]}").exists()


# ---------- 装配族（factory 注册 + feature gate fail-closed） ----------


def test_media_tools_registered_with_gates() -> None:
    from bandi.agent.factory import build_tool_registry
    from bandi.tools.base import ToolCategory

    registry = build_tool_registry(feature_flags={"image": True, "media": True})
    image = registry.get("image_generate")
    audio = registry.get("audio_generate")
    video = registry.get("video_generate")
    assert image is not None and image.category == ToolCategory.IMAGE
    assert audio is not None and audio.category == ToolCategory.MEDIA
    assert video is not None and video.category == ToolCategory.MEDIA
    assert image.to_tool_meta()["capability_tags"] == ["network", "llm_call"]
    assert audio.to_tool_meta()["capability_tags"] == ["network", "llm_call"]
    assert video.to_tool_meta()["capability_tags"] == ["network", "llm_call"]

    closed_image = build_tool_registry(feature_flags={"image": False, "media": True})
    assert closed_image.get("image_generate") is None
    assert closed_image.get("audio_generate") is not None
    closed_media = build_tool_registry(feature_flags={"image": True, "media": False})
    assert closed_media.get("audio_generate") is None
    assert closed_media.get("video_generate") is None
    assert closed_media.get("image_generate") is not None


# ---------- 服务层：remote 双态（无 key → offline；有 key+mock → remote） ----------


def _fresh_service() -> Any:
    from bandi.generation.service import GenerationService
    from bandi.llm.capabilities.registry import CapabilityRegistry

    return GenerationService(registry=CapabilityRegistry())


@pytest.mark.asyncio
async def test_service_offline_when_no_credentials(monkeypatch: Any) -> None:
    _strip_env(monkeypatch)
    from bandi.generation.base import GENERATION_KINDS

    svc = _fresh_service()
    for kind in GENERATION_KINDS:
        out = await svc.generate(kind=kind, prompt="无凭证示例")
        assert out.success is True, f"{kind} offline 兜底失败: {out.error}"
        assert out.provider == "offline"
        assert bool(out.data)
        assert out.meta.get("placeholder") is True


@pytest.mark.asyncio
async def test_service_remote_used_when_credentials_present(
    monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("BANDI_IMAGE_GEN_URL", "http://mock.test/v1/gen")
    monkeypatch.setenv("BANDI_IMAGE_GEN_API_KEY", "test-key")

    import httpx

    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode("utf-8", "replace")
        return httpx.Response(
            200, content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, headers={"content-type": "image/png"}
        )

    svc = _fresh_service()
    from bandi.generation.remote import RemoteGenerationBackend

    svc.register(RemoteGenerationBackend(kind="image", transport=httpx.MockTransport(handler)))
    out = await svc.generate(kind="image", prompt="有凭证示例")
    assert out.success is True, f"remote 失败: {out.error}"
    assert out.provider == "remote_image"
    assert out.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert "prompt" in captured["body"]
    assert "有凭证示例" in captured["body"]
    assert captured["url"] == "http://mock.test/v1/gen"


@pytest.mark.asyncio
async def test_service_remote_unavailable_missing_key(monkeypatch: Any) -> None:
    monkeypatch.setenv("BANDI_IMAGE_GEN_URL", "http://mock.test/v1/gen")
    monkeypatch.delenv("BANDI_IMAGE_GEN_API_KEY", raising=False)
    svc = _fresh_service()
    from bandi.generation.remote import RemoteGenerationBackend

    backend = RemoteGenerationBackend(kind="image")
    assert backend.available() is False
    svc.register(backend)
    out = await svc.generate(kind="image", prompt="无 key 的 url")
    assert out.provider == "offline"  # 有 base_url 无 key → 仍走 offline 兜底


# ---------- 服务层：provider override + failover + stats ----------


@pytest.mark.asyncio
async def test_service_provider_override_pins_backend(monkeypatch: Any) -> None:
    _strip_env(monkeypatch)
    svc = _fresh_service()
    out = await svc.generate(kind="image", prompt="x", provider="offline")
    assert out.provider == "offline" and out.success is True


@pytest.mark.asyncio
async def test_service_failover_to_offline_on_remote_failure(monkeypatch: Any) -> None:
    _strip_env(monkeypatch)
    from bandi.generation.base import GenerationOutput

    class _Flaky:
        name = "flaky"
        kinds = ("image",)

        def available(self) -> bool:
            return True

        async def generate(self, *, kind: str, prompt: str, **params: Any) -> GenerationOutput:
            raise RuntimeError("provider down")

    svc = _fresh_service()
    svc.register(_Flaky(), prepend=True)
    out = await svc.generate(kind="image", prompt="x")
    assert out.success is True
    assert out.provider == "offline"


@pytest.mark.asyncio
async def test_service_records_attempt_stat(monkeypatch: Any) -> None:
    _strip_env(monkeypatch)
    from bandi.llm.capabilities.provider_stats import get_provider_stats

    get_provider_stats().clear()
    svc = _fresh_service()
    await svc.generate(kind="image", prompt="统计示例")
    stat = get_provider_stats().get("image_gen", "offline")
    assert stat is not None and stat.successes == 1


# ---------- DoD-9 确定性同族（sha256 断言最终产物稳定） ----------


def test_offline_generation_deterministic_same_prompt(monkeypatch: Any) -> None:
    _strip_env(monkeypatch)
    from bandi.generation.offline import OfflineGenerationBackend

    backend = OfflineGenerationBackend()

    async def run() -> bytes:
        out = await backend.generate(kind="image", prompt="同一提示词")
        return out.data or b""

    first = asyncio.run(run())
    second = asyncio.run(run())
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    assert first == second


def test_offline_generation_differs_across_prompts(monkeypatch: Any) -> None:
    _strip_env(monkeypatch)
    from bandi.generation.offline import OfflineGenerationBackend

    backend = OfflineGenerationBackend()

    async def run(prompt_str: str) -> bytes:
        out = await backend.generate(kind="image", prompt=prompt_str)
        return out.data or b""

    a = asyncio.run(run("提示A"))
    b = asyncio.run(run("提示B"))
    assert hashlib.sha256(a).digest() != hashlib.sha256(b).digest()
