# 功能：Prompt 注入防御 — 基于正则规则匹配检测注入攻击（支持 YAML 配置加载）

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bandi.core.event_bus import EventBus

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    """注入扫描结果。"""

    blocked: bool
    action: str  # "block" | "warn" | "pass"
    matched_patterns: list[tuple[str, str]] = field(default_factory=list)


DEFAULT_INJECTION_PATTERNS: list[tuple[str, str]] = [
    # 指令覆盖类
    (
        r"(?i)(?:ignore|forget|disregard)\s+(?:all\s+)?(?:previous\s+)?(?:instructions|prompts|commands|rules)",
        "指令覆盖",
    ),
    (
        r"(?i)(?:new\s+rule[s]?|override\s+(?:system|my)\s+(?:instruction[s]?|prompt[s]?|rule[s]?))",
        "指令覆盖",
    ),
    (
        r"(?i)(?:you are now|act as|pretend to be)\s+(?:a[n]?\s+)?(?:developer|sysadmin|root|god|admin)",
        "角色越狱",
    ),
    (r"(?i)(?:DAN|Do Anything|Don't Any Rules|Delete All Norms)", "角色越狱"),
    # 系统标签伪造
    (r"(?i)\[SYSTEM\]", "系统标签注入"),
    (r"(?i)\[INST\]", "系统标签注入"),
    (r"<\|system\|>", "系统标签注入"),
    (r"<\|user\|>", "系统标签注入"),
    # Prompt 泄露
    (
        r"(?i)(?:show|reveal|output|print|display|dump)\s+(?:(?:me|the)\s+)?(?:(?:your|my)\s+system\s+|(?:your|my)\s+|system\s+)(?:prompt|instruction[s]?|rule[s]?|config)",
        "Prompt 泄露",
    ),
    (
        r"(?i)(?:repeat|copy|paste)\s+(?:the\s+)?(?:above|entire|full)?\s*(?:prompt|instruction[s]?|message)",
        "Prompt 泄露",
    ),
    # 数据提取
    (
        r"(?i)(?:extract|leak|exfil|export)\s+(?:all|your|the\s+)?(?:system\s+)?(?:prompt|instructions|context)",
        "数据提取",
    ),
    # 多语言变体
    (r"(?u)(?:忽略|忘记).*?(?:指令|提示|规则)", "中文指令覆盖"),
    (r"(?u)(?:你现在是|扮演|假装你是)\s+(?:一个|开发者|管理员|root)", "中文角色越狱"),
    (r"(?u)(?:显示|输出|打印|泄露)\s+(?:你的)?\s*(?:系统)?(?:提示|指令|规则)", "中文Prompt泄露"),
]


class PromptGuard:
    """

    检测点：InputParser 之后、消息进入 Agent Loop 之前。
    被拦截的消息直接返回预设拒绝消息，不进入 THINKING。
    """

    def __init__(
        self, patterns: list[tuple[str, str]] | None = None, event_bus: EventBus | None = None
    ) -> None:
        self._compiled: list[tuple[re.Pattern[str], str]] = []
        self._event_bus = event_bus
        for pattern_str, category in DEFAULT_INJECTION_PATTERNS if patterns is None else patterns:
            self._compiled.append((re.compile(pattern_str), category))

    def scan(self, user_input: str) -> ScanResult:
        """逐模式匹配，返回拦截决策。blocked=True 时 Agent 不处理。"""
        matched: list[tuple[str, str]] = []
        for regex, category in self._compiled:
            if regex.search(user_input):
                matched.append((regex.pattern, category))
        if matched:
            self._emit_safety_event(matched)
            return ScanResult(
                blocked=True,
                action="block",
                matched_patterns=matched,
            )
        return ScanResult(blocked=False, action="pass")

    def _emit_safety_event(self, matched: list[tuple[str, str]]) -> None:
        if self._event_bus is None:
            return
        try:
            import asyncio

            from bandi.core.events import Event, EventType

            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(
                    self._event_bus.emit(
                        Event(
                            type=EventType.SAFETY_GUARD_TRIGGERED,
                            data={
                                "module": "prompt_guard",
                                "action": "block",
                                "matched_patterns": matched,
                            },
                        )
                    )
                )
        except RuntimeError:
            logger.warning("Failed to emit safety event: no running event loop")
        except ImportError:
            logger.warning("Failed to emit safety event: event types not available")
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to emit safety event: %s", e)
