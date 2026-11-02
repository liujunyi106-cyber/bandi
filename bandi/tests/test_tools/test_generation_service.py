# 功能：GenerationService 单测——候选路由/重试/失败降级/统计
from __future__ import annotations

import os
from typing import Any

import pytest

from bandi.generation.base import GenerationOutput
from bandi.generation.offline import OfflineGenerationBackend
from bandi.generation.service import GenerationService
from bandi.llm.capabilities.registry import CapabilityRegistry, reset_capability_registry


def _fresh() -> GenerationService:
    reset_capability_registry()
    return GenerationService(registry=CapabilityRegistry())


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: Any) -> None:
    for key in list(os.environ):
        if "GEN_" in key and key.startswith("BANDI_"):
            monkeypatch.delenv(key, raising=False)


async def _gen(
    svc: GenerationService, kind: str = "image", prompt: str = "示例"
) -> GenerationOutput:
    return await svc.generate(kind=kind, prompt=prompt)


@pytest.mark.asyncio
async def test_generate_offline_floor_for_each_kind() -> None:
    svc = _fresh()
    for kind in ("image", "audio", "video"):
        out = await _gen(svc, kind=kind)
        assert out.success is True
        assert out.provider == "offline"
        assert out.meta.get("placeholder") is True
        assert bool(out.data)


@pytest.mark.asyncio
async def test_generate_rejects_unknown_kind_and_empty_prompt() -> None:
    svc = _fresh()
    assert (await svc.generate(kind="model3d", prompt="x")).success is False
    assert (await svc.generate(kind="image", prompt="  ")).success is False


@pytest.mark.asyncio
async def test_generate_retries_transient_failure_then_succeeds() -> None:
    svc = _fresh()
    calls: list[int] = []

    class _Flaky:
        name = "flaky"
        kinds = ("image",)

        def available(self) -> bool:
            return True

        async def generate(self, *, kind: str, prompt: str, **params: Any) -> GenerationOutput:
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("transient")
            return GenerationOutput(success=True, kind="image", data=b"ok", provider=self.name)

    svc.register(_Flaky())
    out = await _gen(svc)
    assert out.success is True
    assert len(calls) == 3  # 前两次失败 + 第三次成功，仍优先用 flaky 前候选


@pytest.mark.asyncio
async def test_generate_falls_back_to_offline_on_permanent_failure() -> None:
    svc = _fresh()
    calls = {"flaky": 0}

    class _Dead:
        name = "dead"
        kinds = ("image",)

        def available(self) -> bool:
            return True

        async def generate(self, *, kind: str, prompt: str, **params: Any) -> GenerationOutput:
            calls["flaky"] += 1
            raise RuntimeError("down")

    svc.register(_Dead())
    out = await _gen(svc)
    assert out.success is True
    assert out.provider == "offline"
    assert calls["flaky"] >= 1


@pytest.mark.asyncio
async def test_generate_stats_recorded() -> None:
    from bandi.llm.capabilities.provider_stats import get_provider_stats

    svc = _fresh()
    get_provider_stats().clear()
    await _gen(svc)
    await _gen(svc)
    stat = get_provider_stats().get("image_gen", "offline")
    assert stat is not None
    assert stat.successes == 2


@pytest.mark.asyncio
async def test_candidates_prefer_offline_when_only_offline() -> None:
    svc = _fresh()
    profiles = svc.candidates_for("image")
    assert len(profiles) >= 1
    assert all(p.provider == "offline" for p in profiles)


@pytest.mark.asyncio
async def test_offline_backend_rejects_unknown_kind() -> None:
    backend = OfflineGenerationBackend()
    out = await backend.generate(kind="model3d", prompt="x")
    assert out.success is False
