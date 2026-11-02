# 功能：PII 检测与脱敏 — 正则匹配 + 误报过滤

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bandi.core.event_bus import EventBus

logger = logging.getLogger(__name__)


@dataclass
class PIIScanResult:
    """PII 扫描结果。"""

    has_pii: bool
    pii_types: list[str] = field(default_factory=list)
    findings: dict[str, list[str]] = field(default_factory=dict)


# - api_key 增加常见前缀匹配
# - password 要求上下文词在 ±10 字符内
_HEX_ONLY_RE = re.compile(r"^[0-9a-fA-F]+$")
_BANK_CONTEXT_WINDOW = 20

_BANK_CONTEXT_WORDS = {
    "卡号",
    "银行卡",
    "card",
    "account",
    "bank",
    "credit",
    "debit",
    "622",
    "621",
}


def _has_bank_context(text: str, match_start: int, match_end: int) -> bool:
    """检查匹配位置前后 ±_BANK_CONTEXT_WINDOW 字符内是否有银行卡上下文词。"""
    start = max(0, match_start - _BANK_CONTEXT_WINDOW)
    end = min(len(text), match_end + _BANK_CONTEXT_WINDOW)
    snippet = text[start:end].lower()
    return any(w.lower() in snippet for w in _BANK_CONTEXT_WORDS)


PII_PATTERNS: dict[str, str] = {
    "phone": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    "id_card": r"(?<!\d)\d{17}[\dXx](?!\d)",
    "email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    "bank_card": r"\d{16,19}",
    "api_key": r"(?:sk-|ghp_|gho_|ghu_|AKID|api[_\-]?key[=:]\s*)[a-zA-Z0-9_-]{20,}",
    "password": r"(?:(?:password|passwd|pwd|token|secret)[=:]\s*\S+)",
}

# 不改上下文侧模式（上下文里 IP 可能正是任务对象，如网络调试）
PERSIST_PII_PATTERNS: dict[str, str] = {
    **PII_PATTERNS,
    "bearer_token": r"(?i)\bbearer\s+[a-z0-9._\-]{8,}",
    "ipv4": (r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d)"),
}

_persist_detector: PIIDetector | None = None


def sanitize_for_persist(text: str) -> str:
    """功能：落盘前 PII 脱敏——基础正则五类（sk-*/Bearer/邮箱/手机/IP）
    复用 PIIDetector.sanitize（<PII:TYPE> 占位）；仅落盘路径调用，不改上下文侧模式。"""
    global _persist_detector
    if _persist_detector is None:
        _persist_detector = PIIDetector(patterns=PERSIST_PII_PATTERNS)
    return _persist_detector.sanitize(text)


class PIIDetector:
    def __init__(
        self, patterns: dict[str, str] | None = None, event_bus: EventBus | None = None
    ) -> None:
        self._patterns = patterns or PII_PATTERNS
        self._event_bus = event_bus

    def _is_false_positive(self, pii_type: str, match: re.Match, text: str) -> bool:
        """PII 误报检测。"""
        if pii_type == "bank_card":
            matched = match.group()
            # 跳过纯十六进制（commit hash、时间戳）
            if _HEX_ONLY_RE.match(matched):
                return True
            # 无上下文时不拦截（避免拦截普通长数字串）
            if not _has_bank_context(text, match.start(), match.end()):
                return True
        return False

    def _find_matches(self, text: str) -> list[tuple[int, int, str]]:
        """遍历全部 PII 模式，返回已过滤误报的匹配范围 [(start, end, pii_type)]。"""
        detected: list[tuple[int, int, str]] = []
        for pii_type, pattern in self._patterns.items():
            for match in re.finditer(pattern, text):
                if self._is_false_positive(pii_type, match, text):
                    continue
                detected.append((match.start(), match.end(), pii_type))
        return detected

    def detect(self, text: str) -> PIIScanResult:
        """检测文本中的 PII，返回类型+位置，已过滤误报。"""
        findings: dict[str, list[str]] = {}
        for start, end, pii_type in self._find_matches(text):
            findings.setdefault(pii_type, []).append(text[start:end])
        has_pii = len(findings) > 0
        if has_pii:
            self._emit_pii_event(findings)
        return PIIScanResult(
            has_pii=has_pii,
            pii_types=list(findings.keys()),
            findings=findings,
        )

    def _emit_pii_event(self, findings: dict[str, list[str]]) -> None:
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
                            type=EventType.PII_DETECTED,
                            data={
                                "pii_types": list(findings.keys()),
                                "findings": {k: len(v) for k, v in findings.items()},
                            },
                        )
                    )
                )
        except RuntimeError:
            logger.warning("Failed to emit PII event: no running event loop")
        except ImportError:
            logger.warning("Failed to emit PII event: event types not available")
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to emit PII event: %s", e)

    def sanitize(self, text: str) -> str:
        """脱敏：将 PII 替换为 <PII:TYPE>（跳过误报）。重叠区间先合并再替换——
        原倒序直替在「Bearer sk-xxx」等重叠命中下会索引错位。"""
        ranges = sorted(self._find_matches(text))
        merged: list[tuple[int, int, str]] = []
        for start, end, pii_type in ranges:
            if merged and start < merged[-1][1]:
                prev_start, prev_end, prev_type = merged[-1]
                merged[-1] = (prev_start, max(prev_end, end), prev_type)
                continue
            merged.append((start, end, pii_type))
        result = list(text)
        for start, end, pii_type in reversed(merged):
            result[start:end] = f"<PII:{pii_type.upper()}>"
        return "".join(result)
