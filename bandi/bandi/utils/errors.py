# 功能：异常文案兜底（空消息异常不得产生空错误信息——全仓专项）
from __future__ import annotations


def error_text(exc: BaseException) -> str:
    """功能：异常转用户可见文案——str 为空时回填类名（如 Windows
    SelectorEventLoop 的无参 NotImplementedError；同族全仓统一口径）。"""
    return str(exc) or type(exc).__name__
