# 功能：API 服务化中间件——request_id / 请求体上限 / 限流 / 安全头。
#
# 全部为纯 ASGI 中间件（不创建子任务）：ContextVar 日志上下文可无损耗地流经
# handler（BaseHTTPMiddleware 会把下游放进独立 anyio task，导致 request_id 断链）。
from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

from bandi.core.error_codes import AppErrorCode
from bandi.tools.middleware.ratelimit_mw import SlidingWindowRateLimiter


def error_body(
    code: AppErrorCode | str, message: str, detail: str = "", request_id: str = ""
) -> dict[str, str]:
    """统一错误响应体（.1 冻结形状：code/message/detail/request_id）。"""
    return {
        "code": str(code),
        "message": message,
        "detail": detail,
        "request_id": request_id,
    }


def _request_id_of(scope: dict) -> str:
    return str((scope.get("state") or {}).get("request_id", ""))


class RequestIdMiddleware:
    """生成/透传 X-Request-ID，写入响应头、scope state 与日志上下文（.4）。"""

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = (Headers(scope=scope).get("x-request-id") or "")[:128] or uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid

        from bandi.observability.context import bind_log_context

        async def send_with_header(message: dict) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = rid
            await send(message)

        with bind_log_context(request_id=rid):
            await self.app(scope, receive, send_with_header)


class SizeLimitMiddleware:
    """请求体上限（.2）：超限 → 413 + 统一错误体。

    两条路径：① Content-Length 声明超限 → 不读 body 直接 413（快路径）；
    ② 无声明/chunked → receive 计数，超限截断流并在 send 层**替换内层响应为 413**
    （内层 FastAPI 会把 body 打断异常吞成 400，不能依赖异常穿透）。
    """

    def __init__(
        self,
        app: Callable,
        *,
        limit_provider: Callable[[], int],
        path_limit_provider: Callable[[str], int] | None = None,
    ) -> None:
        self.app = app
        self._limit_provider = limit_provider
        self._path_limit_provider = path_limit_provider

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = max(0, int(self._limit_provider()))
        if self._path_limit_provider is not None:
            limit = max(0, int(self._path_limit_provider(str(scope.get("path") or ""))))

        declared = Headers(scope=scope).get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send, limit)
            return

        received = 0
        exceeded = False
        replaced = False

        async def limited_receive() -> dict:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    # 以**非法字节**截断（而非空串）：攻击者可用「合法 JSON + 尾部空白」
                    # 让内层解析成功并真实执行端点（实锤）；追加 NUL 保证任何
                    # JSON/form 解析必然失败，端点副作用不发生（响应由 send 层替换为 413）。
                    return {"type": "http.request", "body": b"\x00", "more_body": False}
            return message

        async def guarded_send(message: dict) -> None:
            nonlocal replaced
            if exceeded:
                if message["type"] == "http.response.start" and not replaced:
                    replaced = True
                    await self._reject(scope, receive, send, limit)
                return  # 丢弃内层响应消息
            await send(message)

        await self.app(scope, limited_receive, guarded_send)
        if exceeded and not replaced:
            await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(scope: dict, receive: Callable, send: Callable, limit: int) -> None:
        response = JSONResponse(
            error_body(
                AppErrorCode.PAYLOAD_TOO_LARGE,
                "request body too large",
                f"limit={limit} bytes",
                _request_id_of(scope),
            ),
            status_code=413,
        )
        await response(scope, receive, send)


class RateLimitMiddleware:
    """HTTP 限流（.3）：滑动窗口（复用工具层 SlidingWindowRateLimiter 实现）。

    豁免 /health 与 /ready（探针不应被限流干扰）；429 带 Retry-After。
    """

    def __init__(
        self,
        app: Callable,
        *,
        limiter: SlidingWindowRateLimiter,
        exempt_paths: tuple[str, ...] = ("/health", "/ready"),
    ) -> None:
        self.app = app
        self._limiter = limiter
        self._exempt = frozenset(exempt_paths)

    @staticmethod
    def _key(scope: dict) -> str:
        # 仅按来源 host 分桶：Bearer 前缀在认证前不可信（轮换即绕过）
        client = scope.get("client") or ("unknown", 0)
        return f"host:{client[0]}"

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or scope.get("path", "") in self._exempt:
            await self.app(scope, receive, send)
            return
        allowed, retry_after = await self._limiter.check(self._key(scope))
        if not allowed:
            response = JSONResponse(
                error_body(
                    AppErrorCode.RATE_LIMITED,
                    "rate limit exceeded",
                    request_id=_request_id_of(scope),
                ),
                status_code=429,
                headers={"Retry-After": str(max(1, int(retry_after) + 1))},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """标准安全响应头（.4）。"""

    _HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self._HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
