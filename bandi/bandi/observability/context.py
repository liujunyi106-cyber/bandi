# 功能：日志上下文（ContextVar 承载）——把 session/turn/agent/role/run/request 贯穿到结构化日志

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, replace


@dataclass(frozen=True)
class LogContext:
    """一次执行的可观测身份。字段即结构化日志里要贯穿的维度。"""

    session_id: str = ""
    turn_index: int = 0
    agent_id: str = ""
    parent_agent_id: str = ""
    role: str = ""
    run_id: str = ""
    request_id: str = ""
    user_id: str = ""


_EMPTY = LogContext()
_FIELD_NAMES = frozenset(f.name for f in fields(LogContext))

# 按 asyncio task / 线程上下文自动隔离——并发会话不会互相串号。
_current: ContextVar[LogContext] = ContextVar("bandi_log_context", default=_EMPTY)


def current_log_context() -> LogContext:
    """取当前上下文；未绑定时返回全空上下文（不返回 None，调用方无需判空）。"""
    return _current.get()


@contextmanager
def bind_log_context(**fields_to_set: object) -> Iterator[LogContext]:
    """在 with 块内绑定日志上下文，退出时**精确复原**（含嵌套）。

    未知字段直接抛 TypeError——错拼字段名时立刻暴露，不静默丢弃。

    Usage:
        with bind_log_context(session_id=sid, turn_index=n):
            logger.info(...)
    """
    unknown = set(fields_to_set) - _FIELD_NAMES
    if unknown:
        raise TypeError(f"未知的日志上下文字段：{sorted(unknown)}")
    token = _current.set(replace(_current.get(), **fields_to_set))  # type: ignore[arg-type]
    try:
        yield _current.get()
    finally:
        _current.reset(token)


def set_log_context(**fields_to_set: object) -> None:
    """就地更新当前上下文，**不做复原**。

    专供「外层已 bind、内层每轮覆盖」的场景（例如主循环每轮刷新 turn_index）：
    复原有外层 `bind_log_context` 的 finally 统一兜住，内层各自持 token 反而会被
    `continue` / `break` 绕过而失效。

    调用方必须确保存在外层 bind 作用域，否则更新会泄漏给后续调用者。
    """
    unknown = set(fields_to_set) - _FIELD_NAMES
    if unknown:
        raise TypeError(f"未知的日志上下文字段：{sorted(unknown)}")
    _current.set(replace(_current.get(), **fields_to_set))  # type: ignore[arg-type]
