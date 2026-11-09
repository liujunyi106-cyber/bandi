# 功能：工具重试上下文标记——内部重试期间不得重复累计连续失败计数。
from __future__ import annotations

from contextvars import ContextVar

RETRY_IN_FLIGHT: ContextVar[bool] = ContextVar("bandi_tool_retry_in_flight", default=False)
