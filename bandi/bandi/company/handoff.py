# 功能：Agent 间结构化交接契约——结论/关键证据/未解决项/产物/置信度
# 未解决项显式传递，消除「再花 token 解析自由文本 + 静默丢问题」。

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def _clean_str_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    try:
        values = list(raw or [])
    except TypeError:
        return []
    return [str(v).strip() for v in values if str(v).strip()]


@dataclass
class AgentHandoff:
    conclusion: str = ""
    evidence: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "conclusion": self.conclusion,
            "evidence": list(self.evidence),
            "unresolved": list(self.unresolved),
            "artifacts": list(self.artifacts),
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Any) -> AgentHandoff | None:
        """功能：宽松解析（工具入参/落盘 metadata）——非 dict 返回 None 不炸链路。"""
        if not isinstance(data, dict):
            return None
        confidence: float | None = None
        raw_confidence = data.get("confidence")
        if raw_confidence is not None:
            try:
                confidence = max(0.0, min(1.0, float(raw_confidence)))
            except (TypeError, ValueError):
                confidence = None
        return cls(
            conclusion=str(data.get("conclusion") or "").strip(),
            evidence=_clean_str_list(data.get("evidence")),
            unresolved=_clean_str_list(data.get("unresolved")),
            artifacts=_clean_str_list(data.get("artifacts")),
            confidence=confidence,
        )

    def to_json(self) -> str:
        """功能：单行 JSON——comms frontmatter 逐行解析格式的安全载体。"""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: Any) -> AgentHandoff | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return cls.from_dict(data)


__all__ = ["AgentHandoff"]
