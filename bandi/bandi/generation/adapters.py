# 功能：媒体生成后端到能力画像适配（from_generation_backend）
# 1) Bandi 无 VIDEO 模态（Modality 仅 text/image/audio/pdf），video kind 输出模态留空（不约束），任务仍为 VIDEO_GEN；
# 2) 返回 per-kind 画像列表（每个 kind 一个 profile，对齐 Phase4 规划「注册 3 个 OFFLINE 画像」，invoker 由 service 挂载）
from __future__ import annotations

from typing import Any

from bandi.llm.capabilities.profile import BackendClass, CapabilityProfile, Modality, TaskType

_KIND_TO_TASK_OUTPUT: dict[str, tuple[TaskType, Modality | None]] = {
    "image": (TaskType.IMAGE_GEN, Modality.IMAGE),
    "audio": (TaskType.AUDIO_GEN, Modality.AUDIO),
    "video": (TaskType.VIDEO_GEN, None),
}


def kind_to_task(kind: str) -> TaskType | None:
    pair = _KIND_TO_TASK_OUTPUT.get(kind)
    return pair[0] if pair else None


def kind_to_output(kind: str) -> Modality | None:
    pair = _KIND_TO_TASK_OUTPUT.get(kind)
    return pair[1] if pair else None


def from_generation_backend(backend: Any) -> list[CapabilityProfile]:
    """把媒体生成后端提升为 per-kind 能力画像列表。

    - ``offline`` 后端 → ``BackendClass.OFFLINE``（cost_tier=0，无需凭证）；
    - ``remote_*`` 后端 → 借位 ``BackendClass.MULTIMODAL_LLM``（"非离线"语义
      router 仅按 OFFLINE 二分垫底，本方案不新增 BackendClass 枚举）。
    """
    name = str(getattr(backend, "name", "")) or "unknown"
    is_offline = name == "offline"
    backend_class = BackendClass.OFFLINE if is_offline else BackendClass.MULTIMODAL_LLM
    cost_tier = 0 if is_offline else 2
    requires_credentials = not is_offline
    available = getattr(backend, "available", None)

    profiles: list[CapabilityProfile] = []
    for kind in getattr(backend, "kinds", ()):
        task = kind_to_task(kind)
        if task is None:
            continue
        output = kind_to_output(kind)
        profiles.append(
            CapabilityProfile(
                id=f"gen:{kind}:{name}",
                provider=name,
                backend_class=backend_class,
                inputs=frozenset({Modality.TEXT}),
                outputs=frozenset({output}) if output is not None else frozenset(),
                tasks=frozenset({task}),
                requires_credentials=requires_credentials,
                cost_tier=cost_tier,
                availability=available if callable(available) else None,
                metadata={"kind": kind},
            )
        )
    return profiles


__all__ = ["from_generation_backend", "kind_to_output", "kind_to_task"]
