# 功能：错误码枚举（AppErrorCode/ApiResult 统一错误响应）+ .1 服务化错误体
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AppErrorCode(StrEnum):
    TOOL_DENIED = "TOOL_DENIED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    LLM_RATE_LIMIT = "LLM_RATE_LIMIT"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_SERVICE_ERROR = "LLM_SERVICE_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    SESSION_TIMEOUT = "SESSION_TIMEOUT"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"


_STATUS_TO_CODE: dict[int, AppErrorCode] = {
    400: AppErrorCode.VALIDATION_ERROR,
    401: AppErrorCode.UNAUTHORIZED,
    403: AppErrorCode.PERMISSION_DENIED,
    404: AppErrorCode.NOT_FOUND,
    405: AppErrorCode.METHOD_NOT_ALLOWED,
    409: AppErrorCode.CONFLICT,
    413: AppErrorCode.PAYLOAD_TOO_LARGE,
    415: AppErrorCode.UNSUPPORTED_MEDIA_TYPE,
    422: AppErrorCode.VALIDATION_ERROR,
    429: AppErrorCode.RATE_LIMITED,
    500: AppErrorCode.INTERNAL_ERROR,
    503: AppErrorCode.SERVICE_UNAVAILABLE,
}


def code_for_status(status_code: int) -> AppErrorCode:
    """把 HTTP 状态码映射为语义错误码。"""
    return _STATUS_TO_CODE.get(int(status_code), AppErrorCode.INTERNAL_ERROR)


@dataclass
class AppError(Exception):
    """业务异常：统一错误体的显式抛出通道（端点可 raise AppError 直达响应）。

    非 AppError 的异常（HTTPException/裸异常）由 app 层处理器翻译成同一形状。
    """

    code: AppErrorCode = AppErrorCode.UNKNOWN_ERROR
    message: str = ""
    detail: str = ""
    status_code: int = 400


@dataclass
class ApiResult:
    success: bool
    error_code: AppErrorCode | None = None
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, data: dict[str, Any] | None = None, message: str = "") -> ApiResult:
        return cls(success=True, data=data or {}, message=message)

    @classmethod
    def err(
        cls,
        code: AppErrorCode,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> ApiResult:
        return cls(success=False, error_code=code, message=message, data=data or {})
