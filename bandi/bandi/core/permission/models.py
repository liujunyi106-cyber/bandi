# 功能：权限数据模型（Decision/PermissionScope/Resource/上下文）
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Decision(Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionScope(Enum):
    ONCE = "once"
    SESSION = "session"


@dataclass
class Resource:
    type: str
    name: str
    action: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class PermissionContext:
    user_id: str = ""
    role: str = "member"
    mode: str = "default"
    scope: str = "default"
    risk_level: str = "low"


@dataclass
class PermissionDecision:
    decision: Decision
    reason: str = ""
    scope: PermissionScope = PermissionScope.ONCE
