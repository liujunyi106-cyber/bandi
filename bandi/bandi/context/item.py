# 功能：ContextItem — Prompt 上下文输入单元（source_type 区分来源，priority 决定排序）

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextItem:
    id: str
    source_type: str
    content: str
    priority: int = 0
    token_estimate: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
