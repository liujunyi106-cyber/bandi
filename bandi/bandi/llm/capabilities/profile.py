# 功能：能力画像（模态/任务/后端类型定义 + CapabilityProfile）
"""能力注册核心类型 （⭐1）。

`CapabilityProfile` 描述一个后端（模型/Provider）能消费/产出的模态、可填充的任务角色
与后端类别；`CapabilityContract` 是调用方的需求契约，profile 与之匹配。

注意：`TaskType` 保留 完整枚举（含媒体生成/语音类）。当前 Bandi 是文本 Agent
仅 CHAT/VISION/EMBEDDING 会被查询与注册；IMAGE_GEN/
VIDEO_GEN/MESH_GEN/VFX_GEN/AUDIO_GEN/UPSCALE/TTS/ASR 为预留扩展，后续有对应管道时
再注册对应能力。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class Modality(StrEnum):
    """后端可消费/产出的一种输入/输出模态。"""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    PDF = "pdf"


class TaskType(StrEnum):
    """后端可填充的逻辑任务角色。"""

    CHAT = "chat"
    FAST = "fast"
    VISION = "vision"
    COMPRESSION = "compression"
    TITLE = "title"
    EMBEDDING = "embedding"
    # 预留扩展：Bandi 当前无对应管道，不注册不查询
    IMAGE_GEN = "image_gen"
    VIDEO_GEN = "video_gen"
    MESH_GEN = "mesh_gen"
    VFX_GEN = "vfx_gen"
    AUDIO_GEN = "audio_gen"
    UPSCALE = "upscale"
    TTS = "tts"
    ASR = "asr"


class BackendClass(StrEnum):
    """后端的实现类别 — 能力档案的分类器。"""

    # 原生处理多模态的对话模型。
    MULTIMODAL_LLM = "multimodal_llm"
    # 纯文本对话或嵌入模型。
    TEXT_LLM = "text_llm"
    # 始终可用的确定性兜底层（Mock）。
    OFFLINE = "offline"


def _as_modalities(values: Any) -> frozenset[Modality]:
    out: set[Modality] = set()
    for v in values or ():
        if isinstance(v, Modality):
            out.add(v)
            continue
        try:
            out.add(Modality(str(v)))
        except ValueError:
            continue
    return frozenset(out)


def _as_tasks(values: Any) -> frozenset[TaskType]:
    out: set[TaskType] = set()
    for v in values or ():
        if isinstance(v, TaskType):
            out.add(v)
            continue
        try:
            out.add(TaskType(str(v)))
        except ValueError:
            continue
    return frozenset(out)


@dataclass(frozen=True)
class CapabilityProfile:
    """一个已注册后端的能力描述。

    ``id`` 在注册表内唯一（如 ``chat:deepseek:deepseek-flash``）。可选的
    ``availability`` 回调让注册表无需了解 Provider 内部即可报告运行时可用性
    （凭证是否存在、后端是否可导入等）。
    """

    id: str
    provider: str
    backend_class: BackendClass
    inputs: frozenset[Modality] = frozenset({Modality.TEXT})
    outputs: frozenset[Modality] = frozenset({Modality.TEXT})
    tasks: frozenset[TaskType] = frozenset()
    default_model: str = ""
    requires_credentials: bool = False
    supports_progress: bool = False
    supports_streaming: bool = False
    # 0=免费/离线/本地；越大越贵/越受限。
    cost_tier: int = 0
    availability: Callable[[], bool] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", _as_modalities(self.inputs))
        object.__setattr__(self, "outputs", _as_modalities(self.outputs))
        object.__setattr__(self, "tasks", _as_tasks(self.tasks))

    def available(self) -> bool:
        """当前是否可用（availability 回调缺失时视为可用）。"""
        if self.availability is None:
            return True
        try:
            return bool(self.availability())
        except Exception as e:  # noqa: BLE001 - 可用性探测尽力而为
            logger.debug("availability probe failed: %s", e)
            return False

    def supports_input(self, modality: Modality | str) -> bool:
        try:
            return Modality(str(modality)) in self.inputs
        except ValueError:
            return False

    def supports_output(self, modality: Modality | str) -> bool:
        try:
            return Modality(str(modality)) in self.outputs
        except ValueError:
            return False

    def supports_task(self, task: TaskType | str) -> bool:
        try:
            return TaskType(str(task)) in self.tasks
        except ValueError:
            return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "backend_class": self.backend_class.value,
            "inputs": sorted(m.value for m in self.inputs),
            "outputs": sorted(m.value for m in self.outputs),
            "tasks": sorted(t.value for t in self.tasks),
            "default_model": self.default_model,
            "requires_credentials": self.requires_credentials,
            "supports_progress": self.supports_progress,
            "supports_streaming": self.supports_streaming,
            "cost_tier": self.cost_tier,
            "available": self.available(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class CapabilityContract:
    """调用方需求契约，用于与 profile 匹配。

    最小化设计：一个任务 + 需要的输入/输出模态。空模态集合表示「不约束」。
    """

    task: TaskType
    inputs: frozenset[Modality] = frozenset()
    outputs: frozenset[Modality] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.task, TaskType):
            object.__setattr__(self, "task", TaskType(str(self.task)))
        object.__setattr__(self, "inputs", _as_modalities(self.inputs))
        object.__setattr__(self, "outputs", _as_modalities(self.outputs))

    def matches(self, profile: CapabilityProfile) -> bool:
        if self.task not in profile.tasks:
            return False
        if not self.inputs <= profile.inputs:
            return False
        if not self.outputs <= profile.outputs:
            return False
        return True


__all__ = [
    "BackendClass",
    "CapabilityContract",
    "CapabilityProfile",
    "Modality",
    "TaskType",
]
