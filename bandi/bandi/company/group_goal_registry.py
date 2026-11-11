# 功能：群聊目标服务进程内句柄——供 Agent 工具按会话调用
from __future__ import annotations

from typing import Any

_service: Any | None = None


def set_group_goal_service(service: Any | None) -> None:
    global _service
    _service = service


def get_group_goal_service() -> Any | None:
    return _service
