# 功能：safety 模块统一导出 + 从 BandiConfig 创建安全护栏实例

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bandi.core.config import BandiConfig

if TYPE_CHECKING:
    from bandi.core.event_bus import EventBus
from bandi.safety.content_filter import ContentFilter, FilterResult
from bandi.safety.external_content import wrap_external_content
from bandi.safety.output_sanitizer import OutputSanitizer, OutputSanitizerConfig
from bandi.safety.pii_detector import PIIDetector, PIIScanResult
from bandi.safety.prompt_guard import PromptGuard, ScanResult

logger = logging.getLogger(__name__)


def create_safety_modules(
    config: BandiConfig, event_bus: EventBus | None = None
) -> tuple[PromptGuard, ContentFilter, PIIDetector, OutputSanitizer]:
    """从 BandiConfig 创建四大安全护栏实例。必须在应用入口调用以激活安全模块。"""
    try:
        guard = PromptGuard(patterns=config.safety.prompt_guard_patterns, event_bus=event_bus)
    except Exception:  # noqa: BLE001
        logger.warning("PromptGuard construction failed, using default")
        guard = PromptGuard(event_bus=event_bus)
    try:
        content = ContentFilter(
            categories=config.safety.content_filter_categories, event_bus=event_bus
        )
    except Exception:  # noqa: BLE001
        logger.warning("ContentFilter construction failed, using default")
        content = ContentFilter(event_bus=event_bus)
    pii = PIIDetector(event_bus=event_bus)
    try:
        sanitizer = OutputSanitizer(
            config=OutputSanitizerConfig.from_config(
                max_chars=config.safety.max_output_chars,
                max_lines=config.safety.max_output_lines,
            ),
        )
    except Exception:  # noqa: BLE001
        logger.warning("OutputSanitizer construction failed, using default")
        sanitizer = OutputSanitizer()
    return guard, content, pii, sanitizer


__all__ = [
    "ContentFilter",
    "FilterResult",
    "OutputSanitizer",
    "OutputSanitizerConfig",
    "PIIDetector",
    "PIIScanResult",
    "PromptGuard",
    "ScanResult",
    "create_safety_modules",
    "wrap_external_content",
]
