# 功能：媒体生成服务（路由候选/重试/离线兜底/统计）
from __future__ import annotations

import logging
import time
from typing import Any

from bandi.generation.adapters import from_generation_backend, kind_to_task
from bandi.generation.base import GENERATION_KINDS, GenerationBackend, GenerationOutput
from bandi.generation.offline import OfflineGenerationBackend
from bandi.llm.capabilities.profile import (
    CapabilityContract,
    CapabilityProfile,
    Modality,
)
from bandi.llm.capabilities.registry import CapabilityRegistry, Invoker, get_capability_registry
from bandi.llm.capabilities.router import CapabilityRouter

logger = logging.getLogger(__name__)


class GenerationService:
    """媒体生成服务：按任务契约路由候选、重试、以离线占位为兜底地板。"""

    def __init__(
        self,
        *,
        registry: CapabilityRegistry | None = None,
        offline_floor: bool = True,
    ) -> None:
        self.registry = registry if registry is not None else get_capability_registry()
        self.offline_floor = offline_floor
        self._offline = OfflineGenerationBackend()
        self._register(self._offline)
        self._router = CapabilityRouter(self.registry)

    def _register(self, backend: GenerationBackend, *, prepend: bool = False) -> None:
        profiles = from_generation_backend(backend)
        if prepend:
            for profile in reversed(profiles):  # 保持注册序：prepend 的先注册
                self.registry.register(profile, invoker=backend)
        else:
            for profile in profiles:
                self.registry.register(profile, invoker=backend)

    def register(self, backend: GenerationBackend, *, prepend: bool = False) -> None:
        """注册一个额外后端（如 remote）。用 prepend=True 可令其优先于现有 real 候选。"""
        self._register(backend, prepend=prepend)

    def candidates_for(self, kind: str) -> list[CapabilityProfile]:
        task = kind_to_task(kind)
        if task is None:
            return []
        contract = CapabilityContract(task=task, inputs=frozenset({Modality.TEXT}))
        return self._router.candidates(contract, offline_floor=self.offline_floor)

    async def generate(
        self,
        *,
        kind: str,
        prompt: str,
        provider: str | None = None,
        max_retries: int = 2,
        retry_delay_sec: float = 1.0,
        **params: Any,
    ) -> GenerationOutput:
        """对某媒体 kind 发起生成。

        候选按能力路由得分排序；逐个尝试，失败/缺可用者自动下一候选
        始终以离线占位为最终兜底。``provider`` 钉选时只走该 Provider +
        离线地板（显式钉选应被遵守，同 router 语义）。
        """
        if kind not in GENERATION_KINDS:
            return GenerationOutput.failure(kind, f"unknown generation kind: {kind}")
        if not prompt or not str(prompt).strip():
            return GenerationOutput.failure(kind, "prompt is required")

        task = kind_to_task(kind)
        task_name = task.value if task else kind
        profiles = self.candidates_for(kind)

        # 钉选 Provider 时 filter（offline 地板仍保留）
        if provider:
            profiles = [p for p in profiles if p.provider == provider or _is_offline(p)]

        if not profiles:
            return GenerationOutput.failure(
                kind, f"no generation backend available for {kind!r}", provider=provider or ""
            )

        last_error = ""
        attempts = max(0, max_retries) + 1
        tried: set[str] = set()
        for profile in profiles:
            if profile.backend_class.value == "offline" or profile.available():
                for attempt in range(attempts):
                    backend = self._invoker_for(profile)
                    if backend is None:
                        last_error = f"no invoker for {profile.id}"
                        break
                    started = time.monotonic()
                    try:
                        out = await backend.generate(kind=kind, prompt=prompt, **params)
                    except Exception as e:  # noqa: BLE001 - 候选失败继续降级
                        last_error = f"{type(e).__name__}: {e}"
                        self._record_attempt(task_name, profile.provider, False, started)
                        if attempt < attempts - 1:
                            await _sleep(retry_delay_sec)
                        continue

                    self._record_attempt(task_name, profile.provider, out.success, started)
                    if out.success:
                        return out
                    last_error = out.error or "generation returned failure"
                    if attempt < attempts - 1:
                        await _sleep(retry_delay_sec)
            tried.add(profile.id)

        return GenerationOutput.failure(
            kind,
            f"all {len(tried)} candidate(s) failed; last error: {last_error}",
            provider=provider or "",
        )

    def _invoker_for(self, profile: CapabilityProfile) -> Invoker | None:
        return self.registry.get_invoker(profile.id)

    def _record_attempt(self, task: str, provider: str, success: bool, started: float) -> None:
        try:
            from bandi.llm.capabilities.provider_stats import get_provider_stats

            get_provider_stats().record_attempt(
                task,
                provider,
                success=success,
                latency_ms=(time.monotonic() - started) * 1000 if success else None,
            )
        except Exception as e:  # noqa: BLE001 - 统计尽力而为
            logger.debug("provider stats record failed: %s", e)


def _is_offline(profile: CapabilityProfile) -> bool:
    return profile.backend_class.value == "offline"


async def _sleep(secs: float) -> None:
    import asyncio

    await asyncio.sleep(secs)


_SERVICE: GenerationService | None = None


def build_default_generation_service(
    *,
    registry: CapabilityRegistry | None = None,
    enable_remote: bool = True,
) -> GenerationService:
    """构建默认生成服务：离线下垫底，并按 env 配置装载 remote 后端。"""
    svc = GenerationService(registry=registry)
    if enable_remote:
        from bandi.generation.remote import RemoteGenerationBackend

        for kind in GENERATION_KINDS:
            svc.register(RemoteGenerationBackend(kind=kind))
    return svc


def get_generation_service() -> GenerationService:
    """返回进程级默认生成服务（懒创建）。"""
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = build_default_generation_service()
    return _SERVICE


def reset_generation_service() -> None:
    """重置进程级生成服务（测试辅助）。"""
    global _SERVICE
    _SERVICE = None


__all__ = [
    "GenerationService",
    "build_default_generation_service",
    "get_generation_service",
    "reset_generation_service",
]
