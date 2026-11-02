# 功能：integration 契约族——configure_settings env 键存在性（不泄值）+ speech_to_text 双轨 env 门控
# 行为族级覆盖（DoD-10）：settings 存在/缺失/空入参 + 语音无 key 明确失败/远端正向用 fake httpx 断言请求构造
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from bandi.tools.base import ToolPermissionContext


def _ctx() -> ToolPermissionContext:
    return ToolPermissionContext(user_id="unit-test", role="tester", mode="execute")


def _tools() -> Any:
    from bandi.tools.integration.configure_settings import ConfigureSettingsTool
    from bandi.tools.integration.speech_to_text import SpeechToTextTool

    return ConfigureSettingsTool(), SpeechToTextTool()


# ---------- configure_settings 族 ----------


@pytest.mark.asyncio
async def test_settings_env_keys_exist_and_do_not_leak_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _ = _tools()
    monkeypatch.setenv("BANDI_TEST_SECRET_A", "super-secret-value")
    result = await settings.execute(
        {"keys": ["BANDI_TEST_SECRET_A", "BANDI_TEST_MISSING_1"]}, _ctx()
    )
    assert result["success"] is True
    assert result["existing"] == ["BANDI_TEST_SECRET_A"]
    assert result["missing"] == ["BANDI_TEST_MISSING_1"]
    blob = str(result)
    assert "super-secret-value" not in blob


@pytest.mark.asyncio
async def test_settings_empty_keys_returns_empty_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    settings, _ = _tools()
    monkeypatch.setenv("BANDI_TEST_SECRET_B", "v")
    result = await settings.execute({}, _ctx())
    assert result["success"] is True
    assert result["existing"] == []
    assert result["missing"] == []


@pytest.mark.asyncio
async def test_settings_env_key_set_to_empty_string_counts_as_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _ = _tools()
    monkeypatch.setenv("BANDI_TEST_SECRET_C", "")
    result = await settings.execute({"keys": ["BANDI_TEST_SECRET_C"]}, _ctx())
    assert result["success"] is True
    assert result["existing"] == ["BANDI_TEST_SECRET_C"]


# ---------- speech_to_text 族 ----------


@pytest.mark.asyncio
async def test_stt_missing_api_key_fails_with_clear_error_and_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, stt = _tools()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "bandi.tools.integration.speech_to_text.httpx.post",
        lambda *a, **k: calls.append({"args": a, "kwargs": k}) or _raise_fake(),
    )

    def _raise_fake() -> Any:
        raise AssertionError("network call must not happen without key")

    result = await stt.execute(
        {"file_path": str(Path("C:/") / "nonexistent" / "a.mp3")},
        _ctx(),
    )
    assert calls == []
    assert result["success"] is False
    assert "OPENAI_API_KEY" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_stt_missing_file_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings, stt = _tools()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    result = await stt.execute({"file_path": str(tmp_path / "no.wav")}, _ctx())
    assert result["success"] is False
    assert "no.wav" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_stt_remote_sends_expected_request_when_key_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, stt = _tools()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"\xff\xfb fake audio bytes")
    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"text": "好的，明白了"}

    def _fake_post(url: str, **kwargs: Any) -> _FakeResp:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeResp()

    monkeypatch.setattr("bandi.tools.integration.speech_to_text.httpx.post", _fake_post)

    result = await stt.execute(
        {"file_path": str(audio), "model": "whisper-1"},
        _ctx(),
    )
    assert result["success"] is True
    assert result["text"] == "好的，明白了"
    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    headers = captured["kwargs"]["headers"]
    assert headers["Authorization"] == "Bearer sk-fake"
    files = captured["kwargs"]["files"]["file"]
    assert b"\xff\xfb" in files[1]
    assert captured["kwargs"]["data"]["model"] == "whisper-1"


@pytest.mark.asyncio
async def test_stt_remote_api_error_maps_to_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, stt = _tools()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"abc")

    class _FakeErrResp:
        def raise_for_status(self) -> None:
            req = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
            resp = httpx.Response(401, request=req, text="invalid api key")
            raise httpx.HTTPStatusError("unauthorized", request=req, response=resp)

    monkeypatch.setattr(
        "bandi.tools.integration.speech_to_text.httpx.post",
        lambda *a, **k: _FakeErrResp(),
    )
    result = await stt.execute({"file_path": str(audio)}, _ctx())
    assert result["success"] is False
    assert "401" in str(result.get("error", "")) or "invalid api key" in str(
        result.get("error", "")
    )
