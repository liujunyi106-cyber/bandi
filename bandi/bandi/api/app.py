# 功能：FastAPI 应用 — Agent API + Session API + Entity API + 流式 WebSocket + 模型设置

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as _StarletteHTTPException

from bandi.api.middleware import (
    RateLimitMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    SizeLimitMiddleware,
    error_body,
)
from bandi.core.account import DEFAULT_LOCAL_USER_ID
from bandi.core.config import BandiConfig
from bandi.core.error_codes import AppError, AppErrorCode, code_for_status
from bandi.core.security.secret_protect import unprotect
from bandi.core.security.tokens import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    issue_token,
    resolve_token_secret,
    verify_token,
)
from bandi.core.user_interaction import AutoApproveInteraction
from bandi.memory.session.store import SessionStore
from bandi.observability.counters import snapshot as counters_snapshot
from bandi.observability.metrics import MetricsCollector
from bandi.tools.middleware.ratelimit_mw import SlidingWindowRateLimiter
from bandi.utils.db import connect as db_connect
from bandi.utils.timefmt import iso_local

_PROCESS_T0 = time.perf_counter()

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bandi.company.company_mode import CompanyDispatcher

logger = logging.getLogger(__name__)

_STARTUP_T0 = _PROCESS_T0


def _startup_log(label: str) -> None:
    # 功能：启动阶段耗时日志——仅 BANDI_STARTUP_TRACE=1 时输出，避免污染导入校验
    if os.environ.get("BANDI_STARTUP_TRACE", "0") != "1":
        return
    print(
        f"[bandi] startup {label} {time.perf_counter() - _STARTUP_T0:.2f}s",
        file=sys.stderr,
        flush=True,
    )


app = FastAPI(title="Bandi API", version="0.1.0")


@app.on_event("startup")
async def _startup_models_dev_sync() -> None:
    # ensure 内部 _sync_done 短接——CLI+API 双触发安全，失败静默不阻断启动）
    import asyncio

    from bandi.llm.models_dev import trigger_models_dev_sync

    try:
        await asyncio.wait_for(asyncio.to_thread(trigger_models_dev_sync), timeout=2.0)
    except TimeoutError:
        logger.warning("models.dev sync exceeded 2s; startup continues")
    _startup_log("models_dev_sync")


@app.on_event("startup")
async def _warn_subprocess_capability() -> None:
    # workers>1）不能 spawn——bash/python_exec/git 工具全失效且失败 error 为空
    # （无参 NotImplementedError）；启动即告警，避免静默降级
    from bandi.tools.subprocess_tree import subprocess_capability_warning

    warning = subprocess_capability_warning(asyncio.get_running_loop())
    if warning:
        logger.warning(warning)
    _startup_log("subprocess_capability")


@app.on_event("startup")
async def _startup_role_map_selfcheck() -> None:
    # 角色映射装配期自检：坏映射启动即告警（不等某次工具调用才静默失效）
    try:
        from bandi.core.config import BandiConfig
        from bandi.core.permission.role_mapping import check_role_map

        check_role_map(BandiConfig.load().permissions.role_map)
    except Exception as e:  # noqa: BLE001 — 自检失败不阻断启动（配置读取异常另行处置）
        logger.debug("role map selfcheck skipped: %s", e)
    _startup_log("role_map_selfcheck")


# 建卡后不自动执行（须手动 tick / CLI）。启动即起 run_forever 后台任务。
_company_loop_task: asyncio.Task[None] | None = None
_company_start_task: asyncio.Task[None] | None = None


def _company_autorun_enabled(
    environ: Mapping[str, str] | None = None,
    *,
    in_pytest: bool | None = None,
) -> bool:
    # 功能：公司调度常驻循环开关——默认开；pytest 环境强制关（测试进程不得起
    # 调度循环写真实库）；BANDI_COMPANY_AUTORUN=0/false/no/off 显式关。
    import os as _os
    import sys as _sys

    env = _os.environ if environ is None else environ
    pytest_active = ("pytest" in _sys.modules) if in_pytest is None else in_pytest
    if pytest_active:
        return False
    raw = str(env.get("BANDI_COMPANY_AUTORUN", "1")).strip().lower()
    return raw not in ("0", "false", "no", "off")


class _AutorunFlag:
    # 功能：run_forever 的 running 谓词（关闭后主循环退出）
    def __init__(self) -> None:
        self.value = True

    def __call__(self) -> bool:
        return self.value

    def stop(self) -> None:
        self.value = False


@app.on_event("startup")
async def _startup_company_autorun() -> None:
    # 功能：延后 0.5s 启动调度循环——先让 /health 可用，避免启动期被首轮 tick 拖住
    global _company_start_task
    if not _company_autorun_enabled():
        return
    import asyncio

    _company_start_task = asyncio.create_task(
        _start_company_autorun_later(), name="company-autorun-start"
    )


async def _start_company_autorun_later() -> None:
    global _company_loop_task
    import asyncio

    await asyncio.sleep(0.5)
    try:
        dispatcher = _get_company_dispatcher()
    except Exception:
        logger.exception("company dispatcher init failed; autorun disabled")
        return
    flag = _AutorunFlag()
    _company_loop_task = asyncio.create_task(dispatcher.run_forever(flag), name="company-autorun")
    app.state.company_autorun_flag = flag
    logger.info("company dispatcher autorun started")
    _startup_log("company_autorun")


# 记忆巩固周期驱动：合并同类 / episodic→long_term 晋升 / 低价值淘汰。
# 此前 `_headless_runner` 的哨兵消费端存在，但全仓没有任何代码创建该哨兵任务
# ⇒ 巩固链路从未运行（合并/晋升/淘汰全是纸面能力）。此循环把它接上：
# 启动 5 分钟后首跑，此后每小时一次；`BANDI_MEMORY_CONSOLIDATION=0` 可关（测试环境）。
_MEMORY_CONSOLIDATION_INITIAL_SEC = 300.0
_MEMORY_CONSOLIDATION_INTERVAL_SEC = 3600.0
_memory_consolidation_task: asyncio.Task[None] | None = None


async def _memory_consolidation_loop() -> None:
    import asyncio

    await asyncio.sleep(_MEMORY_CONSOLIDATION_INITIAL_SEC)
    while True:
        try:
            from bandi.memory.consolidation import consolidate_default

            report = await asyncio.to_thread(consolidate_default)
            logger.info("memory consolidation: %s", report.to_dict())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("memory consolidation failed")
        await asyncio.sleep(_MEMORY_CONSOLIDATION_INTERVAL_SEC)


@app.on_event("startup")
async def _startup_memory_consolidation() -> None:
    global _memory_consolidation_task
    import asyncio
    import os

    if os.environ.get("BANDI_MEMORY_CONSOLIDATION", "1") == "0":
        return
    _memory_consolidation_task = asyncio.create_task(
        _memory_consolidation_loop(), name="memory-consolidation"
    )
    logger.info("memory consolidation loop started")
    _startup_log("memory_consolidation")


@app.on_event("shutdown")
async def _shutdown_memory_consolidation() -> None:
    global _memory_consolidation_task
    task = _memory_consolidation_task
    if task is None:
        return
    import asyncio
    import contextlib

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    _memory_consolidation_task = None


@app.on_event("shutdown")
async def _shutdown_company_autorun() -> None:
    global _company_loop_task, _company_start_task
    import asyncio
    import contextlib

    start_task = _company_start_task
    if start_task is not None and not start_task.done():
        start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await start_task
    _company_start_task = None
    task = _company_loop_task
    if task is None:
        return
    flag = getattr(app.state, "company_autorun_flag", None)
    if flag is not None:
        flag.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    _company_loop_task = None
    logger.info("company dispatcher autorun stopped")


MetricsCollector().expose_metrics(app)


def _resolve_api_key() -> str:
    # 功能：解析 BANDI_API_KEY（支持 dpapi: 密文——鉴权统一用解密后明文，坏值原样 fail-safe）
    return unprotect(os.environ.get("BANDI_API_KEY", ""))


_API_KEY = _resolve_api_key()

# .2：请求体上限（环境变量可配，默认 10MB）
_MAX_BODY_BYTES = int(os.environ.get("BANDI_MAX_BODY_MB", "10")) * 1024 * 1024
# 资料库上传/资产单独放宽到 55MB，覆盖 50MB 准入上限与表单开销
_KNOWLEDGE_UPLOAD_BYTES = 55 * 1024 * 1024


def _body_limit_for_path(path: str) -> int:
    # 功能：资料库上传/资产走大请求体上限，其余接口沿用全局上限。
    if path.startswith("/api/knowledge/nodes") and (
        path.endswith("/upload") or path.endswith("/assets")
    ):
        return _KNOWLEDGE_UPLOAD_BYTES
    return _MAX_BODY_BYTES


# .3：HTTP 限流（默认 300/min；滑动窗口上限即窗口内允许的突发峰值）
_rate_limiter = SlidingWindowRateLimiter(
    max_calls=int(os.environ.get("BANDI_RATE_LIMIT_PER_MIN", "1200")), window_sec=60
)

HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
WS_CLOSE_UNAUTHORIZED = 4001

# 功能：非 SPA 的路径前缀（API/WS/探针/文档）。两处共用同一口径：
# ① 认证中间件——这些前缀之外的 GET/HEAD 视为静态资源，免凭证（浏览器首屏无 token）；
# ② SPA fallback——落在这些前缀下的未命中请求一律 404，不回落 index.html
#    （否则客户端会把 HTML 当 JSON 解析）。
_NON_SPA_PREFIXES = frozenset(
    {"api", "ws", "health", "ready", "openapi", "openapi.json", "docs", "redoc"}
)


def _is_spa_asset_path(path: str) -> bool:
    """功能：判断路径是否属于前端静态资源（非 API/WS/探针/文档前缀）。"""
    head = path.lstrip("/").split("/", 1)[0]
    return head not in _NON_SPA_PREFIXES


_ws_connections = 0
_share_tokens: dict[str, str] = {}
_share_token_times: dict[str, float] = {}
_SHARE_TOKEN_TTL_SECONDS = 86400.0
_share_lock = threading.Lock()


def _purge_share_tokens() -> None:
    """（批 3）：清理超龄 share token（纯内存字典原无过期，长驻进程无界）。"""
    now = time.time()
    with _share_lock:
        expired = [
            tok
            for tok, created in _share_token_times.items()
            if now - created > _SHARE_TOKEN_TTL_SECONDS
        ]
        for tok in expired:
            _share_tokens.pop(tok, None)
            _share_token_times.pop(tok, None)


_ws_lock = threading.Lock()
# 功能：在跑的 WS 回合任务（持引用防 GC；回合放后台执行，见 websocket_endpoint）
_ws_turn_tasks: set[asyncio.Task[None]] = set()
# 会话 → 在跑轮任务——abort 后同步等待轮次收尾
# （槽位释放/中断注记落库/残留 pendings 清理），否则重试新轮会撞 SESSION_BUSY。
_ws_turn_tasks_by_session: dict[str, asyncio.Task[None]] = {}
_active_abort_controllers: dict[str, Any] = {}
_abort_controllers_lock = threading.Lock()


def _unregister_abort_controller(session_id: str, controller: Any) -> None:
    """条件注销——仅当注册项仍是本轮的 controller（旧轮 finally
    不得弹掉新轮已替换的 controller，否则新轮无法被 abort）。"""
    with _abort_controllers_lock:
        if _active_abort_controllers.get(session_id) is controller:
            _active_abort_controllers.pop(session_id, None)


def _error_response(
    request: Request, status_code: int, code: AppErrorCode, message: str, detail: str = ""
) -> JSONResponse:
    """.1：统一错误响应体构造（中间件与异常处理器共用）。"""
    return JSONResponse(
        error_body(code, message, detail, str(getattr(request.state, "request_id", ""))),
        status_code=status_code,
    )


def _bearer_token_ok(token: str) -> bool:
    """加固：恒定时间 Bearer 校验（HTTP 与 WS 同一实现）。

    用 **bytes** 比较——`hmac.compare_digest` 对含非 ASCII 的 str 会抛 TypeError
    （未认证者可触发 500，审查实锤）；编码后比较支持任意字节且保持恒定时间。
    """
    if not _API_KEY or not token:
        return False
    try:
        return hmac.compare_digest(token.encode("utf-8"), _API_KEY.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def _signed_token_ok(token: str) -> bool:
    # 功能：校验签名令牌（TTL 强制）——静态 key 之外的第二凭证通道；未配置签名密钥时禁用
    if not token:
        return False
    return verify_token(token, secret=resolve_token_secret()) is not None


def _ws_credential(websocket: WebSocket) -> str:
    # 功能：解析 WS 凭证（Authorization Bearer 优先，回落 query ?token=；不发日志）
    token = websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if token:
        return token
    return (websocket.query_params.get("token") or "").strip()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # .5：探针端点豁免（health liveness + /ready readiness 应无凭证可用）
    if (
        request.method == "OPTIONS"
        or request.url.path in ("/health", "/ready")
        or request.url.path.startswith("/api/shared/")
    ):
        return await call_next(request)
    # .6：SPA 静态资源豁免——仅在已挂载前端托管时生效。
    # 浏览器首屏请求（index.html / assets / 前端深链）不可能带 token（token 存前端
    # localStorage，需先加载页面才能读到），因此这些只读请求必须放行；
    # API/WS/探针/文档前缀不受影响，仍走下方鉴权。
    if (
        globals().get("SERVED_FRONTEND_DIST") is not None
        and request.method in ("GET", "HEAD")
        and _is_spa_asset_path(request.url.path)
    ):
        return await call_next(request)
    if _API_KEY:
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not (_bearer_token_ok(token) or _signed_token_ok(token)):
            return _error_response(
                request, HTTP_UNAUTHORIZED, AppErrorCode.UNAUTHORIZED, "Unauthorized"
            )
    else:
        host = request.client.host if request.client else "127.0.0.1"
        if host not in ("127.0.0.1", "::1", "localhost"):
            return _error_response(
                request, HTTP_UNAUTHORIZED, AppErrorCode.UNAUTHORIZED, "Unauthorized"
            )
    return await call_next(request)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_host_allowed(raw: str) -> bool:
    # 功能：浏览器来源只接受本机回环（空串=非浏览器客户端，放行）
    from urllib.parse import urlsplit

    value = (raw or "").strip()
    if not value:
        return True
    try:
        host = (urlsplit(value).hostname or "").strip("[]").lower()
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


def _host_header_allowed(raw: str) -> bool:
    # 功能：Host 头只接受回环名（防 DNS rebinding 把外部域名解析到本机）
    value = (raw or "").strip()
    if not value:
        return True
    if value.startswith("["):
        host = value[1:].split("]", 1)[0]
    else:
        host = value.rsplit(":", 1)[0] if ":" in value else value
    return host.strip().lower() in _LOOPBACK_HOSTS


def _request_host_allowed(request: Request) -> bool:
    # 功能：HTTP 请求 Host 头校验。
    return _host_header_allowed(request.headers.get("host", ""))


def _ws_handshake_allowed(websocket: WebSocket) -> bool:
    # 功能：WS 握手来源校验——浏览器 Origin 必须回环，Host 必须回环，跨站拒绝；
    # 非浏览器客户端（无 Origin）按回环 Host + 回环 client 放行。
    site = (websocket.headers.get("sec-fetch-site") or "").strip().lower()
    if site == "cross-site":
        return False
    if not _host_header_allowed(websocket.headers.get("host", "")):
        return False
    origin = (websocket.headers.get("origin") or "").strip()
    if origin:
        return _origin_host_allowed(origin)
    client_host = websocket.client.host if websocket.client else ""
    return client_host in _LOOPBACK_HOSTS or client_host == "testclient"


@app.middleware("http")
async def local_origin_guard(request: Request, call_next):
    """功能：本地服务的浏览器来源基线——写请求拒绝跨站来源，Origin 缺失时校验 Host。

    口径：Origin 只接受本机回环；`Sec-Fetch-Site: cross-site` 一律拒绝；
    未配置 API key 时**所有方法**的 Host 必须是回环（DNS rebinding 兜底）。
    `Origin: null`（file:// 页面 / 沙箱 iframe）默认拒绝——页面必须由 FastAPI 同源托管；
    P5-6 若改用自定义协议，需在此登记允许来源。
    """
    if request.url.path in ("/health", "/ready"):
        return await call_next(request)
    if request.method in _WRITE_METHODS:
        site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if site == "cross-site":
            return _error_response(
                request,
                403,
                AppErrorCode.PERMISSION_DENIED,
                "Cross-site request blocked",
            )
        origin = (request.headers.get("origin") or "").strip()
        if not _origin_host_allowed(origin):
            return _error_response(
                request, 403, AppErrorCode.PERMISSION_DENIED, "Origin not allowed"
            )
        if not origin:
            referer = request.headers.get("referer", "")
            if not _origin_host_allowed(referer):
                return _error_response(
                    request, 403, AppErrorCode.PERMISSION_DENIED, "Referer not allowed"
                )
    if not _API_KEY and not _request_host_allowed(request):
        return _error_response(request, 403, AppErrorCode.PERMISSION_DENIED, "Host not allowed")
    return await call_next(request)


# （除 CORS）保证从体积/限流拦截到端点处理全链都携带日志上下文；SecurityHeaders
# 在限流外使 429 也带安全头。
app.add_middleware(RateLimitMiddleware, limiter=_rate_limiter)
app.add_middleware(
    SizeLimitMiddleware,
    limit_provider=lambda: _MAX_BODY_BYTES,
    path_limit_provider=_body_limit_for_path,
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://127.0.0.1"],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


@app.exception_handler(AppError)
async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """显式业务异常 → 冻结错误体。"""
    return _error_response(
        request, exc.status_code, exc.code, exc.message or str(exc.code), exc.detail
    )


@app.exception_handler(HTTPException)  # fastapi 子类（端点内 raise）
@app.exception_handler(_StarletteHTTPException)  # 基类（路由内部 raise，如 body 解析失败）
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """.1：散装 HTTPException 统一翻译为冻结错误体（零改端点）。

    两族都注册：FastAPI 路由内部抛 starlette 基类 HTTPException——此前落到
    默认裸 `{"detail": ...}` 响应，统一体在这里补全该缺口。
    """
    logger.warning("api error %s %s: %s", exc.status_code, request.url.path, exc.detail)
    return _error_response(
        request, exc.status_code, code_for_status(exc.status_code), str(exc.detail)
    )


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """.1 补全：请求校验失败（422）统一体（此前裸 `{"detail": [...]}`）。"""
    return _error_response(
        request, 422, AppErrorCode.VALIDATION_ERROR, "request validation failed", str(exc)[:500]
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """.1 补全：未捕获异常统一体。

    该 handler 由 ServerErrorMiddleware 调用——它位于全部 中间件之外，响应头
    不经 RequestId/SecurityHeaders，故在此手动补齐
    追踪 id 的路径）。
    """
    logger.exception("unhandled api error on %s", request.url.path)
    response = _error_response(
        request, 500, AppErrorCode.INTERNAL_ERROR, "internal server error", str(exc)[:500]
    )
    rid = str(getattr(request.state, "request_id", ""))
    if rid:
        response.headers["X-Request-ID"] = rid
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


class _AuthTokenRequest(BaseModel):
    # 功能：令牌签发请求体（subject 缺省 api-client；ttl 秒，取值范围 [1, 86400]）
    subject: str = "api-client"
    ttl_seconds: int = Field(default=DEFAULT_TTL_SECONDS, ge=1, le=MAX_TTL_SECONDS)


@app.post("/api/auth/token")
async def issue_auth_token(body: _AuthTokenRequest) -> dict[str, Any]:
    """⑦ 令牌 TTL：签发带过期时间的签名令牌（端点自身由静态 key 鉴权守卫）。

    签名密钥未配置（BANDI_AUTH_SECRET/BANDI_API_KEY 均无）→ 503 统一错误体。
    """
    secret = resolve_token_secret()
    if not secret:
        raise AppError(
            code=AppErrorCode.SERVICE_UNAVAILABLE,
            message="token signing is disabled",
            status_code=503,
        )
    token = issue_token(body.subject, ttl_seconds=body.ttl_seconds, secret=secret)
    payload = verify_token(token, secret=secret)
    expires_at = datetime.fromtimestamp(payload["exp"], UTC).isoformat() if payload else ""
    return {"token": token, "expires_at": expires_at}


_db_path: Path | None = None  # 惰性：首次使用时解析


def _resolve_db_path() -> Path:
    """功能：解析会话库路径（惰性单点；测试 patch `_db_path` 后直通）。"""
    global _db_path
    if _db_path is None:
        _db_path = Path.home() / ".bandi" / "data" / "bandi.db"
    return _db_path


_store: SessionStore | None = None
_ontology_store: Any | None = None
_company_dispatcher: CompanyDispatcher | None = None
_interaction_store: Any | None = None
_account_store: Any | None = None
_store_lock = threading.Lock()
_ontology_store_lock = threading.Lock()
_company_lock = (
    threading.RLock()
)  # RLock：装配函数嵌套调用（custom runner 内取 dispatcher）需可重入


def _get_account_store() -> Any:
    """AccountStore 单例 + 惰性 ensure_local_user（单机默认账号）。"""
    global _account_store
    if _account_store is None:
        from bandi.core.account import AccountStore

        _resolve_db_path().parent.mkdir(parents=True, exist_ok=True)
        _account_store = AccountStore(str(Path.home() / ".bandi" / "data" / "account.db"))
        _account_store.ensure_local_user()
    return _account_store


def _get_interaction_store() -> Any:
    """InteractionStore 单例（单进程事件循环内线程安全，无需锁）。

    持久化后端落 `_resolve_db_path().parent / interactions.db`（测试 patch
    `_db_path` 后自动进 tmp 隔离）；建库失败降级纯内存（不阻断主流程）。
    """
    global _interaction_store
    if _interaction_store is None:
        from bandi.core.user_interaction import InteractionStore

        persistence = None
        try:
            from bandi.core.interaction_persistence import InteractionPersistence

            db_path = _resolve_db_path().parent / "interactions.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            persistence = InteractionPersistence(db_path)
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "interaction persistence unavailable; memory-only", exc_info=True
            )
        _interaction_store = InteractionStore(persistence=persistence)
    return _interaction_store


def _interaction_frame(item: Any) -> dict[str, Any]:
    """InteractionStore pending → WS/SSE interaction 帧载荷。"""
    frame = {
        "interaction_id": item.interaction_id,
        "session_id": item.session_id,
        "kind": item.kind,
        "payload": item.payload,
    }
    if not (isinstance(item.payload, dict) and item.payload.get("notify")):
        frame["expires_in"] = item.remaining_seconds()
    return frame


def _get_store() -> SessionStore:
    global _store
    with _store_lock:
        if _store is None:
            db_path = _resolve_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            _store = SessionStore(str(db_path))
    return _store


def _get_ontology_store() -> Any:
    """功能：三元组本体存储单例（独立库文件，不与旧 entities/relations 同库）。

    独立文件是刻意的：迁移期新旧并存需要能分别备份与回滚，共库则迁移失败时
    两套 schema 互相污染。
    """
    global _ontology_store
    with _ontology_store_lock:
        if _ontology_store is None:
            from bandi.ontology.spec import open_ontology
            from bandi.ontology.triple_store import OntologyStore

            data_dir = _resolve_db_path().parent
            data_dir.mkdir(parents=True, exist_ok=True)
            _ontology_store = OntologyStore(open_ontology(str(data_dir / "ontology.db")))
    return _ontology_store


@app.get("/health")
async def health() -> dict[str, Any]:
    # 进程级丢弃/漏传计数出口（键名 = counters 模块常量，稳定契约）
    return {"status": "ok", "version": "0.1.0", "counters": counters_snapshot()}


_STALE_READY_PROBE_SECONDS = 600.0


def _sweep_stale_ready_probes(base: Path) -> None:
    # 功能：清扫进程强杀遗留的超龄探针文件——只删龄超阈值的残留；最近的
    # 可能是并发探针的活动文件，不碰（否则重演并发互删假 503）。
    cutoff = time.time() - _STALE_READY_PROBE_SECONDS
    for stale in base.glob(".ready_probe.*"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink(missing_ok=True)
        except OSError:
            continue


def _probe_storage_writable() -> bool:
    """.5 readiness：存储目录真实写探测。

    唯一临时文件名——固定名 `.ready_probe` 在并发探针下互删致假 503（审查
    2 线程 41% / 32 线程 99.97% 假阴性）。
    """
    try:
        base = (
            Path(os.environ.get("BANDI_HOME", os.path.expanduser("~/.bandi"))) / "data"
        )
        base.mkdir(parents=True, exist_ok=True)
        _sweep_stale_ready_probes(base)
        probe = base / f".ready_probe.{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        try:
            probe.unlink()
        except OSError:
            # 功能：删除失败（杀软/沙箱句柄锁）不代表存储不可写；写入成功即就绪，
            # 残留文件由下次清扫兜底，避免假 503 把整个前端挡在启动页。
            pass
        return True
    except OSError:
        return False


def _probe_db_readable() -> bool:
    """.5 readiness：会话库可读探测（库不存在=首次运行，不算故障）。

    路径口径与 BANDI_HOME 对齐（ADR-42 sidecar 场景：壳与后端约定同一
    BANDI_HOME；此前硬编码 Path.home() 在自定义 HOME 时探测错位置）。
    """
    db_path = (
        Path(os.environ.get("BANDI_HOME", os.path.expanduser("~/.bandi"))) / "data" / "bandi.db"
    )
    if not db_path.exists():
        return True
    try:
        conn = db_connect(db_path, timeout=2.0)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return True
    except Exception:  # noqa: BLE001 — 探针只关心可用性，不区分异常类型
        return False


@app.get("/ready")
async def ready() -> JSONResponse:
    """.5：readiness 探针（依赖不可用 → 503；/health 保持 liveness 语义）。

    同步探针经线程池执行——DB 锁等待不得冻结事件循环（审查可卡全 API 2s）。
    """
    db_ok, storage_ok = await asyncio.gather(
        asyncio.to_thread(_probe_db_readable),
        asyncio.to_thread(_probe_storage_writable),
    )
    checks = {"db": db_ok, "storage": storage_ok}
    if not all(checks.values()):
        return JSONResponse({"status": "not_ready", "checks": checks}, status_code=503)
    return JSONResponse({"status": "ready", "checks": checks})


# ── 模型设置 ──────────────────────────────────────────────────


@app.get("/api/settings/model")
async def get_model() -> dict[str, Any]:
    config = BandiConfig.load()
    from bandi.llm.model_catalog import BUILTIN_MODELS
    from bandi.llm.models_dev import lookup_model_capabilities
    from bandi.llm.profiles import active_profile_summary, resolve_llm_config

    # 档案激活时：effective_* = 实际生效值；current_model/has_api_key 仍是 config.yaml 口径
    resolved_llm = resolve_llm_config(config.llm) if config.llm else None
    effective_model = (resolved_llm.default_model if resolved_llm else "") or ""
    available: list[dict[str, Any]] = [
        {
            "name": name,
            "provider": spec.provider,
            "supports_reasoning": bool(spec.supports_reasoning),
            "reasoning_efforts": list(spec.reasoning_efforts or []),
            # -C 组：能否关闭思考（False = 强制思考；前端不渲染「关闭」档）
            "supports_thinking_off": bool(getattr(spec, "supports_thinking_off", True)),
        }
        for name, spec in BUILTIN_MODELS.items()
        if name != "mock"
    ]

    # 实时能力解析补条目，思考挡位对「目录未收录但 metadata 可查」的模型同样可用。
    # 归一判存（xiaomi/mimo-v2.6-flash ↔ mimo-v2.6-flash 视为已有）；查不到保持无条目。
    normalized = {name.rsplit("/", 1)[-1] for name in BUILTIN_MODELS if name != "mock"}

    def _tail(model_name: str) -> str:
        return model_name.rsplit("/", 1)[-1].strip()

    extra_models: dict[str, str] = {}
    if effective_model:
        extra_models[effective_model] = (resolved_llm.provider if resolved_llm else "") or ""
    try:
        from bandi.llm.profiles import store_singleton

        for profile in store_singleton().list_profiles():
            if profile.model:
                extra_models.setdefault(profile.model, profile.provider or "")
    except Exception:  # noqa: BLE001 — 档案不可用时仅补生效模型（不阻断设置面）
        pass
    for extra_name, extra_provider in extra_models.items():
        key = _tail(extra_name)
        if not key or key in normalized:
            continue
        capabilities = lookup_model_capabilities(extra_name)
        if not capabilities:
            continue
        normalized.add(key)
        available.append(
            {
                "name": extra_name,
                "provider": extra_provider,
                "supports_reasoning": bool(capabilities["reasoning"]),
                # "none" = 关闭档语义（前端由 supports_thinking_off 的关闭档承担）
                "reasoning_efforts": [
                    value
                    for value in capabilities["reasoning_efforts"]
                    if value and value != "none"
                ],
                "supports_thinking_off": True,
            }
        )

    return {
        "current_model": (config.llm.default_model if config.llm else "") or "",
        "has_api_key": bool(config.llm and config.llm.api_key) if config.llm else False,
        "active_profile": active_profile_summary(),
        "effective_model": effective_model,
        "effective_has_api_key": bool(resolved_llm and resolved_llm.api_key),
        "available_models": available,
    }


@app.put("/api/settings/model")
async def set_model(body: dict[str, str]) -> dict[str, str]:
    model_name = body.get("model", "").strip()
    if not model_name:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="model name is required")
    config = BandiConfig.load()
    if config.llm:
        config.llm.default_model = model_name
        config.save(patch={"llm": {"default_model": model_name}})
        return {"current_model": model_name}
    raise HTTPException(
        status_code=HTTP_BAD_REQUEST, detail="LLM not configured, run bandi init first"
    )


#


def _profile_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def _profiles_payload() -> dict[str, Any]:
    # 功能：档案列表出参（单一口径——两个 GET 都走它）
    from bandi.llm.profiles import profile_for_api, store_singleton

    store = store_singleton()
    return {
        "active_profile_id": store.active_profile_id(),
        "feedback_profile_id": store.feedback_profile_id(),
        "profiles": [profile_for_api(item) for item in store.list_profiles()],
    }


@app.get("/api/settings/llm/profiles")
async def list_llm_profiles() -> dict[str, Any]:
    from bandi.llm.profiles import ProfilesFileError

    try:
        return _profiles_payload()
    except ProfilesFileError as exc:
        # 坏文件显式暴露（与「无档案」区分：前者要用户处理文件，后者是全新安装的正常态）
        raise _profile_error(HTTP_BAD_REQUEST, f"llm_profiles.json 不可用：{exc}") from exc


def _profile_from_body(body: dict[str, Any], profile_id: str) -> Any:
    # 功能：请求体 → LlmProfile（构造即校验；掩码 key 保留仓库原值）
    from bandi.llm.profiles import LlmProfile, store_singleton

    payload = {key: value for key, value in body.items() if key != "profile_id"}
    api_key = str(payload.get("api_key") or "")
    existing = store_singleton().get(profile_id)
    if existing is not None and "***" in api_key:
        payload["api_key"] = existing.api_key
    try:
        return LlmProfile(profile_id=profile_id, **payload)
    except ValueError as exc:  # pydantic ValidationError 是 ValueError 子类
        raise _profile_error(HTTP_BAD_REQUEST, f"档案字段非法：{exc}") from exc


@app.post("/api/settings/llm/profiles")
async def create_llm_profile(body: dict[str, Any]) -> dict[str, Any]:
    from bandi.llm.profiles import profile_for_api, store_singleton

    profile_id = str(body.get("profile_id") or "").strip()
    if not profile_id:
        raise _profile_error(HTTP_BAD_REQUEST, "profile_id is required")
    store = store_singleton()
    if store.get(profile_id) is not None:
        raise _profile_error(HTTP_CONFLICT, f"档案 {profile_id} 已存在（用 PUT 更新）")
    profile = _profile_from_body(body, profile_id)
    store.upsert(profile)
    return {"profile": profile_for_api(profile), "active_profile_id": store.active_profile_id()}


@app.put("/api/settings/llm/profiles/{profile_id}")
async def update_llm_profile(profile_id: str, body: dict[str, Any]) -> dict[str, Any]:
    from bandi.llm.profiles import profile_for_api, store_singleton

    store = store_singleton()
    if store.get(profile_id) is None:
        raise _profile_error(HTTP_NOT_FOUND, f"档案 {profile_id} 不存在")
    profile = _profile_from_body(body, profile_id)
    store.upsert(profile)
    return {"profile": profile_for_api(profile), "active_profile_id": store.active_profile_id()}


@app.delete("/api/settings/llm/profiles/{profile_id}")
async def delete_llm_profile(profile_id: str) -> dict[str, Any]:
    from bandi.llm.profiles import store_singleton

    store = store_singleton()
    if not store.delete(profile_id):
        raise _profile_error(HTTP_NOT_FOUND, f"档案 {profile_id} 不存在")
    return {"deleted": profile_id, "active_profile_id": store.active_profile_id()}


@app.post("/api/settings/llm/profiles/{profile_id}/activate")
async def activate_llm_profile(profile_id: str) -> dict[str, Any]:
    from bandi.llm.profiles import profile_for_api, store_singleton

    store = store_singleton()
    if not store.activate(profile_id):
        raise _profile_error(HTTP_NOT_FOUND, f"档案 {profile_id} 不存在")
    profile = store.get(profile_id)
    return {
        "active_profile_id": profile_id,
        "profile": profile_for_api(profile) if profile is not None else None,
    }


@app.post("/api/settings/llm/profiles/{profile_id}/feedback")
async def set_feedback_llm_profile(profile_id: str) -> dict[str, Any]:
    """功能：指定「反馈模型」档案（反思验收 LLM-as-Judge 专用；）。

    反馈链路（Verifier / 反思）创建 provider 时该档案优先于激活档案与
    `verification.judge_model`；档案不存在返回 404。
    """
    from bandi.llm.profiles import ProfilesFileError, store_singleton

    store = store_singleton()
    try:
        ok = store.set_feedback(profile_id)
    except ProfilesFileError as exc:
        raise _profile_error(HTTP_BAD_REQUEST, f"llm_profiles.json 不可用：{exc}") from exc
    if not ok:
        raise _profile_error(HTTP_NOT_FOUND, f"档案 {profile_id} 不存在")
    return {"feedback_profile_id": profile_id}


# ── Agent 执行 ──────────────────────────────────────────────


# 功能：端点面回合看门狗默认时长——请求未显式指定时启用，保证任何回合（含卡在
# 等待态的）在上限内产出终帧并释放会话锁；部署侧可用 env 覆写（显式传 0 仍可关闭）。
_DEFAULT_SESSION_TIMEOUT_SEC = int(os.environ.get("BANDI_SESSION_TIMEOUT_SEC", "900"))


def _session_timeout_sec_from(body: dict[str, Any]) -> int:
    # 功能：端点面解析 session_timeout_sec——未传/非法回落启用态默认
    # 显式 0 保留「关闭」语义（测试与 CLI 可显式关）
    if "session_timeout_sec" not in body:
        return _DEFAULT_SESSION_TIMEOUT_SEC
    raw = body.get("session_timeout_sec")
    if raw is None:
        return _DEFAULT_SESSION_TIMEOUT_SEC
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SESSION_TIMEOUT_SEC
    if value < 0:  # 负数属非法输入，回落启用态默认（0 仅由显式值表达）
        return _DEFAULT_SESSION_TIMEOUT_SEC
    return value


def _identity_from(body: dict[str, Any]) -> tuple[str, Any | None]:
    """功能：解析请求身份（方案 A 续）——user_id 非空时惰性附带账户库。

    该用户绑定经 AccountStore → role_map → 内置权限档在工具执行面生效。
    未带 user_id = 不启用身份解析（account_store 为 None）→ 行为与改造前一致。
    """
    user_id = str(body.get("user_id") or "").strip()
    if not user_id:
        return "", None
    return user_id, _get_account_store()


@app.post("/api/agent/chat")
async def chat(body: dict[str, Any]) -> dict[str, Any]:
    message = body.get("message", "")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST, detail="MISSING_FIELDS: message is required"
        )
    session_id = body.get("session_id") or ""
    # mode（plan 会话恢复不丢失）；显式传且与存储不同时写回（切换持久化）
    raw_mode = body.get("mode") or ""
    if raw_mode not in ("", "execute", "plan"):
        if raw_mode == "ask":
            logging.getLogger(__name__).warning(
                "legacy mode 'ask' normalized to 'execute' (dual-mode since day124x-u)"
            )
        raw_mode = ""
    project_id = str(body.get("project_id") or "").strip()
    project_root = _validated_session_project_root(body.get("project_root"), project_id)
    access_block = _project_access_block(project_id, session_id)
    if access_block:
        raise HTTPException(status_code=HTTP_CONFLICT, detail=access_block)
    store = _get_store()
    if not session_id:
        mode = raw_mode or "execute"
        session_id = store.create_session(mode=mode, project_root=project_root)
    else:
        session = store.get_session(session_id)
        if not session:
            raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
        stored_mode = str(session.get("mode") or "execute")
        mode = raw_mode or stored_mode
        if raw_mode and raw_mode != stored_mode:
            store.update_session(session_id, mode=raw_mode)
    from bandi.core.agent_runner import session_slot_busy

    if session_slot_busy(session_id, store):
        raise HTTPException(status_code=HTTP_CONFLICT, detail=f"SESSION_BUSY: {session_id}")
    from bandi.core.message import Message, MessageRole

    msg = Message(role=MessageRole.USER, content=message)
    store.append_message(session_id, msg)
    entity_matches = re.findall(r"@([\w-]+)", message)
    entity_refs = []
    if entity_matches:
        o_store = _get_ontology_store()
        o_graph = o_store.graph()
        for eid in entity_matches:
            e = o_graph.entities.get(eid)
            if e:
                entity_refs.append(
                    {"id": e.id, "name": e.label or e.id, "type": e.classes[0] if e.classes else ""}
                )

    from bandi.core.agent_runner import run_agent_turn

    user_id, account_store = _identity_from(body)
    result = await run_agent_turn(
        user_input=message,
        mode=mode,
        session_id=session_id or None,
        session_store=store,
        triple_ontology_store=_get_ontology_store(),
        append_user_message=False,
        project_id=project_id or None,
        employee_id=str(body.get("employee_id") or "").strip() or None,
        project_root=str(body.get("project_root") or "").strip() or None,
        session_timeout_sec=_session_timeout_sec_from(body),
        user_id=user_id,
        account_store=account_store,
    )
    reply = result.reply
    session_id = result.session_id
    if result.terminal_reason == "SESSION_BUSY":
        raise HTTPException(status_code=HTTP_CONFLICT, detail=f"SESSION_BUSY: {session_id}")

    return {
        "reply": reply,
        "session_id": session_id,
        "mode": mode,
        "terminal_reason": result.terminal_reason,
        "total_tokens": result.total_tokens,
        "turn_index": result.turn_index,
        "entities_referenced": entity_refs,
    }


@app.post("/api/agent/stream")
async def agent_stream(body: dict[str, Any]) -> StreamingResponse:
    message = body.get("message", "")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST, detail="MISSING_FIELDS: message is required"
        )
    raw_mode = body.get("mode") or ""
    if raw_mode not in ("", "execute", "plan"):
        if raw_mode == "ask":
            logging.getLogger(__name__).warning(
                "legacy mode 'ask' normalized to 'execute' (dual-mode since day124x-u)"
            )
        raw_mode = ""
    session_id = body.get("session_id") or ""
    project_id = str(body.get("project_id") or "").strip()
    project_root = _validated_session_project_root(body.get("project_root"), project_id)
    access_block = _project_access_block(project_id, session_id)
    if access_block:
        raise HTTPException(status_code=HTTP_CONFLICT, detail=access_block)
    store = _get_store()
    if not session_id:
        mode = raw_mode or "execute"
        session_id = store.create_session(mode=mode, project_root=project_root)
    else:
        session = store.get_session(session_id)
        if not session:
            raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
        stored_mode = str(session.get("mode") or "execute")
        mode = raw_mode or stored_mode
        if raw_mode and raw_mode != stored_mode:
            store.update_session(session_id, mode=raw_mode)
    from bandi.core.agent_runner import session_slot_busy

    if session_slot_busy(session_id, store):
        raise HTTPException(status_code=HTTP_CONFLICT, detail=f"SESSION_BUSY: {session_id}")

    async def event_stream() -> AsyncGenerator[str, None]:
        from bandi.core.agent_runner import run_agent_turn
        from bandi.core.message import Message, MessageRole

        msg = Message(role=MessageRole.USER, content=message)
        store.append_message(session_id, msg)
        handler = _SSEStreamHandler()
        # -3c：过程累积器（SSE 路径）——done 发帧前落 process 条目
        from bandi.core.process_recorder import ProcessRecorder

        recorder = ProcessRecorder(handler, store, session_id)

        async def _run() -> None:
            from bandi.core.agent_runner import (
                _active_contexts,
                _active_lock,
                discard_pending_injection_subs,
                register_pending_injection_sub,
            )
            from bandi.core.events import Event, EventType

            # run_agent_turn 内部才注册，旧"查 active_ctx 才订阅"时序上
            # 必然落空；register 后由 flush_pending_injection_subs 补挂
            _injection_subs: list = []

            async def _on_injected(event: Event) -> None:
                sid = event.data.get("session_id", "") if isinstance(event.data, dict) else ""
                if sid == session_id:
                    await handler.on_user_input_injected(
                        sid, event.data.get("turn_index", 0) if isinstance(event.data, dict) else 0
                    )

            register_pending_injection_sub(session_id, _on_injected)
            with _active_lock:
                active_ctx = _active_contexts.get(session_id)
            if active_ctx is not None:
                await active_ctx.bus.subscribe(EventType.USER_INPUT_INJECTED, _on_injected)
                _injection_subs.append(
                    (active_ctx.bus, EventType.USER_INPUT_INJECTED, _on_injected)
                )

            try:
                from bandi.core.abort_controller import AbortController
                from bandi.core.user_interaction import HTTPUserInteraction

                abort_controller = AbortController()
                if session_id:
                    _active_abort_controllers[session_id] = abort_controller
                user_id, account_store = _identity_from(body)
                # 推 interaction 帧到本 SSE 流 → 挂起等用户 POST /confirm|/clarify
                http_ui = HTTPUserInteraction(
                    _get_interaction_store(),
                    session_id=session_id,
                    on_interaction=lambda item: handler.put(
                        "interaction", _interaction_frame(item)
                    ),
                )
                result = await run_agent_turn(
                    user_input=message,
                    mode=mode,
                    session_id=session_id or None,
                    session_store=store,
                    stream_handler=recorder,
                    append_user_message=False,
                    abort_controller=abort_controller,
                    user_interaction=http_ui,
                    project_id=project_id or None,
                    employee_id=str(body.get("employee_id") or "").strip() or None,
                    project_root=str(body.get("project_root") or "").strip() or None,
                    session_timeout_sec=_session_timeout_sec_from(body),
                    user_id=user_id,
                    account_store=account_store,
                )
                if result.terminal_reason == "SESSION_BUSY":
                    await handler.put("error", {"message": f"SESSION_BUSY: {result.session_id}"})
                else:
                    # -3c：轮末落 process 条目（recorder.on_done 对无 on_done 的
                    await recorder.on_done(
                        result.session_id,
                        result.terminal_reason or "",
                        result.total_tokens,
                        result.turn_index,
                    )
                    await handler.put(
                        "done",
                        {
                            "session_id": result.session_id,
                            "terminal_reason": result.terminal_reason,
                            "total_tokens": result.total_tokens,
                            "turn_index": result.turn_index,
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error("agent stream failed: %s", exc)
                try:
                    await recorder.persist_partial(session_id, 0)
                except Exception:
                    logger.exception("process persist on error failed: session=%s", session_id)
                await handler.put("error", {"message": str(exc)})
            finally:
                for bus_ref, et, fn in _injection_subs:
                    await bus_ref.unsubscribe(et, fn)
                discard_pending_injection_subs(session_id)
                if session_id:
                    _unregister_abort_controller(session_id, abort_controller)

        task = asyncio.create_task(_run())
        try:
            async for frame in handler.frames():
                yield frame
                if frame.startswith("event: done") or frame.startswith("event: error"):
                    break
        finally:
            if not task.done():
                task.cancel()
            await task

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/sessions/{session_id}/abort")
async def session_abort(session_id: str) -> dict[str, Any]:
    """中断指定会话的流式执行（中断的 API 面）。

     修复：仅 `controller.abort()` 不足以中断一个**正阻塞在待决策
    交互上的**轮次——该轮协程停在 `InteractionStore.get_decision()` 等待
    resolve/reject，收不到 abort 信号 ⇒ 永不出终态帧 ⇒ 前端持续「正在回复中…」
    （未结算，只能等 TTL 到期）。故中止时必须一并拒绝该会话的
    挂起交互，让挂起点返回、轮次得以终结。
    """
    with _abort_controllers_lock:
        controller = _active_abort_controllers.get(session_id)
    if controller is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="NO_ACTIVE_ABORT_CONTROLLER")
    controller.abort("aborted via API")
    # 解开挂起的交互（否则轮次停在 await get_decision，abort 信号到不了）
    rejected: list[str] = []
    try:
        istore = _get_interaction_store()
        for item in istore.list_pending(session_id=session_id):
            if istore.reject(item.interaction_id):
                rejected.append(item.interaction_id)
    except Exception:  # noqa: BLE001 - 中止主语义优先，清理失败不得阻断
        pass
    # 注：abort 是异步语义（信号+拒绝挂起交互），不在此同步等待轮次收尾——
    # settle 等待块在 WS 路径上悬挂
    # 登记级残余）；重试竞态由前端 abort-first + 300ms 间隔 + 异实例租约接管兜住。
    return {
        "aborted": True,
        "session_id": session_id,
        "rejected_interactions": rejected,
    }


@app.post("/api/agent/plan")
async def agent_plan(body: dict[str, Any]) -> dict[str, Any]:
    body["mode"] = "plan"
    return await chat(body)


# ── Session ─────────────────────────────────────────────────


@app.get("/api/memory/events")
async def memory_events(
    event_type: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    from bandi.cli.memory import query_memory_events

    db = Path.home() / ".bandi" / "data" / "hierarchy_global.db"
    if not db.exists():
        return {"events": []}
    events = await asyncio.to_thread(query_memory_events, db, event_type=event_type, limit=limit)
    return {"events": events}


# 测试注入 override：审批重放的写入目标（缺省落 ~/.bandi/data 默认库）
_memory_replay_overrides: dict[str, Any] | None = None


def _get_memory_pending_store() -> Any:
    """功能：记忆待批准队列单例（测试经 staging.reset_pending_store 注入 override）。"""
    from bandi.memory.staging import get_pending_store

    return get_pending_store()


def _default_memory_replay_targets() -> dict[str, Any]:
    """功能：审批重放默认写入目标（与维护/整合默认库同路径）。"""
    from bandi.memory.episodic import EpisodicStore
    from bandi.memory.long_term import LONG_TERM_DECAY_HALF_LIFE_DAYS, LongTermMemory
    from bandi.memory.markdown_memory import MarkdownMemoryStore

    data_dir = Path.home() / ".bandi" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return {
        "long_term": LongTermMemory(
            str(data_dir / "long_term.db"),
            decay_half_life_days=LONG_TERM_DECAY_HALF_LIFE_DAYS,
        ),
        "episodic": EpisodicStore(str(data_dir / "episodic.db")),
        "markdown": MarkdownMemoryStore(data_dir),
    }


def _replay_memory_record(record: Any) -> bool:
    """功能：批准重放（复用目标 store 公开写入 API；失败记 warning 并保留记录）。"""
    from bandi.memory.staging import replay_record

    overrides = _memory_replay_overrides
    targets = overrides if overrides is not None else _default_memory_replay_targets()
    try:
        return replay_record(
            record,
            long_term=targets.get("long_term"),
            episodic=targets.get("episodic"),
            markdown=targets.get("markdown"),
        )
    except Exception as exc:  # noqa: BLE001 — 坏 payload 重放失败不得丢弃待批准记录
        logger.warning("memory pending replay failed: %s", exc)
        return False
    finally:
        if overrides is None:
            for target in targets.values():
                closer = getattr(target, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001 — 关闭失败不遮蔽重放结果
                        pass


# 校验兼容两种形态（存量 8 位记录与新 24 位记录均合法）
_PENDING_ID_RE = re.compile(r"^[0-9a-fA-F]{8}$|^[0-9a-fA-F]{24}$")


def _validate_pending_id(pending_id: str) -> str:
    """功能：校验待批准记录 id 格式（8 位 hex）——非法格式 400 而非 404。"""
    value = str(pending_id or "").strip()
    if not _PENDING_ID_RE.match(value):
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_PENDING_ID")
    return value


def _pending_conflict_old_value(record: Any) -> str:
    """功能：冲突待批准记录的旧值摘要（≤60 字且脱敏；读取不到时为空串）。"""
    from bandi.memory.staging import mask_summary

    payload = record.payload or {}
    item = payload.get("item") or {}
    item_id = str(item.get("id") or "").strip()
    if not item_id:
        return ""
    overrides = _memory_replay_overrides
    store: Any | None = None
    owned = False
    if overrides is not None:
        store = overrides.get("long_term")
    else:
        from bandi.memory.long_term import LONG_TERM_DECAY_HALF_LIFE_DAYS, LongTermMemory

        data_dir = Path.home() / ".bandi" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        store = LongTermMemory(
            str(data_dir / "long_term.db"),
            decay_half_life_days=LONG_TERM_DECAY_HALF_LIFE_DAYS,
        )
        owned = True
    try:
        hit = store.get(item_id) if store is not None else None
        content = str(getattr(hit, "content", "") or "")
        return mask_summary(content[:60])
    finally:
        if owned and store is not None:
            closer = getattr(store, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001 — 关闭失败不遮蔽摘要结果
                    pass


_FEEDBACK_VALUES = ("up",)


@app.post("/api/sessions/{session_id}/system-notes")
async def append_session_system_note(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """功能：持久化追加会话系统提示（同 dedupe_key 只保留一条）。"""
    import json

    from bandi.core.message import Message, MessageRole

    content = str(body.get("content") or "").strip()[:200]
    note_kind = str(body.get("note_kind") or "").strip()
    dedupe_key = str(body.get("dedupe_key") or "").strip()
    if not content or not note_kind or not dedupe_key:
        raise HTTPException(status_code=400, detail="content/note_kind/dedupe_key required")
    if note_kind not in {"team_dissolved", "employee_retired"}:
        raise HTTPException(status_code=400, detail="note_kind not allowed")
    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND")
    # 去重需覆盖压缩边界之前的原始历史（防同一注记重复追加）
    for entry in store.get_branch(session_id, full=True):
        raw_meta = entry.get("metadata") or {}
        if isinstance(raw_meta, str):
            try:
                raw_meta = json.loads(raw_meta)
            except json.JSONDecodeError:
                raw_meta = {}
        if isinstance(raw_meta, dict) and (
            str(raw_meta.get("dedupe_key") or "") == dedupe_key
            or (
                str(raw_meta.get("note_kind") or "") == note_kind
                and str(entry.get("content") or "") == content
            )
        ):
            return {"created": False, "entry_id": str(entry.get("id") or "")}
    entry_id = store.append_message(
        session_id,
        Message(
            role=MessageRole.SYSTEM,
            content=content,
            meta={"note_kind": note_kind, "dedupe_key": dedupe_key},
        ),
    )
    return {"created": True, "entry_id": entry_id}


@app.post("/api/company/threads/{thread_id}/system-notes")
async def append_thread_system_note(
    thread_id: str, body: dict[str, Any], project_id: str = ""
) -> dict[str, Any]:
    """功能：持久化追加单聊系统提示（同 dedupe_key / 同内容只保留一条）。"""
    content = str(body.get("content") or "").strip()[:200]
    note_kind = str(body.get("note_kind") or "").strip()
    dedupe_key = str(body.get("dedupe_key") or "").strip()
    if not content or not note_kind:
        raise HTTPException(status_code=400, detail="content/note_kind required")
    if note_kind not in {"team_dissolved", "employee_retired"}:
        raise HTTPException(status_code=400, detail="note_kind not allowed")
    threads = _get_project_threads(project_id or "company")
    thread = threads.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="THREAD_NOT_FOUND")
    for message in thread.messages:
        if str(getattr(message, "sender", "")) != "system":
            continue
        meta = getattr(message, "metadata", {}) or {}
        if dedupe_key and str(meta.get("dedupe_key") or "") == dedupe_key:
            logger.info("system_note_dedupe_hit kind=%s key=%s", note_kind, dedupe_key)
            return {"created": False, "thread_id": thread_id}
        if str(getattr(message, "content", "")) == content:
            logger.info("system_note_dedupe_hit kind=%s content=%s", note_kind, content[:40])
            return {"created": False, "thread_id": thread_id}
    message = threads.add_message(
        thread_id,
        "system",
        content,
        metadata={"note_kind": note_kind, "dedupe_key": dedupe_key},
    )
    return {
        "created": True,
        "thread_id": thread_id,
        "message_id": message.message_id if message else "",
    }


@app.patch("/api/sessions/{session_id}/entries/{entry_id}")
async def patch_session_entry_feedback(
    session_id: str, entry_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """消息点赞标记（up / 空值清除）。点赞落库并提名候选记忆进待批准队列（人工闸门）。

    提名失败不影响点赞本身（点赞是用户已表达的事实，记忆晋升是派生收益）。
    """
    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    raw = (body or {}).get("feedback")
    value = str(raw).strip() if raw is not None else ""
    if value and value not in _FEEDBACK_VALUES:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_FEEDBACK_VALUE")
    entry = store.get_entry(entry_id, session_id)
    if entry is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="ENTRY_NOT_FOUND")

    ok = store.set_entry_feedback(entry_id, value or None, session_id)
    nominated = ""
    if value == "up":
        nominated = await _nominate_liked_memory(session_id, entry_id, entry)
    return {
        "ok": ok,
        "entry_id": entry_id,
        "feedback": value or None,
        "nominated": bool(nominated),
    }


async def _nominate_liked_memory(session_id: str, entry_id: str, entry: dict[str, Any]) -> str:
    """功能：把被点赞的回复提名进记忆待批准队列（幂等去重，按 entry 唯一）。

    提名是派生收益——失败只记日志，点赞本身照常落库。
    """
    text = str(entry.get("content") or "").strip()
    if not text:
        return ""
    try:
        pending = _get_memory_pending_store()
        dedup_key = f"liked:{entry_id}"
        for record in await asyncio.to_thread(pending.list_pending):
            if (record.payload or {}).get("dedup_key") == dedup_key:
                return record.id8
        record = await asyncio.to_thread(
            pending.stage,
            {
                "subsystem": "memory",
                "action": "store_long_term",
                "summary": text[:500],
                "payload": {
                    "summary": text[:500],
                    "session_id": session_id,
                    "entry_id": entry_id,
                    "origin_source": "user_like",
                    "dedup_key": dedup_key,
                    "draft": {"kind": "点赞提名", "target": "长期记忆"},
                },
            },
        )
        return str(record.id8)
    except Exception:  # noqa: BLE001 —— 提名失败不得影响点赞落库
        logging.getLogger(__name__).warning("点赞记忆提名失败: entry=%s", entry_id)
        return ""


@app.get("/api/memory/pending")
async def list_memory_pending() -> dict[str, Any]:
    """待批准记忆写入列表（摘要脱敏——只回摘要/动作/来源/时间/冲突标记/草稿元数据）。"""
    from bandi.memory.staging import mask_summary

    store = _get_memory_pending_store()
    records = await asyncio.to_thread(store.list_pending)
    rows: list[dict[str, Any]] = []
    for record in records:
        payload = record.payload or {}
        conflict = bool(payload.get("conflict", False))
        row: dict[str, Any] = {
            "id": record.id8,
            "subsystem": record.subsystem,
            "action": record.action,
            "summary": mask_summary(record.summary),
            "origin": record.origin,
            "origin_source": str(payload.get("origin_source") or ""),
            "created_at": iso_local(record.created_at),
            "conflict": conflict,
            "draft": dict(payload.get("draft") or {}),
        }
        if conflict:
            row["old_value"] = await asyncio.to_thread(_pending_conflict_old_value, record)
        rows.append(row)
    return {"pending": rows}


@app.post("/api/memory/pending/{pending_id}/approve")
async def approve_memory_pending(pending_id: str) -> dict[str, Any]:
    """批准一条待批准写入——重放成功后才出队（失败保留待重试）。"""
    pending_id = _validate_pending_id(pending_id)
    store = _get_memory_pending_store()
    record = await asyncio.to_thread(store.get_pending, pending_id)
    if record is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="PENDING_NOT_FOUND")
    if not await asyncio.to_thread(_replay_memory_record, record):
        raise HTTPException(status_code=500, detail="MEMORY_REPLAY_FAILED")
    await asyncio.to_thread(store.discard_pending, pending_id)
    return {"approved": True, "id": pending_id, "action": record.action}


@app.post("/api/memory/pending/{pending_id}/reject")
async def reject_memory_pending(pending_id: str) -> dict[str, Any]:
    """拒绝一条待批准写入（丢弃，不落库）。

    整合提名额外打「晋升已拒绝」标记——拒绝必须粘滞，否则每小时巩固周期
    重扫情节层会把同一条重新提名进暂存区，表现为「删了又回来」。
    """
    pending_id = _validate_pending_id(pending_id)
    store = _get_memory_pending_store()
    record = await asyncio.to_thread(store.get_pending, pending_id)
    if record is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="PENDING_NOT_FOUND")
    discarded = await asyncio.to_thread(store.discard_pending, pending_id)
    if discarded and record.action == "consolidate_episode":
        episode_id = _consolidation_episode_id(record)
        if episode_id:
            from bandi.memory.consolidation import mark_promotion_rejected

            await asyncio.to_thread(mark_promotion_rejected, episode_id)
    return {"rejected": bool(discarded), "id": pending_id}


def _consolidation_episode_id(record: Any) -> str:
    """功能：从整合暂存记录解析源情节 id（metadata 优先，稳定键前缀兜底）。"""
    item = (record.payload or {}).get("item")
    if not isinstance(item, dict):
        return ""
    episode_id = str((item.get("metadata") or {}).get("episode_id") or "").strip()
    if episode_id:
        return episode_id
    item_id = str(item.get("id") or "")
    if item_id.startswith("consolidated:"):
        return item_id.split(":", 1)[1]
    return ""


# ── 团队记忆写入门（Phase2-2）：提名列表 / 上报建议 / 用户确认落库 / 驳回 ──


def _get_team_staging_store() -> Any:
    from bandi.memory.staging import default_memory_store_dir
    from bandi.memory.team_staging import TeamMemoryStaging

    return TeamMemoryStaging(default_memory_store_dir())


@app.get("/api/memory/team-pending")
async def list_team_memory_pending() -> dict[str, Any]:
    """待确认团队记忆列表（超时项顺带清理——拍板：超时丢弃；TTL 单一来源
    在 team_staging.DEFAULT_TTL_DAYS，写侧 propose 亦清理）。"""
    from bandi.memory.team_staging import DEFAULT_TTL_DAYS

    store = _get_team_staging_store()
    await asyncio.to_thread(store.discard_expired, DEFAULT_TTL_DAYS)
    records = await asyncio.to_thread(store.list_pending)
    team_names: dict[str, str] = {}
    try:
        for team in (await company_org_teams()).get("teams", []):
            team_names[str(team.get("team_id") or "")] = str(team.get("name") or "")
    except Exception:  # noqa: BLE001 — 名称解析失败回落 id，不阻断审批
        team_names = {}
    employee_names: dict[str, str] = {}
    try:
        for employee in _org_api_dispatcher().org_engine.list_employees(include_inactive=True):
            employee_names[str(employee.employee_id)] = str(employee.name or "")
    except Exception:  # noqa: BLE001
        employee_names = {}
    project_names: dict[str, str] = {}
    try:
        for project in _get_project_store().list_projects():
            project_names[str(project.project_id)] = str(project.name or "")
    except Exception:  # noqa: BLE001
        project_names = {}
    pending = []
    for record in records:
        payload = dict(record.payload)
        employee_id = str(payload.get("employee_id") or "")
        team_id = str(payload.get("team_id") or "")
        project_id = str(payload.get("project_id") or "")
        pending.append(
            {
                "id": record.id8,
                "summary": record.summary,
                "payload": payload,
                "created_at": iso_local(record.created_at),
                "employee_name": employee_names.get(employee_id, employee_id),
                "team_name": team_names.get(team_id, team_id),
                "project_name": project_names.get(project_id, project_id),
            }
        )
    return {"pending": pending}


@app.post("/api/memory/team-pending/{pending_id}/report")
async def report_team_memory(pending_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """上报上级角色——意见仅存档为建议，无落库效力（确认权仅用户本人）。"""
    body = body or {}
    ok = await asyncio.to_thread(
        _get_team_staging_store().report_to_superior,
        pending_id,
        opinion=str(body.get("opinion") or ""),
        reporter_role=str(body.get("reporter_role") or ""),
    )
    if not ok:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="PENDING_NOT_FOUND")
    return {"reported": True, "id": pending_id}


def _create_team_memory_skill(payload: dict[str, Any], skill_name: str) -> str:
    """功能：把团队返工教训写成 SKILL.md，并 best-effort 绑定到目标员工。"""
    import tempfile

    import yaml

    from bandi.skills.importer import SkillImporter

    name = (skill_name or "").strip() or "团队经验"
    reason = str(payload.get("reason") or "").strip()
    analysis = str(payload.get("analysis") or "").strip().replace("分析：", "", 1)
    description = f"团队返工经验：{reason or name}"[:1024]
    frontmatter = {
        "name": name,
        "description": description,
        "version": "1.0.0",
        "triggers": [],
    }
    body = "\n".join(
        [
            f"# {name}",
            "",
            "## Instructions",
            f"本技能来自团队返工复盘：{reason or '同任务执行经验'}。",
            "",
            analysis or "执行同类任务前，先确认关键输入齐全，再进入产出阶段。",
            "",
        ]
    )
    text = (
        "---\n"
        + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + body
    )
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "SKILL.md"
        source.write_text(text, encoding="utf-8")
        result = SkillImporter(os.environ.get("BANDI_SKILLS_DIR")).import_skill(str(source))
    employee_id = str(payload.get("employee_id") or "").strip()
    if employee_id:
        try:
            from bandi.company.agent_profile import AgentProfileManager

            AgentProfileManager().bind_resource(employee_id, "general_skill", result.skill_name)
        except Exception:  # 档案不存在/已绑定时技能文件仍保留
            logger.debug("team skill bind skipped (employee=%s)", employee_id, exc_info=True)
    return result.skill_path


@app.post("/api/memory/team-pending/{pending_id}/confirm")
async def confirm_team_memory(
    pending_id: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """用户本人确认——payload 带 team_id 落 episodic 团队分区。

    顺序铁律（端到端 暴露的静默丢数据）：**落库先于消费暂存项**。
    原实现先 `store.confirm()`（内含 discard）再落库，落库失败即 500 且 pending 归零
    ⇒ 用户点一次「批准」这条记忆永久消失且不可重试。落库用稳定 episode_id
    （`team-pending-<id8>`）⇒ 消费失败/进程崩溃后重试是 `INSERT OR REPLACE` 覆盖，不重复。
    """
    store = _get_team_staging_store()
    record = store.get_pending(pending_id)
    if record is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="PENDING_NOT_FOUND")
    payload = dict(record.payload)
    team_id = str(payload.get("team_id") or "").strip()
    if team_id:
        from bandi.company.team_registry import TeamRegistry

        if TeamRegistry().get_status(team_id) == "dissolved":
            raise HTTPException(status_code=409, detail="TEAM_DISSOLVED")

    def _land() -> None:
        from bandi.memory.episodic import Episode, get_shared_episodic

        # （否则行被员工分区隔离，团队查询永远查不到）
        get_shared_episodic().record(
            Episode(
                episode_id=f"team-pending-{pending_id}",
                summary=str(payload.get("summary") or ""),
                project_id=payload.get("project_id"),
                team_id=payload.get("team_id"),
            )
        )

    try:
        await asyncio.to_thread(_land)
    except Exception as e:
        # 落库失败必须让暂存项留在原位（可重试）——异常上抛走全局错误体，不吞
        logger.error("team_memory_land_failed (pending kept, id=%s): %s", pending_id, e)
        raise HTTPException(status_code=503, detail="TEAM_MEMORY_LAND_FAILED") from e

    skill_path = ""
    if bool((body or {}).get("create_skill")):
        try:
            skill_path = await asyncio.to_thread(
                _create_team_memory_skill,
                payload,
                str((body or {}).get("skill_name") or ""),
            )
        except Exception as e:  # 技能写入失败保留暂存项可重试
            logger.error("team_memory_skill_failed (pending kept, id=%s): %s", pending_id, e)
            raise HTTPException(status_code=503, detail="TEAM_SKILL_CREATE_FAILED") from e

    from bandi.utils.errors import error_text

    try:
        store.confirm(pending_id, confirmer=DEFAULT_LOCAL_USER_ID)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=error_text(e)) from e
    except KeyError as e:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="PENDING_NOT_FOUND") from e
    return {
        "confirmed": True,
        "id": pending_id,
        **({"skill_path": skill_path} if skill_path else {}),
    }


@app.post("/api/memory/team-pending/{pending_id}/reject")
async def reject_team_memory(pending_id: str) -> dict[str, Any]:
    """用户本人驳回（丢弃，不落库）。"""
    from bandi.utils.errors import error_text

    try:
        discarded = await asyncio.to_thread(
            _get_team_staging_store().reject, pending_id, confirmer=DEFAULT_LOCAL_USER_ID
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=error_text(e)) from e
    if not discarded:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="PENDING_NOT_FOUND")
    return {"rejected": True, "id": pending_id}


# ── 技能晋升审批队列（技能侧无自动批准路径：自动晋升只进暂存区） ──────────

# 测试注入 override：技能重放落盘目录（缺省 ~/.bandi/skills）
_skill_write_overrides: dict[str, Any] | None = None


def _get_skill_pending_store() -> Any:
    """功能：技能待批准队列单例（测试经 staging.reset_skill_pending_store 注入）。"""
    from bandi.memory.staging import get_skill_pending_store

    return get_skill_pending_store()


def _write_back_approved_skill(record: Any) -> bool:
    """功能：批准后回写员工档案（payload 带定位时）——pending 引用转真实技能名。

    无定位字段（历史/手工暂存记录）视为无需回写；带定位但档案写失败返回
    False，审批端点据此保留待批准记录以便重试（防技能已落盘而档案永久失联）。
    """
    payload = dict(getattr(record, "payload", None) or {})
    profile_path = str(payload.get("profile_path") or "").strip()
    employee_id = str(payload.get("employee_id") or "").strip()
    pattern_key = str(payload.get("pattern_key") or "").strip()
    if not (profile_path and employee_id and pattern_key):
        return True
    from bandi.company.employee_evolution import EmployeeEvolutionManager

    attached = EmployeeEvolutionManager.attach_approved_skill(
        profile_path,
        employee_id=employee_id,
        pattern_key=pattern_key,
        skill_name=str(payload.get("skill_name") or "").strip(),
    )
    if attached:
        # A6 桥接：批准回写档案后同步组织员工 metadata（前端档案/招募同源可见）
        try:
            home = Path(profile_path).resolve().parents[2]
            manager = EmployeeEvolutionManager(home_dir=home)
            manager.sync_org_employee_metadata(_org_api_dispatcher().org_engine, employee_id)
        except Exception:
            logger.debug("approved skill metadata sync failed", exc_info=True)
    return attached


def _replay_skill_record(record: Any) -> bool:
    """功能：技能批准重放（原子写 SKILL.md + 档案回写；失败记 warning 并保留记录）。"""
    from bandi.memory.staging import replay_skill_record

    overrides = _skill_write_overrides
    skills_dir = (overrides or {}).get("skills_dir")
    try:
        if not replay_skill_record(record, skills_dir=skills_dir):
            return False
        return _write_back_approved_skill(record)
    except Exception as exc:  # noqa: BLE001 — 坏 payload 重放失败不得丢弃待批准记录
        logger.warning("skill pending replay failed: %s", exc)
        return False


@app.get("/api/skills/pending")
async def list_skills_pending() -> dict[str, Any]:
    """技能审批：待批准晋升列表（脱敏——只回技能名/动作/来源/时间/摘要）。"""
    from bandi.memory.staging import mask_summary

    store = _get_skill_pending_store()
    records = await asyncio.to_thread(store.list_pending)
    rows = [
        {
            "id": record.id8,
            "skill_name": str((record.payload or {}).get("skill_name") or ""),
            "action": record.action,
            "summary": mask_summary(record.summary),
            "origin": record.origin,
            "created_at": iso_local(record.created_at),
        }
        for record in records
    ]
    return {"pending": rows}


@app.post("/api/skills/pending/{pending_id}/approve")
async def approve_skill_pending(pending_id: str) -> dict[str, Any]:
    """技能审批：批准并落盘 SKILL.md——写盘成功后才出队（失败保留待重试）。"""
    pending_id = _validate_pending_id(pending_id)
    store = _get_skill_pending_store()
    record = await asyncio.to_thread(store.get_pending, pending_id)
    if record is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="PENDING_NOT_FOUND")
    if not await asyncio.to_thread(_replay_skill_record, record):
        raise HTTPException(status_code=500, detail="SKILL_REPLAY_FAILED")
    await asyncio.to_thread(store.discard_pending, pending_id)
    return {
        "approved": True,
        "id": pending_id,
        "skill_name": str((record.payload or {}).get("skill_name") or ""),
    }


@app.post("/api/skills/pending/{pending_id}/reject")
async def reject_skill_pending(pending_id: str) -> dict[str, Any]:
    """技能审批：拒绝一条待批准晋升（丢弃，不写技能库）。"""
    pending_id = _validate_pending_id(pending_id)
    store = _get_skill_pending_store()
    record = await asyncio.to_thread(store.get_pending, pending_id)
    if record is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="PENDING_NOT_FOUND")
    discarded = await asyncio.to_thread(store.discard_pending, pending_id)
    return {"rejected": bool(discarded), "id": pending_id}


# ── SOP 蒸馏（NL / 程序性记忆 → draft 技能卡；不写 SKILL.md、不进审批暂存区） ──

_DISTILL_MEMORY_TYPES = frozenset({"procedural", "process_conclusion"})
_DISTILL_MEMORY_DEFAULT_LIMIT = 50
_DISTILL_MEMORY_MAX_LIMIT = 200
_DISTILL_MEMORY_SCAN_LIMIT = 500
_DISTILL_CARD_SUFFIX = ".card.json"

# 测试注入 override：{"distiller", "long_term", "skills_dir", "available_tools"}
_skill_distill_overrides: dict[str, Any] | None = None
_skill_distill_lock = threading.Lock()
_skill_distiller: Any | None = None
_skill_distill_long_term: Any | None = None
_skill_distill_tools: list[dict[str, Any]] | None = None


def _get_skill_distill_long_term() -> Any:
    """功能：蒸馏记忆来源长期库（测试经 _skill_distill_overrides 注入）。"""
    overrides = _skill_distill_overrides or {}
    injected = overrides.get("long_term")
    if injected is not None:
        return injected
    global _skill_distill_long_term
    with _skill_distill_lock:
        if _skill_distill_long_term is None:
            from bandi.memory.long_term import (
                LONG_TERM_DECAY_HALF_LIFE_DAYS,
                LongTermMemory,
            )

            data_dir = Path.home() / ".bandi" / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            _skill_distill_long_term = LongTermMemory(
                str(data_dir / "long_term.db"),
                decay_half_life_days=LONG_TERM_DECAY_HALF_LIFE_DAYS,
            )
    return _skill_distill_long_term


def _get_skill_distiller() -> Any:
    """功能：SOP 蒸馏器（懒建缓存 + 测试/宿主注入 override；与 CLI skills distill 同口径）。"""
    overrides = _skill_distill_overrides or {}
    injected = overrides.get("distiller")
    if injected is not None:
        return injected
    global _skill_distiller
    with _skill_distill_lock:
        if _skill_distiller is None:
            from bandi.agent.factory import build_provider
            from bandi.core.config import BandiConfig
            from bandi.skills.distiller import SkillDistiller
            from bandi.skills.loader import first_cascade_skills_dir

            skills_dir = str(overrides.get("skills_dir") or first_cascade_skills_dir())
            _skill_distiller = SkillDistiller(
                build_provider(BandiConfig.load()), skills_dir=skills_dir
            )
    return _skill_distiller


def _get_skill_distill_tools() -> list[dict[str, Any]]:
    """功能：蒸馏可用工具目录（懒建缓存；构建失败降级空目录不阻断蒸馏）。"""
    overrides = _skill_distill_overrides or {}
    injected = overrides.get("available_tools")
    if injected is not None:
        return list(injected)
    global _skill_distill_tools
    with _skill_distill_lock:
        if _skill_distill_tools is None:
            try:
                from bandi.agent.factory import build_tool_registry
                from bandi.skills.distiller import MODEL_TOOL_DESCRIPTION_CHAR_LIMIT

                _skill_distill_tools = [
                    {
                        "name": tool.name,
                        "description": (tool.description or "")[:MODEL_TOOL_DESCRIPTION_CHAR_LIMIT],
                        "input_schema": dict(tool.parameters or {}),
                    }
                    for tool in build_tool_registry().list_tools()
                ]
            except Exception as exc:  # noqa: BLE001 — 工具目录缺失不得阻断蒸馏
                logger.warning("skill distill tool catalog unavailable: %s", exc)
                _skill_distill_tools = []
    return list(_skill_distill_tools)


def _distill_memory_limit(body: dict[str, Any]) -> int:
    """功能：解析记忆条数上限（缺省默认；非整数/小于 1/非整值浮点显式 400
    不静默截断；超上限截断）。"""
    raw = body.get("limit")
    if raw is None:
        return _DISTILL_MEMORY_DEFAULT_LIMIT
    if isinstance(raw, bool):
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_DISTILL_LIMIT")
    if isinstance(raw, float) and not raw.is_integer():
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_DISTILL_LIMIT")
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_DISTILL_LIMIT") from exc
    if value < 1:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_DISTILL_LIMIT")
    return min(value, _DISTILL_MEMORY_MAX_LIMIT)


def _load_distill_memories(memory_source: str, body: dict[str, Any]) -> list[str]:
    """功能：按 metadata.memory_type 取长期记忆条目内容（时间倒序，上限 limit）。

    memory_type 精确匹配（大小写敏感）；content 非字符串条目跳过（防 None/int
    被 str() 污染为 "None" 之类文本进入蒸馏输入）。
    """
    limit = _distill_memory_limit(body)
    store = _get_skill_distill_long_term()
    items = store.list_items(
        project_id=str(body.get("project_id") or "").strip() or None,
        employee_id=str(body.get("employee_id") or "").strip() or None,
        limit=_DISTILL_MEMORY_SCAN_LIMIT,
    )
    contents = [
        item.content.strip()
        for item in items
        if isinstance(item.content, str)
        and item.content.strip()
        and str((item.metadata or {}).get("memory_type") or "").strip() == memory_source
    ]
    return contents[:limit]


def _reset_skill_tool_cache() -> None:
    """功能：蒸馏落卡后重置技能工具注册表缓存（同进程工具面立即可见新卡）。"""
    from bandi.tools.skill_tool import reset_skill_registry

    reset_skill_registry()


def _distill_card_path(distiller: Any, skill_id: str) -> Path | None:
    """功能：蒸馏卡落盘路径解析——优先取蒸馏器实例 skills_dir（与装载同源）
    其次注入 skills_dir，最后级联首目录；skill_id/目录值非法时返回 None
    （按未持久化处理，路径穿越不得借存在性检查冒充已落卡）。"""
    if not skill_id or Path(skill_id).name != skill_id:
        return None
    root = getattr(distiller, "skills_dir", None)
    if root is None:
        root = (_skill_distill_overrides or {}).get("skills_dir")
    if root is None:
        from bandi.skills.loader import first_cascade_skills_dir

        root = first_cascade_skills_dir()
    try:
        return Path(root).expanduser() / f"{skill_id}{_DISTILL_CARD_SUFFIX}"
    except (TypeError, ValueError):
        return None


@app.post("/api/skills/distill")
async def skills_distill(body: dict[str, Any]) -> dict[str, Any]:
    """SOP 蒸馏：raw_content 或程序性记忆 → draft 技能卡落 skills_dir。

    只产 draft（不写 SKILL.md、不进技能审批暂存区）；技能晋升仍走
    /api/skills/pending 审批门。memory_source 无命中 404，空内容/非法参数 400。
    """
    from bandi.skills.distiller import SkillDistillRequest

    raw_content = str((body or {}).get("raw_content") or "").strip()
    memory_source = str((body or {}).get("memory_source") or "").strip()
    if raw_content and memory_source:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="DISTILL_SOURCE_CONFLICT")
    memory_count: int | None = None
    if memory_source:
        if memory_source not in _DISTILL_MEMORY_TYPES:
            raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_MEMORY_SOURCE")
        contents = await asyncio.to_thread(_load_distill_memories, memory_source, body)
        if not contents:
            raise HTTPException(status_code=HTTP_NOT_FOUND, detail="MEMORY_SOURCE_EMPTY")
        raw_content = "\n\n".join(
            f"[记忆 {index}] {content}" for index, content in enumerate(contents, start=1)
        )
        memory_count = len(contents)
    if not raw_content:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_DISTILL_PAYLOAD")
    request = SkillDistillRequest(
        title=str((body or {}).get("title") or "").strip(),
        raw_content=raw_content,
        business_domain=str((body or {}).get("business_domain") or "").strip() or None,
        available_tools=await asyncio.to_thread(_get_skill_distill_tools),
    )
    distiller = _get_skill_distiller()
    try:
        response = await distiller.distill(
            request, session_id=f"skill_distill:{uuid.uuid4().hex[:8]}"
        )
    except Exception as exc:  # 失败显式 500（保留语义），不落任何产物
        logger.warning("skill distill failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"SKILL_DISTILL_FAILED: {exc}"[:500]) from exc
    skill_id = response.draft_skill.skill_id
    card_path = _distill_card_path(distiller, skill_id)
    if not skill_id or card_path is None or not card_path.is_file():
        # 落卡失败不得 200——调用方（Agent/前端）会误以为技能已可调用
        raise HTTPException(
            status_code=500,
            detail=f"SKILL_DISTILL_NOT_PERSISTED: {skill_id or 'missing-skill-id'}",
        )
    payload: dict[str, Any] = {
        "skill_id": skill_id,
        "name": response.draft_skill.name,
        "version": response.draft_skill.version,
        "warnings": list(response.warnings),
        "tool_suggestions": [
            {
                "name": item.name,
                "resolution_status": item.resolution_status,
                "matched_tool_name": item.matched_tool_name,
            }
            for item in response.tool_suggestions
        ],
        "persisted": True,
    }
    if memory_count is not None:
        payload["memory_count"] = memory_count
    try:
        _reset_skill_tool_cache()
    except Exception as exc:  # noqa: BLE001 — 缓存刷新失败不阻断蒸馏结果返回
        logger.warning("skill tool cache reset failed: %s", exc)
    return payload


# 测试注入 override：反馈写入目标（缺省落 ~/.bandi/data 默认长期库）
_memory_feedback_overrides: dict[str, Any] | None = None
_memory_feedback_target: Any | None = None
_memory_feedback_target_lock = threading.Lock()


def _get_memory_feedback_target() -> Any:
    """功能：反馈写入目标单例（懒建缓存；测试经 _memory_feedback_overrides 注入）。"""
    overrides = _memory_feedback_overrides
    if overrides is not None:
        return overrides["long_term"]
    global _memory_feedback_target
    with _memory_feedback_target_lock:
        if _memory_feedback_target is None:
            from bandi.memory.long_term import LONG_TERM_DECAY_HALF_LIFE_DAYS, LongTermMemory

            data_dir = Path.home() / ".bandi" / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            _memory_feedback_target = LongTermMemory(
                str(data_dir / "long_term.db"),
                decay_half_life_days=LONG_TERM_DECAY_HALF_LIFE_DAYS,
            )
    return _memory_feedback_target


def _run_memory_feedback(item_id: str, helpful: bool) -> bool:
    """功能：执行反馈写回（单例目标常驻进程；测试 override 由调用方持有）。"""
    target = _get_memory_feedback_target()
    return bool(target.note_feedback(item_id, helpful=helpful))


@app.post("/api/memory/feedback")
async def memory_feedback(body: dict[str, Any]) -> dict[str, Any]:
    """来源可靠性反馈（404 = 条目不存在；409 = metadata 损坏无法安全合并）。"""
    item_id = str((body or {}).get("item_id") or "").strip()
    helpful = (body or {}).get("helpful")
    if not item_id or not isinstance(helpful, bool):
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_FEEDBACK_PAYLOAD")
    try:
        hit = await asyncio.to_thread(_run_memory_feedback, item_id, helpful)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_CONFLICT, detail="MEMORY_METADATA_CONFLICT") from exc
    if not hit:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="MEMORY_ITEM_NOT_FOUND")
    return {"item_id": item_id, "helpful": helpful}


def _session_project_id(session: dict[str, Any]) -> str:
    """功能：从会话 metadata 解析 project_id（字符串/字典两种形态容错；损坏/缺失返回空串）。"""
    metadata = session.get("metadata") or "{}"
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            metadata = {}
    return str((metadata or {}).get("project_id") or "").strip()


def _session_origin(session: dict[str, Any]) -> str:
    """功能：会话 metadata 中的 origin。"""
    raw = session.get("metadata")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        meta = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("origin") or "")


def _session_target_role(session: dict[str, Any]) -> str:
    """功能：会话 metadata 中的员工单聊目标角色（普通/群聊返回空串）。"""
    raw = session.get("metadata")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("target_role") or "").strip()


def _session_employee_id(session: dict[str, Any]) -> str:
    """功能：会话 metadata 中的员工单聊目标员工 id。"""
    raw = session.get("metadata")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("employee_id") or "").strip()


def _session_employee_name(session: dict[str, Any]) -> str:
    """功能：会话 metadata 中的员工显示名（列表分组标题）。"""
    raw = session.get("metadata")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("employee_name") or "").strip()


def _session_thread_id(session: dict[str, Any]) -> str:
    """功能：会话 metadata 中绑定的线程 id（线程消息按会话过滤）。"""
    raw = session.get("metadata")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("thread_id") or "").strip()


@app.get("/api/sessions")
async def list_sessions(archived: bool = False) -> list[dict[str, Any]]:
    store = _get_store()
    status = "archived" if archived else "active"
    return [
        {
            "id": s["id"],
            "title": s.get("title", ""),
            "created_at": iso_local(s.get("created_at", "")),
            "message_count": store.count_branch(s["id"]),
            "project_id": _session_project_id(s),
            "origin": _session_origin(s),
            "target_role": _session_target_role(s),
            "employee_id": _session_employee_id(s),
            "employee_name": _session_employee_name(s),
            "thread_id": _session_thread_id(s),
            "status": str(s.get("status") or "active"),
        }
        for s in store.list_sessions(status=status)
        # 员工持久会话（role-session::，跨任务共享历史）不进任务会话列表——
        # 单聊/线程视图专用；直接按 id 取会话不受影响
        if not str(s["id"]).startswith("role-session::")
    ]


@app.post("/api/sessions")
async def create_session_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    """功能：显式创建空会话（团队群聊条目点击即建——先落库再打开，选中态可持久）。"""
    store = _get_store()
    session_id = str(body.get("session_id") or "").strip() or None
    if session_id and _is_reserved_session_id(session_id):
        raise HTTPException(status_code=400, detail="session_id is reserved")
    title = str(body.get("title") or "").strip()[:120]
    project_id = str(body.get("project_id") or "").strip() or None
    project_root = _validated_session_project_root(body.get("project_root"), project_id or "")
    target_role = str(body.get("target_role") or "").strip()
    employee_id = str(body.get("employee_id") or "").strip()
    employee_name = str(body.get("employee_name") or "").strip()
    origin = str(body.get("origin") or "").strip()
    if session_id and store.get_session(session_id) is not None:
        raise HTTPException(status_code=409, detail="session exists")
    if project_id and project_id != "company":
        project = _get_project_store().get_project(project_id)
        if project is None:
            raise HTTPException(status_code=400, detail=f"project not found: {project_id}")
    if target_role:
        from bandi.api.company_chat import _resolve_registered_role

        try:
            target_role = _resolve_registered_role(target_role, _org_api_dispatcher().org_engine)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if _employee_tombstones().get(target_role) is not None:
            raise HTTPException(status_code=400, detail="EMPLOYEE_RETIRED")
        employees = _org_api_dispatcher().org_engine.list_employees(role_id=target_role)
        employee = employees[0] if employees else None
        if employee is None:
            raise HTTPException(
                status_code=400, detail=f"employee not found for role: {target_role}"
            )
        employee_id = employee_id or str(employee.employee_id or "")
        employee_name = employee_name or str(employee.name or "")
        origin = "employee_dm"
    created = store.create_session(
        session_id=session_id,
        title=title,
        project_id=project_id,
        origin=origin or None,
        target_role=target_role or None,
        employee_id=employee_id or None,
        employee_name=employee_name or None,
        project_root=project_root,
    )
    return {"session": store.get_session(created)}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str, at: str | None = None) -> dict[str, Any]:
    store = _get_store()
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    try:
        entries = store.resolved_branch(session_id, at=at)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail=str(exc)) from exc
    # checkpoint 是状态快照条目（role/content 天然为空），不属于对话消息——
    # 不下发以免客户端拿到几十条空条目自己过滤（每轮 4 条，长会话累积显著）
    messages = [entry for entry in entries if str(entry.get("entry_type") or "") != "checkpoint"]
    return {"session": session, "messages": messages}


@app.patch("/api/sessions/{session_id}")
async def rename_session(session_id: str, body: dict[str, str]) -> dict[str, Any]:
    """会话重命名（title；会话侧栏右键菜单消费）。"""
    title = str((body or {}).get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="title is required")
    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    store.update_session(session_id, title=title[:120])
    return {"ok": True, "session_id": session_id, "title": title[:120]}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, bool]:
    store = _get_store()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    raw_meta = session.get("metadata") or {}
    if isinstance(raw_meta, str):
        try:
            raw_meta = json.loads(raw_meta)
        except ValueError:
            raw_meta = {}
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    thread_id = str(meta.get("thread_id") or "").strip()
    project_id = str(meta.get("project_id") or "").strip()
    ok = store.delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    await _interrupt_session_work_items(session_id)
    # 群聊窗口级联：会话删除即终止其线程（新会话新线程，不继承旧对话）
    thread_deleted = False
    if thread_id:
        threads = _threads_for_project(project_id)
        if threads is not None:
            thread_deleted = threads.delete_thread(thread_id)
    await _run_session_end_hooks(session_id)
    return {"ok": True, "thread_deleted": thread_deleted}


def _bind_session_thread(session_id: str, thread_id: str) -> None:
    """功能：群聊窗口绑定——会话 metadata 记线程 id（删除会话时级联删线程，新会话新窗口）。"""
    sid = str(session_id or "").strip()
    tid = str(thread_id or "").strip()
    if not sid or not tid:
        return
    try:
        store = _get_store()
        current = store.get_session(sid) or {}
        raw_meta = current.get("metadata") or {}
        if isinstance(raw_meta, str):
            raw_meta = json.loads(raw_meta) if raw_meta.strip() else {}
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        if str(meta.get("thread_id") or "") == tid:
            return
        meta["thread_id"] = tid
        store.update_session(sid, metadata=json.dumps(meta, ensure_ascii=False))
    except Exception:  # 绑定失败不影响消息处理
        logger.debug("session thread bind failed", exc_info=True)


async def _interrupt_session_work_items(session_id: str) -> None:
    """功能：会话删除联动——中止由该会话发起的活动执行（员工任务不残留继续跑）。"""
    try:
        dispatcher = _get_company_dispatcher()
        round_service = getattr(dispatcher, "group_rounds", None)
        if round_service is not None:
            active_round = round_service.get_active(session_id)
            if active_round is not None:
                round_service.cancel(active_round.round_id, reason="session_deleted")
        for item in dispatcher.store.list_all():
            metadata = getattr(item, "metadata", None) or {}
            if str(metadata.get("origin_session_id") or "") != session_id:
                continue
            cancel = getattr(dispatcher, "cancel_work_item", None)
            if callable(cancel):
                await cancel(str(item.id), reason="session_deleted")
            else:
                await dispatcher.interrupt_work_item(str(item.id))
    except Exception as exc:  # noqa: BLE001 —— 中断失败不阻断会话删除
        logger.warning("session work item interrupt failed: %s (%s)", session_id, exc)


async def _run_session_end_hooks(session_id: str) -> None:
    # 功能：SessionEnd hook 执行（配置加载 + 项目层信任门禁；provider 缺省时
    # prompt/agent 动作记 hook_errors 跳过）
    try:
        from bandi.core.config import BandiConfig
        from bandi.tools.hooks import HOOK_SESSION_END, HookRunner, load_hook_rules

        cfg = BandiConfig.load()
        runner = HookRunner(load_hook_rules(cfg.hooks), session_id=session_id)
        if runner.has_rules:
            await runner.run_event(
                HOOK_SESSION_END, {"session_id": session_id, "reason": "deleted"}
            )
    except Exception as exc:  # noqa: BLE001 — hook 失败不阻断会话删除
        logger.warning("SessionEnd hook failed: %s", exc)


@app.post("/api/sessions/{session_id}/fork")
async def fork_session(session_id: str, body: dict[str, str]) -> dict[str, Any]:
    """会话 Fork：复制恢复点前缀到独立新会话并记录溯源。"""
    from bandi.session.manager import SessionManager

    entry_id = (body.get("entry_id") or "").strip()
    if not entry_id:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="entry_id is required")
    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    manager = SessionManager(store)
    manager.session_id = session_id
    try:
        new_sid = manager.fork(entry_id)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail=str(exc)) from exc
    return {"forked_session_id": new_sid, "parent_session_id": session_id}


@app.post("/api/sessions/{session_id}/revert/stage")
async def revert_stage(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Stage：暂存恢复点（软标记，不删数据）。
    -3：entry_id 允许空串 = 清空全部（编辑首条消息的「从此处重开」）。"""
    from bandi.session.manager import SessionManager

    # -3 修复：entry_id 必须是字符串（null/数字不得归一为空串触发全截断）
    if not isinstance(body.get("entry_id"), str):
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="entry_id must be a string")
    entry_id = body["entry_id"].strip()
    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    manager = SessionManager(store)
    manager.session_id = session_id
    from bandi.core.agent_runner import session_slot_busy

    if session_slot_busy(session_id, store):
        raise HTTPException(status_code=HTTP_CONFLICT, detail=f"SESSION_BUSY: {session_id}")
    try:
        manager.stage(entry_id)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail=str(exc)) from exc
    return {"staged": True, "session_id": session_id, "entry_id": entry_id}


@app.post("/api/sessions/{session_id}/revert/commit")
async def revert_commit(session_id: str) -> dict[str, Any]:
    """Commit：截断到暂存恢复点并归档，返回可撤销操作 id。"""
    from bandi.session.manager import SessionManager

    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    manager = SessionManager(store)
    manager.session_id = session_id
    from bandi.core.agent_runner import session_slot_busy

    if session_slot_busy(session_id, store):
        raise HTTPException(status_code=HTTP_CONFLICT, detail=f"SESSION_BUSY: {session_id}")
    try:
        removed = manager.commit()
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_CONFLICT, detail=str(exc)) from exc
    return {
        "committed": True,
        "session_id": session_id,
        "removed_entries": removed,
        "operation_id": manager.last_operation_id,
    }


@app.post("/api/sessions/{session_id}/revert/undo")
async def revert_undo(session_id: str) -> dict[str, Any]:
    """功能：撤销上一次 Commit 的截断（归档恢复；无可撤销操作时 restored=0）"""
    from bandi.session.manager import SessionManager

    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    manager = SessionManager(store)
    manager.session_id = session_id
    from bandi.core.agent_runner import session_slot_busy

    if session_slot_busy(session_id, store):
        raise HTTPException(status_code=HTTP_CONFLICT, detail=f"SESSION_BUSY: {session_id}")
    restored = manager.undo()
    return {"restored_entries": restored, "session_id": session_id}


@app.post("/api/sessions/{session_id}/revert/clear")
async def revert_clear(session_id: str) -> dict[str, Any]:
    """Clear：撤销暂存恢复点，数据全量保留（幂等）。"""
    from bandi.session.manager import SessionManager

    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    manager = SessionManager(store)
    manager.session_id = session_id
    manager.clear()
    return {"cleared": True, "session_id": session_id}


@app.post("/api/sessions/{session_id}/revert-files")
async def revert_files(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """文件级回滚：按轮恢复工作区文件（dry_run 预览）。"""
    from bandi.agent.file_history import get_file_history_store

    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    turn = body.get("turn")
    # -3 修复：bool 是 int 子类——true 不得当 turn 触发真回滚；dry_run 必须真 bool
    if isinstance(turn, bool) or not isinstance(turn, int):
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="turn must be an integer")
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="dry_run must be a boolean")
    report = get_file_history_store().rewind(session_id, turn, dry_run=dry_run)
    return {
        "session_id": session_id,
        "turn_index": turn,
        "dry_run": report.dry_run,
        "actions": [{"path": a.path, "action": a.action} for a in report.actions],
        "changed": report.changed,
    }


@app.post("/api/sessions/{session_id}/share")
async def share_session(session_id: str) -> dict[str, str]:
    """会话共享：生成只读访问 token。"""
    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    token = uuid.uuid4().hex[:16]
    _purge_share_tokens()
    with _share_lock:
        _share_tokens[token] = session_id
        _share_token_times[token] = time.time()
    return {"share_token": token}


@app.get("/api/shared/{token}")
async def view_shared_session(token: str, at: str | None = None) -> dict[str, Any]:
    """会话共享：只读查看共享会话的消息列表（支持 ?at= 预览截断）。"""
    _purge_share_tokens()
    with _share_lock:
        session_id = _share_tokens.get(token)
    if session_id is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="INVALID_SHARE_TOKEN")
    store = _get_store()
    try:
        entries = store.resolved_branch(session_id, at=at)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail=str(exc)) from exc
    return {"session_id": session_id, "messages": entries}


@app.post("/api/sessions/{session_id}/inject")
async def inject_session_input(session_id: str, body: dict[str, str]) -> dict[str, Any]:
    """实时转向：向活跃会话注入新指令，触发新一轮 THINKING。"""
    from bandi.core.agent_runner import inject_user_input

    text = (body.get("message") or body.get("input") or "").strip()
    if not text:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="message is required")
    ok = inject_user_input(session_id, text)
    if not ok:
        raise HTTPException(
            status_code=HTTP_CONFLICT,
            detail="no active run for session (run chat first)",
        )
    return {"injected": True, "session_id": session_id}


@app.post("/api/sessions/{session_id}/steer")
async def steer_session(session_id: str, body: dict[str, str]) -> dict[str, Any]:
    """
    被 drain 注入（区别于 inject 的单槽即时触发）。"""
    from bandi.agent.steer import get_default_registry

    text = (body.get("message") or body.get("content") or "").strip()
    if not text:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="message is required")
    get_default_registry().push(session_id, text)
    return {"queued": True, "session_id": session_id}


@app.post("/api/sessions/{session_id}/retry-tool")
async def session_retry_tool(session_id: str, body: dict[str, str]) -> dict[str, Any]:
    """-3b 节点级工具重试：按 tool_call_id 定位历史调用并 force 真跑。

    权限链与正常一致——headless 下 ask 类工具 fail-closed（APPROVAL_REQUIRED）
    不开提权通道；结果不接回 Agent 循环（前端内存节点消费）。"""
    from bandi.tools.tool_retry import retry_tool_call

    tool_call_id = (body.get("tool_call_id") or "").strip()
    if not tool_call_id:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="tool_call_id is required")
    store = _get_store()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    try:
        meta = json.loads(session.get("metadata") or "{}")
    except (TypeError, ValueError):
        meta = {}
    result = await retry_tool_call(
        session_id,
        tool_call_id,
        store=store,
        project_root=meta.get("project_root"),
    )
    if result is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="TOOL_CALL_NOT_FOUND")
    return result


@app.get("/api/sessions/{session_id}/artifacts")
async def session_artifacts(session_id: str) -> dict[str, Any]:
    """-3d：会话产物清单（右栏制品预览 Tab 数据源——b 方案后端登记）。"""
    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    return {"session_id": session_id, "artifacts": store.list_artifacts(session_id)}


@app.get("/api/sessions/{session_id}/context-usage")
async def session_context_usage(session_id: str) -> dict[str, Any]:
    """功能：当前轮的上下文用量五类分桶（前端「上下文用量」环与面板数据源）。

    数据由 agent 在每次组装请求时落库（真正发出去的那份请求），本端点只读；
    无快照时 `available=false`，前端显示「暂无」态而不是伪造 0。
    """
    from bandi.agent.context_usage import BUCKETS

    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    snapshot = store.get_context_usage(session_id) or {}
    used = int(snapshot.get("used_tokens") or 0)
    window = int(snapshot.get("context_window") or 0)
    raw_buckets = snapshot.get("buckets") or {}
    buckets = [
        {
            "key": key,
            "tokens": int(raw_buckets.get(key) or 0),
            "ratio": round(int(raw_buckets.get(key) or 0) / window, 4) if window else 0.0,
        }
        for key in BUCKETS
    ]
    return {
        "session_id": session_id,
        "available": bool(snapshot),
        "model": str(snapshot.get("model") or ""),
        "context_window": window,
        "used_tokens": used,
        "ratio": round(used / window, 4) if window else 0.0,
        "buckets": buckets,
        "updated_at": iso_local(snapshot.get("updated_at") or ""),
    }


@app.get("/api/sessions/{session_id}/artifacts/{artifact_id}/content")
async def session_artifact_content(
    session_id: str, artifact_id: str, max_bytes: int = 65536
) -> dict[str, Any]:
    """-制品内容预览（只读）。

    安全面三条硬约束（fail-closed，全部先判后读）：
    ① 解析后路径必须落在会话 project_root（无则 workspace_root）内——防穿越；
    ② 敏感路径/扩展名剔除（复用 `fs_tool.sensitive_path_reason` 单一判断）；
    ③ 超大/二进制**不返回内容**，只回元数据（`kind=too_large|binary`）——不猜编码
       不把二进制灌进前端。
    """
    from bandi.tools.fs_tool import _looks_binary, sensitive_path_reason, workspace_root

    store = _get_store()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    artifact = next(
        (item for item in store.list_artifacts(session_id) if item["id"] == artifact_id), None
    )
    if artifact is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="ARTIFACT_NOT_FOUND")
    try:
        meta = json.loads(session.get("metadata") or "{}")
    except (TypeError, ValueError):
        meta = {}
    root_raw = str(meta.get("project_root") or "").strip()
    root = Path(root_raw).expanduser().resolve() if root_raw else workspace_root().resolve()
    raw_path = str(artifact.get("path") or "").strip()
    if not raw_path:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="ARTIFACT_PATH_EMPTY")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        target = candidate.resolve()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_PATH") from exc
    # ① 目录边界（fail-closed）
    if not target.is_relative_to(root):
        raise HTTPException(status_code=403, detail="PATH_OUTSIDE_ROOT")
    # ② 敏感路径/类型（单一判断入口，与文件工具同源）
    reason = sensitive_path_reason(target)
    if reason:
        raise HTTPException(status_code=403, detail="SENSITIVE_PATH")
    if not target.is_file():
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="FILE_NOT_FOUND")
    limit = max(1024, min(int(max_bytes or 0) or 65536, 1_048_576))
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="FILE_NOT_FOUND") from exc
    base = {
        "artifact_id": artifact_id,
        "path": str(target),
        "size": size,
        "truncated": False,
    }
    # ③ 超大/二进制 fail-closed（不返回内容）
    if size > limit:
        return {**base, "kind": "too_large", "content": "", "reason": f"size>{limit}"}
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="FILE_UNREADABLE") from exc
    if _looks_binary(raw, target):
        return {**base, "kind": "binary", "content": "", "reason": "binary content"}
    return {
        **base,
        "kind": "text",
        "content": raw.decode("utf-8", errors="replace"),
        "truncated": len(raw) >= limit,
    }


# ── -3 右栏 v5 数据面：文件浏览 / 技能列表 / SOP 进度 ──────────────────────

# 文件树忽略名单（目录名精确匹配，大小写不敏感——默认隐藏，前端不可见）
_IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        ".next",
        ".cache",
        ".idea",
        ".vscode",
    }
)
# 文件预览单次上限（与制品预览同口径）
_SESSION_FILE_MAX_BYTES = 1_048_576


def _session_file_root(session_id: str) -> Path:
    """功能：会话文件根（project_root 优先，无则 workspace_root）——文件树/预览共享。"""
    from bandi.tools.fs_tool import workspace_root

    session = _get_store().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    try:
        meta = json.loads(session.get("metadata") or "{}")
    except (TypeError, ValueError):
        meta = {}
    root_raw = str(meta.get("project_root") or "").strip()
    if root_raw:
        return Path(root_raw).expanduser().resolve()
    # 功能：项目会话按项目隔离工作区（无 project_root 时不再共用全局 workspace，
    # 否则右栏文件树会混入其他项目的文件）；首次访问自动建目录
    project_id = str(meta.get("project_id") or "").strip()
    if project_id:
        root = (workspace_root() / project_id).resolve()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("project workspace mkdir failed: %s", root)
        return root
    return workspace_root().resolve()


def _resolve_within_root(root: Path, raw_path: str) -> Path:
    """功能：路径解析 + 安全校验（越界/敏感 fail-closed）——与制品预览同口径（-；
    起会话文件面与项目文件面共用）。"""
    from bandi.tools.fs_tool import sensitive_path_reason

    candidate = Path(raw_path).expanduser() if raw_path else root
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        target = candidate.resolve()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_PATH") from exc
    if not target.is_relative_to(root):
        raise HTTPException(status_code=403, detail="PATH_OUTSIDE_ROOT")
    reason = sensitive_path_reason(target)
    if reason:
        raise HTTPException(status_code=403, detail="SENSITIVE_PATH")
    return target


def _resolve_preview_path(root: Path, raw_path: str) -> tuple[Path, Path]:
    """功能：预览路径解析——项目根优先；绝对路径在 BandiProjects 根下时允许只读预览。"""
    try:
        return root, _resolve_within_root(root, raw_path)
    except HTTPException as exc:
        if exc.status_code != 403 or str(exc.detail) != "PATH_OUTSIDE_ROOT":
            raise
        projects_root = default_projects_root().expanduser().resolve()
        try:
            candidate = Path(raw_path).expanduser().resolve()
        except (OSError, ValueError):
            raise exc
        if not candidate.is_relative_to(projects_root):
            raise exc
        return projects_root, _resolve_within_root(projects_root, raw_path)


def _list_dir_entries(target: Path) -> list[dict[str, Any]]:
    """功能：列目录一级条目（目录在前 + 忽略名单过滤）——会话/项目文件树共享（抽取）。"""
    try:
        children = sorted(target.iterdir(), key=lambda item: item.name.lower())
    except OSError as exc:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="DIRECTORY_NOT_FOUND") from exc
    entries: list[dict[str, Any]] = []
    for item in children:
        if item.name in _IGNORED_DIR_NAMES:
            continue
        try:
            is_dir = item.is_dir()
            stat = item.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": item.name,
                "type": "dir" if is_dir else "file",
                "size": 0 if is_dir else int(stat.st_size),
                "mtime": stat.st_mtime,
            }
        )
    entries.sort(key=lambda entry: (entry["type"] != "dir", entry["name"].lower()))
    return entries


def _clamp_file_preview_limit(max_bytes: int) -> int:
    """功能：文件预览字节上限钳制（默认 64KB，硬上限 1MB）——会话/项目预览共享。"""
    return max(1024, min(int(max_bytes or 0) or 65536, _SESSION_FILE_MAX_BYTES))


def _file_content_payload(root: Path, target: Path, limit: int) -> dict[str, Any]:
    """功能：文件内容预览载荷（文本/二进制/超大三分支，只回元数据于后两者）——共享（抽取）。"""
    from bandi.tools.fs_tool import _looks_binary

    if not target.is_file():
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="FILE_NOT_FOUND")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="FILE_NOT_FOUND") from exc
    rel = str(target.relative_to(root)).replace("\\", "/")
    base = {"path": rel, "size": size}
    if size > limit:
        return {**base, "kind": "too_large", "content": "", "reason": f"size>{limit}"}
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="FILE_UNREADABLE") from exc
    if _looks_binary(raw, target):
        return {**base, "kind": "binary", "content": "", "reason": "binary content"}
    return {
        **base,
        "kind": "text",
        "content": raw.decode("utf-8", errors="replace"),
        "truncated": len(raw) >= limit,
    }


@app.get("/api/sessions/{session_id}/files")
async def session_files(session_id: str, path: str = "") -> dict[str, Any]:
    """-3 右栏 v5：项目文件树（列一级目录；前端按 path 懒加载逐级展开）。

    忽略名单（.git/node_modules 等）不列出；越界/敏感 fail-closed（与制品预览同口径）。
    """
    root = _session_file_root(session_id)
    target = _resolve_within_root(root, path)
    if not target.is_dir():
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="DIRECTORY_NOT_FOUND")
    entries = _list_dir_entries(target)
    rel = "" if target == root else str(target.relative_to(root)).replace("\\", "/")
    return {"session_id": session_id, "root": str(root), "path": rel, "entries": entries}


@app.get("/api/sessions/{session_id}/files/content")
async def session_file_content(
    session_id: str, path: str, max_bytes: int = 65536
) -> dict[str, Any]:
    """-3 右栏 v5：文件内容预览（只读）——文本返回内容；二进制/超大只回元数据。"""
    root = _session_file_root(session_id)
    content_root, target = _resolve_preview_path(root, path)
    payload = _file_content_payload(content_root, target, _clamp_file_preview_limit(max_bytes))
    return {**payload, "session_id": session_id}


@app.get("/api/skills")
async def list_installed_skills(team_id: str = "") -> dict[str, Any]:
    """-3 右栏 v5：已安装技能列表（右栏「员工/助理」技能区数据源）。"""
    from bandi.skills.manager import create_skill_registry

    registry = create_skill_registry()
    skills = registry.collect_for_team(team_id) if team_id else registry.list_all()
    return {
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "tags": list(skill.tags),
                "team_id": skill.team_id,
                "always": skill.always,
            }
            for skill in skills
        ]
    }


def _resource_kind(kind: str) -> str:
    """功能：资源类型校验（技能 / SOP）。"""
    value = str(kind or "").strip().lower()
    if value not in {"skill", "sop"}:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_RESOURCE_KIND")
    return value


def _resource_names() -> dict[str, str]:
    """功能：员工 id → 姓名（归属展示用；组织不可读时返回空映射）。"""
    try:
        dispatcher = _org_api_dispatcher()
        return {
            str(employee.employee_id): str(employee.name)
            for employee in dispatcher.org_engine.list_employees(include_inactive=True)
        }
    except Exception:  # noqa: BLE001 - 资源列表不因组织读取失败而不可用
        return {}


def _resource_http_error(exc: Exception) -> HTTPException:
    from bandi.resources.catalog import ResourceConflictError, ResourceNotFoundError

    if isinstance(exc, ResourceNotFoundError):
        return HTTPException(status_code=HTTP_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ResourceConflictError):
        return HTTPException(status_code=HTTP_CONFLICT, detail=str(exc))
    return HTTPException(status_code=HTTP_BAD_REQUEST, detail=str(exc))


@app.get("/api/resources")
async def resources_list(kind: str) -> dict[str, Any]:
    """资源中心列表：技能 / SOP。"""
    from bandi.resources.catalog import list_skills, list_sops

    normalized = _resource_kind(kind)
    names = _resource_names()
    if normalized == "skill":
        items = list_skills(names)
    else:
        items = list_sops(names)
    return {"items": items}


@app.get("/api/resources/{kind}/{resource_id}")
async def resources_detail(kind: str, resource_id: str) -> dict[str, Any]:
    """资源详情（元数据 + 原始内容）。"""
    from bandi.resources.catalog import ResourceError, get_resource

    normalized = _resource_kind(kind)
    try:
        return get_resource(normalized, resource_id, _get_knowledge_store(), _resource_names())
    except ResourceError as exc:
        raise _resource_http_error(exc) from exc


@app.post("/api/resources/sop")
async def resources_create_sop(body: dict[str, Any]) -> dict[str, Any]:
    """新建 SOP（空白卡或完整卡 JSON 落盘）。"""
    from bandi.resources.catalog import ResourceError, import_sop

    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="CONTENT_REQUIRED")
    try:
        item = import_sop(content)
    except ResourceError as exc:
        raise _resource_http_error(exc) from exc
    _reset_skill_tool_cache()
    return {"ok": True, "item": item}


@app.put("/api/resources/{kind}/{resource_id}")
async def resources_save(kind: str, resource_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """保存资源内容（技能写回 SKILL.md / SOP 校验写回 / 文档重摄取）。"""
    from bandi.resources.catalog import ResourceError, save_resource

    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="CONTENT_REQUIRED")
    normalized = _resource_kind(kind)
    try:
        item = save_resource(
            normalized, resource_id, content, _get_knowledge_store(), _resource_names()
        )
    except ResourceError as exc:
        raise _resource_http_error(exc) from exc
    _reset_skill_tool_cache()
    return {"ok": True, "item": item}


@app.patch("/api/resources/{kind}/{resource_id}")
async def resources_toggle(kind: str, resource_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """启用/停用资源（知识文档不支持）。"""
    from bandi.resources.catalog import ResourceError, set_enabled

    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="ENABLED_REQUIRED")
    normalized = _resource_kind(kind)
    try:
        item = set_enabled(
            normalized, resource_id, enabled, _get_knowledge_store(), _resource_names()
        )
    except ResourceError as exc:
        raise _resource_http_error(exc) from exc
    _reset_skill_tool_cache()
    return {"ok": True, "item": item}


@app.delete("/api/resources/{kind}/{resource_id}")
async def resources_uninstall(kind: str, resource_id: str) -> dict[str, Any]:
    """卸载资源（配置移入归档目录，本地文件保留）。"""
    from bandi.resources.catalog import ResourceError, uninstall

    normalized = _resource_kind(kind)
    try:
        result = uninstall(normalized, resource_id, _get_knowledge_store())
    except ResourceError as exc:
        raise _resource_http_error(exc) from exc
    _reset_skill_tool_cache()
    return {"ok": True, **result}


@app.post("/api/resources/{kind}/{resource_id}/open-folder")
async def resources_open_folder(kind: str, resource_id: str) -> dict[str, Any]:
    """打开资源所在目录。"""
    from bandi.resources.catalog import ResourceError, folder_path

    normalized = _resource_kind(kind)
    try:
        target = folder_path(normalized, resource_id, _get_knowledge_store())
    except ResourceError as exc:
        raise _resource_http_error(exc) from exc
    if not Path(target).is_dir():
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="FOLDER_NOT_FOUND")
    _open_local_folder(target)
    return {"ok": True, "opened": target}


@app.post("/api/resources/upload")
async def resources_upload(kind: str, file: UploadFile) -> dict[str, Any]:
    """上传本地资源到全局（技能 / SOP）。"""
    from bandi.resources.catalog import (
        ResourceError,
        import_skill,
        import_sop,
    )

    normalized = _resource_kind(kind)
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="RESOURCE_FILE_TOO_LARGE")
    if b"\x00" in raw:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="BINARY_RESOURCE_UNSUPPORTED")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="RESOURCE_ENCODING") from exc
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="FILENAME_REQUIRED")
    try:
        if normalized == "skill":
            item = import_skill(text, filename)
        else:
            item = import_sop(text)
    except ResourceError as exc:
        raise _resource_http_error(exc) from exc
    _reset_skill_tool_cache()
    return {"ok": True, "item": item}


@app.get("/api/sessions/{session_id}/sop")
async def session_sop_progress(session_id: str) -> dict[str, Any]:
    """-3 右栏 v5：SOP 进度（帧栈只读投影——与组织层 sop_progress 同源口径）。"""
    from bandi.skills.frame_store import load_frames
    from bandi.skills.progress import build_sop_progress, frame_scope_for_session

    if _get_store().get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    stack = None
    try:
        stack = load_frames(frame_scope_for_session(session_id))
    except Exception:  # noqa: BLE001 — 坏帧/读错不阻断只读投影（null 由前端空态兜底）
        logger.warning("sop frame load failed: session=%s", session_id)
    return {"session_id": session_id, "sop": build_sop_progress(stack)}


@app.post("/api/sessions/{session_id}/process-skip")
async def session_process_skip(session_id: str, body: dict[str, str]) -> dict[str, Any]:
    """-3c：跳过态随存——按 tool_call_id 定位 process 条目内工具节点翻转 skipped。"""
    tool_call_id = str((body or {}).get("tool_call_id") or "").strip()
    if not tool_call_id:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="tool_call_id is required")
    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    ok = store.skip_process_tool_node(session_id, tool_call_id)
    if not ok:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="PROCESS_NODE_NOT_FOUND")
    return {"ok": True, "tool_call_id": tool_call_id}


@app.post("/api/sessions/{session_id}/approval-decision")
async def session_approval_decision(session_id: str, body: dict[str, str]) -> dict[str, Any]:
    """
    回填消息体（客户端据此发起新一轮 re-issue）；deny 仅清 pending。"""
    from bandi.tools.approval import apply_decision, get_approval_store

    decision = (body.get("decision") or "").strip()
    feedback = str(body.get("feedback") or "").strip()
    tool_call_id = str(body.get("tool_call_id") or "").strip() or None
    store = get_approval_store()
    pending = store.get_pending(session_id, tool_call_id)
    if pending is None:
        raise HTTPException(status_code=409, detail="no pending approval for this session")
    if decision not in ("allow_once", "allow_session", "deny"):
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST, detail="decision must be allow_once/allow_session/deny"
        )
    reply = await apply_decision(  # type: ignore[arg-type]
        store, session_id, decision, pending.tool_name, tool_call_id=pending.tool_call_id
    )
    if decision == "deny" and feedback:
        from bandi.tools.approval import approval_reply_content

        reply = approval_reply_content("deny", pending.tool_name, feedback)
    cascaded: list[str] = []
    if decision == "allow_session":
        cascaded = [
            p.tool_call_id
            for p in store.cascade_pending_grants(session_id, pending.tool_name, scope="session")
        ]
    # `_approval_session_id` 找回，re-queue 并注入 resume 指令；失败不阻断决策返回
    # （人工仍可走 /api/company/work-items/{id}/retry 重派）。
    resumed = 0
    try:
        dispatcher = _get_company_dispatcher()
        resumed = dispatcher.resume_after_approval(
            session_id,
            decision=decision,
            tool_name=pending.tool_name,
            note=reply,
            tool_call_id=pending.tool_call_id,
        )
    except Exception:
        logger.debug("approval resume after decision failed", exc_info=True)
    return {
        "session_id": session_id,
        "decision": decision,
        "tool_name": pending.tool_name,
        "tool_call_id": pending.tool_call_id,
        "reply_content": reply,
        "cascaded": cascaded,
        "resumed_work_items": resumed,
    }


@app.get("/api/company/approvals")
async def company_pending_approvals() -> dict[str, Any]:
    """员工卡待审批列表（审批决策入口数据源）。

    员工执行 headless 落 ApprovalStore pending 后前端无从感知——本接口列出
    全部会话的 pending（session_id 即决策端点所需 sid），配合
    `POST /api/sessions/{sid}/approval-decision` 完成「列表 → 决策 → 卡恢复」闭环。
    """
    from bandi.tools.approval import get_approval_store

    store = get_approval_store()
    from bandi.tools.approval import approval_params_preview, build_permission_prompt

    items = [
        {
            "session_id": sid,
            "tool_name": item.tool_name,
            "reason": item.reason,
            "tool_call_id": item.tool_call_id,
            "params_digest": item.params_digest,
            "params": approval_params_preview(item.params),
            "detail": item.detail,
            "rule_pattern": item.rule_pattern,
            "rule_source": item.rule_source,
            "prompt": build_permission_prompt(item.tool_name),
        }
        for sid, item in store.list_all_pending()
    ]
    return {"approvals": items, "count": len(items)}


def _require_interaction(interaction_id: str, session_id: str) -> Any:
    """取 pending 交互并校验归属会话（跨会话决策 404）。"""
    from bandi.core.user_interaction import _INTERACTION_STATUS_PENDING

    store = _get_interaction_store()
    item = store.get(interaction_id)
    if item is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="INTERACTION_NOT_FOUND")
    if item.session_id != session_id:
        raise HTTPException(
            status_code=HTTP_NOT_FOUND,
            detail="INTERACTION_NOT_FOUND",
        )
    # 已结束（超时/拒绝/已决）交互不得再收决策：晚到 POST 若被当「批准」成功返回，
    # 与卡片上「已超时」状态自相矛盾（store.resolve 对非 pending 返回 False，这里显式化）
    if item.status != _INTERACTION_STATUS_PENDING:
        raise HTTPException(status_code=409, detail="INTERACTION_ALREADY_SETTLED")
    return store, item


@app.get("/api/sessions/{session_id}/interactions")
async def session_pending_interactions(session_id: str) -> dict[str, Any]:
    """挂起交互重放——页面加载/刷新后拉取 pending 决策卡。

    交互帧只在挂起瞬间经 WS/SSE 推送一次；客户端此后刷新/重连将永远看不到卡
    。本端点让前端在
    会话加载时补拉 InteractionStore 里的 pending 帧（含剩余倒计时）。
    """
    istore = _get_interaction_store()
    frames: list[dict[str, Any]] = []
    for item in istore.list_pending(session_id=session_id, include_orphans=False):
        frame = _interaction_frame(item)
        # 跨重启孤儿标记：水合条目无进程内挂起方，前端可据此提示「已失效」
        frame["orphaned"] = istore.is_orphaned(item)
        frames.append(frame)
    return {"interactions": frames}


@app.post("/api/sessions/{session_id}/confirm")
async def session_confirm(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """用户确认/拒绝一次挂起的授权/确认交互（真挂起恢复）。

    body: {"interaction_id": "...", "decision": true|false, "feedback": "可选拒绝理由"}
    decision=true → 唤醒挂起方为允许；false → 拒绝（带 feedback 时经 dict 决策值
    回灌理由，CorrectedError 等价物）。未知/跨会话 → 404。
    """
    interaction_id = str(body.get("interaction_id") or "").strip()
    if not interaction_id:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="interaction_id is required")
    store, item = _require_interaction(interaction_id, session_id)
    # -3 修复：decision 必填且必须 bool（原缺省 True=静默批准，fail-open）
    if not isinstance(body.get("decision"), bool):
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="decision must be a boolean")
    decision = body["decision"]
    feedback = str(body.get("feedback") or "").strip()
    orphaned = store.is_orphaned(item)
    # approval-decision 端点，而该端点只认 ApprovalStore 的 pending——生产 API 模式
    # 走 ask_permission 挂起 + 本端点，ApprovalStore 无 pending ⇒ 恒 409）。
    # 现把三态映射到生产真实机制：allow_once=本轮放行；allow_session=放行并向
    # ApprovalStore 落**会话级**授权（executor._consume_approval_grant 在主执行
    # 路径消费它 ⇒ 本会话内同工具同参不再重复询问）。
    scope = str(body.get("scope") or "once").strip() or "once"
    if scope not in ("once", "session"):
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="scope must be once or session")
    session_granted: list[str] = []
    payload = item.payload if isinstance(item.payload, dict) else {}
    # 员工权限卡展示在 origin 聊天，但授权必须写到实际执行会话（role-session::）。
    grant_session = str(payload.get("executor_session_id") or session_id).strip() or session_id
    batch_items = payload.get("items") if payload.get("batch") is True else None
    if decision and isinstance(batch_items, list) and batch_items and item.kind == "permission":
        # 批量审批：一次决策对卡内全部调用生效（once=逐参摘要；session=工具级通配）
        from bandi.tools.approval import get_approval_store, params_digest

        store_approval = get_approval_store()
        for raw in batch_items:
            if not isinstance(raw, dict):
                continue
            batch_tool = str(raw.get("tool_name") or "").strip()
            if not batch_tool:
                continue
            batch_args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
            store_approval.grant(
                grant_session,
                batch_tool,
                params_digest="" if scope == "session" else params_digest(batch_args),
                scope=scope,
            )
            if scope == "session":
                store_approval.cascade_pending_grants(grant_session, batch_tool, scope="session")
            if batch_tool not in session_granted:
                session_granted.append(batch_tool)
        if not store.resolve(interaction_id, {"granted": True, "scope": scope, "batch": True}):
            raise HTTPException(status_code=409, detail="INTERACTION_ALREADY_SETTLED")
        outcome = "approved"
    elif decision:
        if scope == "session" and item.kind == "permission" and not orphaned:
            from bandi.tools.approval import get_approval_store

            tool_name = str(payload.get("tool_name") or "").strip()
            if tool_name:
                store_approval = get_approval_store()
                store_approval.grant(
                    grant_session,
                    tool_name,
                    # 工具级放行（digest 留空=任意参数通配）：本会话批准一次后该工具
                    # 不再逐参弹卡——调研任务每条命令都不同，逐参授权必被审批疲劳拖死
                    params_digest="",
                    scope="session",
                )
                store_approval.cascade_pending_grants(grant_session, tool_name, scope="session")
                session_granted.append(tool_name)
        if scope == "once" and item.kind == "permission" and not orphaned:
            # allow_once=仅本次这一条具体调用——写真实参数摘要（改参数
            # 即视为另一次调用、重新审批）；session 仍保持工具级通配。
            from bandi.tools.approval import get_approval_store, params_digest

            tool_once = str(payload.get("tool_name") or "").strip()
            args_once = payload.get("args")
            if tool_once:
                get_approval_store().grant(
                    session_id,
                    tool_once,
                    params_digest=params_digest(args_once if isinstance(args_once, dict) else {}),
                    scope="once",
                )
        if not store.resolve(interaction_id, True):
            # 竞态兜底：resolve 返回 False=卡已被并发决策/超时收口——不得谎报 approved
            raise HTTPException(status_code=409, detail="INTERACTION_ALREADY_SETTLED")
        outcome = "approved"
    elif feedback and item.kind == "permission":
        # feedback dict 仅限 permission 交互——confirm/input 类消费方按 bool() 解析
        # 会把 dict 误当批准（bug 检查批次修复）
        store.resolve(interaction_id, {"granted": False, "feedback": feedback})
        outcome = "denied"
    else:
        store.reject(interaction_id)
        outcome = "denied"
    return {
        "session_id": session_id,
        "interaction_id": interaction_id,
        "kind": item.kind,
        "outcome": outcome,
        "scope": scope,
        "session_granted": session_granted,
        "orphaned": orphaned,
    }


@app.post("/api/sessions/{session_id}/clarify")
async def session_clarify(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """用户答复一次挂起的提问（request_user_input / ask_input 真挂起恢复）。

    body: {"interaction_id": "...", "answer": "文本答复"}
    """
    interaction_id = str(body.get("interaction_id") or "").strip()
    answer = str(body.get("answer") or "").strip()
    if not interaction_id:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="interaction_id is required")
    store, item = _require_interaction(interaction_id, session_id)
    if item.kind != "input":
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST,
            detail=f"interaction kind {item.kind!r} is not clarifiable",
        )
    orphaned = store.is_orphaned(item)
    store.resolve(interaction_id, answer)
    return {
        "session_id": session_id,
        "interaction_id": interaction_id,
        "kind": item.kind,
        "answered": True,
        "answer": answer,
        "orphaned": orphaned,
    }


@app.post("/api/sessions/{session_id}/regenerate")
async def session_regenerate(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """功能：重新生成——保留触发轮的用户消息与附件，只重跑该轮助手回复。"""
    entry_id = str(body.get("entry_id") or "").strip()
    if not entry_id:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="entry_id is required")
    store = _get_store()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"SESSION_NOT_FOUND: {session_id}")
    entries = store.get_full_branch(session_id)
    index = next((i for i, entry in enumerate(entries) if str(entry.get("id")) == entry_id), -1)
    if index < 0:
        raise HTTPException(status_code=404, detail=f"ENTRY_NOT_FOUND: {entry_id}")
    if entries[index].get("role") != "assistant":
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST, detail="entry_id must be an assistant message"
        )
    user_index = next((i for i in range(index - 1, -1, -1) if entries[i].get("role") == "user"), -1)
    if user_index < 0:
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST, detail="no user message before this assistant reply"
        )
    user_entry = entries[user_index]
    metadata = user_entry.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    attachments = metadata.get("attachments") if isinstance(metadata, dict) else None

    from bandi.agent.checkpoint import CheckpointStore
    from bandi.core.agent_runner import run_agent_turn, session_slot_busy

    if session_slot_busy(session_id, store):
        raise HTTPException(status_code=HTTP_CONFLICT, detail=f"SESSION_BUSY: {session_id}")

    removed, operation_id = store.truncate_branch_archived(session_id, str(user_entry["id"]))

    checkpoints = CheckpointStore()
    try:
        checkpoints.delete_by_session(session_id)
    finally:
        checkpoints.close()

    session_meta = session.get("metadata") or {}
    if isinstance(session_meta, str):
        try:
            session_meta = json.loads(session_meta)
        except (TypeError, ValueError):
            session_meta = {}
    project_id = (
        str(session.get("project_id") or "").strip()
        or str(
            (session_meta.get("project_id") if isinstance(session_meta, dict) else "") or ""
        ).strip()
    )

    try:
        result = await run_agent_turn(
            user_input=str(user_entry.get("content") or ""),
            mode=str(session.get("mode") or ""),
            session_id=session_id,
            session_store=store,
            append_user_message=False,
            attachments=attachments or None,
            project_id=project_id or None,
            project_root=store.session_project_root(session_id),
        )
    except BaseException:
        if removed and operation_id:
            store.restore_archived(session_id, operation_id)
        raise
    if result.terminal_reason == "SESSION_BUSY":
        if removed and operation_id:
            store.restore_archived(session_id, operation_id)
        raise HTTPException(status_code=HTTP_CONFLICT, detail=f"SESSION_BUSY: {session_id}")
    return {
        "session_id": session_id,
        "entry_id": str(user_entry["id"]),
        "reply": result.reply,
        "terminal_reason": result.terminal_reason,
    }


@app.post("/api/sessions/{session_id}/compact")
async def compact_session_endpoint(session_id: str) -> dict[str, Any]:
    """强制压缩会话（单块全量摘要 → fork 新会话；原会话保留）。"""
    from bandi.agent.factory import build_provider
    from bandi.core.session_compact import compact_session

    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND")
    provider = build_provider(BandiConfig.load())
    result = await compact_session(store, session_id, provider)
    return {
        "compacted": result.success,
        "new_session_id": result.new_session_id,
        "stage": result.stage,
        "before": result.before_messages,
        "after": result.after_messages,
        "before_tokens": result.before_tokens,
        "after_tokens": result.after_tokens,
        "has_summary": result.has_summary,
        "reason": result.reason,
    }


@app.get("/api/cost/sessions/{session_id}")
async def cost_session_history(session_id: str, limit: int = 100) -> dict[str, Any]:
    """DoR-4：LLM 台账落库查询——重启后仍可查历史（实时内存值见 cost-detail）。"""
    from bandi.core.config import default_cost_db_path
    from bandi.observability.cost_store import get_shared_cost_store

    store = get_shared_cost_store(default_cost_db_path())
    return {
        "session_id": session_id,
        "session_cost": store.session_cost(session_id),
        "records": store.list_records(limit=max(1, min(int(limit), 1000)), session_id=session_id),
    }


@app.get("/api/usage/stats")
async def usage_stats(
    range_: str = Query(default="all", alias="range", description="7d | 30d | all"),
) -> dict[str, Any]:
    """**用量与缓存聚合**——设置页「用量与缓存」的数据源。

    形态 `GET /api/activity-stats[/{range}]`：一次返回总量 + 按模型 +
    按项目三段。命中率的分母是 **provider 归一后的总输入**（`TokenUsage.
    input_tokens_total`）——Anthropic 的 input_tokens 不含已缓存部分，直接用它会
    算出 >100%（故服务端已统一口径并钳制 [0,1]）。

    **按项目**维度——项目归属两处派生：
    ① 员工执行会话 id 前缀（`role-session::<项目>::…`）；② 会话元数据 project_id
    （项目内会话的对话调用）。故以「按会话」分桶为中间量折叠合并。

    台账不存在时返回零值空态（首次打开设置页不得 500）。
    """
    from bandi.company.session_scoping import project_from_session_id
    from bandi.core.config import default_cost_db_path
    from bandi.observability.cost_store import fold_buckets_by, get_shared_cost_store

    days = {"7d": 7, "30d": 30}.get(str(range_).strip().lower(), 0)
    try:
        store = get_shared_cost_store(default_cost_db_path())
        agg = store.aggregate_usage(days=days)
    except Exception as exc:  # noqa: BLE001 — 观测面失败不得 500 前端
        logger.warning("usage stats unavailable: %s", exc)
        agg = {"total": {}, "by_model": {}, "by_role": {}, "by_session": {}}
    session_projects = _session_project_map()
    by_project = fold_buckets_by(
        agg.get("by_session") or {},
        lambda sid: project_from_session_id(sid) or session_projects.get(sid, ""),
    )
    return {
        "range": str(range_),
        "days": days,
        "generated_at": time.time(),
        "total": agg.get("total", {}),
        "by_model": agg.get("by_model", {}),
        "by_project": by_project,
    }


def _session_project_map() -> dict[str, str]:
    """功能：会话 id → 项目归属（一次性读会话元数据，避免逐会话查询）。

    存储不可用时返回空表（降级为「只有员工执行会话可归项目」），绝不因观测面
    失败影响用量接口本身。
    """
    try:
        return {str(s.get("id")): _session_project_id(s) for s in _get_store().list_sessions()}
    except Exception:  # noqa: BLE001 — 会话库不可用不阻断用量聚合
        return {}


@app.get("/api/sessions/{session_id}/cost-detail")
async def session_cost_detail(session_id: str) -> dict[str, Any]:
    """per-tool token 归因详情 + 会话用量（**落库口径优先**）。

     修复：原实现只读进程内存账本 ⇒ 内核重启后同一会话的用量显示 0
    （台账里其实有 22 万 tokens）。面板要回答的是「这个会话用了多少」
    必须跨重启可读 ⇒ 以台账为准，内存仅补 per-tool 明细（该归因未落库）。
    """
    from bandi.llm.cost_tracker import _shared_cost_tracker

    tool_stats: dict[str, Any] = {}
    if _shared_cost_tracker is not None:
        for name, agg in _shared_cost_tracker.tool_token_stats().items():
            tool_stats[name] = {
                "calls": int(agg["calls"]),
                "failures": int(agg["failures"]),
                "total_ms": float(agg["total_ms"]),
                "prompt_tokens": int(agg["prompt_tokens"]),
                "completion_tokens": int(agg["completion_tokens"]),
            }

    # 团队会话（群聊）：员工执行的 token 记在各自 role-session 名下——按发起会话
    # 直查恒 0。据会话的 project 元数据聚合「员工会话家族」，让群聊概览如实显示
    # 含全员执行成本的用量。
    family_prefix = ""
    try:
        session_record = _get_store().get_session(session_id)
        raw_meta = (session_record or {}).get("metadata")
        if isinstance(raw_meta, str):
            raw_meta = json.loads(raw_meta or "{}")
        project_id = str((raw_meta or {}).get("project_id") or "").strip()
        if project_id:
            family_prefix = f"role-session::{project_id}::%"
    except Exception:  # noqa: BLE001 — 元数据不可读不阻断（回落单会话口径）
        family_prefix = ""

    persisted = _persisted_session_usage(session_id, family_prefix=family_prefix)
    if persisted is not None:
        return {"session_id": session_id, **persisted, "tool_stats": tool_stats}

    if _shared_cost_tracker is None:
        return {
            "session_id": session_id,
            "total_tokens": 0,
            "total_cost_cny": 0.0,
            "per_model": {},
            "tool_stats": {},
        }
    sc = _shared_cost_tracker.get_session_cost(session_id)
    return {
        "session_id": session_id,
        "total_tokens": sc.total_tokens,
        "total_cost_cny": round(sc.total_cost_cny, 6),
        "per_model": sc.per_model,
        "tool_stats": tool_stats,
    }


def _persisted_session_usage(session_id: str, *, family_prefix: str = "") -> dict[str, Any] | None:
    """功能：从本地台账读会话用量（family_prefix 非空时聚合员工会话家族；失败返回 None）。"""
    try:
        from bandi.core.config import default_cost_db_path
        from bandi.observability.cost_store import get_shared_cost_store

        return get_shared_cost_store(default_cost_db_path()).session_usage_detail(
            session_id, family_prefix=family_prefix
        )
    except Exception as exc:  # noqa: BLE001 — 观测面不可用不得让面板 500
        logger.warning("persisted session usage unavailable: %s", exc)
        return None


@app.post("/api/sessions/{session_id}/explain")
async def explain_session(session_id: str, at: str | None = None) -> dict[str, Any]:
    store = _get_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    try:
        entries = store.resolved_branch(session_id, at=at)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail=str(exc)) from exc
    from bandi.observability.trace_explainer import TraceExplainer

    explainer = TraceExplainer(event_bus=None, session_store=store)
    steps = [
        {"action": s.action, "turn": s.turn, "detail": s.detail}
        for s in explainer.get_steps(session_id, at=at)
    ]
    explanation = explainer.explain(session_id, at=at)
    checkpoints = explainer.list_checkpoints(session_id)
    return {
        "tree": entries,
        "steps": steps,
        "explanation": explanation,
        "checkpoints": checkpoints,
    }


# ── Entity ──────────────────────────────────────────────────


@app.get("/api/account/users")
async def account_users_list(tenant_id: str | None = None) -> dict[str, Any]:
    """账号列表（含 local 默认账号）。.6：tenant_id 过滤（缺省全量）。"""

    store = _get_account_store()
    scoped = str(tenant_id).strip() if tenant_id else None
    return {
        "users": [
            {
                "user_id": u.user_id,
                "display_name": u.display_name,
                "tenant_id": u.tenant_id,
                "roles": store.list_user_roles(u.user_id),
            }
            for u in store.list_users(tenant_id=scoped)
        ]
    }


@app.post("/api/account/users")
async def account_users_create(body: dict[str, Any]) -> dict[str, Any]:
    """创建账号（user_id/display_name 必填；tenant_id/roles 可选，默认 default/member）。"""
    user_id = str(body.get("user_id") or "").strip()
    display_name = str(body.get("display_name") or "").strip()
    if not user_id or not display_name:
        raise HTTPException(status_code=400, detail="user_id/display_name required")
    store = _get_account_store()
    try:
        user = store.create_user(
            user_id=user_id,
            display_name=display_name,
            tenant_id=str(body.get("tenant_id") or "default").strip(),
            roles=[str(r) for r in body.get("roles", [])] if body.get("roles") else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "user_id": user.user_id,
        "display_name": user.display_name,
        "tenant_id": user.tenant_id,
        "roles": store.list_user_roles(user.user_id),
    }


@app.get("/api/account/users/{user_id}")
async def account_users_get(user_id: str) -> dict[str, Any]:
    """账号详情（含角色）。"""
    store = _get_account_store()
    user = store.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"USER_NOT_FOUND: {user_id}")
    return {
        "user_id": user.user_id,
        "display_name": user.display_name,
        "tenant_id": user.tenant_id,
        "roles": store.list_user_roles(user.user_id),
    }


_task_store: Any | None = None
_task_runner: Any | None = None
_task_scheduler: Any | None = None
_task_scheduler_lock = threading.Lock()


def _get_task_store() -> Any:
    """TaskStore 单例（~/.bandi/data/scheduler.db）。"""
    global _task_store
    if _task_store is None:
        from bandi.scheduler.store import TaskStore

        _task_store = TaskStore(str(Path.home() / ".bandi" / "data" / "scheduler.db"))
    return _task_store


def _get_task_scheduler() -> Any:
    """后台调度器单例（runner 绑定 headless agent turn）。start 端点驱动生命周期。"""
    global _task_scheduler
    with _task_scheduler_lock:
        if _task_scheduler is None:
            from bandi.scheduler.scheduler import SchedulerConfig, TaskScheduler

            scheduler_config = SchedulerConfig()
            try:
                from bandi.core.config import BandiConfig
                from bandi.memory.maintenance import default_maintenance_callable

                maintenance = BandiConfig.load().memory_maintenance
                interval_hours = float(maintenance.interval_hours)
                # 功能：interval_hours<=0 显式视为关闭维护——0 直传调度器会每 tick 全量维护
                if maintenance.enabled and interval_hours > 0:
                    scheduler_config.maintenance_enabled = True
                    scheduler_config.maintenance_interval_sec = interval_hours * 3600.0
                    scheduler_config.maintenance_callable = default_maintenance_callable(
                        ttl_days=maintenance.session_ttl_days,
                        forget_older_than_days=maintenance.forget_older_than_days,
                        promote_limit=maintenance.promote_limit,
                    )
                elif maintenance.enabled:
                    scheduler_config.maintenance_enabled = False
                    logger.warning(
                        "memory maintenance disabled: interval_hours=%s <= 0", interval_hours
                    )
            except Exception as e:  # noqa: BLE001 — 配置缺失不阻断调度器装配
                logger.warning("memory maintenance wiring skipped: %s", e)
            _task_scheduler = TaskScheduler(
                store=_get_task_store(),
                runner=_get_task_runner(),
                config=scheduler_config,
            )
    return _task_scheduler


@app.on_event("startup")
async def _startup_scheduler_autorun() -> None:
    """功能：serve 启动时拉起后台定时任务调度器；测试/嵌入导入默认不启动。"""
    if os.environ.get("BANDI_SCHEDULER_AUTOSTART", "0") != "1":
        return
    try:
        await _get_task_scheduler().start()
        logger.info("scheduler autostart enabled")
    except Exception as e:  # noqa: BLE001 — 调度器启动失败不阻断 API
        logger.warning("scheduler autostart skipped: %s", e)


@app.on_event("shutdown")
async def _shutdown_scheduler() -> None:
    """功能：API 关闭时停止后台调度循环，避免进程残留。"""
    scheduler = _task_scheduler
    if scheduler is None or not scheduler.running:
        return
    try:
        await scheduler.stop()
    except Exception as e:  # noqa: BLE001 — 关闭失败不阻断进程退出
        logger.warning("scheduler shutdown failed: %s", e)


_CONSOLIDATION_SENTINEL = "[bandi:memory-consolidate]"


async def _run_employee_scheduled_task(
    *,
    task: Any,
    session_id: str,
    project_id: str,
    employee_id: str,
    prompt: str,
    session_store: Any,
) -> str:
    """功能：员工定时任务走公司工作项执行，结果摘要回写原会话。"""
    from bandi.api.company_chat import _resolve_registered_role, handle_chat_message
    from bandi.core.message import Message, MessageRole

    dispatcher = _get_company_dispatcher()
    role_id = employee_id
    try:
        role_id = _resolve_registered_role(employee_id, dispatcher.org_engine)
    except Exception:  # noqa: BLE001 — 角色未注册时按原 id 尝试
        role_id = employee_id
    project_key = project_id or "company"
    communication = _get_project_comms(project_key)
    threads = _get_project_threads(project_key)
    session_store.append_message(session_id, Message(role=MessageRole.USER, content=prompt))
    items = handle_chat_message(
        content=prompt,
        sender_role="user",
        communication=communication,
        store=dispatcher.store,
        org_engine=dispatcher.org_engine,
        thread_manager=threads,
        target_role=role_id,
        project_id=project_id,
        session_id=session_id,
        intent="task",
    )
    if not items:
        return "员工任务未创建：目标角色不在项目中或缺少可用席位。"
    mcp_servers = [
        str(name) for name in (task.payload or {}).get("mcp_servers") or [] if str(name).strip()
    ]
    if mcp_servers:
        for item in items:
            item.metadata["mcp_allowed_servers"] = mcp_servers
            dispatcher.store.save(item)
    for item in items:
        item.metadata["background_run"] = True
        dispatcher.store.save(item)
    outputs: list[str] = []
    claimed = dispatcher.claim(role_id)
    if not claimed:
        return "员工当前正在执行其他任务，本次定时任务已跳过。"
    try:
        for item in items:
            result = await dispatcher.run_work_item(item.id)
            outputs.append(str(getattr(result, "output", "") or "").strip() or "员工执行完成")
    finally:
        if claimed:
            dispatcher.release(role_id)
    summary = "\n\n".join(outputs)
    session_store.append_message(session_id, Message(role=MessageRole.ASSISTANT, content=summary))
    return summary


class _BackgroundInteraction(AutoApproveInteraction):
    """功能：后台定时任务的无人工交互策略——权限与确认全部自动放行。"""


def _get_task_runner() -> Any:
    """触发执行体（headless agent turn）。测试可 patch _task_runner 替换 spy。"""
    global _task_runner
    if _task_runner is None:
        from bandi.core.agent_runner import run_agent_turn

        async def _headless_runner(task: Any) -> str:
            """功能：调度任务执行体——返回产出文本（10.3 静默协议据此判空/剔除
            HEARTBEAT_OK）；`[bandi:memory-consolidate]` 哨兵任务挂 scheduler
            执行记忆整合（10.7），不发起 LLM turn。会话内创建的任务把执行结果
            追加回原会话；无会话的旧任务首次执行时补建 automation 会话。"""
            prompt = str(getattr(task, "prompt", "") or "").strip()
            if prompt == _CONSOLIDATION_SENTINEL:
                from bandi.memory.consolidation import consolidate_default

                return json.dumps(consolidate_default().to_dict(), ensure_ascii=False)
            from bandi.memory.session.store import SessionStore
            from bandi.memory.staging import reset_write_origin, set_write_origin

            payload = dict(getattr(task, "payload", None) or {})
            session_id = str(payload.get("session_id") or "").strip()
            project_id = str(payload.get("project_id") or "").strip()
            employee_id = str(payload.get("employee_id") or "").strip()
            session_store = SessionStore(str(Path.home() / ".bandi" / "data" / "bandi.db"))
            if not session_id or session_store.get_session(session_id) is None:
                session_id = session_store.create_session(
                    session_id=session_id or None,
                    title=str(getattr(task, "name", "") or "定时任务")[:120],
                    origin="automation",
                    project_id=project_id or None,
                    employee_id=employee_id or None,
                    target_role=employee_id or None,
                )
                payload["session_id"] = session_id
                task.payload = payload
                _get_task_store().save(task)

            token = set_write_origin("background")
            try:
                # 功能：后台任务无人值守——权限与确认自动放行，全部工具可用
                background_prompt = (
                    "【后台定时任务】当前没有人工审批界面，所有工具均可用；"
                    "请避免不可逆操作，产物优先落到项目目录。\n\n" + str(task.prompt)
                )
                if employee_id:
                    return await _run_employee_scheduled_task(
                        task=task,
                        session_id=session_id,
                        project_id=project_id,
                        employee_id=employee_id,
                        prompt=background_prompt,
                        session_store=session_store,
                    )
                outcome = await run_agent_turn(
                    user_input=background_prompt,
                    mode="execute",
                    session_id=session_id,
                    session_store=session_store,
                    project_id=project_id or None,
                    employee_id=employee_id or None,
                    session_title=str(getattr(task, "name", "") or "")[:120],
                    session_origin="automation",
                    user_interaction=_BackgroundInteraction(),
                    mcp_allowed_servers=[
                        str(name)
                        for name in (task.payload or {}).get("mcp_servers") or []
                        if str(name).strip()
                    ]
                    or None,
                )
            finally:
                reset_write_origin(token)
            return str(getattr(outcome, "reply", "") or "")

        _task_runner = _headless_runner
    return _task_runner


def _task_to_dict(task: Any) -> dict[str, Any]:
    payload = dict(getattr(task, "payload", None) or {})
    return {
        "task_id": task.task_id,
        "name": task.name,
        "cron": task.cron,
        "prompt": task.prompt,
        "enabled": task.enabled,
        "status": task.status.value,
        "next_run_at": iso_local(task.next_run_at) or None,
        "last_run_at": iso_local(task.last_run_at) or None,
        "last_error": task.last_error,
        "last_output": getattr(task, "last_output", ""),
        "target_type": getattr(task, "target_type", ""),
        "target_id": getattr(task, "target_id", ""),
        "description": getattr(task, "description", ""),
        "session_id": payload.get("session_id", ""),
        "project_id": payload.get("project_id", ""),
        "employee_id": payload.get("employee_id", ""),
        "schedule_kind": getattr(task, "schedule_kind", "cron") or "cron",
        "schedule_spec": dict(getattr(task, "schedule_spec", None) or {}),
        "effective_from": getattr(task, "effective_from", "") or "",
        "effective_until": getattr(task, "effective_until", "") or "",
        "mcp_servers": [
            str(name) for name in payload.get("mcp_servers") or [] if str(name).strip()
        ],
    }


def _parse_enabled_flag(body: dict[str, Any], default: bool) -> bool:
    """严格解析 enabled：JSON bool / 0·1 / 字符串 true·false（拒绝歧义值）。

    破坏者防线：裸 bool() 会把字符串 "false" 当 True——显式关闭语义被字符串
    注入绕过（PATCH {"enabled": "false"} 反而开启任务）。
    """
    value = body.get("enabled", default)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    raise HTTPException(status_code=400, detail=f"INVALID_ENABLED: {value!r}")


@app.post("/api/scheduler/tasks")
async def scheduler_task_create(body: dict[str, Any]) -> dict[str, Any]:
    """创建定时任务（name/cron/prompt 必填，enabled 可选）。"""
    from bandi.scheduler.cron import InvalidCronExpressionError
    from bandi.scheduler.store import ScheduledTask

    name = str(body.get("name") or "").strip()
    cron = str(body.get("cron") or "").strip()
    prompt = str(body.get("prompt") or "").strip()
    schedule_kind = str(body.get("schedule_kind") or "cron").strip() or "cron"
    schedule_spec = body.get("schedule_spec") if isinstance(body.get("schedule_spec"), dict) else {}
    if schedule_kind not in ("cron", "periodic", "interval", "once"):
        raise HTTPException(status_code=400, detail=f"INVALID_SCHEDULE_KIND: {schedule_kind}")
    if not name or not prompt or (schedule_kind == "cron" and not cron):
        raise HTTPException(status_code=400, detail="name/cron/prompt required")
    if schedule_kind != "cron" and not schedule_spec:
        raise HTTPException(status_code=400, detail="schedule_spec required")
    store = _get_task_store()
    task = ScheduledTask(
        task_id=f"st_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}",
        name=name,
        cron=cron,
        prompt=prompt,
        enabled=_parse_enabled_flag(body, True),
        target_type=str(body.get("target_type") or "").strip(),
        target_id=str(body.get("target_id") or "").strip(),
        description=str(body.get("description") or "").strip(),
        payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
        schedule_kind=schedule_kind,
        schedule_spec=schedule_spec,
        effective_from=str(body.get("effective_from") or "").strip(),
        effective_until=str(body.get("effective_until") or "").strip(),
    )
    for key in ("session_id", "project_id", "employee_id"):
        value = str(body.get(key) or "").strip()
        if value:
            task.payload[key] = value
    mcp_servers = body.get("mcp_servers")
    if isinstance(mcp_servers, list):
        names = [str(name).strip() for name in mcp_servers if str(name).strip()]
        if names:
            task.payload["mcp_servers"] = names
    try:
        saved = store.save(task)
    except InvalidCronExpressionError as exc:
        raise HTTPException(status_code=400, detail=f"INVALID_CRON: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"INVALID_SCHEDULE: {exc}")
    return _task_to_dict(saved)


@app.get("/api/scheduler/tasks")
async def scheduler_task_list() -> dict[str, Any]:
    """定时任务列表。"""
    store = _get_task_store()
    return {"tasks": [_task_to_dict(t) for t in store.list()]}


@app.get("/api/scheduler/tasks/{task_id}")
async def scheduler_task_get(task_id: str) -> dict[str, Any]:
    """单任务详情。"""
    store = _get_task_store()
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"TASK_NOT_FOUND: {task_id}")
    return _task_to_dict(task)


@app.patch("/api/scheduler/tasks/{task_id}")
async def scheduler_task_update(task_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """部分更新（name/cron/prompt/enabled）。cron 变更必须重排程。"""
    from bandi.scheduler.cron import InvalidCronExpressionError
    from bandi.scheduler.store import ScheduledTask

    store = _get_task_store()
    existing = store.get(task_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"TASK_NOT_FOUND: {task_id}")
    updated = ScheduledTask(
        task_id=existing.task_id,
        name=str(body.get("name") or existing.name),
        cron=str(body.get("cron") or existing.cron),
        prompt=str(body.get("prompt") or existing.prompt),
        enabled=_parse_enabled_flag(body, existing.enabled),
        status=existing.status,
        next_run_at=existing.next_run_at,
        last_run_at=existing.last_run_at,
        last_error=existing.last_error,
        last_output=existing.last_output,
        target_type=existing.target_type,
        target_id=existing.target_id,
        description=existing.description,
        payload=dict(existing.payload or {}),
        schedule_kind=existing.schedule_kind,
        schedule_spec=dict(existing.schedule_spec or {}),
        effective_from=existing.effective_from,
        effective_until=existing.effective_until,
    )
    schedule_changed = False
    if "schedule_kind" in body:
        kind = str(body.get("schedule_kind") or "").strip() or "cron"
        if kind not in ("cron", "periodic", "interval", "once"):
            raise HTTPException(status_code=400, detail=f"INVALID_SCHEDULE_KIND: {kind}")
        schedule_changed = schedule_changed or kind != updated.schedule_kind
        updated.schedule_kind = kind
    if "schedule_spec" in body:
        spec = body.get("schedule_spec")
        if not isinstance(spec, dict):
            raise HTTPException(status_code=400, detail="schedule_spec must be an object")
        schedule_changed = schedule_changed or spec != updated.schedule_spec
        updated.schedule_spec = spec
    if "effective_from" in body:
        updated.effective_from = str(body.get("effective_from") or "").strip()
        schedule_changed = True
    if "effective_until" in body:
        updated.effective_until = str(body.get("effective_until") or "").strip()
        schedule_changed = True
    for key in ("session_id", "project_id", "employee_id"):
        if key in body:
            value = str(body.get(key) or "").strip()
            if value:
                updated.payload[key] = value
            else:
                updated.payload.pop(key, None)
    if "mcp_servers" in body:
        raw_servers = body.get("mcp_servers")
        if not isinstance(raw_servers, list):
            raise HTTPException(status_code=400, detail="mcp_servers must be a list")
        names = [str(name).strip() for name in raw_servers if str(name).strip()]
        if names:
            updated.payload["mcp_servers"] = names
        else:
            updated.payload.pop("mcp_servers", None)
    if "target_type" in body:
        updated.target_type = str(body.get("target_type") or "").strip()
    if "target_id" in body:
        updated.target_id = str(body.get("target_id") or "").strip()
    if "cron" in body and str(body.get("cron") or "").strip() != existing.cron:
        schedule_changed = True
    if schedule_changed:
        updated.next_run_at = None  # save 时按当前时间重排
    try:
        saved = store.save(updated)
    except InvalidCronExpressionError as exc:
        raise HTTPException(status_code=400, detail=f"INVALID_CRON: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"INVALID_SCHEDULE: {exc}")
    return _task_to_dict(saved)


@app.delete("/api/scheduler/tasks/{task_id}")
async def scheduler_task_delete(task_id: str) -> dict[str, Any]:
    """删除定时任务；会话化任务同时删除其运行会话。"""
    store = _get_task_store()
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"TASK_NOT_FOUND: {task_id}")
    session_id = str((task.payload or {}).get("session_id") or "").strip()
    deleted_session = False
    if session_id:
        from bandi.memory.session.store import SessionStore

        session_store = SessionStore(str(Path.home() / ".bandi" / "data" / "bandi.db"))
        deleted_session = session_store.delete_session(session_id)
    store.delete(task_id)
    return {"deleted": task_id, "deleted_session": deleted_session}


@app.post("/api/scheduler/tasks/{task_id}/trigger")
async def scheduler_task_trigger(task_id: str) -> dict[str, Any]:
    """手动立即触发一次（调 runner；不推进自动排程的 next_run_at）。"""
    from bandi.scheduler.store import TaskStatus

    store = _get_task_store()
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"TASK_NOT_FOUND: {task_id}")
    runner = _get_task_runner()
    if not store.try_begin_run(task_id):
        raise HTTPException(status_code=409, detail=f"TASK_BUSY: {task_id}")
    try:
        output = await runner(task)
        store.set_status(task_id, TaskStatus.IDLE)
        store.set_last_run(task_id, datetime.now())
        from bandi.scheduler.scheduler import is_silent_completion

        store.set_last_output(task_id, "" if is_silent_completion(output) else str(output)[:2000])
    except Exception as exc:  # noqa: BLE001 — 触发失败要可观测（status+error 落库）
        store.set_status(task_id, TaskStatus.FAILED, error=str(exc))
        raise HTTPException(status_code=500, detail=f"TRIGGER_FAILED: {exc}")
    return _task_to_dict(store.get(task_id))


@app.post("/api/scheduler/start")
async def scheduler_start() -> dict[str, Any]:
    """启动后台调度循环（到期自动触发）。幂等。"""
    await _get_task_scheduler().start()
    return {"running": True}


@app.post("/api/scheduler/stop")
async def scheduler_stop() -> dict[str, Any]:
    """停止后台调度循环。幂等。"""
    await _get_task_scheduler().stop()
    return {"running": False}


@app.get("/api/scheduler/status")
async def scheduler_status() -> dict[str, Any]:
    """后台循环运行状态。"""
    sched = _get_task_scheduler()
    return {"running": sched.running, "tasks": len(sched.store.list())}


# ── Tool 能力管理面 ─────────────────────────────────────────

_tools_meta_registry: Any | None = None
_tools_meta_lock = threading.Lock()


def _get_tools_meta_registry() -> Any:
    global _tools_meta_registry
    with _tools_meta_lock:
        if _tools_meta_registry is None:
            from bandi.agent.factory import build_tool_registry

            _tools_meta_registry = build_tool_registry()
    return _tools_meta_registry


@app.get("/api/tools/meta")
async def tools_meta() -> list[dict[str, Any]]:
    """/能力管理面：全量工具能力元数据（capability_tags/feature_group）
    供前端能力管理页与 ToolSearch 筛选消费（list_tool_meta 生产接线点）。"""
    return _get_tools_meta_registry().list_tool_meta()


# ── Entity ──────────────────────────────────────────────────


# 三元组本体端点族：stats/graph/facts/entities——类由用户声明（onto_terms 表），
# 无固定枚举校验（原 EntityType 五值会把任意用户声明的类名拒成 400）。


@app.get("/api/ontology/stats")
async def ontology_stats() -> dict[str, Any]:
    """三元组本体计数：类/关系/实体/断言事实 + 可推出事实数。"""
    from bandi.ontology.graph_api import stats_payload

    return stats_payload(_get_ontology_store())


@app.get("/api/ontology/graph")
async def ontology_graph(
    includeInferred: bool = False,  # noqa: N803 — 查询参数名与前端契约一致
    limit: int = 500,
) -> dict[str, Any]:
    """本体图谱导出（节点=实体，边=实体值三元组）。

    与旧 `/api/entities/graph` 同形，知识页渲染管线可平移；两处有意的差别：
    `type` 取实体的最具体类（类现由用户声明，不再是固定枚举），截断显式回报
    （`truncated`/`totalEntities`）而非静默丢弃。
    """
    from bandi.ontology.graph_api import build_graph_payload

    return build_graph_payload(
        _get_ontology_store(), include_inferred=bool(includeInferred), limit=int(limit)
    )


@app.get("/api/ontology/facts")
async def ontology_facts(
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,  # 查询参数名与三元组位序一致
    includeInferred: bool = False,  # noqa: N803 — 查询参数名与前端契约一致
    limit: int = 500,
) -> dict[str, Any]:
    """事实列表——每条带 `via` 标明断言或推理来源（inverse/symmetric/transitive）。"""
    from bandi.ontology.graph_api import facts_payload

    return facts_payload(
        _get_ontology_store(),
        subject=subject,
        predicate=predicate,
        object_id=object,
        include_inferred=bool(includeInferred),
        limit=int(limit),
    )


@app.get("/api/ontology/entities")
async def ontology_entities(
    class_id: str | None = None, search: str | None = None, limit: int = 100
) -> dict[str, Any]:
    """实体列表——按类（含子类）或文本匹配，附 `allClasses` 继承闭包。"""
    from bandi.ontology.engine import class_ancestors, query_entities

    store = _get_ontology_store()
    graph = store.graph()
    found = query_entities(
        graph,
        {
            **({"class_id": class_id} if class_id else {}),
            **({"search": search} if search else {}),
            "limit": int(limit),
        },
    )
    return {
        "entities": [
            {
                "id": entity.id,
                "classes": list(entity.classes),
                "allClasses": sorted(
                    {
                        ancestor
                        for direct in entity.classes
                        for ancestor in class_ancestors(graph.terms, direct)
                    }
                ),
                "label": entity.label or entity.id,
                "comment": entity.comment or "",
            }
            for entity in found
        ]
    }


def _dispatcher_config() -> Any:
    """功能：调度器装配参数——并发/单 tick 预算/总量统一取配置（不可用时回落默认）。"""
    from bandi.company.company_mode import DispatcherConfig

    try:
        company = BandiConfig.load().company
    except Exception:  # noqa: BLE001 — 配置异常不阻断调度器装配
        return DispatcherConfig()
    return DispatcherConfig(
        max_concurrent=company.max_concurrent,
        tick_budget_sec=company.tick_budget_sec,
        max_items_per_tick=company.max_items_per_tick,
    )


def _get_company_dispatcher() -> CompanyDispatcher:
    """懒加载 CompanyDispatcher 单例（~/.bandi/company/company.db）。

    提供 tick 驱动与 kanban 查询两个最小端点，供整合日端到端验证。
    """
    global _company_dispatcher
    with _company_lock:
        if _company_dispatcher is None:
            from bandi.company.company_mode import CompanyDispatcher
            from bandi.company.gate_harness import GateHarness
            from bandi.company.org_engine import OrgEngine
            from bandi.company.recruiter import Recruiter
            from bandi.company.store import WorkItemStore

            company_dir = Path.home() / ".bandi" / "company"
            company_dir.mkdir(parents=True, exist_ok=True)
            store = WorkItemStore(str(company_dir / "company.db"))
            org = OrgEngine()
            org.register_role("worker", "Worker", "Executes work items")
            recruiter = Recruiter()
            recruiter.register_seat("worker")
            # （CLI add-role 持久化的组织在 API tick 链路必须可见）
            from bandi.company.org_persistence import load_org, save_org

            load_org(org, recruiter)
            # 此后 register_role/register_employee 变更经 _persist 自动写 org.json
            # 端点显式 save_org 仅作兜底（双重保障，防新增变更点漏写）
            org._persist_callable = lambda: save_org(org, recruiter)
            from bandi.company.communication import CommunicationManager
            from bandi.company.external_seat_executor import ExternalSeatExecutor
            from bandi.company.seat_executor import EngineSeatExecutor

            # 装配通信管理器——工作项内协作工具（send_dm/inbox/route_work 等 9 个）
            # 经 EngineSeatExecutor 透传给 AgentLoop，company 链路不 fail-closed
            communication = CommunicationManager(
                workspace=str(company_dir),
                project_id="company",
                session_id="default",
                org_engine=org,
                on_message_sent=_broadcast_company_message,
            )
            # {company_dir}/.bandi-comms/company/default/threads/*.md
            # 经 dispatcher.threads 供 chat 接线与 run_work_item 产出回写。
            from bandi.company.discussion_thread import ThreadManager

            thread_manager = ThreadManager(
                workspace=str(company_dir),
                project_id="company",
                session_id="default",
            )
            thread_manager.hydrate()
            thread_manager.bind_on_message(_broadcast_thread_message)
            from bandi.api.company_chat import _round_mention_resolver
            from bandi.company.group_round import GroupRoundService
            from bandi.company.group_round_store import GroupRoundStore

            def _broadcast_group_round(snapshot: dict[str, Any]) -> None:
                _broadcast_thread_progress({"type": "group_round", "data": snapshot})

            round_service = GroupRoundService(
                GroupRoundStore(store.db_path),
                store,
                org,
                mention_resolver=_round_mention_resolver(org),
                broadcast=_broadcast_group_round,
            )

            progress_seen_at: dict[str, float] = {}

            def _progress_with_round(frame: dict[str, Any]) -> None:
                _broadcast_thread_progress(frame)
                data = frame.get("data") if isinstance(frame, dict) else None
                if not isinstance(data, dict):
                    return
                work_item_id = str(data.get("work_item_id") or "")
                event = data.get("event") or {}
                event_type = str((event or {}).get("type") or "")
                if not work_item_id:
                    return
                now = time.time()
                should_note = event_type.startswith("tool") or event_type in (
                    "started",
                    "done",
                    "error",
                )
                if not should_note and now - progress_seen_at.get(work_item_id, 0.0) < 5.0:
                    return
                progress_seen_at[work_item_id] = now
                round_service.note_progress(
                    work_item_id,
                    has_tool=event_type.startswith("tool"),
                )

            _company_dispatcher = CompanyDispatcher(
                store=store,
                recruiter=recruiter,
                org_engine=org,
                gate_harness=GateHarness(),
                communication=communication,
                # 装配 seat_executor，tick 执行工作项所需
                seat_executor=EngineSeatExecutor(
                    communication=communication,
                    org_engine=org,
                    store=store,
                    thread_manager=thread_manager,
                    progress_callback=_progress_with_round,
                    group_context_provider=round_service.context_for,
                    # 员工交互通道（多问卡/权限卡）：问题落到发起会话，帧经公司 WS 广播
                    interaction_provider=_employee_interaction_provider,
                    # 槽位门等待器：执行前 await 用户对系统预建多问卡的答复（口径写回任务）
                    slot_answer_provider=_slot_answer_provider,
                    # 员工产物登记（写类工具成功 → 员工执行会话；右栏「产物」数据源）
                    artifact_store=_get_store(),
                ),
                # 装配：外部引擎一次性执行路径（真外聘 ）
                external_seat_executor=ExternalSeatExecutor(),
                # 并发/预算来自配置（同时在跑上限、单 tick 预算、单 tick 总量）
                config=_dispatcher_config(),
                thread_manager=thread_manager,
            )
            _company_dispatcher.group_rounds = round_service
            # 首次装立即恢复为 READY 的挂起项，避免重启后永不启用（幂等）
            # 保护内容：是否 phase 中，不阻塞开球装配——损坏仅告警
            try:
                _company_dispatcher.recover_stale_claims()
            except Exception:
                logger.exception("recover_stale_claims failed; dispatcher still usable")
            # 产出回写线程路由——项目卡（metadata.project_id）回写对应项目线程
            # （resolve_thread_manager_for 消费；函数定义在模块后部，运行时已加载）
            _company_dispatcher.thread_manager_resolver = _get_project_threads
            # 跑完把回复落回该会话——刷新/切走/新开会话仍可见）。幂等/跳过秘书会话在
            # session_writeback 内（与前端 live 注入口径一致）。
            from bandi.company.session_writeback import write_employee_reply_to_session

            def _session_writer(
                session_id: str,
                sender_role: str,
                sender_name: str,
                content: str,
                work_item_id: str,
                process_events: list[dict[str, Any]] | None = None,
            ) -> str | None:
                return write_employee_reply_to_session(
                    _get_store(),
                    session_id,
                    sender_role=sender_role,
                    sender_name=sender_name,
                    content=content,
                    work_item_id=work_item_id,
                    process_events=process_events,
                    next_assignee=_next_thread_assignee(_company_dispatcher.store, work_item_id),
                )

            _company_dispatcher.session_writer = _session_writer

            def _employee_interaction_cleanup(employee_session_id: str) -> None:
                # 员工执行结束 → 作废其工作会话内仍挂起的交互（孤儿卡防重放：
                # 挂起方已随执行退出，reject 后重放端点不再返回该卡）
                try:
                    istore = _get_interaction_store()
                    for pending_item in istore.list_pending(session_id=employee_session_id):
                        istore.reject(pending_item.interaction_id)
                except Exception:
                    logger.debug("employee interaction cleanup failed", exc_info=True)

            _company_dispatcher.employee_interaction_cleanup = _employee_interaction_cleanup
    return _company_dispatcher


_TERMINAL_PHASES = {"approved", "cancelled", "needs_attention"}


def _next_thread_assignee(store: Any, work_item_id: str) -> dict[str, str] | None:
    """功能：同线程下一个待执行卡的执行者（回复底部「下一步」交接行数据源）。"""
    try:
        current = store.get(work_item_id)
        if current is None:
            return None
        thread_id = str((current.metadata or {}).get("thread_id") or "").strip()
        if not thread_id:
            return None
        candidates: list[Any] = []
        for item in store.list_all():
            if str(item.id) == str(work_item_id):
                continue
            if str((item.metadata or {}).get("thread_id") or "").strip() != thread_id:
                continue
            raw_phase = getattr(item, "phase", "")
            phase = str(getattr(raw_phase, "value", raw_phase) or "")
            if phase in _TERMINAL_PHASES:
                continue
            candidates.append(item)
        if not candidates:
            return None
        candidates.sort(key=lambda item: str(getattr(item, "created_at", "") or ""))
        first = candidates[0]
        return {
            "role_id": str(getattr(first, "assignee_role", "") or ""),
            "card": str(getattr(first, "title", "") or "")[:60],
        }
    except Exception:  # noqa: BLE001 —— 交接行 best-effort，不影响回写主链
        return None


_custom_runtime_runner: Any | None = None


def _get_custom_runtime_runner() -> Any:
    """CustomRuntimeRunner 生产接线——自定义 org 会话入口
    Recruiter/Secretary 链/org_work_item_planner 经此可达（此前生产零实例化）。"""
    global _custom_runtime_runner
    with _company_lock:
        if _custom_runtime_runner is None:
            from bandi.company.custom_runtime import CustomRuntimeRunner, RuntimeHost

            dispatcher = _get_company_dispatcher()
            company_dir = Path.home() / ".bandi" / "company"
            host = RuntimeHost(
                store=dispatcher.store,
                project_id="company",
                workspace=str(company_dir),
                session_id="default",
            )
            _custom_runtime_runner = CustomRuntimeRunner(host)
    return _custom_runtime_runner


@app.post("/api/company/custom/message")
async def company_custom_message(body: dict[str, Any]) -> dict[str, Any]:
    """自定义 org 会话消息入口——Secretary 链编排（创建 work item → 调度执行）。"""
    from bandi.company.company_runtime_profiles import CompanyProfile, RoleProfile

    content = str(body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    roles_payload = body.get("roles") or []
    if not roles_payload:
        raise HTTPException(status_code=400, detail="roles is required")
    roles = [
        RoleProfile(
            id=str(r["id"]),
            name=str(r.get("name") or r["id"]),
            responsibility=str(r.get("responsibility") or ""),
        )
        for r in roles_payload
    ]
    profile_raw = str(body.get("profile") or "") or None
    profile = CompanyProfile(profile_raw) if profile_raw else None
    runner = _get_custom_runtime_runner()
    output = await runner.process_message(
        content,
        roles=roles,
        profile=profile,
        org_id=str(body.get("org_id") or "") or None,
        session_id=str(body.get("session_id") or "") or None,
    )
    return {"output": output}


@app.post("/api/company/tick")
async def company_tick() -> dict[str, Any]:
    """驱动一轮 dispatcher tick，返回调度报告。"""
    report = await _get_company_dispatcher().tick()
    return {
        "tick_id": report.tick_id,
        "started_at": iso_local(report.started_at),
        "processed": report.processed,
        "claimed": report.claimed,
        "released": report.released,
        "transitions": list(report.transitions),
        "errors": report.errors,
        "gate_results": list(report.gate_results),
        "timeline": [list(entry) for entry in report.timeline],
    }


@app.post("/api/company/chat/mode")
async def company_chat_mode(body: dict[str, Any]) -> dict[str, Any]:
    """功能：会话模式切换——落库到 sessions.mode；转执行模式时释放该会话下 plan 门挂起的派发卡。"""
    mode = str(body.get("mode") or "").strip().lower()
    session_id = str(body.get("session_id") or "").strip()
    if mode not in {"execute", "plan"}:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="mode must be execute or plan")
    if not session_id:
        return {"released": 0, "mode": mode}
    store = _get_store()
    if store.get_session(session_id) is not None:
        store.update_session(session_id, mode=mode)
    if mode != "execute":
        return {"released": 0, "mode": mode}
    dispatcher = _get_company_dispatcher()
    released = 0
    for item in dispatcher.store.list_all():
        metadata = dict(item.metadata or {})
        if str(metadata.get("origin_session_id", "") or "").strip() != session_id:
            continue
        if str(metadata.get("mode_gate", "") or "").strip().lower() != "plan":
            continue
        metadata.pop("mode_gate", None)
        metadata["created_mode"] = "execute"
        item.metadata = metadata
        try:
            dispatcher.store.save_checked(item, getattr(item, "revision", 0))
            released += 1
        except Exception as exc:  # noqa: BLE001 — 单卡乐观锁冲突不阻断其余释放
            logger.debug("release mode gate failed for %s: %s", item.id, exc)
    if released:
        dispatcher.signal_dispatcher_wake()
    return {"released": released, "mode": mode}


@app.get("/api/company/kanban")
async def company_kanban() -> dict[str, Any]:
    """返回当前 kanban 列分组快照。"""
    snapshot = _get_company_dispatcher().get_report()
    return {
        "captured_at": iso_local(snapshot.captured_at),
        "column_counts": snapshot.column_counts,
    }


@app.post("/api/company/work-items/{work_item_id}/abort")
async def company_abort_work_item(work_item_id: str) -> dict[str, Any]:
    """中断执行中的工作项（公司链用户中断；无活动执行 404）。"""
    interrupted = await _get_company_dispatcher().interrupt_work_item(work_item_id)
    if not interrupted:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="NO_ACTIVE_EXECUTION")
    return {"work_item_id": work_item_id, "interrupted": True}


@app.get("/api/company/external/preflight")
async def company_external_preflight(
    project_id: str = "default",
    probe_commands: bool = True,
    prepare_surfaces: bool = True,
) -> dict[str, Any]:
    """外部执行引擎预检——codex/claude_code/cursor/opencode 四 CLI 的
    可用性/版本/隔离 home/写权限合约探测（真外聘设计 §8 前台登记数据源；
    Day141 组织面板「执行引擎」下拉依赖此端点）。
    """
    from bandi.company.preflight import run_external_agent_preflight

    try:
        results = run_external_agent_preflight(
            project_id=project_id,
            probe_commands=probe_commands,
            prepare_surfaces=prepare_surfaces,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"preflight failed: {exc}") from exc
    agents = [r.as_dict() for r in results]
    return {
        "ok": bool(agents) and all(a.get("ok") for a in agents),
        "agents": agents,
        "available_agents": [a["agent"] for a in agents if a.get("available")],
    }


_knowledge_store: Any | None = None


def _get_knowledge_store() -> Any:
    """功能：知识库共享单例（进程级，tests monkeypatch 此函数隔离）。"""
    global _knowledge_store
    if _knowledge_store is None:
        from bandi.knowledge.store import get_shared_store

        _knowledge_store = get_shared_store()
    return _knowledge_store


class KnowledgeIngestRequest(BaseModel):
    base_id: str
    filename: str
    text: str
    title: str = ""
    employee_id: str = ""


@app.post("/api/company/knowledge/ingest")
async def company_knowledge_ingest(req: KnowledgeIngestRequest) -> dict[str, Any]:
    """文档投放——三层投影落库 + OKF 概念自动构建。"""
    from bandi.knowledge.okf import build_okf_for_document

    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text is blank")
    if not req.filename.strip():
        raise HTTPException(status_code=400, detail="filename is required")
    if not req.base_id.strip():
        raise HTTPException(status_code=400, detail="base_id is required")
    store = _get_knowledge_store()
    store.ensure_base(req.base_id, employee_id=req.employee_id or None)
    try:
        result = store.ingest_document(
            base_id=req.base_id, filename=req.filename, text=req.text, title=req.title
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    document = store.get_document(result.document_id)
    buckets = store.list_buckets(result.document_id)
    concepts = store.upsert_concepts(req.base_id, build_okf_for_document(document, buckets))
    try:
        from bandi.resources.knowledge_sources import write_source

        write_source(req.base_id, req.filename, req.text)
    except Exception as exc:  # noqa: BLE001 - 源文件是辅助面，不阻断摄取
        logger.warning("knowledge source write failed: %s", exc)
    return {
        "ok": True,
        "document_id": result.document_id,
        "bucket_count": result.bucket_count,
        "chunk_count": result.chunk_count,
        "concept_count": len(concepts),
    }


@app.get("/api/company/knowledge/documents")
async def company_knowledge_documents(base_id: str) -> dict[str, Any]:
    """知识库文档清单（含三层计数；前端 KnowledgePage 依赖）。"""
    store = _get_knowledge_store()
    documents = [
        {
            "id": d.id,
            "filename": d.filename,
            "title": d.title,
            "status": d.status,
            "bucket_count": d.bucket_count,
            "chunk_count": d.chunk_count,
            "updated_at": iso_local(d.updated_at),
        }
        for d in store.list_documents(base_id)
    ]
    return {"ok": True, "documents": documents}


@app.get("/api/company/knowledge/bases")
async def company_knowledge_bases(employee_id: str = "") -> dict[str, Any]:
    """功能：知识库清单（employee_id 为空返回全部知识库）。"""
    store = _get_knowledge_store()
    bases = store.list_bases(employee_id=employee_id or None)
    return {
        "bases": [
            {
                "base_id": base.id,
                "name": base.name,
                "project_id": base.project_id or "",
                "employee_id": base.employee_id or "",
                "document_count": len(store.list_documents(base.id)),
            }
            for base in bases
        ]
    }


@app.get("/api/company/skills/cards")
async def company_skill_cards() -> dict[str, Any]:
    """功能：SOP 卡片清单（skills 目录下的 *.card.json）。"""
    import os

    from bandi.company.agent_profile import DEFAULT_SKILLS_DIR
    from bandi.skills.serializer import load_card

    skills_dir = Path(os.environ.get("BANDI_SKILLS_DIR") or DEFAULT_SKILLS_DIR).expanduser()
    cards: list[dict[str, Any]] = []
    if skills_dir.is_dir():
        for path in sorted(skills_dir.glob("*.card.json")):
            try:
                card = load_card(path)
            except Exception:  # noqa: BLE001 单卡损坏不阻断清单
                continue
            cards.append(
                {
                    "skill_id": card.skill_id,
                    "name": card.name,
                    "description": card.description,
                    "version": card.version,
                    "business_domain": card.business_domain or "",
                }
            )
    return {"cards": cards}


@app.post("/api/company/resources/import")
async def company_resource_import(
    kind: str = "",
    file: UploadFile | None = None,
    base_id: str = "",
    employee_id: str = "",
    name: str = "",
) -> dict[str, Any]:
    """功能：导入本地资源文件（知识库文本 / SOP 卡片 / 技能 SKILL.md）。"""
    import json
    import os
    import re

    from bandi.company.agent_profile import DEFAULT_SKILLS_DIR
    from bandi.skills.serializer import save_card

    if file is None:
        raise HTTPException(status_code=400, detail="file required")
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="file too large (max 5MB)")
    filename = Path(file.filename or "upload").name
    normalized = str(kind).strip().lower()

    if normalized == "knowledge":
        if b"\x00" in raw:
            raise HTTPException(status_code=400, detail="仅支持文本知识文件")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gbk")
            except UnicodeDecodeError:
                text = raw.decode("utf-8", errors="ignore")
        if not text.strip():
            raise HTTPException(status_code=400, detail="文件内容为空")
        target_base = base_id.strip() or (f"emp-{employee_id}" if employee_id else "shared")
        store = _get_knowledge_store()
        store.ensure_base(target_base, employee_id=employee_id or None)
        result = store.ingest_document(
            base_id=target_base, filename=filename, text=text, title=name or filename
        )
        try:
            from bandi.resources.knowledge_sources import write_source

            write_source(target_base, filename, text)
        except Exception as exc:  # noqa: BLE001 - 源文件是辅助面，不阻断摄取
            logger.warning("knowledge source write failed: %s", exc)
        return {
            "imported": True,
            "kind": normalized,
            "base_id": target_base,
            "document_id": result.document_id,
            "chunk_count": result.chunk_count,
        }

    skills_dir = Path(os.environ.get("BANDI_SKILLS_DIR") or DEFAULT_SKILLS_DIR).expanduser()
    skills_dir.mkdir(parents=True, exist_ok=True)

    if normalized == "sop":
        from bandi.skills.card import SkillCard

        try:
            card = SkillCard.model_validate(json.loads(raw.decode("utf-8-sig")))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"无效 SOP 卡片: {exc}") from exc
        path = save_card(card, skills_dir)
        return {
            "imported": True,
            "kind": normalized,
            "skill_id": card.skill_id,
            "name": card.name,
            "path": str(path),
        }

    if normalized == "skill":
        skill_name = (name or Path(filename).stem).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", skill_name):
            raise HTTPException(status_code=400, detail="技能名仅允许字母/数字/._-")
        text = raw.decode("utf-8-sig", errors="ignore")
        if not re.match(r"^\s*---\s*\n", text):
            text = f"---\nname: {skill_name}\ndescription: {skill_name}\n---\n\n{text}"
        target_dir = skills_dir / skill_name
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "SKILL.md").write_text(text, encoding="utf-8")
        return {
            "imported": True,
            "kind": normalized,
            "skill_id": skill_name,
            "name": skill_name,
            "path": str(target_dir / "SKILL.md"),
        }

    raise HTTPException(status_code=400, detail=f"unknown kind: {kind!r}")


@app.get("/api/company/knowledge/search")
async def company_knowledge_search(query: str, base_id: str = "") -> dict[str, Any]:
    """知识检索——chunk FTS 命中 + OKF 概念卡（evidence 引用链数据源）。"""
    from bandi.knowledge.okf import search_concepts, selected_concept_cards

    store = _get_knowledge_store()
    chunks = [
        {
            "id": c.id,
            "content": c.content,
            "document_id": c.document_id,
            "bucket_id": c.bucket_id,
            "source_ref": c.source_ref,
        }
        for c in store.search_chunks(query, base_id=base_id or None)
    ]
    concepts = store.list_all_concepts() if not base_id else store.list_concepts(base_id)
    concept_cards = selected_concept_cards(search_concepts(query, concepts))
    return {"ok": True, "chunks": chunks, "concepts": concept_cards}


@app.get("/api/company/employees/{employee_id}/knowledge")
async def company_employee_knowledge(employee_id: str) -> dict[str, Any]:
    """员工个体知识库（私有桶——knowledge_bases.employee_id 归属；含文档计数）。"""
    store = _get_knowledge_store()
    bases = [
        {
            "base_id": base.id,
            "name": base.name,
            "project_id": base.project_id or "",
            "document_count": len(store.list_documents(base.id)),
        }
        for base in store.list_bases(employee_id=employee_id)
    ]
    return {"employee_id": employee_id, "bases": bases}


@app.get("/api/company/presets")
async def company_presets() -> dict[str, Any]:
    from bandi.company.architecture_registry import get_all_presets

    presets = get_all_presets()
    return {"presets": [{"id": p.id, "name": p.name, "category": p.category} for p in presets]}


@app.get("/api/company/presets/{preset_id}")
async def company_preset(preset_id: str) -> dict[str, Any]:
    from bandi.company.architecture_registry import get_preset

    preset = get_preset(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"preset {preset_id!r} not found")
    return {
        "id": preset.id,
        "name": preset.name,
        "category": preset.category,
        "description": preset.description,
    }


@app.post("/api/company/packages/install")
async def company_package_install(body: dict[str, Any]) -> dict[str, Any]:
    from bandi.company.package_loader import PackageLoader

    pkg_path_str = str(body.get("path") or "").strip()
    if not pkg_path_str:
        raise HTTPException(status_code=400, detail="package path required")
    home = Path(os.environ.get("BANDI_HOME", os.path.expanduser("~/.bandi")))
    loader = PackageLoader(home=home)
    try:
        package = loader.load_from_path(Path(pkg_path_str).expanduser())
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    dispatcher = _get_company_dispatcher()
    loader.org_engine = dispatcher.org_engine
    info = loader.install(package)
    # 落盘（会抹掉全部已招募席位状态）；已存在的 seat 保持原状态
    for role_id in info.role_ids:
        if dispatcher.recruiter.get_seat(role_id) is None:
            dispatcher.recruiter.register_seat(role_id)
    # 持久化：将 org_engine 写回 company.db（含新增角色/员工/包信息）
    from bandi.company.org_persistence import save_org

    save_org(dispatcher.org_engine, dispatcher.recruiter)
    return {
        "installed": True,
        "package_id": info.package_id,
        "role_ids": info.role_ids,
        "template_ids": info.template_ids,
    }


@app.post("/api/company/packages/export")
async def company_package_export(body: dict[str, Any]) -> dict[str, Any]:
    from bandi.company.package_exporter import PackageExporter
    from bandi.company.talent_market import TalentMarket

    package_id = str(body.get("package_id") or "exported-org").strip()
    name = str(body.get("name") or package_id).strip()
    home = Path(os.environ.get("BANDI_HOME", os.path.expanduser("~/.bandi")))
    try:
        dispatcher = _get_company_dispatcher()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="company not initialized")
    exporter_org_engine = dispatcher.org_engine
    exporter_talent_market = TalentMarket()
    exporter_talent_market.bind_org_engine(exporter_org_engine)
    exporter = PackageExporter(
        org_engine=exporter_org_engine, talent_market=exporter_talent_market, home=home
    )
    pkg = exporter.export_current(package_id, name)
    out_path = home / "packages" / f"{package_id}.opcpkg"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 穿越值在写盘前被拒绝
    exporter.write_to_path(pkg, out_path, package_id=package_id)
    return {"exported": True, "package_id": package_id, "path": str(out_path)}


@app.post("/api/company/collab/{tool_name}")
async def company_collab_dispatch(
    tool_name: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """调用协作工具（同进程直调 dispatch）。"""
    from bandi.tools.collaboration_dispatch import dispatch_collaboration_tool

    role = os.environ.get("BANDI_COMMS_FROM", "")
    if not role:
        raise HTTPException(status_code=400, detail="BANDI_COMMS_FROM required")

    env: dict[str, str] = {
        "BANDI_WORKSPACE_ROOT": os.environ.get("BANDI_WORKSPACE_ROOT", ""),
        "BANDI_COMMS_PROJECT": os.environ.get("BANDI_COMMS_PROJECT", "company"),
        "BANDI_COMMS_SESSION": os.environ.get("BANDI_COMMS_SESSION", "default"),
        "BANDI_COMMS_FROM": role,
    }
    dispatcher = _get_company_dispatcher()
    result, is_error = await dispatch_collaboration_tool(
        tool_name, dict(body or {}), env=env, org_engine=dispatcher.org_engine
    )
    return {"result": result, "is_error": is_error}


# ── WebSocket 流式 ──────────────────────────────────────────


_WS_MAX_FRAME_CHARS = 1_000_000
_SSE_QUEUE_MAXSIZE = 2048


@app.websocket("/api/agent/stream")
async def websocket_endpoint(websocket: WebSocket) -> None:
    global _ws_connections
    if _API_KEY:
        token = _ws_credential(websocket)
        if not (_bearer_token_ok(token) or _signed_token_ok(token)):
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="Unauthorized")
            return
    else:
        client_host = websocket.client.host if websocket.client else ""
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="Unauthorized")
            return
    if not _ws_handshake_allowed(websocket):
        await websocket.close(code=1008, reason="Origin not allowed")
        return
    await websocket.accept()
    with _ws_lock:
        _ws_connections += 1
        logger.info("WebSocket connected, active: %d", _ws_connections)
    try:
        while True:
            data = await websocket.receive_text()
            if len(data) > _WS_MAX_FRAME_CHARS:
                await websocket.send_json(
                    {
                        "type": "error",
                        "data": {"message": f"frame too large (>{_WS_MAX_FRAME_CHARS} chars)"},
                    }
                )
                continue
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, ValueError):
                await websocket.send_json({"type": "error", "data": {"message": "invalid JSON"}})
                continue
            msg_type = payload.get("type", "")
            if msg_type == "chat":
                # 回合放后台任务：接收循环必须继续服务 ping / resume / 断连检测。
                # 此前内联 await 使整轮（数十秒）连接失聪——前端 25s 心跳发出的 ping 无人应答，
                # 超 75s 无任何入站帧即主动断开重连，重连后旧连接上的推送全部落空。
                task = asyncio.create_task(_run_ws_chat(websocket, payload))
                _ws_turn_tasks.add(task)
                task.add_done_callback(_ws_turn_tasks.discard)
                turn_sid = str(payload.get("session_id") or "").strip()
                if turn_sid:
                    _ws_turn_tasks_by_session[turn_sid] = task

                def _drop_task(t: asyncio.Task[None], _sid: str = turn_sid) -> None:
                    if _sid and _ws_turn_tasks_by_session.get(_sid) is t:
                        _ws_turn_tasks_by_session.pop(_sid, None)

                task.add_done_callback(_drop_task)
            elif msg_type == "resume":
                # 10.10：重连续传——按会话补发终态帧（done/error）。前端据补发的 done 定稿
                # 消息（消除永久未定稿），随后重载会话历史补齐断线期间的正文内容
                scope = str(payload.get("scope") or "")
                try:
                    last_seq = int(payload.get("last_seq") or 0)
                except (TypeError, ValueError):
                    last_seq = 0
                missed = _replay_session_terminal(scope, last_seq) if scope else []
                for frame in missed:
                    await websocket.send_json(frame)
                await websocket.send_json(
                    {
                        "type": "resumed",
                        "replayed": len(missed),
                        "last_seq": _session_terminal_last_seq(scope) if scope else 0,
                    }
                )
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("WebSocket error: %s", e)
    finally:
        with _ws_lock:
            _ws_connections -= 1
            logger.info("WebSocket disconnected, active: %d", _ws_connections)


class _SSEStreamHandler:
    """SSE 实现的 StreamHandler：Agent 事件转 SSE 帧，实现 token 级真流式。"""

    def __init__(self) -> None:
        # 丢帧有 warning 可观测；原无界队列在长挂起+慢消费下无上限堆内存）
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_SSE_QUEUE_MAXSIZE)

    async def put(self, event: str, data: dict[str, Any]) -> None:
        frame = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        try:
            self._queue.put_nowait(frame)
            return
        except asyncio.QueueFull:
            pass
        try:
            self._queue.get_nowait()  # 丢最旧
        except asyncio.QueueEmpty:
            pass
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            logger.warning("SSE queue full, frame dropped: %s", event)

    async def on_content_delta(self, session_id: str, delta: str) -> None:
        await self.put("content_delta", {"text": delta, "session_id": session_id})

    async def on_thinking(self, session_id: str, delta: str) -> None:
        await self.put("thinking_delta", {"text": delta, "session_id": session_id})

    async def on_tool_call_start(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        tool_call_id: str = "",
    ) -> None:
        await self.put(
            "tool_call_start",
            {
                "session_id": session_id,
                "tool_name": tool_name,
                "tool_input": tool_input or {},
                "tool_call_id": tool_call_id,
            },
        )

    async def on_tool_result(
        self,
        session_id: str,
        tool_name: str,
        success: bool,
        output_preview: str,
        duration_ms: int,
        tool_call_id: str = "",
        citations: list[dict[str, Any]] | None = None,
    ) -> None:
        await self.put(
            "tool_result",
            {
                "session_id": session_id,
                "tool_name": tool_name,
                "success": success,
                "output_preview": output_preview,
                "duration_ms": duration_ms,
                "tool_call_id": tool_call_id,
                "citations": citations or [],
            },
        )

    async def on_tool_progress(
        self, session_id: str, tool_name: str, progress_type: str, data: dict[str, Any]
    ) -> None:
        await self.put(
            "tool_progress",
            {
                "session_id": session_id,
                "tool_name": tool_name,
                "progress_type": progress_type,
                "data": data,
            },
        )

    async def on_skill_injected(self, session_id: str, skills: list[str]) -> None:
        await self.put("skill_injected", {"skills": list(skills), "session_id": session_id})

    async def on_note(self, session_id: str, note_type: str) -> None:
        await self.put("note", {"note_type": note_type, "session_id": session_id})

    async def on_memory_written(self, session_id: str, channels: list[str]) -> None:
        await self.put(
            "memory_written",
            {"channels": list(channels), "count": len(channels), "session_id": session_id},
        )

    async def on_task_ledger(self, session_id: str, items: list[dict[str, Any]]) -> None:
        await self.put(
            "task_ledger", {"items": list(items), "count": len(items), "session_id": session_id}
        )

    async def on_subagent_progress(
        self, subagent: dict[str, Any], kind: str, payload: dict[str, Any]
    ) -> None:
        """子代理嵌套过程帧（前端挂到对应 task 节点的 children）。"""
        # 补顶层 session_id——前端据此把帧路由到正确会话流，
        # 不再依赖全局单槽兜底（多会话并行时防串台）。
        data = {"subagent": dict(subagent), "kind": kind, **payload}
        parent_sid = str(subagent.get("parent_session_id") or "")
        if parent_sid:
            data.setdefault("session_id", parent_sid)
        await self.put("subagent_progress", data)

    async def frames(self) -> AsyncGenerator[str, None]:
        while True:
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=15)
            except TimeoutError:
                yield ": keepalive\n\n"

    async def on_user_input_injected(self, session_id: str, turn_index: int) -> None:
        await self.put("user_input_injected", {"session_id": session_id, "turn_index": turn_index})


# 10.10：会话 WS 终态帧序号 + 按会话分桶的有界回放缓冲（断线重连补发；超窗丢弃）
# 与公司通道（10.9）的差异：seq 全局单调（前端单一通道内各会话分别记水位），缓冲
# **按 session 分桶**（否则 A 会话重连会回放到 B 会话的帧）；且**仅终态帧入缓冲**——
# 断线期间的内容完整性不靠补 delta，而由前端在收到补发终态帧后重载会话历史补齐
# （轮末已落库），故缓冲体积恒定、内存不随回复长度增长。
_SESSION_WS_SEQ = 0
_SESSION_WS_TERMINAL_MAXLEN = 20
_session_ws_terminal: dict[str, deque[dict[str, Any]]] = {}
_session_ws_terminal_lock = threading.Lock()


def _record_session_terminal(scope: str, frame: dict[str, Any]) -> dict[str, Any]:
    """功能（10.10）：给会话终态帧分配单调 seq 并按会话入缓冲，返回带 seq/scope 的帧。"""
    global _SESSION_WS_SEQ
    with _session_ws_terminal_lock:
        _SESSION_WS_SEQ += 1
        recorded = {**frame, "seq": _SESSION_WS_SEQ, "scope": scope}
        bucket = _session_ws_terminal.get(scope)
        if bucket is None:
            bucket = deque(maxlen=_SESSION_WS_TERMINAL_MAXLEN)
            _session_ws_terminal[scope] = bucket
        bucket.append(recorded)
    return recorded


def _replay_session_terminal(scope: str, last_seq: int) -> list[dict[str, Any]]:
    """功能（10.10）：回放该会话 seq > last_seq 的终态帧（断线期间错过的 done/error）。"""
    with _session_ws_terminal_lock:
        return [
            frame
            for frame in _session_ws_terminal.get(scope, ())
            if int(frame.get("seq", 0)) > last_seq
        ]


def _session_terminal_last_seq(scope: str) -> int:
    """功能（10.10）：该会话缓冲中的最大 seq（无记录 0；供 resumed 回包抬前端水位）。"""
    with _session_ws_terminal_lock:
        bucket = _session_ws_terminal.get(scope, ())
        return max((int(frame.get("seq", 0)) for frame in bucket), default=0)


class _WSStreamHandler:
    """WebSocket 实现的 StreamHandler，将 Agent 事件转发到客户端。

    实现 core StreamHandler Protocol，供 run_agent_turn 调用。
    断连后 Agent 任务继续执行（费用照付）。
    """

    def __init__(self, websocket: WebSocket, session_scope: str = "") -> None:
        self._ws = websocket
        self._disconnected = False
        # 10.10：终态帧入缓冲用的会话作用域（重连补发按会话分桶）
        self._session_scope = session_scope

    async def _send(self, payload: dict[str, Any]) -> None:
        # 断连后静默丢弃：Agent 回合照常执行完并落库（费用照付的既有语义），
        # 但不再对每一帧重试发送
        if self._disconnected:
            return
        # 10.10：仅终态帧分配 seq 并入会话缓冲——重连后由 resume 补发，消除「终态帧丢失
        # ⇒ 消息永久停在未定稿态」；非终态帧直发不入缓冲（内存不随回复长度增长）
        if self._session_scope and payload.get("type") in ("done", "error"):
            payload = _record_session_terminal(self._session_scope, payload)
        try:
            await self._ws.send_json(payload)
        except Exception:  # noqa: BLE001 —— 客户端离开的连接上任何发送失败都只记一次断连
            # Starlette 在已发出 close 的连接上再 send 抛的是 RuntimeError
            # （`Unexpected ASGI message 'websocket.send', after sending 'websocket.close'`），
            # 只捕 WebSocketDisconnect 会漏判 ⇒ 整轮每帧都重试失败并刷 ERROR 日志；
            # asyncio.CancelledError 是 BaseException，不被本 except 捕获（真实取消照常传播）。
            self._disconnected = True

    async def on_content_delta(self, session_id: str, delta: str) -> None:
        await self._send(
            {"type": "content_delta", "data": {"text": delta, "session_id": session_id}}
        )

    async def on_thinking(self, session_id: str, delta: str) -> None:
        await self._send(
            {"type": "thinking_delta", "data": {"text": delta, "session_id": session_id}}
        )

    async def on_tool_call_start(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        tool_call_id: str = "",
    ) -> None:
        await self._send(
            {
                "type": "tool_call_start",
                "data": {
                    "session_id": session_id,
                    "tool_name": tool_name,
                    "tool_input": tool_input or {},
                    "tool_call_id": tool_call_id,
                },
            }
        )

    async def on_tool_result(
        self,
        session_id: str,
        tool_name: str,
        success: bool,
        output_preview: str,
        duration_ms: int,
        tool_call_id: str = "",
        citations: list[dict[str, Any]] | None = None,
    ) -> None:
        await self._send(
            {
                "type": "tool_result",
                "data": {
                    "session_id": session_id,
                    "tool_name": tool_name,
                    "success": success,
                    "output_preview": output_preview,
                    "duration_ms": duration_ms,
                    "tool_call_id": tool_call_id,
                    "citations": citations or [],
                },
            }
        )

    async def on_tool_progress(
        self, session_id: str, tool_name: str, progress_type: str, data: dict[str, Any]
    ) -> None:
        await self._send(
            {
                "type": "tool_progress",
                "data": {
                    "session_id": session_id,
                    "tool_name": tool_name,
                    "progress_type": progress_type,
                    "data": data,
                },
            }
        )

    async def on_skill_injected(self, session_id: str, skills: list[str]) -> None:
        await self._send(
            {"type": "skill_injected", "data": {"skills": list(skills), "session_id": session_id}}
        )

    async def on_note(self, session_id: str, note_type: str) -> None:
        await self._send(
            {"type": "note", "data": {"note_type": note_type, "session_id": session_id}}
        )

    async def on_memory_written(self, session_id: str, channels: list[str]) -> None:
        await self._send(
            {
                "type": "memory_written",
                "data": {
                    "session_id": session_id,
                    "channels": list(channels),
                    "count": len(channels),
                },
            }
        )

    async def on_task_ledger(self, session_id: str, items: list[dict[str, Any]]) -> None:
        await self._send(
            {
                "type": "task_ledger",
                "data": {
                    "session_id": session_id,
                    "items": list(items),
                    "count": len(items),
                },
            }
        )

    async def on_subagent_progress(
        self, subagent: dict[str, Any], kind: str, payload: dict[str, Any]
    ) -> None:
        """子代理嵌套过程帧（前端挂到对应 task 节点的 children）。"""
        # 补顶层 session_id（与 SSE 同口径）
        data = {"subagent": dict(subagent), "kind": kind, **payload}
        parent_sid = str(subagent.get("parent_session_id") or "")
        if parent_sid:
            data.setdefault("session_id", parent_sid)
        await self._send({"type": "subagent_progress", "data": data})

    async def on_user_input_injected(self, session_id: str, turn_index: int) -> None:
        await self._send(
            {
                "type": "user_input_injected",
                "data": {"session_id": session_id, "turn_index": turn_index},
            }
        )

    async def on_interaction_frame(self, payload: dict[str, Any]) -> None:
        """推送交互卡片帧（真挂起时前端据此渲染确认/提问卡片）。"""
        await self._send({"type": "interaction", "data": payload})

    async def on_done(
        self, session_id: str, terminal_reason: str, total_tokens: int, turn_index: int
    ) -> None:
        await self._send(
            {
                "type": "done",
                "data": {
                    "session_id": session_id,
                    "terminal_reason": terminal_reason or None,
                    "total_tokens": total_tokens,
                    "turn_index": turn_index,
                },
            }
        )

    async def on_error(self, session_id: str, error: str) -> None:
        await self._send({"type": "error", "data": {"message": error, "session_id": session_id}})


def _ws_provider_config(payload: dict[str, Any]) -> dict[str, Any] | None:
    """功能：WS chat 会话级模型覆盖。"""
    overrides: dict[str, Any] = {}
    model = str(payload.get("model") or "").strip()
    if model:
        overrides["default_model"] = model
    thinking = str(payload.get("thinking_mode") or "").strip()
    if thinking:
        overrides["thinking_mode"] = thinking
    return overrides or None


async def _run_ws_chat(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """功能：后台回合包装——回合异常不得冒泡成「未被消费的任务异常」。

    回合失败时显式回一帧 error（前端据此结束「正在回复中」并展示失败），
    与内联版本行为一致；连接已断则发送静默失败（不影响回合已落库的结果）。
    """
    try:
        await _handle_ws_chat(websocket, payload)
    except Exception as exc:  # 后台任务边界，异常必须就地收口并告知客户端
        logger.exception("ws chat turn failed: %s", exc)
        try:
            await websocket.send_json({"type": "error", "data": {"message": str(exc)}})
        except Exception:  # noqa: BLE001 —— 客户端已断，无需再报
            pass


async def _handle_ws_chat(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """WebSocket 聊天处理器 — 委托给 core/agent_runner.py::run_agent_turn()。"""
    from bandi.core.agent_runner import run_agent_turn

    message = payload.get("message", "")
    if not isinstance(message, str) or not message.strip():
        await websocket.send_json({"type": "error", "data": {"message": "message is required"}})
        return
    mode = payload.get("mode", "execute")
    if mode not in ("execute", "plan"):
        if mode == "ask":
            logging.getLogger(__name__).warning(
                "legacy mode 'ask' normalized to 'execute' (dual-mode since day124x-u)"
            )
        mode = "execute"

    session_id = payload.get("session_id", "") or ""
    if session_id and _is_reserved_session_id(session_id):
        if _get_store().get_session(session_id) is None:
            await _WSStreamHandler(websocket, session_id or "ws-default").on_error(
                session_id, "session_id is reserved"
            )
            return
    # WS 路径此前从不注册 abort controller → 停止永远
    # 404 NO_ACTIVE_ABORT_CONTROLLER。与 SSE 路径（app.py:994）同口径补注册。
    from bandi.core.abort_controller import AbortController

    abort_controller = AbortController()
    if session_id:
        _active_abort_controllers[session_id] = abort_controller
    # 前端另发 PATCH 与后端建会话的竞态）
    session_title = str(payload.get("title") or "").strip() or None
    session_origin = (
        "secretary" if str(payload.get("session_origin") or "").strip() == "secretary" else ""
    )
    # 10.10：传会话作用域——终态帧按会话入缓冲，供断线重连 resume 补发
    ws_handler = _WSStreamHandler(websocket, session_id or "ws-default")
    # -3c：过程累积器——透传全部帧 + 轮末持久化 process 条目（forward 语义不变）
    from bandi.core.process_recorder import ProcessRecorder

    recorder = ProcessRecorder(ws_handler, _get_store(), session_id or "ws-default")
    from bandi.core.user_interaction import HTTPUserInteraction

    http_ui = HTTPUserInteraction(
        _get_interaction_store(),
        session_id=session_id or "ws-default",
        on_interaction=lambda item: ws_handler.on_interaction_frame(_interaction_frame(item)),
    )
    user_id, account_store = _identity_from(payload)
    project_id_raw = str(payload.get("project_id") or "").strip()
    access_block = _project_access_block(
        project_id_raw, session_id, str(payload.get("target_role") or "")
    )
    if access_block:
        await websocket.send_json({"type": "error", "code": access_block, "data": access_block})
        if session_id:
            _unregister_abort_controller(session_id, abort_controller)
        return
    ws_attachments = _resolve_ws_attachments(payload.get("attachments"), project_id_raw)
    _ensure_project_agents_md(project_id_raw)
    try:
        _company_org_engine_for_tools: Any = _org_api_dispatcher().org_engine
    except Exception:  # noqa: BLE001 —— 组织环境不可用不阻断普通对话（团队工具 fail-closed）
        _company_org_engine_for_tools = None
    target_role_payload = str(payload.get("target_role") or "").strip()
    if not target_role_payload and session_id:
        target_role_payload = _session_target_role(_get_store().get_session(session_id) or {})
    if target_role_payload:
        if session_id:
            _unregister_abort_controller(session_id, abort_controller)
        await _handle_employee_dm(
            websocket,
            payload,
            session_id=session_id or "ws-default",
            project_id=project_id_raw,
            mode=mode,
            org_engine=_company_org_engine_for_tools,
            session_title=session_title,
            ws_handler=ws_handler,
            target_role=target_role_payload,
        )
        return
    # 团队会话接管：项目有团队且非秘书会话 → 不走通用助理回合，统一由公司聊天
    # 路由承接（无 @ 由顶部角色/内容路由；员工产出经线程回写本会话）；回终帧释放流槽
    if (
        project_id_raw
        and session_origin != "secretary"
        and not _is_secretary_session(_get_store(), session_id)
        and _company_org_engine_for_tools is not None
        and _project_member_roles(project_id_raw, _company_org_engine_for_tools)
    ):
        if session_id:
            _unregister_abort_controller(session_id, abort_controller)
        await _handle_team_chat_takeover(
            websocket,
            payload,
            session_id=session_id or "ws-default",
            project_id=project_id_raw,
            mode=mode,
            org_engine=_company_org_engine_for_tools,
            session_title=session_title,
            ws_handler=ws_handler,
        )
        return
    try:
        result = await run_agent_turn(
            user_input=message,
            mode=mode,
            session_id=session_id or None,
            session_store=_get_store(),
            triple_ontology_store=_get_ontology_store(),
            stream_handler=recorder,
            user_interaction=http_ui,
            project_id=project_id_raw or None,
            company_org_engine=_company_org_engine_for_tools,
            employee_id=str(payload.get("employee_id") or "").strip() or None,
            project_root=_validated_session_project_root(
                payload.get("project_root"), str(project_id_raw or "")
            ),
            session_timeout_sec=_session_timeout_sec_from(payload),
            user_id=user_id,
            account_store=account_store,
            session_title=session_title,
            session_origin=session_origin,
            attachments=ws_attachments or None,
            provider_config=_ws_provider_config(payload),
            abort_controller=abort_controller,
        )
    except Exception:
        try:
            await recorder.persist_partial(session_id or "ws-default", 0)
        except Exception:
            logger.exception("process persist on error failed: session=%s", session_id)
        raise
    finally:
        if session_id:
            _unregister_abort_controller(session_id, abort_controller)
            # 轮末清扫：拒绝该会话残留的挂起交互（abort 后同迭代新建的卡不再滞留至 TTL）
            try:
                _istore = _get_interaction_store()
                for _item in _istore.list_pending(session_id=session_id):
                    _istore.reject(_item.interaction_id)
            except Exception:  # noqa: BLE001 - 清理失败不阻断终态
                logger.warning("pending interaction sweep failed: session=%s", session_id)

    # -3c：轮末经 recorder 落 process 条目后转发 done（on_done 幂等于帧语义）
    await recorder.on_done(
        result.session_id,
        result.terminal_reason or "",
        result.total_tokens,
        result.turn_index,
    )


# ---- Chat-First 协作面板（WS broadcast + @mention 触发 work_item）----

# -本端公司 WS 能力清单（连接即 hello 声明）——前端据此决定是否发送
# `subscribe`（版本错配时旧后端会把它落进 chat 分支回 "content required" 噪音）；
# 新增能力时同时更新前端 `use-host-events` 的消费判定。
_COMPANY_WS_CAPABILITIES: tuple[str, ...] = ("progress_scope", "notify")

_company_ws_clients: set[WebSocket] = set()
_company_ws_lock = threading.Lock()
_company_ws_send_locks: dict[int, asyncio.Lock] = {}
# 语义（fail-open 优先，保证老包/未订阅连接不丢帧）：
# - 连接**从未**发过 subscribe 帧 → 不在本表 → 收回**全部**过程帧（旧行为）；
# - 发过 subscribe（`project_ids` 合法列表）→ 只收列表中项目的过程帧（可空集 = 什么都不收）；
# - subscribe 帧格式非法 → 视为**取消订阅**（移出本表 → 回到 fail-open）
# 避免畸形帧把客户端静默关进「什么都不收」的兜底。
# 只作用于 `thread_progress`（过程帧）；业务帧（thread_message/任务卡/审批）恒广播。
# 并发前提：本表**只在事件循环上读写**（写入在 WS 路由的 async 分支、清理在该路由 finally
# 读取在 `_push` 协程内）⇒ 不需要 `_company_ws_lock`（那是给跨线程的 `_company_ws_clients` 用的）。
_company_ws_scopes: dict[int, set[str]] = {}


async def _company_ws_send(ws: WebSocket, payload: dict[str, Any]) -> None:
    """功能：经 per-connection 锁串行发送——广播任务与端点响应帧不会交叠。"""
    lock = _company_ws_send_locks.get(id(ws))
    if lock is None:
        lock = asyncio.Lock()
        _company_ws_send_locks[id(ws)] = lock
    async with lock:
        await ws.send_json(payload)


# 10.9：公司 WS 事件单调序号 + 有界回放缓冲（断线重连 resume 补发；超窗丢弃）
_company_ws_seq = 0
_company_ws_history: deque[dict[str, Any]] = deque(maxlen=200)
_company_ws_history_lock = threading.Lock()
# 10.9 扩展：逐客户端 ack 位点（id(websocket) → seq）——ack 驱动裁剪数据源
_company_ws_acks: dict[int, int] = {}


def _record_company_ack(ws_id: int, seq: int) -> None:
    """功能（10.9 ack 驱动裁剪）：记录客户端确认位点（只增不减）。"""
    with _company_ws_history_lock:
        _company_ws_acks[ws_id] = max(int(seq), _company_ws_acks.get(ws_id, 0))


def _trim_company_events() -> int:
    """功能（10.9 ack 驱动裁剪）：仅当全部在线客户端都有 ack 时裁剪——以最小
    确认 seq 为界删除缓冲头部；有客户端未 ack 则保持缓冲（保证 resume 可补发）。
    返回裁剪帧数。"""
    with _company_ws_history_lock:
        if not _company_ws_history:
            return 0
        client_ids = [id(ws) for ws in list(_company_ws_clients)]
        if not client_ids or any(cid not in _company_ws_acks for cid in client_ids):
            return 0
        min_ack = min(_company_ws_acks[cid] for cid in client_ids)
        removed = 0
        while _company_ws_history and int(_company_ws_history[0].get("seq", 0)) <= min_ack:
            _company_ws_history.popleft()
            removed += 1
        return removed


def _record_company_event(frame: dict[str, Any]) -> dict[str, Any]:
    """功能（10.9）：给帧分配单调 seq 并存回放缓冲，返回带 seq 的帧。"""
    global _company_ws_seq
    with _company_ws_history_lock:
        _company_ws_seq += 1
        recorded = {**frame, "seq": _company_ws_seq}
        _company_ws_history.append(recorded)
    return recorded


def _replay_company_events(last_seq: int) -> list[dict[str, Any]]:
    """功能（10.9）：回放 seq > last_seq 的缓冲帧（客户端断线期间丢失的事件）。"""
    with _company_ws_history_lock:
        return [frame for frame in _company_ws_history if int(frame.get("seq", 0)) > last_seq]


def _company_ws_last_seq() -> int:
    with _company_ws_history_lock:
        return _company_ws_seq


# per-project 轻量工厂——CommunicationManager/ThreadManager 底层已参数化
# project_id（<workspace>/.bandi-comms/<project>/<session>），此处按项目懒建缓存路由；
# company 项目复用 dispatcher 单例（既有行为零变化）。

_project_comms: dict[str, Any] = {}
_project_threads: dict[str, Any] = {}
_project_runtime_lock = threading.Lock()


def _company_workspace() -> str:
    """功能：company 通信根（与 dispatcher 装配同一口径，.bandi-comms 的 workspace）。"""
    return str(Path.home() / ".bandi" / "company")


def _get_project_comms(project_id: str) -> Any:
    """功能：按项目获取通信管理器（company 复用 dispatcher 单例；其他项目懒建缓存）。"""
    if project_id == "company":
        return _get_company_dispatcher().communication
    dispatcher = _get_company_dispatcher()
    with _project_runtime_lock:
        comms = _project_comms.get(project_id)
        if comms is None:
            from bandi.company.communication import CommunicationManager

            comms = CommunicationManager(
                workspace=_company_workspace(),
                project_id=project_id,
                session_id="default",
                org_engine=getattr(dispatcher, "org_engine", None),
                on_message_sent=partial(_broadcast_company_message, project_id=project_id),
            )
            _project_comms[project_id] = comms
        return comms


def _get_project_threads(project_id: str) -> Any:
    """功能：按项目获取线程管理器（company 复用 dispatcher 单例；其他项目懒建 + hydrate）。"""
    if project_id == "company":
        return getattr(_get_company_dispatcher(), "threads", None)
    with _project_runtime_lock:
        threads = _project_threads.get(project_id)
        if threads is None:
            from bandi.company.discussion_thread import ThreadManager

            threads = ThreadManager(
                workspace=_company_workspace(),
                project_id=project_id,
                session_id="default",
                on_message=partial(_broadcast_thread_message, project_id=project_id),
            )
            threads.hydrate()
            _project_threads[project_id] = threads
        return threads


def _broadcast_company_message(meta: dict[str, Any], project_id: str = "company") -> None:
    """on_message_sent 回调——向全部 /ws/company 在线连接广播新消息（帧带 project_id）。"""

    async def _push() -> None:
        frame = _record_company_event(
            {"type": "message", "data": {**meta, "project_id": project_id}}
        )
        for ws in list(_company_ws_clients):
            try:
                await _company_ws_send(ws, frame)
            except Exception:  # noqa: BLE001 — 单连接失败不阻断其余广播
                pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 无运行 loop（同步上下文）——广播跳过，不阻塞发送主链
    loop.create_task(_push())


def _broadcast_thread_message(event: dict[str, Any], project_id: str = "company") -> None:
    """ThreadManager.on_message 回调——生产主链路线程事件（thread_created/thread_message）
    向全部 /ws/company 在线连接实时广播（ThreadView 实时气泡的数据源；帧带 project_id）。"""

    async def _push() -> None:
        kind = event.get("type")
        payload = {**event, "project_id": project_id}
        if kind in ("thread_created", "thread_message"):
            thread_id = str(event.get("thread_id") or "")
            manager = (
                _get_company_dispatcher().threads
                if project_id == "company"
                else _project_threads.get(project_id)
            )
            thread = manager.get_thread(thread_id) if manager is not None and thread_id else None
            session_id = str(getattr(thread, "session_id", "") or "") if thread is not None else ""
            if session_id:
                payload["session_id"] = session_id
            frame = {"type": kind, "data": payload}
        else:
            frame = {"type": "thread_event", "data": payload}
        frame = _record_company_event(frame)
        for ws in list(_company_ws_clients):
            try:
                await _company_ws_send(ws, frame)
            except Exception:  # noqa: BLE001 — 单连接失败不阻断其余广播
                pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 无 running loop（同步上下文）——跳过，不阻塞线程写主链
    loop.create_task(_push())


def _broadcast_interaction_frame(item: Any) -> None:
    """员工交互帧（多问卡/权限卡）→ /ws/company 广播——用户在发起会话现场看到决策卡。

    帧携带登记会话 id（HTTPUserInteraction 的 session_id = 发起会话 origin_session_id），
    前端按当前会话过滤渲染；进回放缓冲（业务帧口径：断线期间的决策卡重连后补齐）。
    无 running loop 时跳过（不阻塞执行主链）；单连接失败不阻断其余广播。
    """

    async def _push() -> None:
        frame = _record_company_event({"type": "interaction", "data": _interaction_frame(item)})
        for ws in list(_company_ws_clients):
            try:
                await _company_ws_send(ws, frame)
            except Exception:  # noqa: BLE001 — 单连接失败不阻断其余广播
                pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 无运行 loop（同步上下文）——跳过，不阻塞员工执行主链
    loop.create_task(_push())


_SLOT_PLACEHOLDERS: tuple[str, ...] = ("【目标市场】", "【品类】")


def _slot_question_payload() -> dict[str, Any]:
    """功能：占位符槽位的系统预置问题（目标市场/品类/候选/生意模式）。"""
    return {
        "questions": [
            {
                "id": "market",
                "question": "目标市场按哪个走？（决定认证、渠道与价格带）",
                "choices": [
                    "美国（Amazon 主渠道 + Chewy/独立站对照）",
                    "欧洲（德/法/英，需 CE/REACH/WEEE）",
                    "日本（需 PSE/TELEC，价格带下移）",
                    "英语市场打包（US+CA+UK）",
                ],
            },
            {
                "id": "category",
                "question": "品类口径怎么定？",
                "choices": [
                    "宠物智能硬件宽口径（联网/App/含电控）",
                    "只做猫用智能硬件",
                    "只做犬用智能硬件",
                    "猫狗通用喂养类",
                ],
            },
            {
                "id": "candidates",
                "question": "候选方向池？",
                "choices": [
                    "A 智能猫砂盆 / B 智能喂食·饮水 / C 智能摄像头+陪伴",
                    "只做喂养类（喂食+饮水）",
                    "我另外指定（在下方输入）",
                ],
            },
            {
                "id": "business",
                "question": "生意模式与首年门槛？",
                "choices": [
                    "Amazon 自有品牌 DTC，首年营收 ≥ 30 万美元，单件毛利 ≥ 45%",
                    "独立站+Amazon 双渠道，门槛同上",
                    "代工/贴牌为主，毛利目标 30%",
                    "小批量验证（首单 ≤ 1,000 件），暂不设营收门槛",
                ],
            },
        ]
    }


def _attach_slot_gate(created: list[Any], *, message: str, session_id: str) -> None:
    """功能：占位符槽位门——需求含未填槽位时预建多问卡并挂起执行卡。

    系统在卡上登记 slot_gate，执行前等待用户答复并把口径写回任务描述
    （不依赖模型自觉发起提问）。
    """
    if not created:
        return
    if not any(token in message for token in _SLOT_PLACEHOLDERS):
        return
    for item in created:
        try:
            meta = dict(item.metadata or {})
            if meta.get("slot_gate"):
                continue
            extension = _get_interaction_store().register(
                session_id=session_id,
                kind="input",
                payload=_slot_question_payload(),
                ttl_seconds=1800.0,
            )
            meta["slot_gate"] = extension.interaction_id
            item.metadata = meta
            _get_company_dispatcher().store.save(item)
            _broadcast_interaction_frame(extension)
        except Exception as exc:  # noqa: BLE001 — 单卡门失败不阻断建卡
            logger.debug("slot gate attach failed for %s: %s", getattr(item, "id", ""), exc)


async def _slot_answer_provider(
    interaction_id: str, timeout: float = 1800.0
) -> dict[str, Any] | None:
    """功能：等待系统槽位多问卡的答复（返回 answers 映射）。"""
    try:
        result = await _get_interaction_store().get_decision(interaction_id, timeout=timeout)
    except Exception:  # noqa: BLE001 — 等待失败按未答复处理
        return None
    return _parse_slot_answers(result)


def _parse_slot_answers(result: Any) -> dict[str, Any] | None:
    """功能：槽位答复归一——clarify 通道 wire 为 JSON 字符串（前端 stringify 的 answers
    映射），同一入口兼容 dict 直传与 {answers: {...}} 包裹形态；空/坏/非映射一律 None。"""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(result, dict):
        return None
    answers = result.get("answers")
    return answers if isinstance(answers, dict) else result


def _employee_interaction_provider(session_id: str) -> Any:
    """员工交互通道工厂（seat_executor 的 interaction_provider）——每轮执行按会话构建。"""
    from bandi.core.user_interaction import HTTPUserInteraction

    return HTTPUserInteraction(
        _get_interaction_store(),
        session_id=session_id,
        on_interaction=_broadcast_interaction_frame,
    )


def _progress_scope(frame: dict[str, Any]) -> str:
    """功能：过程帧的项目作用域（与前端 `normalizeProjectScope()` 同口径：空值回落 "company"）。

    两侧必须一致：前端订阅用归一化后的 scope，后端比较也必须归一化，否则空 project 帧
    会被判成「不在订阅集合里」而静默丢弃。
    """
    data = frame.get("data")
    project = ""
    if isinstance(data, dict):
        project = str(data.get("project_id") or "").strip()
    return project or "company"


def _progress_event_type(frame: dict[str, Any]) -> str:
    """功能：过程帧内层事件类型（`{data: {event: {type}}}`）。"""
    data = frame.get("data")
    event = data.get("event") if isinstance(data, dict) else None
    return str((event or {}).get("type") or "") if isinstance(event, dict) else ""


def _broadcast_thread_progress(frame: dict[str, Any]) -> None:
    """插队批：员工执行过程帧（EmployeeProgressHandler 完整帧）→ /ws/company 广播。

    帧结构 = {"type": "thread_progress", "data": {project_id, thread_id, role_id, event}}
    （外层与归属由 EmployeeProgressHandler 构造——本函数不得二次包装）；
    前端按 project_id/thread_id/role_id 过滤消费。单连接失败不阻断其余广播；
    无 running loop 时跳过（不阻塞执行主链）。

    **回放缓冲策略**：
    - **非终态**（thinking/tool/content 等高频中间态）**不进**缓冲——一次执行数百帧
      与 `thread_message`/任务卡共用 200 条有界窗口会把未 ack 的**业务帧**挤出去；
      中间态本身断线不可追溯（无补发价值）。
    - **终态**（`done`/`error`）**进**缓冲——每次执行仅 1 帧，占用可忽略；而它决定前端
      「这次执行结束了吗」：断线期间结束的那次执行若不补发终态，客户端只能停在「执行中」
      （前端 `` 的断线结算是近似兜底，终态补发才能精确收敛）。

    **按项目定向**（只作用于过程帧；业务帧恒广播）：
    - 未订阅的连接（`_company_ws_scopes` 无记录）→ **fail-open 全发**（兼容旧包）；
    - 已订阅 → 只发 scope ∈ 订阅集合的连接。
    """

    async def _push() -> None:
        # 终态帧入回放缓冲（拿到带 seq 的新帧）；非终态原帧直发
        is_terminal = _progress_event_type(frame) in ("done", "error")
        payload = _record_company_event(frame) if is_terminal else frame
        scope = _progress_scope(payload)
        for ws in list(_company_ws_clients):
            scopes = _company_ws_scopes.get(id(ws))
            if scopes is not None and scope not in scopes:
                continue
            try:
                await _company_ws_send(ws, payload)
            except Exception:  # noqa: BLE001 — 单连接失败不阻断其余广播
                pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_push())


def _project_member_roles(project_id: str, org_engine: Any | None) -> list[Any]:
    """功能：项目团队成员角色列表（与 /api/company/projects 同源动态解析）。"""
    if org_engine is None:
        return []
    try:
        members = _get_project_store().resolve_members(project_id, org_engine)
    except Exception:  # noqa: BLE001 项目成员解析失败返回空
        return []
    roles: list[Any] = []
    for member in members or []:
        if str(getattr(member, "employee_status", "") or "") != "active":
            continue
        role_id = str(getattr(member, "role_id", "") or "").strip()
        role = org_engine.get_role(role_id) if role_id else None
        if role is not None:
            roles.append(role)
    return roles


def _is_secretary_session(store: Any, session_id: str) -> bool:
    """功能：会话是否秘书来源（已落库会话 metadata.origin 判定）。"""
    if not session_id:
        return False
    try:
        record = store.get_session(session_id)
    except Exception:  # noqa: BLE001 —— 查询失败按非秘书处理（接管判定不误伤）
        return False
    if not record:
        return False
    raw = record.get("metadata") or "{}"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return False
    return isinstance(raw, dict) and str(raw.get("origin") or "") == "secretary"


def _project_access_block(project_id: str, session_id: str = "", target_role: str = "") -> str:
    """功能：群聊访问守卫——已删除员工 / 已解散且无 active 团队的群聊 fail-closed。"""
    role_key = str(target_role or "").strip()
    if role_key and _employee_tombstones().get(role_key) is not None:
        return "EMPLOYEE_RETIRED"
    project_key = str(project_id or "").strip()
    if not project_key or project_key == "company":
        return ""
    try:
        project = _get_project_store().get_project(project_key)
    except Exception:
        logger.debug("project access guard skipped (project=%s)", project_key, exc_info=True)
        return ""
    if project is None:
        return ""
    if (
        not project.team_ids
        and project.dissolved_team_ids
        and not str(target_role or "").strip()
        and not _is_secretary_session(_get_store(), session_id)
    ):
        return "TEAM_DISSOLVED"
    return ""


async def _handle_employee_dm(
    websocket: WebSocket,
    payload: dict[str, Any],
    *,
    session_id: str,
    project_id: str,
    mode: str,
    org_engine: Any,
    session_title: str | None,
    ws_handler: _WSStreamHandler,
    target_role: str,
) -> None:
    """功能：员工单聊接管——每会话独立线程/工作项，员工回复写回本会话。"""
    from bandi.api.company_chat import _resolve_registered_role, handle_chat_message
    from bandi.core.message import Message, MessageRole
    from bandi.utils.errors import error_text

    message = _with_attachment_notes(
        str(payload.get("message") or "").strip(), payload.get("attachments"), project_id
    )
    if not message:
        await ws_handler.on_error(session_id, "message is required")
        return
    store = _get_store()
    dispatcher = _get_company_dispatcher()
    try:
        role_id = _resolve_registered_role(target_role, org_engine)
    except ValueError as exc:
        await ws_handler.on_error(session_id, str(exc))
        return
    employees = org_engine.list_employees(role_id=role_id) if org_engine is not None else []
    employee = employees[0] if employees else None
    if employee is None:
        await ws_handler.on_error(session_id, f"employee not found for role: {role_id}")
        return
    if store.get_session(session_id) is None:
        store.create_session(
            session_id=session_id,
            title=(session_title or message[:40])[:120],
            mode=mode,
            project_id=project_id or None,
            origin="employee_dm",
            target_role=role_id,
            employee_id=str(employee.employee_id or ""),
            employee_name=str(employee.name or ""),
        )
    store.append_message(session_id, Message(role=MessageRole.USER, content=message))
    try:
        threads = _get_project_threads(project_id or "company")
        existing = threads.find_by_session(session_id) if threads is not None else None
        created = handle_chat_message(
            content=message,
            sender_role="user",
            communication=_get_project_comms(project_id or "company"),
            store=dispatcher.store,
            org_engine=org_engine,
            thread_manager=threads,
            thread_id=existing.thread_id if existing is not None else None,
            target_role=role_id,
            project_id=project_id,
            target_work_item_id="",
            session_id=session_id,
            mode=mode,
            send_dm=True,
            intent=_classify_chat_intent(message),
        )
        if created:
            _bind_session_thread(
                session_id, str((created[0].metadata or {}).get("thread_id") or "")
            )
            dispatcher.signal_dispatcher_wake()
    except Exception as exc:  # noqa: BLE001 —— 单聊落盘失败显式回 error 帧
        await ws_handler.on_error(session_id, f"employee dm failed: {error_text(exc)}")
        return
    await ws_handler.on_done(session_id, "employee_dm_routed", 0, 0)


async def _handle_team_chat_takeover(
    websocket: WebSocket,
    payload: dict[str, Any],
    *,
    session_id: str,
    project_id: str,
    mode: str,
    org_engine: Any,
    session_title: str | None,
    ws_handler: _WSStreamHandler,
) -> None:
    """功能：团队会话消息接管——落库用户消息后按公司聊天路由建卡/商讨；
    员工产出稍后经线程回写本会话，此处回终帧结束「正在回复中」。"""
    from bandi.api.company_chat import (
        extract_mentions,
        handle_chat_message,
        unresolved_mentions,
    )
    from bandi.company.group_chat_protocol import MentionError, mention_error_text
    from bandi.core.message import Message, MessageRole
    from bandi.utils.errors import error_text

    message = _with_attachment_notes(
        str(payload.get("message") or "").strip(), payload.get("attachments"), project_id
    )
    store = _get_store()
    if store.get_session(session_id) is None:
        store.create_session(
            session_id=session_id,
            mode=mode,
            project_root=_validated_session_project_root(payload.get("project_root"), project_id),
            project_id=project_id,
            title=(session_title or message[:40])[:120],
        )
    store.append_message(session_id, Message(role=MessageRole.USER, content=message))
    # 意图四态：confirm=顶层接单出计划 / adjust=调整返工（注入中断点）/
    # query=咨询问答（不出发计划）/ task=执行指令。@ 显式提及时不分类（用户定向为准）。
    # @ 只决定「问谁」，不决定「是否执行」——@ 员工问句仍按 query 分类（不建执行计划卡），
    # 派活/执行指令才落 task
    intent = _classify_chat_intent(message)
    try:
        member_roles = _project_member_roles(project_id, org_engine)
        member_role_ids = {str(role.id) for role in member_roles}
        default_role = ""
        extra_targets: list[str] = []
        if not extract_mentions(message):
            # 路由策略：
            # - confirm（确认执行）/ 想法陈述（我想做 X）：顶层角色单项承接——单卡；
            # - query（咨询问题）：内容路由指定最对口角色应答（无果顶层兜底）——单卡，
            #   让「概念图风格」归设计、「数据分析」归数据（问对人）；
            # - task/adjust：顶层承接 + 内容路由补充（多角色协作）
            is_idea = any(pattern in message for pattern in _IDEA_PATTERNS)
            if intent == "confirm" or is_idea:
                default_role = _pick_top_role(member_roles)
            elif intent == "query":
                routed = await _content_route_targets(message, project_id, org_engine)
                default_role = routed[0] if routed else _pick_top_role(member_roles)
            else:
                # task：顶层单卡承接，由管理者卡按组织白名单派发串行链；
                # 不做内容路由并行建卡——否则下游单卡会越过顶层直接开跑。
                default_role = _pick_top_role(member_roles)
        # 调整类消息的承接上下文——路由出目标角色后按「角色+关键词」挑相关卡注入
        # （应带对口卡而非恰好最新的卡）
        if intent == "adjust":
            context_target = (extra_targets[0] if extra_targets else "") or default_role
            context_note = _recent_work_context_note(message, target_role=context_target)
            if context_note:
                message = f"{context_note}\n\n【用户调整要求】{message}"
        created = handle_chat_message(
            content=message,
            sender_role="user",
            communication=_get_project_comms(project_id),
            store=_get_company_dispatcher().store,
            org_engine=org_engine,
            thread_manager=_get_project_threads(project_id),
            thread_id=None,
            target_role=None,
            project_id=project_id,
            target_work_item_id="",
            session_id=session_id,
            mode=mode,
            default_role=default_role,
            extra_targets=extra_targets,
            member_role_ids=member_role_ids,
            intent=intent,
            group_round_service=getattr(_get_company_dispatcher(), "group_rounds", None),
        )
        _attach_slot_gate(created, message=message, session_id=session_id)
        if created:
            # 建卡即唤醒调度器：否则新卡要等 run_forever 的 30s 超时轮询
            _get_company_dispatcher().signal_dispatcher_wake()
        if created and not str(payload.get("target_role") or "").strip():
            _bind_session_thread(
                session_id, str((created[0].metadata or {}).get("thread_id") or "")
            )
        if not created:
            missing = unresolved_mentions(message, org_engine, member_role_ids)
            if missing:
                await ws_handler.on_error(
                    session_id,
                    "未找到被 @ 的角色："
                    + ", ".join(f"@{m}" for m in missing)
                    + "。请先实例化团队模板或由秘书创建该角色后重试。",
                )
                return
    except MentionError as exc:
        await ws_handler.on_error(session_id, mention_error_text(exc))
        return
    except Exception as exc:  # noqa: BLE001 —— 落盘失败显式回 error 帧（不静默、不建任务）
        await ws_handler.on_error(session_id, f"team route failed: {error_text(exc)}")
        return
    await ws_handler.on_done(session_id, "team_routed", 0, 0)


def _pick_top_role(member_roles: list[Any]) -> str:
    """功能：群聊默认回复人——团队首位在职成员。"""
    return str(member_roles[0].id) if member_roles else ""


# 功能：内容级路由最少字符数——过短（问候）直接走默认承接，不烧 LLM
_ROUTE_MIN_CHARS = 6

# 功能：群聊意图判定词表（四态）——
# confirm=确认执行（顶层接单出计划）/ adjust=调整返工 / query=咨询问答（不出发计划）/ task=执行指令。
_CONFIRM_PATTERNS = (
    "就这个方案",
    "就这个吧",
    "就它了",
    "开始吧",
    "确认执行",
    "按这个来",
    "就这么办",
    "动手吧",
    "执行吧",
    "可以，执行",
    "确定执行",
    "开始执行",
    "照这个做",
)
_ADJUST_PATTERNS = ("调整", "修改", "返工", "重做", "改一下", "改成", "换成", "重来", "再改")
_QUERY_PATTERNS = (
    "？",
    "?",
    "吗",
    "呢",
    "什么",
    "怎么",
    "如何",
    "为什么",
    "该用",
    "应该用",
    "推荐",
)
# 功能：想法陈述词——「我想做 X」是讨论而非执行指令；归入咨询态由顶层承接应答，
# 待用户「开始吧」确认后才出计划。
_IDEA_PATTERNS = ("我想", "我要", "打算", "计划做", "准备做", "考虑做", "琢磨")


def _classify_chat_intent(text: str) -> str:
    """功能：群聊消息意图分类——confirm / adjust / query / task（默认）。
    确认词仅在短句（≤24 字）内生效，防长文误判。"""
    raw = (text or "").strip()
    if len(raw) <= 24 and any(pattern in raw for pattern in _CONFIRM_PATTERNS):
        return "confirm"
    if any(pattern in raw for pattern in _ADJUST_PATTERNS):
        return "adjust"
    if any(pattern in raw for pattern in _IDEA_PATTERNS):
        return "query"
    if any(pattern in raw for pattern in _QUERY_PATTERNS):
        return "query"
    return "task"


def _recent_work_context_note(
    message: str = "", target_role: str = "", limit_chars: int = 280
) -> str:
    """功能：调整类消息的承接上下文注记（负责人+原任务+产出摘要），供被调整角色
    对齐目标。选取顺序（对口卡优先——「概念图调整」命中概念图卡而非恰好最新的卡）：
    ① 目标角色的卡中与消息关键词重叠最高者；② 目标角色的最近卡；③ 全局最近卡。
    失败返回空串（best-effort，不影响主链）。"""
    try:
        store = _get_company_dispatcher().store
        items = [
            item
            for item in store.list_all()
            if str(getattr(item, "assignee_role", "") or "").strip()
        ]
        if not items:
            return ""
        items.sort(key=lambda item: str(getattr(item, "updated_at", "") or ""), reverse=True)
        target = str(target_role or "").strip()
        scoped = [item for item in items if str(getattr(item, "assignee_role", "")) == target]
        pool = scoped or items
        picked = pool[0]
        keywords = [token for token in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", message or "")][
            :8
        ]
        if keywords:
            best_score = 0
            for candidate in pool[:12]:
                haystack = (
                    f"{getattr(candidate, 'title', '') or ''} "
                    f"{getattr(candidate, 'description', '') or ''}"
                )
                score = sum(1 for token in keywords if token in haystack)
                if score > best_score:
                    best_score = score
                    picked = candidate
        runtime = getattr(picked, "runtime_metadata", None) or {}
        output = str(runtime.get("_last_output") or "").strip()
        desc = str(getattr(picked, "description", "") or getattr(picked, "title", "") or "").strip()
        parts = [f"【承接上下文·此前任务】负责人：{getattr(picked, 'assignee_role', '')}"]
        if desc:
            parts.append(f"原任务（摘要）：{desc[:120]}")
        if output:
            parts.append(f"最近产出（摘要）：{output[:limit_chars]}")
        return "\n".join(parts)
    except Exception:  # noqa: BLE001 —— 上下文注记 best-effort，不影响主链
        return ""


async def _content_route_targets(
    content: str, project_id: str, org_engine: Any | None
) -> list[str]:
    """功能：无 @ 消息的内容级路由——LLM 从项目成员角色中选出应回应的角色（≤2）。

    fail-open：无模型/超时/解析失败返回空列表（调用方回落团队顶部角色默认承接）。
    """
    text = (content or "").strip()
    if len(text) < _ROUTE_MIN_CHARS:
        return []
    members = _project_member_roles(project_id, org_engine)
    if len(members) < 2:
        return []
    listing = "\n".join(
        f"- {role.id}（{str(getattr(role, 'name', '') or role.id)}）" for role in members
    )
    prompt = (
        "你是团队群聊的消息路由器。根据用户消息，从下列角色中选出最应该回应的角色"
        "（最多 2 个；纯寒暄、无明确指向或无人适合时返回空数组）。\n"
        f"角色列表：\n{listing}\n\n用户消息：{text[:500]}\n\n"
        '只输出 JSON：{"roles": ["角色id", ...]}'
    )
    try:
        import json as _json
        import re as _re

        from bandi.agent.factory import build_provider
        from bandi.core.config import BandiConfig
        from bandi.llm.accounted import accounted_complete

        provider = build_provider(BandiConfig.load())
        resp = await accounted_complete(
            provider,
            [{"role": "user", "content": prompt}],
            label="group_router",
            session_id="group-router",
        )
        raw = resp.content if hasattr(resp, "content") else str(resp)
        match = _re.search(r"\{.*\}", raw or "", _re.DOTALL)
        if match is None:
            return []
        data = _json.loads(match.group(0))
        wanted = [str(item).strip() for item in (data.get("roles") or []) if str(item).strip()]
        valid = {str(role.id).lower() for role in members}
        by_id = {str(role.id).lower(): role for role in members}
        selected = [item for item in wanted if item.lower() in valid][:2]

        def _hierarchy_rank(role_id: str) -> int:
            # 功能：组织层级排序键——final_decider 或无上级（最上级）排前（0），其余（1）；
            # 同级保持 LLM 给序（稳定排序），保证最上级先行回应。
            role = by_id.get(role_id.lower())
            if role is None:
                return 1
            if bool(getattr(role, "is_final_decider", False)):
                return 0
            return 0 if getattr(role, "reports_to", None) in (None, "") else 1

        selected.sort(key=_hierarchy_rank)
        return selected
    except Exception:
        logger.debug("content route failed", exc_info=True)
        return []


@app.websocket("/ws/company")
async def company_ws_route(websocket: WebSocket) -> None:
    """群聊面板端点（前置）：接收群聊消息（落盘 + @mention 触发 work_item）
    并向全部在线连接广播新消息。"""
    if _API_KEY:
        token = _ws_credential(websocket)
        if not (_bearer_token_ok(token) or _signed_token_ok(token)):
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="Unauthorized")
            return
    else:
        client_host = websocket.client.host if websocket.client else ""
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="Unauthorized")
            return
    if not _ws_handshake_allowed(websocket):
        await websocket.close(code=1008, reason="Origin not allowed")
        return
    await websocket.accept()
    with _company_ws_lock:
        _company_ws_clients.add(websocket)
    _company_ws_send_locks[id(websocket)] = asyncio.Lock()
    # -能力握手——连接建立即声明本端能力（**首帧**，早于任何客户端上行处理）
    await _company_ws_send(
        websocket,
        {"type": "hello", "data": {"capabilities": list(_COMPANY_WS_CAPABILITIES)}},
    )
    try:
        while True:
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, ValueError):
                await _company_ws_send(websocket, {"type": "error", "data": "invalid JSON"})
                continue
            # 10.9：断线重连协议——resume 补发缓冲事件；ack 为客户端确认（记录语义暂无）
            message_type = str(payload.get("type") or "")
            if message_type == "ping":
                await _company_ws_send(websocket, {"type": "pong", "ts": payload.get("ts")})
                continue
            if message_type == "resume":
                try:
                    last_seq = int(payload.get("last_seq") or 0)
                except (TypeError, ValueError):
                    last_seq = 0
                missed = _replay_company_events(last_seq)
                for frame in missed:
                    await _company_ws_send(websocket, frame)
                await _company_ws_send(
                    websocket,
                    {
                        "type": "resumed",
                        "replayed": len(missed),
                        "last_seq": _company_ws_last_seq(),
                    },
                )
                continue
            if message_type == "ack":
                try:
                    ack_seq = int(payload.get("seq") or 0)
                except (TypeError, ValueError):
                    ack_seq = 0
                _record_company_ack(id(websocket), ack_seq)
                _trim_company_events()
                continue
            if message_type == "subscribe":
                # 合法列表 → 记录订阅集合（可空 = 什么都不收）；格式非法 → 取消订阅回 fail-open。
                raw_projects = payload.get("project_ids")
                if isinstance(raw_projects, list):
                    scopes = {str(item).strip() for item in raw_projects if str(item).strip()}
                    # 上限保护：防超长列表把 per-connection 集合撑爆（正常只需 1~数个）
                    if len(scopes) > 50:
                        scopes = set(list(scopes)[:50])
                    _company_ws_scopes[id(websocket)] = scopes
                else:
                    _company_ws_scopes.pop(id(websocket), None)
                    scopes = None
                await _company_ws_send(
                    websocket,
                    {
                        "type": "subscribed",
                        "project_ids": sorted(scopes) if scopes is not None else [],
                        "scoped": scopes is not None,
                    },
                )
                continue
            content = str(payload.get("content") or "").strip()
            sender = str(payload.get("sender_role") or "user")
            thread_id = str(payload.get("thread_id") or "").strip() or None
            project_id = str(payload.get("project_id") or "").strip() or "company"
            session_id = str(payload.get("session_id") or "").strip()
            if session_id and _is_reserved_session_id(session_id):
                if _get_store().get_session(session_id) is None:
                    await _company_ws_send(
                        websocket, {"type": "error", "data": "session_id is reserved"}
                    )
                    continue
            target_role = str(payload.get("target_role") or "").strip() or None
            # 群聊/单聊统一模式：plan=全员规划商讨轮（前端会话级状态携带）
            chat_mode = str(payload.get("mode") or "").strip().lower()
            # 带目标卡 = 用户对话修正已派发任务（不新建卡，受控回转原卡）
            target_work_item_id = str(payload.get("target_work_item_id") or "").strip()
            # v5（轮三-5）：对话附件（单聊/群聊）——upload_id 解析为文本注记拼入内容；
            # 纯附件消息（无文字）也放行（注记非空）
            content = _with_attachment_notes(content, payload.get("attachments"), project_id)
            if not content:
                await _company_ws_send(websocket, {"type": "error", "data": "content required"})
                continue
            # 项目路由 fail-closed——非 company 项目必须先有项目记录
            project = None
            if project_id != "company":
                from bandi.company.projects import ProjectError

                try:
                    project = _get_project_store().get_project(project_id)
                except ProjectError as exc:
                    await _company_ws_send(
                        websocket, {"type": "error", "data": f"project not found: {exc}"}
                    )
                    continue
                if project is None:
                    await _company_ws_send(
                        websocket, {"type": "error", "data": f"project not found: {project_id}"}
                    )
                    continue
            access_block = _project_access_block(project_id, session_id, target_role or "")
            if access_block:
                await _company_ws_send(
                    websocket,
                    {"type": "error", "code": access_block, "data": access_block},
                )
                continue
            from bandi.api.company_chat import (
                extract_mentions,
                handle_chat_message,
                unresolved_mentions,
            )
            from bandi.company.group_chat_protocol import MentionError, mention_error_text

            org_engine = getattr(_get_company_dispatcher(), "org_engine", None)
            try:
                dispatcher = _get_company_dispatcher()
                comms = _get_project_comms(project_id)
                threads = _get_project_threads(project_id)
                # 项目成员角色集合：默认承接 + @ 显示名消歧共用（同源一次解析）
                member_roles = _project_member_roles(project_id, org_engine)
                member_role_ids = {str(role.id) for role in member_roles}
                # 无 @ 消息兜底：顶层角色单卡承接（由管理者卡派发串行链；
                # 不做内容路由并行建卡，与 /api/agent/stream 同口径）
                default_role = ""
                extra_targets: list[str] = []
                if not target_role and not target_work_item_id and not extract_mentions(content):
                    default_role = _pick_top_role(member_roles)
                created = handle_chat_message(
                    content=content,
                    sender_role=sender,
                    communication=comms,
                    store=dispatcher.store,
                    org_engine=org_engine,
                    thread_manager=threads,
                    thread_id=thread_id,
                    target_role=target_role,
                    project_id=project_id,
                    target_work_item_id=target_work_item_id,
                    session_id=session_id,
                    mode=chat_mode,
                    default_role=default_role,
                    extra_targets=extra_targets,
                    member_role_ids=member_role_ids,
                    group_round_service=getattr(dispatcher, "group_rounds", None),
                )
                _attach_slot_gate(
                    created,
                    message=content,
                    session_id=str(payload.get("session_id") or "").strip(),
                )
                active_thread_id = None
                if thread_id and threads is not None:
                    active_thread_id = thread_id
                elif created and threads is not None:
                    bound = [
                        c.metadata.get("thread_id") for c in created if c.metadata.get("thread_id")
                    ]
                    if bound:
                        active_thread_id = bound[0]
                # 群聊窗口绑定：会话 metadata 记线程 id（删除会话时级联删线程）
                if session_id and active_thread_id:
                    _bind_session_thread(session_id, active_thread_id)
                if created:
                    # 建卡即唤醒调度器（去掉最多 30s 空等）
                    dispatcher.signal_dispatcher_wake()
            except MentionError as exc:
                await _company_ws_send(
                    websocket,
                    {
                        "type": "error",
                        "code": exc.code,
                        "data": mention_error_text(exc),
                    },
                )
                continue
            except Exception as e:  # noqa: BLE001 — 落盘失败须显式回 error 帧（不静默、不建任务）
                from bandi.utils.errors import error_text

                await _company_ws_send(
                    websocket,
                    {"type": "error", "data": f"message not persisted: {error_text(e)}"},
                )
                continue
            # @ 未注册角色 → 显式提示（不静默变广播）
            missing_mentions: list[str] = []
            if not created and not target_role and not target_work_item_id:
                missing_mentions = unresolved_mentions(content, org_engine, member_role_ids)
                if missing_mentions:
                    await _company_ws_send(
                        websocket,
                        {
                            "type": "error",
                            "data": (
                                "未找到被 @ 的角色："
                                + ", ".join(f"@{m}" for m in missing_mentions)
                                + "。请先实例化团队模板或由秘书创建该角色后重试。"
                            ),
                        },
                    )
            # 线程事件经 ThreadManager.on_message → _broadcast_thread_message 实时广播
            # （thread_created/thread_message 覆盖全部在线连接含发起者），此处不重复单发帧；
            thread_meta = None
            if active_thread_id and threads is not None:
                t = threads.get_thread(active_thread_id)
                if t is not None:
                    thread_meta = {
                        "thread_id": t.thread_id,
                        "participants": list(t.participants),
                        "parent_task_id": t.parent_task_id,
                    }
            await _company_ws_send(
                websocket,
                {
                    "type": "ok",
                    "created": [c.id for c in created],
                    "thread_id": active_thread_id,
                    "thread": thread_meta,
                    "project_id": project_id,
                },
            )
    except WebSocketDisconnect:
        pass
    finally:
        with _company_ws_lock:
            _company_ws_clients.discard(websocket)
        _company_ws_send_locks.pop(id(websocket), None)
        _company_ws_scopes.pop(id(websocket), None)
        # 10.9：断开清理 ack 位点，并在剩余客户端全部确认时补一次裁剪
        with _company_ws_history_lock:
            _company_ws_acks.pop(id(websocket), None)
        if _company_ws_clients:
            _trim_company_events()


# 后端能力全现成（OrgEngine/TalentMarket/WorkItemStore/CommunicationManager）
# 本区补 REST 面（员工 CRUD / 组织配置 / WorkItem / 人才市场 / 群聊读取）
# 并懒装配 TalentMarket 到 dispatcher（定义装配点）。


def _org_api_dispatcher() -> CompanyDispatcher:
    """返回装配了 TalentMarket 的 dispatcher（懒挂属性）。

    读面对齐 org.json——组织本体可能被 CLI / 另一后端 / 桌面壳写入（团队实例化
    即典型路径），而 dispatcher 的 org_engine 是启动时的内存快照；写前合并只在**写**
    路径生效，故读面长期陈旧，「团队已组建」校验会把盘上明明存在的团队判成未组建/
    不存在。这里在每次取用前按文件指纹增量对齐（未变时仅一次 stat）。
    """
    dispatcher = _get_company_dispatcher()
    from bandi.company.org_persistence import refresh_org_from_disk

    refresh_org_from_disk(dispatcher.org_engine, dispatcher.recruiter)
    if getattr(dispatcher, "talent", None) is None:
        from bandi.company.talent_market import TalentMarket

        dispatcher.talent = TalentMarket()
        dispatcher.talent.bind_org_engine(dispatcher.org_engine)
    return dispatcher


_team_store: Any | None = None


def _get_team_store() -> Any:
    """功能：团队模板库共享单例（进程级，tests monkeypatch 此函数/全局变量隔离）。"""
    global _team_store
    if _team_store is None:
        from bandi.company.team_template_store import TeamTemplateStore

        _team_store = TeamTemplateStore()
    return _team_store


class TeamInstantiateBody(BaseModel):
    team_id: str = ""
    # 功能：团队显示名（空 = 用模板名；team_id 仍作员工 id 前缀）
    team_name: str = ""


class TeamImportBody(BaseModel):
    path: str
    overwrite: bool = False


class TeamExportBody(BaseModel):
    out_path: str


@app.get("/api/company/teams")
async def company_teams_list() -> dict[str, Any]:
    """团队模板列表（内置 + 本地库；带角色计数供卡片展示）。"""
    store = _get_team_store()
    teams: list[dict[str, Any]] = []
    for template in store.list_templates():
        payload = template.model_dump()
        payload["role_count"] = len(template.roles)
        teams.append(payload)
    return {"teams": teams}


@app.get("/api/company/teams/{template_id}")
async def company_team_detail(template_id: str) -> dict[str, Any]:
    """模板详情（含角色集）。"""
    from bandi.company.team_template import TeamTemplateError

    store = _get_team_store()
    try:
        template = store.get_template(template_id)
    except TeamTemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if template is None:
        raise HTTPException(status_code=404, detail=f"team template not found: {template_id}")
    return {"team": template.model_dump()}


@app.post("/api/company/teams/{template_id}/instantiate")
async def company_team_instantiate(template_id: str, body: TeamInstantiateBody) -> dict[str, Any]:
    """一键实例化（前缀角色/员工 1:1/席位/profiles），返回报告。"""
    from dataclasses import asdict

    from bandi.company.agent_profile import AgentProfileManager
    from bandi.company.team_template import TeamTemplateError
    from bandi.company.team_template_store import build_org_from_template

    store = _get_team_store()
    try:
        template = store.get_template(template_id)
    except TeamTemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if template is None:
        raise HTTPException(status_code=404, detail=f"team template not found: {template_id}")
    if body.team_id:
        template = template.model_copy(update={"team_id": body.team_id})
    dispatcher = _get_company_dispatcher()
    try:
        report = build_org_from_template(
            template,
            dispatcher.org_engine,
            recruiter=dispatcher.recruiter,
            profile_manager=AgentProfileManager(),
        )
    except TeamTemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 否则重启后席位丢失，团队卡不可 claim）
    from bandi.company.org_persistence import save_org
    from bandi.company.team_registry import TeamRegistry

    save_org(dispatcher.org_engine, dispatcher.recruiter)
    registry = TeamRegistry()
    instantiated_id = str(getattr(report, "team_id", "") or template.template_id)
    display_name = str(body.team_name or "").strip() or template.name
    if not registry.get(instantiated_id) or display_name:
        registry.set(instantiated_id, display_name)
    registry.set_status(instantiated_id, "active")
    return {"report": asdict(report)}


@app.post("/api/company/teams/import")
async def company_team_import(body: TeamImportBody) -> dict[str, Any]:
    """从 JSON 导入模板（已存在默认 409，overwrite=true 覆盖）。"""
    from bandi.company.team_template import (
        TeamTemplateError,
        TeamTemplateLoadError,
        TeamTemplateSaveError,
    )

    store = _get_team_store()
    try:
        template = store.import_template(body.path, overwrite=body.overwrite)
    except TeamTemplateLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TeamTemplateSaveError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except TeamTemplateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"team": template.model_dump()}


@app.post("/api/company/teams/{template_id}/export")
async def company_team_export(template_id: str, body: TeamExportBody) -> dict[str, Any]:
    """导出模板 JSON（内置可直接导出分享）。"""
    from bandi.company.team_template import (
        TeamTemplateError,
        TeamTemplateNotFoundError,
        TeamTemplateSaveError,
    )

    store = _get_team_store()
    try:
        path = store.export_template(template_id, body.out_path)
    except TeamTemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TeamTemplateSaveError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except TeamTemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"path": str(path)}


@app.post("/api/company/teams/{team_id}/roles")
async def company_team_roles_add(team_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """功能：批量把角色拉入团队（已有归属时转移）。"""
    from bandi.company.org_persistence import save_org

    normalized = str(team_id).strip()
    role_ids = [str(x).strip() for x in body.get("role_ids", []) if str(x).strip()]
    if not normalized or not role_ids:
        raise HTTPException(status_code=400, detail="team_id/role_ids required")
    dispatcher = _org_api_dispatcher()
    updated: list[str] = []
    for role_id in role_ids:
        role = dispatcher.org_engine.get_role(role_id)
        if role is None:
            raise HTTPException(status_code=404, detail=f"role {role_id!r} not found")
        try:
            dispatcher.org_engine.register_role(
                role_id,
                role.name,
                role.responsibility,
                reports_to=role.reports_to,
                can_spawn=list(role.can_spawn) or None,
                tool_tags=list(role.tool_tags),
                is_final_decider=role.is_final_decider,
                team_id=normalized,
                fs_scopes=list(role.fs_scopes),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        updated.append(role_id)
    save_org(dispatcher.org_engine, dispatcher.recruiter)
    from bandi.company.team_registry import TeamRegistry

    TeamRegistry().set_status(normalized, "active")
    return {"team_id": normalized, "role_ids": updated}


@app.post("/api/company/teams/custom")
async def company_team_create_custom(body: dict[str, Any]) -> dict[str, Any]:
    """功能：创建自定义团队（命名 + 至少两名员工 + 上下级）。"""
    import re
    import uuid

    from bandi.company.org_persistence import save_org
    from bandi.company.team_registry import TeamRegistry

    name = str(body.get("name") or "").strip()
    members = body.get("members") or []
    if not name:
        raise HTTPException(status_code=400, detail="团队名称必填")
    if not isinstance(members, list) or len(members) < 2:
        raise HTTPException(status_code=400, detail="至少需要两名员工")
    team_id = str(body.get("team_id") or "").strip() or f"team-{uuid.uuid4().hex[:8]}"
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", team_id):
        raise HTTPException(status_code=400, detail="team_id 仅允许字母/数字/._-")
    dispatcher = _org_api_dispatcher()
    org = dispatcher.org_engine
    employee_ids: list[str] = []
    for item in members:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="members 格式错误")
        employee_id = str(item.get("employee_id") or "").strip()
        if not employee_id:
            raise HTTPException(status_code=400, detail="employee_id required")
        if employee_id in employee_ids:
            raise HTTPException(status_code=400, detail=f"员工重复: {employee_id}")
        if org.get_employee(employee_id) is None:
            raise HTTPException(status_code=404, detail=f"employee {employee_id!r} not found")
        employee_ids.append(employee_id)

    for employee_id in employee_ids:
        employee = org.get_employee(employee_id)
        role_id = str(employee.role_id or "").strip() or employee.employee_id
        if org.get_role(role_id) is None:
            if _employee_tombstones().get(role_id) is not None:
                raise HTTPException(status_code=409, detail="IDENTITY_RESERVED")
            org.register_role(
                role_id,
                employee.name,
                employee.description,
                team_id=team_id,
            )
            employee.role_id = role_id

    parent_by_employee = {
        str(item.get("employee_id") or "").strip(): str(item.get("reports_to") or "").strip()
        for item in members
        if isinstance(item, dict)
    }
    for employee_id in employee_ids:
        employee = org.get_employee(employee_id)
        role = org.get_role(str(employee.role_id))
        if role is None:
            raise HTTPException(status_code=400, detail=f"role {employee.role_id!r} not found")
        parent_employee_id = parent_by_employee.get(employee_id, "")
        parent_role_id: str | None = None
        if parent_employee_id:
            parent = org.get_employee(parent_employee_id)
            if parent is None:
                raise HTTPException(status_code=400, detail=f"上级员工不存在: {parent_employee_id}")
            parent_role_id = str(parent.role_id or "").strip() or parent.employee_id
            if parent_role_id == role.id:
                raise HTTPException(status_code=400, detail="上级不能是自己")
        try:
            org.register_role(
                role.id,
                role.name,
                role.responsibility,
                reports_to=parent_role_id,
                can_spawn=list(role.can_spawn) or None,
                tool_tags=list(role.tool_tags),
                is_final_decider=role.is_final_decider,
                team_id=team_id,
                fs_scopes=list(role.fs_scopes),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    save_org(org, dispatcher.recruiter)
    registry = TeamRegistry()
    registry.set(team_id, name)
    registry.set_status(team_id, "active")
    return {"team_id": team_id, "name": name, "employee_ids": employee_ids}


@app.delete("/api/company/org-teams/{team_id}")
async def company_org_team_dissolve(team_id: str, actor: str = "local") -> dict[str, Any]:
    """功能：解散团队——角色转为未归属并从项目解绑，不删除角色与员工。"""
    from bandi.company.org_persistence import save_org

    normalized = str(team_id).strip()
    dispatcher = _org_api_dispatcher()
    roles = [role for role in dispatcher.org_engine.list_roles() if role.team_id == normalized]
    if not roles:
        from bandi.company.team_registry import TeamRegistry

        store = _get_project_store()
        projects = store.list_projects()
        affected = [project for project in projects if normalized in project.team_ids]
        marked = any(normalized in project.dissolved_team_ids for project in projects)
        registry = TeamRegistry()
        if affected or marked:
            detached_projects = 0
            for project in affected:
                dissolved = [*project.dissolved_team_ids]
                if normalized not in dissolved:
                    dissolved.append(normalized)
                store.save_project(
                    project.model_copy(
                        update={
                            "team_ids": [item for item in project.team_ids if item != normalized],
                            "dissolved_team_ids": dissolved,
                        }
                    )
                )
                detached_projects += 1
            if registry.get_status(normalized) != "dissolved":
                registry.set_status(normalized, "dissolved")
            return {
                "team_id": normalized,
                "detached_roles": 0,
                "detached_projects": detached_projects,
                "dissolved": True,
            }
        if registry.get_status(normalized) == "dissolved":
            return {
                "team_id": normalized,
                "detached_roles": 0,
                "detached_projects": 0,
                "dissolved": True,
            }
        raise HTTPException(status_code=404, detail=f"team {normalized!r} not found")
    role_ids = {str(role.id) for role in roles}
    running = [
        item
        for item in dispatcher.store.list_all()
        if str(getattr(item, "assignee_role", "")) in role_ids
        and _work_item_phase(item) == "running"
    ]
    if running:
        raise HTTPException(status_code=409, detail="TEAM_RUNNING")
    operations = _company_operations()
    op_id = operations.begin("dissolve", normalized, str(actor or "local"))
    try:
        from bandi.company.team_registry import TeamRegistry

        TeamRegistry().set_members(normalized, [str(role.id) for role in roles])
        for role in roles:
            dispatcher.org_engine.register_role(
                role.id,
                role.name,
                role.responsibility,
                reports_to=role.reports_to,
                can_spawn=list(role.can_spawn) or None,
                tool_tags=list(role.tool_tags),
                is_final_decider=role.is_final_decider,
                team_id="",
                fs_scopes=list(role.fs_scopes),
            )
        save_org(dispatcher.org_engine, dispatcher.recruiter)
        from bandi.company.team_registry import TeamRegistry

        TeamRegistry().set_status(normalized, "dissolved")
        detached_projects = 0
        store = _get_project_store()
        for project in store.list_projects():
            if normalized in project.team_ids:
                dissolved = [*project.dissolved_team_ids]
                if normalized not in dissolved:
                    dissolved.append(normalized)
                store.save_project(
                    project.model_copy(
                        update={
                            "team_ids": [item for item in project.team_ids if item != normalized],
                            "dissolved_team_ids": dissolved,
                        }
                    )
                )
                detached_projects += 1
    except Exception as exc:
        operations.fail(op_id, type(exc).__name__)
        raise
    operations.commit(op_id)
    logger.info(
        "team dissolved team_id=%s detached_roles=%d detached_projects=%d actor=%s op_id=%s",
        normalized,
        len(roles),
        detached_projects,
        actor,
        op_id,
    )
    return {
        "team_id": normalized,
        "detached_roles": len(roles),
        "detached_projects": detached_projects,
        "dissolved": True,
        "op_id": op_id,
    }


# -- 项目协作 API（api/company/projects：list/create/拉入团队） --------

_project_store: Any | None = None


def _get_project_store() -> Any:
    """功能：项目库共享单例（进程级，tests monkeypatch 此函数/全局变量隔离）。"""
    global _project_store
    if _project_store is None:
        from bandi.company.projects import ProjectStore

        _project_store = ProjectStore()
    return _project_store


_RESERVED_SESSION_PREFIXES = (
    "role-session::",
    "role-runtime::",
    "skill_distill:",
    "team-pending-",
    "automation:",
)


def _is_reserved_session_id(session_id: str) -> bool:
    # 功能：客户端不得创建/占用内部保留会话 id（员工/蒸馏/待审/自动化命名空间）。
    value = str(session_id or "")
    return any(value.startswith(prefix) for prefix in _RESERVED_SESSION_PREFIXES)


def _validated_session_project_root(raw: Any, project_id: str = "") -> str | None:
    """功能：会话 project_root 只允许工作区或已登记项目目录，拒绝控制面/任意路径。"""
    text = str(raw or "").strip()
    if not text:
        return None
    from bandi.tools.fs_tool import control_plane_path_reason

    try:
        root = Path(text).expanduser().resolve()
    except OSError as exc:
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST, detail=f"invalid project_root: {exc}"
        ) from exc
    if control_plane_path_reason(root):
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST, detail="project_root is a control-plane path"
        )
    store = _get_project_store()
    pid = str(project_id or "").strip()
    if pid:
        record = store.get_project(pid)
        if record is not None and str(getattr(record, "root_dir", "") or "").strip():
            expected = Path(str(record.root_dir)).expanduser().resolve()
            if root != expected:
                raise HTTPException(
                    status_code=HTTP_BAD_REQUEST,
                    detail="project_root does not match the registered project root",
                )
            return str(root)
    if root == Path.home().resolve():
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST, detail="project_root cannot be the home root"
        )
    if not root.exists() or not root.is_dir():
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST, detail="project_root must be an existing directory"
        )
    return str(root)


def _ensure_project_agents_md(project_id: str) -> None:
    """功能：项目会话首次发言时确保项目目录存在空 AGENTS.md（opencode/claude code 语义）。

    失败静默（项目库异常/无目录/写失败均不阻断对话）。
    """
    from bandi.company.projects import ensure_agents_md

    if not project_id:
        return
    try:
        record = _get_project_store().get_project(project_id)
    except Exception:  # noqa: BLE001 —— 项目库异常不得阻断对话
        return
    if record is None:
        return
    ensure_agents_md(str(record.root_dir or ""))


class ProjectCreateBody(BaseModel):
    project_id: str
    name: str
    root_dir: str = ""
    # 功能：新建项目空间时自动创建本地同名文件夹（root_dir 为空才生效；）
    create_folder: bool = False


class ProjectAddTeamBody(BaseModel):
    team_id: str


DEFAULT_PROJECTS_DIR_NAME = "BandiProjects"


def default_projects_root() -> Path:
    """功能：新建项目空间的默认落盘根（用户主目录下可见目录，便于本机查找）。"""
    return Path.home() / DEFAULT_PROJECTS_DIR_NAME


def _fs_list_subdirs(target: Path) -> list[dict[str, str]]:
    """功能：列一级子目录（跳过忽略名单/隐藏目录/敏感目录；不列文件）。"""
    from bandi.tools.fs_tool import sensitive_path_reason

    try:
        children = sorted(target.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return []
    entries: list[dict[str, str]] = []
    for item in children:
        if item.name.startswith(".") or item.name in _IGNORED_DIR_NAMES:
            continue
        try:
            if not item.is_dir():
                continue
        except OSError:
            continue
        if sensitive_path_reason(item):
            continue
        entries.append({"name": item.name, "path": str(item)})
    return entries


def _fs_drive_entries() -> list[dict[str, str]]:
    """功能：Windows 盘符列表（非 Windows 返回空——由 home 兜底）。"""
    if os.name != "nt":
        return []
    entries: list[dict[str, str]] = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        try:
            if root.is_dir():
                entries.append({"name": f"{letter}:", "path": str(root)})
        except OSError:
            continue
    return entries


_PICK_DIR_TIMEOUT_SECS = 600
_WIN_FOLDER_DIALOG_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
    "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
    "$d.Description = '选择项目空间文件夹';"
    "$d.ShowNewFolderButton = $true;"
    "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
    "{ [Console]::Out.Write($d.SelectedPath) }"
)


def _directory_picker_available() -> bool:
    """功能：原生文件夹选择框能力探测（无 GUI 依赖的平台返回 False → 前端回落内置选择器）。"""
    if os.name == "nt":
        return shutil.which("powershell") is not None
    if sys.platform == "darwin":
        return shutil.which("osascript") is not None
    return shutil.which("zenity") is not None


def _pick_directory_native() -> str:
    """功能：调系统原生文件夹选择框——返回绝对路径；用户取消/失败返回空串。

    阻塞式（等待用户操作），调用方必须放线程池执行，避免卡住事件循环。
    """
    if os.name == "nt":
        cmd = ["powershell", "-NoProfile", "-STA", "-Command", _WIN_FOLDER_DIALOG_SCRIPT]
    elif sys.platform == "darwin":
        cmd = [
            "osascript",
            "-e",
            'POSIX path of (choose folder with prompt "选择项目空间文件夹")',
        ]
    else:
        cmd = ["zenity", "--file-selection", "--directory", "--title=选择项目空间文件夹"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_PICK_DIR_TIMEOUT_SECS, check=False
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


@app.get("/api/company/org-teams/{team_id}/export")
async def company_org_team_export(team_id: str) -> Response:
    """功能：把团队当前组织快照成团队模板 JSON（不含任务进度、产物与记忆）。"""
    from bandi.api.team_export_ext import TeamExportError, build_team_template
    from bandi.company.agent_profile import AgentProfileManager
    from bandi.company.team_registry import TeamRegistry

    normalized = str(team_id).strip()
    dispatcher = _org_api_dispatcher()
    roles = list(dispatcher.org_engine.list_roles())
    team_roles = [role for role in roles if str(getattr(role, "team_id", "") or "") == normalized]
    if not team_roles:
        raise HTTPException(status_code=404, detail=f"team {normalized!r} not found")
    profiles: dict[str, Any] = {}
    manager = AgentProfileManager()
    for role in team_roles:
        try:
            profiles[str(role.id)] = manager.get_profile(str(role.id))
        except Exception:  # noqa: BLE001 — 档案缺失不影响组织结构导出
            profiles[str(role.id)] = None
    try:
        template = build_team_template(
            team_id=normalized,
            team_name=TeamRegistry().get(normalized) or normalized,
            roles=roles,
            employees=list(dispatcher.org_engine.list_employees(include_inactive=True)),
            profiles=profiles,
        )
    except TeamExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = json.dumps(template.model_dump(), ensure_ascii=False, indent=2)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{normalized}.team.json"'},
    )


class PickDirectoryBody(BaseModel):
    # 功能：只做能力探测（不弹窗）——供自动化验证与前端预判使用
    dry_run: bool = False


@app.post("/api/company/fs/pick-directory")
async def company_fs_pick_directory(body: PickDirectoryBody) -> dict[str, Any]:
    """功能：系统原生文件夹选择框（真实桌面弹窗）——「打开本地文件夹」路径。

    返回 `{"path": <绝对路径>, "fallback": <bool>}`：`fallback=true` = 原生不可用
    前端回落内置选择器；用户取消 → path 为空串（非错误）。
    """
    if not _directory_picker_available():
        return {"path": "", "fallback": True}
    if body.dry_run:
        return {"path": "", "fallback": False}
    path = await asyncio.to_thread(_pick_directory_native)
    return {"path": path, "fallback": False}


@app.get("/api/company/fs/directories")
async def company_fs_directories(path: str = "") -> dict[str, Any]:
    """功能：本地目录列举（仅目录名；敏感路径 fail-closed）——「打开本地文件夹」选择器。

    path 为空 → 返回盘符（Windows）/ 用户主目录；否则列该目录下一级子目录。
    只回目录结构，不读任何文件内容；用于把本机已有文件夹登记为项目空间。
    """
    from bandi.tools.fs_tool import sensitive_path_reason

    raw = str(path or "").strip()
    if not raw:
        drives = _fs_drive_entries()
        if drives:
            return {"path": "", "parent": "", "entries": drives, "is_root": True}
        home = Path.home()
        return {
            "path": str(home),
            "parent": str(home.parent),
            "entries": _fs_list_subdirs(home),
            "is_root": False,
        }
    try:
        target = Path(raw).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_PATH") from exc
    if not target.is_dir():
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="DIRECTORY_NOT_FOUND") from None
    if sensitive_path_reason(target):
        raise HTTPException(status_code=403, detail="SENSITIVE_PATH")
    parent = "" if target.parent == target else str(target.parent)
    return {
        "path": str(target),
        "parent": parent,
        "entries": _fs_list_subdirs(target),
        "is_root": False,
    }


@app.get("/api/company/projects")
async def company_projects_list() -> dict[str, Any]:
    """项目列表（每条含 team_ids 动态解析的成员，=A 不落盘快照）。"""
    from dataclasses import asdict

    store = _get_project_store()
    dispatcher = _org_api_dispatcher()
    projects = []
    for record in store.list_projects():
        entry = record.model_dump()
        entry["members"] = [
            asdict(member)
            for member in store.resolve_members(record.project_id, dispatcher.org_engine)
        ]
        projects.append(entry)
    return {"projects": projects}


@app.post("/api/company/projects")
async def company_project_create(body: ProjectCreateBody) -> dict[str, Any]:
    """创建项目（project_id 路径安全 + 重复 409，=A）。

     扩展：`create_folder=true` 且未给 root_dir → 在默认项目空间根下
    创建同名文件夹并登记为工作目录（「新建项目空间」路径，不以桌面文件为项目空间）。
    """
    from bandi.company.projects import ProjectError, ProjectExistsError
    from bandi.skills.path_guard import validate_path_segment

    store = _get_project_store()
    root_dir = str(body.root_dir or "").strip()
    if body.create_folder and not root_dir:
        try:
            folder_name = validate_path_segment(
                str(body.name or "").strip(), label="项目空间名", error_cls=ProjectError
            )
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        folder = default_projects_root() / folder_name
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"项目空间目录创建失败: {exc}") from exc
        root_dir = str(folder)
    try:
        record = store.create_project(body.project_id, body.name, root_dir=root_dir)
    except ProjectExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"project": record.model_dump()}


@app.post("/api/company/projects/{project_id}/teams")
async def company_project_add_team(project_id: str, body: ProjectAddTeamBody) -> dict[str, Any]:
    """团队拉入项目（已组建校验，=A；重复拉入幂等）。"""
    from bandi.company.projects import (
        ProjectError,
        ProjectNotFoundError,
        ProjectTeamNotFoundError,
        ProjectTeamNotReadyError,
    )

    store = _get_project_store()
    dispatcher = _org_api_dispatcher()
    from bandi.company.team_registry import TeamRegistry

    try:
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    team_id = str(body.team_id).strip()
    registry = TeamRegistry()
    if not any(role.team_id == team_id for role in dispatcher.org_engine.list_roles()):
        for role_id in registry.get_members(team_id):
            role = dispatcher.org_engine.get_role(role_id)
            if role is None:
                continue
            dispatcher.org_engine.register_role(
                role.id,
                role.name,
                role.responsibility,
                reports_to=role.reports_to,
                can_spawn=list(role.can_spawn) or None,
                tool_tags=list(role.tool_tags),
                is_final_decider=role.is_final_decider,
                team_id=team_id,
                fs_scopes=list(role.fs_scopes),
            )
        from bandi.company.org_persistence import save_org

        save_org(dispatcher.org_engine, dispatcher.recruiter)
    try:
        record = store.add_team(
            project_id,
            body.team_id,
            dispatcher.org_engine,
            template_store=_get_team_store(),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProjectTeamNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProjectTeamNotReadyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    registry.set_status(team_id, "active")
    return {"project": record.model_dump()}


@app.get("/api/company/projects/{project_id}/files")
async def list_project_files_endpoint(project_id: str, query: str = "") -> dict[str, Any]:
    """项目文件列表（@文件引用数据源；root_dir 未设置 → 空列表）。"""
    from bandi.company.projects import ProjectError, list_project_files

    try:
        record = _get_project_store().get_project(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    root_dir = str(getattr(record, "root_dir", "") or "")
    return {"files": list_project_files(root_dir, query), "root_dir": root_dir}


def _project_root_dir(project_id: str) -> Path | None:
    """功能：项目工作目录解析——项目不存在 404 / 非法 400 / 未设置或目录不在 None。"""
    from bandi.company.projects import ProjectError

    try:
        record = _get_project_store().get_project(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    root_raw = str(getattr(record, "root_dir", "") or "").strip()
    if not root_raw:
        return None
    root = Path(root_raw).expanduser()
    return root.resolve() if root.is_dir() else None


@app.get("/api/company/projects/{project_id}/files/tree")
async def project_files_tree(project_id: str, path: str = "") -> dict[str, Any]:
    """项目文件树（右栏员工作用域「文件」页签数据源——懒加载逐级展开）。

    根 = 项目工作目录（root_dir 未设置 → 空列表）；越界/敏感 fail-closed（与会话文件面同口径）。
    """
    root = _project_root_dir(project_id)
    if root is None:
        return {"project_id": project_id, "root": "", "path": "", "entries": []}
    target = _resolve_within_root(root, path)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="DIRECTORY_NOT_FOUND")
    entries = _list_dir_entries(target)
    rel = "" if target == root else str(target.relative_to(root)).replace("\\", "/")
    return {"project_id": project_id, "root": str(root), "path": rel, "entries": entries}


@app.get("/api/company/projects/{project_id}/files/content")
async def project_file_content(
    project_id: str, path: str, max_bytes: int = 65536
) -> dict[str, Any]:
    """项目文件内容预览（只读；文本/二进制/超大三分支；越界/敏感 fail-closed）。"""
    from bandi.tools.fs_tool import workspace_root

    root = _project_root_dir(project_id)
    if root is None:
        # 未配置项目目录时回退全局工作根，供左栏面板文件预览使用。
        # 路径仍经 _resolve_within_root 校验在该根内。
        root = workspace_root()
    content_root, target = _resolve_preview_path(root, path)
    payload = _file_content_payload(content_root, target, _clamp_file_preview_limit(max_bytes))
    return {**payload, "project_id": project_id}


def _role_from_role_session(session_id: str) -> str:
    """功能：从员工执行会话 id 反解角色——委托统一解析入口
    （`company/session_scoping.role_from_session_id`，生成/解析同文件成对）。"""
    from bandi.company.session_scoping import role_from_session_id

    return role_from_session_id(session_id)


@app.get("/api/company/projects/{project_id}/usage")
async def project_usage(project_id: str, role: str = "", thread_id: str = "") -> dict[str, Any]:
    """项目用量（团队/员工两口径；团队口径**排除项目内秘书会话**）。

    - 团队（role 空）：项目普通会话 + 项目全部员工执行会话（`role-session::<项目>::…`）
    - 员工（role 非空）：仅该角色员工执行会话（会话 id 倒数第二段匹配）
    - 空台账零值不 500；项目缺失 404 / 非法 400。
    """
    from bandi.company.projects import ProjectError
    from bandi.company.session_scoping import project_from_session_id
    from bandi.core.config import default_cost_db_path
    from bandi.observability.cost_store import get_shared_cost_store

    try:
        record = _get_project_store().get_project(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")

    try:
        store = get_shared_cost_store(default_cost_db_path())
        by_session = (store.aggregate_usage(days=0) or {}).get("by_session") or {}
    except Exception as exc:  # noqa: BLE001 — 观测面不可用不得 500（零值空态）
        logger.warning("project usage unavailable: %s", exc)
        by_session = {}
    wanted_role = role.strip()
    wanted_thread = thread_id.strip()
    session_projects = _session_project_map()
    try:
        secretary_ids = {
            str(s.get("id"))
            for s in _get_store().list_sessions()
            if _session_origin(s) == "secretary"
        }
    except Exception:  # noqa: BLE001 — 会话库不可用降级为空（仅按前缀归项目）
        secretary_ids = set()

    input_tokens = 0
    output_tokens = 0
    cost_cny = 0.0
    requests = 0
    for session_id, bucket in by_session.items():
        project = project_from_session_id(session_id) or session_projects.get(session_id, "")
        if project != project_id:
            continue
        if session_id in secretary_ids:
            continue
        if wanted_role and _role_from_role_session(session_id) != wanted_role:
            continue
        if wanted_thread and f"::{wanted_thread}::" not in session_id:
            continue
        input_tokens += int(bucket.get("input_tokens") or 0)
        output_tokens += int(bucket.get("output_tokens") or 0)
        cost_cny += float(bucket.get("cost_cny") or 0.0)
        requests += int(bucket.get("requests") or 0)
    return {
        "project_id": project_id,
        "role": wanted_role,
        "thread_id": wanted_thread,
        "total_tokens": input_tokens + output_tokens,
        "total_cost_cny": round(cost_cny, 6),
        "requests": requests,
    }


@app.get("/api/company/projects/{project_id}/artifacts")
async def project_artifacts(project_id: str, role: str = "") -> dict[str, Any]:
    """/团队产物聚合（项目内全部会话产物 + 员工执行会话产物，created_at 升序）。

    - 团队（role 空）：普通项目会话产物（**排除项目内秘书会话**）+ 项目全部 role-session 产物
    - 员工（role 非空）：仅该角色 role-session 产物（会话 id 倒数第二段匹配）
    """
    from bandi.company.projects import ProjectError

    try:
        record = _get_project_store().get_project(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    store = _get_store()
    artifacts: list[dict[str, Any]] = []
    if not role.strip():
        from bandi.company.session_scoping import is_member_session_id

        for session in store.list_sessions():
            session_id = str(session["id"])
            if is_member_session_id(session_id):
                continue  # 员工执行会话由前缀扫描统一处理（防双收）
            if _session_project_id(session) != project_id:
                continue
            if _session_origin(session) == "secretary":
                continue
            for artifact in store.list_artifacts(session_id):
                artifacts.append({**artifact, "session_id": session_id})
    wanted_role = role.strip()
    from bandi.company.session_scoping import member_session_prefix

    for artifact in store.list_artifacts_by_prefix(member_session_prefix(project_id)):
        session_id = str(artifact.get("session_id") or "")
        if wanted_role and _role_from_role_session(session_id) != wanted_role:
            continue
        artifacts.append({**artifact, "session_id": session_id})
    artifacts.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
    return {
        "project_id": project_id,
        "role": wanted_role,
        "artifacts": artifacts,
        "count": len(artifacts),
    }


@app.patch("/api/company/projects/{project_id}")
async def company_project_rename(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """**项目改名**：仅改 name（project_id 不可变），其他字段不受影响。"""
    from bandi.company.projects import ProjectError, ProjectNotFoundError

    store = _get_project_store()
    try:
        record = store.rename_project(project_id, str(payload.get("name") or ""))
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND") from exc
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"project": record.model_dump()}


@app.delete("/api/company/projects/{project_id}")
async def company_project_delete(project_id: str) -> dict[str, Any]:
    """：从列表删除项目（仅删项目记录；会话与磁盘文件不动）。"""
    from bandi.company.projects import ProjectError

    store = _get_project_store()
    try:
        removed = store.delete_project(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
    return {"deleted": True}


@app.post("/api/company/projects/{project_id}/deliver")
async def company_project_deliver(project_id: str) -> dict[str, Any]:
    """显式项目交付——建交付里程碑卡并触发按员工反思与技能晋升。

    前端入口（项目面板「交付」动作）留后续 Day；本端点为交付语义的后端原语。
    返回 `status`：recorded / already_delivered / no_work_items。
    """
    from bandi.company.project_delivery import deliver_project
    from bandi.company.projects import ProjectError

    try:
        record = _get_project_store().get_project(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")

    dispatcher = _get_company_dispatcher()
    return deliver_project(
        store=dispatcher.store,
        evolution=dispatcher.employee_evolution,
        project_id=project_id,
    )


def _open_local_folder(path: str) -> None:
    """功能：在系统文件管理器中打开本地目录（Windows startfile / macOS open / Linux xdg-open）。"""
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]  # 本机服务打开已校验目录
        return
    import platform
    import subprocess

    opener = "open" if platform.system() == "Darwin" else "xdg-open"
    subprocess.Popen([opener, path])


@app.post("/api/company/projects/{project_id}/open-folder")
async def company_project_open_folder(project_id: str) -> dict[str, Any]:
    """：打开项目工作目录（未设置目录 400 / 目录不存在 404）。"""
    from bandi.company.projects import ProjectError

    store = _get_project_store()
    try:
        record = store.get_project(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
    root = str(record.root_dir or "").strip()
    if not root:
        raise HTTPException(status_code=400, detail="项目未设置工作目录")
    target = Path(root).expanduser()
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"目录不存在: {target}")
    _open_local_folder(str(target))
    return {"opened": str(target)}


@app.post("/api/fs/open")
async def open_local_path(payload: dict[str, Any]) -> dict[str, Any]:
    """功能：用系统默认程序打开本地文件/目录（回复引用文件的点击落地）。

    路径必须在工作区根或项目 root_dir 之内（复用工具层沙箱口径），
    避免渲染层传入任意路径让内核打开任意本地资源。
    """
    from bandi.tools.fs_tool import workspace_root

    raw = str(payload.get("path") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="PATH_REQUIRED")
    target = Path(raw).expanduser()
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {target}")
    resolved = target.resolve()
    roots = [workspace_root()]
    try:
        for project in _get_project_store().list_projects():
            root = str(getattr(project, "root_dir", "") or "").strip()
            if root:
                roots.append(Path(root).expanduser())
    except Exception:  # noqa: BLE001 — 项目枚举失败只收窄到工作区根，不阻断打开
        logger.warning("open_path: project roots unavailable, fall back to workspace root")
    for base in roots:
        try:
            resolved.relative_to(base.resolve())
            break
        except ValueError:
            continue
    else:
        raise HTTPException(status_code=403, detail="PATH_OUTSIDE_WORKSPACE")
    _open_local_folder(str(resolved))
    return {"opened": str(resolved)}


# -- 会话归档沉淀（POST /api/sessions/{id}/archive：LLM 摘要写项目 AGENTS.md）----

_archive_provider: Any | None = None


def _get_archive_provider() -> Any:
    """功能：归档摘要 provider 单例（tests monkeypatch 隔离）。"""
    global _archive_provider
    if _archive_provider is None:
        from bandi.agent.factory import build_provider
        from bandi.core.config import BandiConfig

        _archive_provider = build_provider(BandiConfig.load())
    return _archive_provider


@app.post("/api/sessions/{session_id}/archive")
async def archive_session_to_project(session_id: str) -> dict[str, Any]:
    """归档会话：项目会话写项目 AGENTS.md 受管区块并沉淀摘要；普通会话仅置 status。

    已归档 → 409。沉淀失败（项目缺失/无工作目录/摘要或写入失败）→ summarized False
    + reason，且不改变会话状态（归档动作本身不受影响）。
    """
    from bandi.company.project_archive import summarize_session, write_archive_entry

    store = _get_store()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SESSION_NOT_FOUND")
    if str(session.get("status") or "active") == "archived":
        raise HTTPException(status_code=HTTP_CONFLICT, detail="SESSION_ALREADY_ARCHIVED")

    project_id = _session_project_id(session)
    distilled = False
    if project_id:
        try:
            record = _get_project_store().get_project(project_id)
        except Exception:  # noqa: BLE001 —— 项目库异常归为未沉淀，不阻断归档
            record = None
        if record is None:
            return _archive_failed("项目不存在")
        root_dir = str(record.root_dir or "").strip()
        if not root_dir:
            return _archive_failed("项目未设置工作目录")

        # 归档=审计语义，必须含压缩边界之前的原始历史
        entries = store.get_full_branch(session_id)
        messages = [
            {
                "role": "user" if entry.get("role") == "user" else "assistant",
                "content": str(entry.get("content") or ""),
            }
            for entry in entries
            if entry.get("entry_type") == "message" and entry.get("content")
        ]
        title = str(session.get("title") or "")
        try:
            points = await summarize_session(
                _get_archive_provider(), title, messages, session_id=session_id
            )
        except Exception as exc:  # noqa: BLE001 —— 摘要失败不阻断归档
            logging.getLogger(__name__).warning("归档摘要失败: %s", exc)
            return _archive_failed(f"摘要失败: {exc}")
        if not points:
            return _archive_failed("摘要为空")
        if not write_archive_entry(root_dir, title, points):
            return _archive_failed("写入 AGENTS.md 失败")
        distilled = True

    store.update_session(session_id, status="archived")
    return {"summarized": distilled, "distilled": distilled, "archived": True, "reason": ""}


def _archive_failed(reason: str) -> dict[str, Any]:
    """功能：沉淀失败响应体（会话保持 active，不得标记已归档）。"""
    return {"summarized": False, "distilled": False, "archived": False, "reason": reason}


# -- 附件上传 API（api/company/uploads：multipart 落盘 + 读回引用） --------

_attachment_store: Any | None = None


def _get_attachment_store() -> Any:
    """功能：附件库共享单例（进程级，tests monkeypatch 此函数/全局变量隔离）。"""
    global _attachment_store
    if _attachment_store is None:
        from bandi.company.attachments import AttachmentStore

        _attachment_store = AttachmentStore()
    return _attachment_store


def _validate_upload_project(project_id: str) -> str:
    """功能：附件项目上下文校验（company 缺省放行；其他项目必须存在——404/400 语义）。"""
    pid = str(project_id or "").strip() or "company"
    if pid == "company":
        return pid
    from bandi.company.projects import ProjectError

    try:
        record = _get_project_store().get_project(pid)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail=f"project not found: {pid}")
    return pid


def _resolve_ws_attachments(raw: Any, project_id: str) -> list[dict[str, Any]]:
    """功能：WS chat 附件解析。"""
    if not isinstance(raw, list):
        return []
    from bandi.company.attachments import AttachmentError, extract_attachment_text

    store = _get_attachment_store()
    pid = str(project_id or "").strip() or "company"
    resolved: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        upload_id = str(item.get("upload_id") or "").strip()
        if not upload_id:
            continue
        try:
            path = store.resolve(upload_id, project_id=pid)
        except AttachmentError:
            continue
        record: dict[str, Any] = {
            "upload_id": upload_id,
            "name": str(item.get("name") or store.display_name(path)),
            "size": path.stat().st_size,
        }
        text = extract_attachment_text(path)
        if text is not None:
            record["text"] = text
        resolved.append(record)
    return resolved


def _with_attachment_notes(content: str, raw: Any, project_id: str) -> str:
    """功能：company 通道附件注记（轮三-5）——upload_id 解析为文本注记拼入消息内容。

    单聊/群聊派单链路无独立附件字段：文本类附件按「[附件 名] 内容」注记随消息进入建卡
    description，员工执行时可见；非文本附件标注占位（不回传二进制内容）。纯附件消息
    （content 为空）也会产出非空注记——调用方据此放行发送。
    """
    attachments = _resolve_ws_attachments(raw, project_id)
    if not attachments:
        return content
    notes: list[str] = []
    for item in attachments:
        text = str(item.get("text") or "").strip()
        if text:
            notes.append(f"[附件 {item['name']}]\n{text}")
        else:
            notes.append(f"[附件 {item['name']}（{item['size']} 字节，非文本内容）]")
    joined = "\n\n".join(notes)
    return f"{content}\n\n{joined}" if content else joined


@app.post("/api/company/uploads")
async def company_upload_file(file: UploadFile, project_id: str = "") -> dict[str, Any]:
    """对话附件上传（multipart 分块读取 → 项目附件目录，返回引用 upload_id）。"""
    from dataclasses import asdict

    from bandi.company.attachments import (
        MAX_UPLOAD_BYTES,
        AttachmentError,
        AttachmentTooLargeError,
    )

    pid = _validate_upload_project(project_id)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="upload exceeds size limit")
        chunks.append(chunk)
    try:
        attachment = _get_attachment_store().save(
            b"".join(chunks), filename=file.filename or "", project_id=pid
        )
    except AttachmentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"upload": asdict(attachment)}


@app.get("/api/company/uploads/{upload_id}")
async def company_download_file(upload_id: str, project_id: str = "") -> FileResponse:
    """附件读回（预览/引用消费；文件名回填原始安全化名）。"""
    from bandi.company.attachments import (
        AttachmentError,
        AttachmentNotFoundError,
        AttachmentStore,
    )

    pid = _validate_upload_project(project_id)
    try:
        path = _get_attachment_store().resolve(upload_id, project_id=pid)
    except AttachmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(path, filename=AttachmentStore.display_name(path))


def _org_role_to_dict(role: Any) -> dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "reports_to": role.reports_to,
        "responsibility": role.responsibility,
        "is_final_decider": role.is_final_decider,
        "tool_tags": list(role.tool_tags),
        "can_spawn": list(role.can_spawn),
        "team_id": role.team_id,
    }


def _work_item_to_dict(item: Any) -> dict[str, Any]:
    """WorkItem dataclass → JSON 安全 dict（Enum 转 value；补项目归属字段）。"""
    metadata = item.metadata if isinstance(getattr(item, "metadata", None), dict) else {}
    base: dict[str, Any] = {
        "id": item.id,
        "title": item.title,
        "description": item.description,
        "phase": item.phase.value if hasattr(item.phase, "value") else str(item.phase),
        "turn_type": item.turn_type.value
        if hasattr(item.turn_type, "value")
        else str(item.turn_type),
        "assignee_role": item.assignee_role,
        "parent_id": item.parent_id,
        "project_id": str(metadata.get("project_id") or ""),
        "thread_id": str(metadata.get("thread_id") or ""),
        "source": str(metadata.get("source") or ""),
        # 意图标记（四态）：query=咨询问答（右栏/任务卡过滤）/ confirm=确认执行 / adjust=调整
        "chat_kind": str(metadata.get("chat_kind") or ""),
        "created_mode": str(metadata.get("created_mode") or ""),
        "mode_gate": str(metadata.get("mode_gate") or ""),
        "round_id": str(metadata.get("round_id") or ""),
        "group_speaker_index": metadata.get("group_speaker_index", -1),
    }
    for f in ("status", "priority", "revision", "created_at", "updated_at", "column"):
        v = getattr(item, f, None)
        if v is None:
            continue
        if f in ("created_at", "updated_at"):
            # 功能：时间统一带本地时区偏移输出，客户端不必猜是 UTC 还是本地
            base[f] = iso_local(v)
        else:
            base[f] = v.value if hasattr(v, "value") else str(v)
    # 看板列：WorkItem 无 column 属性——按 phase 归一（右栏计划状态：执行中/已完成）
    try:
        from bandi.company.phase import kanban_column

        base["column"] = kanban_column(item.phase)
    except Exception:  # noqa: BLE001 — 未知 phase 不阻断列表
        pass
    # 功能：看板卡片需要的派生字段——评审门有无、依赖条数
    base["has_gate"] = bool(metadata.get("gate"))
    depends = metadata.get("depends_on")
    base["dependency_count"] = len(depends) if isinstance(depends, list) else 0
    runtime = getattr(item, "runtime_metadata", None)
    preview = str((runtime or {}).get("_last_output") or "").strip()
    if preview:
        base["last_output_preview"] = preview[:140]
    return base


def _column_counts(items: list[Any]) -> dict[str, int]:
    """按 kanban 列统计给定 work item 集合（全零起始，仅计传入集合）。"""
    from bandi.company.phase import kanban_column

    counts: dict[str, int] = {"todo": 0, "in_progress": 0, "in_review": 0, "done": 0}
    for item in items:
        col = kanban_column(item.phase)
        counts[col] = counts.get(col, 0) + 1
    return counts


def _employee_general_skill_refs(employee_id: str) -> list[str]:
    """功能：员工档案 general_skill 绑定清单（best-effort；无档案/读失败返回空）。"""
    if not employee_id:
        return []
    try:
        from bandi.company.agent_profile import AgentProfileManager

        profile = AgentProfileManager().get_profile(employee_id)
    except Exception:  # noqa: BLE001 — 档案不可读按无绑定（右栏空态兜底）
        return []
    if profile is None:
        return []
    refs: list[str] = []
    for item in getattr(profile, "resources", []) or []:
        if str(getattr(item, "resource_type", "")) != "general_skill":
            continue
        if str(getattr(item, "status", "active")) != "active":
            continue
        ref = str(getattr(item, "resource_id", "") or "").strip()
        if ref:
            refs.append(ref)
    return refs


@app.get("/api/company/employees")
async def company_employees_list(include_inactive: bool = False) -> dict[str, Any]:
    """功能：员工列表 + 技能绑定；include_inactive=true 时含下线与退役员工。"""
    dispatcher = _org_api_dispatcher()
    employees = dispatcher.org_engine.list_employees(include_inactive=include_inactive)
    payload: list[dict[str, Any]] = []
    for employee in employees:
        record = employee.to_dict()
        record["skills"] = _employee_general_skill_refs(str(employee.employee_id))
        payload.append(record)
    return {"employees": payload}


@app.get("/api/company/employees/{employee_id}/profile")
async def company_employee_profile(employee_id: str) -> dict[str, Any]:
    """员工能力档案查询（无档案时按员工资料补建；员工不存在 404）。"""
    from bandi.company.agent_profile import AgentProfileError

    try:
        _, profile = _ensure_employee_profile(employee_id)
    except AgentProfileError as exc:
        # 非法 id（路径穿越等）与损坏档案统一 fail-closed（不泄漏路径细节）
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_EMPLOYEE_ID") from exc
    return {"profile": profile.model_dump(mode="json")}


def _employee_tombstones() -> Any:
    from bandi.company.employee_lifecycle import EmployeeTombstoneStore

    return EmployeeTombstoneStore()


_company_operations_store: Any | None = None


def _company_operations() -> Any:
    global _company_operations_store
    if _company_operations_store is None:
        from bandi.company.company_operations import CompanyOperationsStore

        _company_operations_store = CompanyOperationsStore()
    return _company_operations_store


def _session_matches_identity(session_id: str, keys: set[str]) -> bool:
    """功能：会话 id 是否指向员工/角色（角色运行时 id 形如 role-runtime::run::role）。"""
    sid = str(session_id or "")
    if not sid:
        return False
    return any(key == sid or sid.endswith(f"::{key}") or f"::{key}::" in sid for key in keys)


def _employee_pending_block(
    employee_id: str, role_id: str, session_ids: set[str] | None = None
) -> str:
    """功能：检查员工/岗位是否有待审批或挂起交互，返回阻断码。

    关联口径：WorkItem 的 session_id/origin_session_id 精确命中，或会话 id 带角色后缀；
    不再用整段 JSON blob 子串匹配（PendingApproval.to_meta 不含身份字段，必然漏检）。
    """
    keys = {str(employee_id or ""), str(role_id or "")} - {""}
    if not keys:
        return ""
    session_scope = session_ids or None
    try:
        from bandi.tools.approval import get_approval_store

        for session_id, pending in get_approval_store().list_all_pending():
            sid = str(session_id)
            if not (
                (session_scope is not None and sid in session_scope)
                or _session_matches_identity(sid, keys)
            ):
                continue
            return "APPROVAL_PENDING"
    except Exception:
        logger.debug("approval pending scan skipped", exc_info=True)
    try:
        for item in _get_interaction_store().list_pending(None):
            sid = str(item.session_id)
            if not (
                (session_scope is not None and sid in session_scope)
                or _session_matches_identity(sid, keys)
            ):
                continue
            return "INTERACTION_PENDING"
    except Exception:
        logger.debug("interaction pending scan skipped", exc_info=True)
    return ""


def _active_role_employee(org_engine: Any, role_id: str, *, exclude: str = "") -> Any | None:
    """功能：返回占用角色的在职/暂停员工（1:1 创建校验）。"""
    from bandi.company.staffing import find_active_role_employee

    return find_active_role_employee(org_engine, role_id, exclude_employee_id=exclude)


_PURGE_TERMINAL_PHASES = frozenset({"approved", "failed", "cancelled"})


def _work_item_phase(item: Any) -> str:
    """功能：工作项相位文本（枚举/字符串统一）。"""
    raw = getattr(item, "phase", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()


def _detach_role_references(org_engine: Any, role_id: str) -> None:
    """功能：删除角色前清理其他角色的 reports_to / can_spawn 引用。"""
    for role in list(org_engine.list_roles()):
        if role.id == role_id:
            continue
        reports_to = None if role.reports_to == role_id else role.reports_to
        can_spawn = [item for item in role.can_spawn if item != role_id]
        if reports_to == role.reports_to and can_spawn == role.can_spawn:
            continue
        org_engine.register_role(
            role.id,
            role.name,
            role.responsibility,
            reports_to=reports_to,
            can_spawn=can_spawn or None,
            permissions=role.permissions,
            tool_tags=role.tool_tags,
            is_final_decider=role.is_final_decider,
            team_id=role.team_id,
            fs_scopes=role.fs_scopes,
        )


@app.post("/api/company/employees")
async def company_employees_create(body: dict[str, Any]) -> dict[str, Any]:
    """注册数字员工（employee_id/name/role_id 必填，role 须存在）。"""
    from bandi.company.org_engine import OrgEmployee
    from bandi.company.org_persistence import save_org

    employee_id = str(body.get("employee_id") or "").strip()
    name = str(body.get("name") or "").strip()
    role_id = str(body.get("role_id") or "").strip()
    if not employee_id or not name:
        raise HTTPException(status_code=400, detail="employee_id/name required")
    dispatcher = _org_api_dispatcher()
    tombstones = _employee_tombstones()
    if tombstones.get(employee_id) is not None or (role_id and tombstones.get(role_id) is not None):
        raise HTTPException(status_code=409, detail="IDENTITY_RESERVED")
    if role_id and dispatcher.org_engine.get_role(role_id) is None:
        raise HTTPException(status_code=400, detail=f"role {role_id!r} not found")
    if dispatcher.org_engine.get_employee(employee_id) is not None:
        raise HTTPException(status_code=409, detail=f"employee {employee_id!r} exists")
    if role_id and _active_role_employee(dispatcher.org_engine, role_id) is not None:
        raise HTTPException(status_code=409, detail=f"role {role_id!r} already staffed")
    status = str(body.get("status") or "active")
    if status not in {"active", "paused"}:
        raise HTTPException(status_code=400, detail=f"status {status!r} not allowed")
    emp = OrgEmployee(
        employee_id=employee_id,
        name=name,
        role_id=role_id,
        template_id=str(body.get("template_id") or ""),
        description=str(body.get("description") or ""),
        category=str(body.get("category") or ""),
        domains=[str(x) for x in body.get("domains", []) if str(x).strip()],
        status=str(body.get("status") or "active"),
        llm_config=body.get("llm_config"),
        metadata=dict(body.get("metadata") or {}),
    )
    dispatcher.org_engine.register_employee(emp)
    save_org(dispatcher.org_engine, dispatcher.recruiter)
    _ensure_employee_profile(employee_id)
    return emp.to_dict()


@app.get("/api/company/employees/{employee_id}")
async def company_employee_get(employee_id: str) -> dict[str, Any]:
    """数字员工详情。"""
    dispatcher = _org_api_dispatcher()
    emp = dispatcher.org_engine.get_employee(employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail=f"employee {employee_id!r} not found")
    return emp.to_dict()


@app.get("/api/company/employee-tombstones/{identity_id}")
async def company_employee_tombstone(identity_id: str) -> dict[str, Any]:
    """功能：查询已硬删除员工/角色的最小身份墓碑。"""
    tombstone = _employee_tombstones().get(identity_id)
    if tombstone is None:
        raise HTTPException(status_code=404, detail="TOMBSTONE_NOT_FOUND")
    return {"tombstone": tombstone}


@app.post("/api/company/employee-tombstones/lookup")
async def company_employee_tombstones_lookup(body: dict[str, Any]) -> dict[str, Any]:
    """功能：批量查询墓碑（只返回命中身份）。"""
    ids = [str(item).strip() for item in body.get("ids", []) if str(item).strip()]
    if len(ids) > 200:
        raise HTTPException(status_code=400, detail="too many ids")
    store = _employee_tombstones()
    found: dict[str, dict[str, Any]] = {}
    for identity_id in ids:
        item = store.get(identity_id)
        if item is not None:
            found[identity_id] = item
    return {"tombstones": found}


@app.post("/api/company/employees/{employee_id}/purge")
async def company_employee_purge(
    employee_id: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """功能：硬删除员工、角色、profile 与绑定；保留最小墓碑与历史会话。"""
    from bandi.company.agent_profile import AgentProfileError, AgentProfileManager
    from bandi.company.org_persistence import save_org

    if not bool((body or {}).get("confirm")):
        raise HTTPException(status_code=400, detail="confirm required")
    dispatcher = _org_api_dispatcher()
    tombstones = _employee_tombstones()
    existing_tombstone = tombstones.get(employee_id)
    emp = dispatcher.org_engine.get_employee(employee_id)
    if emp is None:
        if existing_tombstone is not None:
            return {
                "purged": True,
                "tombstone": existing_tombstone,
                "role_deleted": False,
            }
        raise HTTPException(status_code=404, detail=f"employee {employee_id!r} not found")

    role_id = str(emp.role_id or "").strip()
    other = (
        _active_role_employee(dispatcher.org_engine, role_id, exclude=employee_id)
        if role_id
        else None
    )
    if other is not None:
        raise HTTPException(status_code=409, detail=f"role {role_id!r} still in use")
    role_items = (
        [
            item
            for item in dispatcher.store.list_all()
            if str(getattr(item, "assignee_role", "")) == role_id
        ]
        if role_id
        else []
    )
    running = [item for item in role_items if _work_item_phase(item) == "running"]
    if running:
        raise HTTPException(status_code=409, detail="EMPLOYEE_RUNNING")
    active_tasks = [
        item for item in role_items if _work_item_phase(item) not in _PURGE_TERMINAL_PHASES
    ]
    if active_tasks:
        raise HTTPException(status_code=409, detail="EMPLOYEE_HAS_ACTIVE_TASKS")
    related_sessions = {
        str((getattr(item, "metadata", {}) or {}).get(key) or "")
        for item in role_items
        for key in ("session_id", "origin_session_id")
    }
    related_sessions.discard("")
    pending = _employee_pending_block(employee_id, role_id, related_sessions or None)
    if pending:
        raise HTTPException(status_code=409, detail=pending)
    role = dispatcher.org_engine.get_role(role_id) if role_id else None
    team_id = str(getattr(role, "team_id", "") or "").strip() if role is not None else ""

    operations = _company_operations()
    op_id = operations.begin("purge", employee_id, str((body or {}).get("actor") or "local"))
    try:
        # 墓碑先写：后续任一步失败时，重试仍能凭墓碑完成（ID 不释放）
        tombstone = tombstones.add(
            employee_id=employee_id,
            role_id=role_id,
            name=emp.name,
            reason=str((body or {}).get("reason") or "user_purge"),
        )
        try:
            AgentProfileManager().delete_profile(employee_id)
        except AgentProfileError as exc:
            raise HTTPException(status_code=500, detail="PROFILE_DELETE_FAILED") from exc

        role_deleted = False
        if role_id and role is not None:
            _detach_role_references(dispatcher.org_engine, role_id)
            dispatcher.org_engine.remove_role(role_id)
            role_deleted = True
        dispatcher.org_engine.remove_employee(employee_id)
        save_org(dispatcher.org_engine, dispatcher.recruiter)
        if team_id and not any(
            str(getattr(item, "team_id", "") or "").strip() == team_id
            for item in dispatcher.org_engine.list_roles()
        ):
            from bandi.company.team_registry import TeamRegistry

            TeamRegistry().set_status(team_id, "dissolved")
            store = _get_project_store()
            for project in store.list_projects():
                if team_id not in project.team_ids:
                    continue
                dissolved = [*project.dissolved_team_ids]
                if team_id not in dissolved:
                    dissolved.append(team_id)
                store.save_project(
                    project.model_copy(
                        update={
                            "team_ids": [item for item in project.team_ids if item != team_id],
                            "dissolved_team_ids": dissolved,
                        }
                    )
                )
    except Exception as exc:
        operations.fail(op_id, type(exc).__name__)
        raise
    operations.commit(op_id)
    return {
        "purged": True,
        "tombstone": tombstone,
        "role_deleted": role_deleted,
        "op_id": op_id,
    }


@app.delete("/api/company/employees/{employee_id}")
async def company_employee_retire(employee_id: str) -> dict[str, Any]:
    """功能：兼容旧删除入口，统一执行硬删除并写墓碑。"""
    return await company_employee_purge(employee_id, {"confirm": True})


@app.get("/api/company/operations")
async def company_operations_list(state: str = "") -> dict[str, Any]:
    """功能：操作日志列表（只含 ID/状态/错误摘要）。"""
    return {"operations": _company_operations().list(state)}


@app.get("/api/company/operations/stats")
async def company_operations_stats() -> dict[str, Any]:
    """功能：操作/墓碑/团队记忆待确认计数（审计与排障用）。"""
    try:
        tombstone_count = _employee_tombstones().count()
    except Exception:
        logger.warning("tombstone stats failed", exc_info=True)
        tombstone_count = -1
    try:
        team_pending = _get_team_staging_store().list_pending_count()
    except Exception:
        logger.warning("team pending stats failed", exc_info=True)
        team_pending = -1
    return {
        "tombstones": tombstone_count,
        "operations": _company_operations().stats(),
        "team_pending": team_pending,
    }


async def _retry_company_operation(item: dict[str, Any]) -> dict[str, Any]:
    kind = str(item.get("kind") or "")
    target = str(item.get("target_id") or "")
    operations = _company_operations()
    if kind == "purge":
        tombstone = _employee_tombstones().get(target)
        dispatcher = _org_api_dispatcher()
        employee = dispatcher.org_engine.get_employee(target)
        if tombstone is not None and employee is None:
            # 墓碑已写、内存已删但 org.json 未落盘：补写后收口
            from bandi.company.org_persistence import save_org

            save_org(dispatcher.org_engine, dispatcher.recruiter)
            operations.commit(str(item["op_id"]))
            return {"op_id": item["op_id"], "state": "committed"}
        if employee is None:
            operations.fail(str(item["op_id"]), "EMPLOYEE_NOT_FOUND")
            return {"op_id": item["op_id"], "state": "failed"}
        await company_employee_purge(
            target, {"confirm": True, "reason": "reconcile", "actor": "reconcile"}
        )
        operations.commit(str(item["op_id"]))
        return {"op_id": item["op_id"], "state": "committed"}
    if kind == "dissolve":
        try:
            await company_org_team_dissolve(target, actor="reconcile")
        except HTTPException as exc:
            operations.fail(str(item["op_id"]), str(exc.detail)[:120])
            return {"op_id": item["op_id"], "state": "failed"}
        operations.commit(str(item["op_id"]))
        return {"op_id": item["op_id"], "state": "committed"}
    operations.fail(str(item["op_id"]), "UNKNOWN_OPERATION")
    return {"op_id": item["op_id"], "state": "failed"}


@app.post("/api/company/operations/{op_id}/retry")
async def company_operation_retry(op_id: str) -> dict[str, Any]:
    """功能：重放 pending/failed 操作（幂等）。"""
    item = _company_operations().get(op_id)
    if item is None:
        raise HTTPException(status_code=404, detail="OPERATION_NOT_FOUND")
    if item["state"] == "committed":
        return item
    return await _retry_company_operation(item)


@app.on_event("startup")
async def _startup_reconcile_company_operations() -> None:
    try:
        for item in _company_operations().list("pending"):
            await _retry_company_operation(item)
    except Exception:
        logger.warning("company operations reconcile failed", exc_info=True)
    _startup_log("company_operations_reconcile")


@app.put("/api/company/employees/{employee_id}")
async def company_employee_update(employee_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """更新数字员工字段（name/role_id/description/status/llm_config 等可选）。"""
    from bandi.company.org_persistence import save_org

    dispatcher = _org_api_dispatcher()
    emp = dispatcher.org_engine.get_employee(employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail=f"employee {employee_id!r} not found")
    tombstones = _employee_tombstones()
    for field in ("name", "role_id", "template_id", "description", "category", "status"):
        if field in body:
            value = body[field]
            if field == "role_id":
                value = str(value or "").strip()
                if value and dispatcher.org_engine.get_role(value) is None:
                    raise HTTPException(status_code=400, detail=f"role {value!r} not found")
                if value and tombstones.get(value) is not None:
                    raise HTTPException(status_code=409, detail="IDENTITY_RESERVED")
                if (
                    value
                    and _active_role_employee(dispatcher.org_engine, value, exclude=employee_id)
                    is not None
                ):
                    raise HTTPException(status_code=409, detail=f"role {value!r} already staffed")
            elif field == "status" and value not in ("active", "paused"):
                raise HTTPException(status_code=400, detail=f"invalid status {value!r}")
            setattr(emp, field, value)
    if "domains" in body:
        emp.domains = [str(x) for x in body["domains"] if str(x).strip()]
    if "llm_config" in body:
        emp.llm_config = body["llm_config"]
    if "persona_prompt" in body:
        emp.metadata = {**emp.metadata, "persona_prompt": str(body.get("persona_prompt") or "")}
    if isinstance(body.get("metadata"), dict):
        emp.metadata = {**emp.metadata, **body["metadata"]}
    save_org(dispatcher.org_engine, dispatcher.recruiter)
    return emp.to_dict()


def _ensure_employee_profile(employee_id: str) -> Any:
    """功能：确保员工档案存在（首次绑定资源时按员工资料建档）。"""
    from bandi.company.agent_profile import AgentProfileManager

    manager = AgentProfileManager()
    profile = manager.get_profile(employee_id)
    if profile is not None:
        return manager, profile
    employee = _org_api_dispatcher().org_engine.get_employee(employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail=f"employee {employee_id!r} not found")
    persona = str(employee.metadata.get("persona_prompt") or "")
    return manager, manager.create_profile(
        employee_id,
        name=employee.name,
        description=employee.description,
        persona_prompt=persona,
    )


@app.post("/api/company/employees/{employee_id}/resources")
async def company_employee_resource_bind(employee_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """功能：绑定员工能力资源（SOP/技能/知识库/工具）。"""
    from bandi.company.agent_profile import (
        AgentProfileError,
        AgentResourceDuplicateError,
        AgentResourceInvalidError,
    )

    resource_type = str(body.get("resource_type") or "").strip()
    resource_id = str(body.get("resource_id") or "").strip()
    if not resource_type or not resource_id:
        raise HTTPException(status_code=400, detail="resource_type/resource_id required")
    manager, _ = _ensure_employee_profile(employee_id)
    try:
        profile = manager.bind_resource(employee_id, resource_type, resource_id)
    except AgentResourceDuplicateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentResourceInvalidError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AgentProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "bound": True,
        "resources": [item.model_dump(mode="json") for item in profile.resources],
    }


@app.delete("/api/company/employees/{employee_id}/resources/{resource_type}/{resource_id}")
async def company_employee_resource_unbind(
    employee_id: str, resource_type: str, resource_id: str
) -> dict[str, Any]:
    """功能：解绑员工能力资源。"""
    from bandi.company.agent_profile import (
        AgentProfileError,
        AgentProfileManager,
        AgentResourceInvalidError,
    )

    manager = AgentProfileManager()
    if manager.get_profile(employee_id) is None:
        raise HTTPException(status_code=404, detail="PROFILE_NOT_FOUND")
    try:
        manager.unbind_resource(employee_id, resource_type, resource_id)
    except AgentResourceInvalidError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"unbound": True}


@app.get("/api/company/work-items")
async def company_work_items_list(
    phase: str = "",
    column: str = "",
    role: str = "",
    hide_chat: bool = True,
) -> dict[str, Any]:
    """WorkItem 查询（phase/column/role 组合过滤交集 + 列计数）。

    phase/column/role 可同时传，结果为其交集；非法 phase/column 返回 400。
    hide_chat=True（默认）隐藏对话承载卡（chat_kind=query/adjust 或
    source=chat_mention/chat_dm），返回 hidden_chat_count；过滤必须在这里做，
    否则 column_counts 会与前端本地过滤后的卡片数不一致（两套口径）。
    """
    from bandi.company.phase import Phase, kanban_column
    from bandi.company.work_item import (
        is_chat_only_work_item,
        is_hidden_work_item,
        is_legacy_draft_work_item,
    )

    dispatcher = _org_api_dispatcher()
    items = [
        i
        for i in dispatcher.store.list_all()
        if not is_hidden_work_item(i) and not is_legacy_draft_work_item(i)
    ]
    phase = phase.strip()
    if phase:
        try:
            target_phase = Phase(phase.lower())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid phase {phase!r}")
        items = [i for i in items if i.phase == target_phase]
    column = column.strip()
    if column:
        if column not in ("todo", "in_progress", "in_review", "done"):
            raise HTTPException(status_code=400, detail=f"invalid column {column!r}")
        items = [i for i in items if kanban_column(i.phase) == column]
    role = role.strip()
    if role:
        items = [i for i in items if i.assignee_role == role]
    hidden_chat_count = 0
    if hide_chat:
        chat_items = [i for i in items if is_chat_only_work_item(i)]
        hidden_chat_count = len(chat_items)
        items = [i for i in items if not is_chat_only_work_item(i)]
    column_counts = _column_counts(items)
    return {
        "items": [_work_item_to_dict(i) for i in items],
        "column_counts": column_counts,
        "hidden_chat_count": hidden_chat_count,
    }


def _group_round_service() -> tuple[Any, Any]:
    """功能：取公司调度器上的群聊回合服务（未装配 fail-closed）。"""
    dispatcher = _get_company_dispatcher()
    service = getattr(dispatcher, "group_rounds", None)
    if service is None:
        raise HTTPException(status_code=503, detail="GROUP_ROUND_UNAVAILABLE")
    return dispatcher, service


@app.get("/api/company/rounds")
async def company_round_get(session_id: str = "", project_id: str = "") -> dict[str, Any]:
    """功能：查询会话当前运行/暂停中的群聊回合快照。"""
    _dispatcher, service = _group_round_service()
    sid = str(session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="session_id is required")
    round_ = service.get_active(sid)
    if round_ is None:
        return {"round": None}
    if project_id and round_.project_id != project_id:
        return {"round": None}
    return {"round": service.snapshot(round_)}


@app.post("/api/company/rounds/{round_id}/resume")
async def company_round_resume(round_id: str) -> dict[str, Any]:
    """功能：恢复被接力上限暂停的群聊回合。"""
    _dispatcher, service = _group_round_service()
    if service.get(round_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="ROUND_NOT_FOUND")
    card = service.resume(round_id)
    round_ = service.get(round_id)
    return {
        "round": service.snapshot(round_) if round_ is not None else None,
        "work_item_id": str(getattr(card, "id", "") or ""),
    }


@app.post("/api/company/rounds/{round_id}/cancel")
async def company_round_cancel(round_id: str) -> dict[str, Any]:
    """功能：取消群聊回合并中断其已派发的卡。"""
    dispatcher, service = _group_round_service()
    if service.get(round_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="ROUND_NOT_FOUND")
    work_item_ids = service.cancel(round_id, reason="user_cancel")
    for work_item_id in work_item_ids:
        cancel = getattr(dispatcher, "cancel_work_item", None)
        if callable(cancel):
            await cancel(work_item_id, reason="user_cancel")
    round_ = service.get(round_id)
    return {
        "round": service.snapshot(round_) if round_ is not None else None,
        "cancelled_work_items": work_item_ids,
    }


def _threads_for_project(project_id: str) -> Any | None:
    """功能：线程管理器解析——company 走 dispatcher 单例；项目走 per-project 工厂。

    项目线程由 WS 路由的 `_get_project_threads` 空间化；REST 查询必须带
    `project_id` 才能看到项目线程（缺省 company 空间）。
    """
    pid = str(project_id or "").strip() or "company"
    if pid == "company":
        return getattr(_org_api_dispatcher(), "threads", None)
    return _get_project_threads(pid)


@app.get("/api/company/threads")
async def company_threads_list(project_id: str = "") -> dict[str, Any]:
    """讨论线程列表（thread_id/parent_task_id/participants/created_at/message_count）。

    per-project 路由（project_id 缺省 company）。
    """
    threads = _threads_for_project(project_id)
    if threads is None:
        return {"threads": []}
    return {
        "threads": [
            {
                "thread_id": t.thread_id,
                "parent_task_id": t.parent_task_id,
                "participants": t.participants,
                "created_at": iso_local(t.created_at),
                "message_count": len(t.messages),
                "session_id": t.session_id,
                "kind": t.kind,
            }
            for t in threads.list_threads()
        ]
    }


@app.get("/api/company/threads/{thread_id}")
async def company_threads_detail(thread_id: str, project_id: str = "") -> dict[str, Any]:
    """线程详情（thread_id/parent_task_id/participants/created_at/messages）。

    per-project 路由（project_id 缺省 company）。
    """
    threads = _threads_for_project(project_id)
    if threads is None:
        raise HTTPException(status_code=404, detail=f"thread {thread_id!r} not found")
    thread = threads.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail=f"thread {thread_id!r} not found")
    return {
        "thread_id": thread.thread_id,
        "parent_task_id": thread.parent_task_id,
        "participants": thread.participants,
        "created_at": iso_local(thread.created_at),
        "messages": [
            {
                "sender": m.sender,
                "content": m.content,
                "mentions": m.mentions,
                "created_at": iso_local(m.created_at),
                "message_id": m.message_id,
                "process_events": m.process_events,
            }
            for m in thread.messages
        ],
    }


async def _interrupt_thread_work_items(thread_id: str) -> None:
    """功能：线程删除联动——中止绑定该线程的活动卡，避免删后仍回写旧线程。"""
    try:
        dispatcher = _get_company_dispatcher()
        for item in dispatcher.store.list_all():
            metadata = getattr(item, "metadata", None) or {}
            if str(metadata.get("thread_id") or "") != thread_id:
                continue
            await dispatcher.interrupt_work_item(str(item.id))
    except Exception as exc:  # noqa: BLE001 —— 中断失败不阻断删除
        logger.warning("thread work item interrupt failed thread=%s: %s", thread_id, exc)


@app.delete("/api/company/threads/{thread_id}")
async def company_threads_delete(thread_id: str, project_id: str = "") -> dict[str, Any]:
    """功能：删除讨论线程（员工单聊窗口）——窗口即上下文边界，删除后新线程新上下文。"""
    threads = _threads_for_project(project_id)
    if threads is None or threads.get_thread(thread_id) is None:
        raise HTTPException(status_code=404, detail="THREAD_NOT_FOUND")
    await _interrupt_thread_work_items(thread_id)
    deleted = threads.delete_thread(thread_id)
    return {"deleted": deleted, "thread_id": thread_id}


@app.get("/api/company/talent/templates/{template_name}")
async def company_talent_template(template_name: str) -> dict[str, Any]:
    """人才模板详情。"""
    dispatcher = _org_api_dispatcher()
    template = dispatcher.talent.get_template(template_name)
    if template is None:
        raise HTTPException(status_code=404, detail=f"template {template_name!r} not found")
    return _talent_template_dict(template)


@app.post("/api/company/talent/hire")
async def company_talent_hire(body: dict[str, Any]) -> dict[str, Any]:
    """按人才模板 hire 数字员工（template_name 必填，employee_id/role_id 可选）。"""
    from bandi.company.org_engine import OrgEmployee
    from bandi.company.org_persistence import save_org

    template_name = str(body.get("template_name") or "").strip()
    if not template_name:
        raise HTTPException(status_code=400, detail="template_name required")
    dispatcher = _org_api_dispatcher()
    template = dispatcher.talent.get_template(template_name)
    if template is None:
        raise HTTPException(status_code=404, detail=f"template {template_name!r} not found")
    role_id = str(body.get("role_id") or "").strip()
    if not role_id:
        role_id = template.name
    team_id = str(body.get("team_id") or "").strip()
    employee_id = str(body.get("employee_id") or "").strip() or dispatcher.talent.build_employee_id(
        role_id=role_id, template_id=template_name
    )
    tombstones = _employee_tombstones()
    if tombstones.get(employee_id) is not None or tombstones.get(role_id) is not None:
        raise HTTPException(status_code=409, detail="IDENTITY_RESERVED")
    if _active_role_employee(dispatcher.org_engine, role_id) is not None:
        raise HTTPException(status_code=409, detail=f"role {role_id!r} already staffed")
    existing_role = dispatcher.org_engine.get_role(role_id)
    if existing_role is None:
        dispatcher.org_engine.register_role(
            role_id, template.name, template.description, team_id=team_id
        )
        dispatcher.recruiter.register_seat(role_id)
    elif team_id:
        dispatcher.org_engine.register_role(
            role_id,
            existing_role.name,
            existing_role.responsibility,
            reports_to=existing_role.reports_to,
            can_spawn=list(existing_role.can_spawn) or None,
            tool_tags=list(existing_role.tool_tags),
            is_final_decider=existing_role.is_final_decider,
            team_id=team_id,
            fs_scopes=list(existing_role.fs_scopes),
        )
    if dispatcher.org_engine.get_employee(employee_id) is not None:
        raise HTTPException(status_code=409, detail=f"employee {employee_id!r} exists")
    emp = OrgEmployee(
        employee_id=employee_id,
        name=str(body.get("name") or template.name),
        role_id=role_id,
        template_id=template_name,
        description=template.description,
        domains=list(template.domains),
        category=template.category,
    )
    dispatcher.org_engine.register_employee(emp)
    save_org(dispatcher.org_engine, dispatcher.recruiter)
    return emp.to_dict()


@app.get("/api/company/roles")
async def company_roles_list() -> dict[str, Any]:
    """角色列表。"""
    dispatcher = _org_api_dispatcher()
    return {"roles": [_org_role_to_dict(r) for r in dispatcher.org_engine.list_roles()]}


@app.post("/api/company/roles")
async def company_roles_create(body: dict[str, Any]) -> dict[str, Any]:
    """注册角色
    is_final_decider 可选——团队组建与横连组织架构数据源）。"""
    from bandi.company.org_persistence import save_org

    role_id = str(body.get("id") or "").strip()
    name = str(body.get("name") or "").strip()
    if not role_id or not name:
        raise HTTPException(status_code=400, detail="id/name required")
    dispatcher = _org_api_dispatcher()
    if _employee_tombstones().get(role_id) is not None:
        raise HTTPException(status_code=409, detail="IDENTITY_RESERVED")
    if dispatcher.org_engine.get_role(role_id) is not None:
        raise HTTPException(status_code=409, detail=f"role {role_id!r} exists")
    can_spawn = [str(x).strip() for x in body.get("can_spawn", []) if str(x).strip()]
    tool_tags = [str(x).strip() for x in body.get("tool_tags", []) if str(x).strip()]
    try:
        dispatcher.org_engine.register_role(
            role_id,
            name,
            str(body.get("responsibility") or ""),
            reports_to=str(body.get("reports_to") or "").strip() or None,
            can_spawn=can_spawn or None,
            tool_tags=tool_tags or None,
            is_final_decider=bool(body.get("is_final_decider")),
            team_id=str(body.get("team_id") or "").strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    save_org(dispatcher.org_engine, dispatcher.recruiter)
    return _org_role_to_dict(dispatcher.org_engine.get_role(role_id))


@app.patch("/api/company/roles/{role_id}")
async def company_role_update(role_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """功能：更新岗位（名称/职责/上级/可派生角色/团队归属）。"""
    from bandi.company.org_persistence import save_org

    dispatcher = _org_api_dispatcher()
    role = dispatcher.org_engine.get_role(role_id)
    if role is None:
        raise HTTPException(status_code=404, detail=f"role {role_id!r} not found")
    reports_to = role.reports_to
    if "reports_to" in body:
        reports_to = str(body.get("reports_to") or "").strip() or None
    can_spawn = role.can_spawn
    if "can_spawn" in body:
        can_spawn = [str(x).strip() for x in body.get("can_spawn", []) if str(x).strip()]
    team_id = role.team_id
    if "team_id" in body:
        team_id = str(body.get("team_id") or "").strip()
    try:
        dispatcher.org_engine.register_role(
            role_id,
            str(body.get("name") or role.name).strip() or role.name,
            str(body["responsibility"]) if "responsibility" in body else role.responsibility,
            reports_to=reports_to,
            can_spawn=can_spawn or None,
            tool_tags=list(role.tool_tags),
            is_final_decider=role.is_final_decider,
            team_id=team_id,
            fs_scopes=list(role.fs_scopes),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    save_org(dispatcher.org_engine, dispatcher.recruiter)
    return _org_role_to_dict(dispatcher.org_engine.get_role(role_id))


@app.get("/api/company/org-teams")
async def company_org_teams() -> dict[str, Any]:
    """功能：已配置团队清单（org 角色 team_id 聚合——选择团队弹窗数据源）。

    名称解析：模板库有同名模板用模板名；自定义团队回落 team_id。
    未归属团队的角色不进清单（空 team_id 跳过）。
    """
    dispatcher = _org_api_dispatcher()
    org = dispatcher.org_engine
    grouped: dict[str, dict[str, Any]] = {}
    for role in org.list_roles():
        team_id = str(getattr(role, "team_id", "") or "").strip()
        if not team_id:
            continue
        entry = grouped.setdefault(
            team_id,
            {"team_id": team_id, "name": team_id, "roles": []},
        )
        entry["roles"].append({"role_id": str(role.id), "name": str(role.name)})
    employees = list(org.list_employees(include_inactive=True))
    employee_role_ids = {str(item.role_id) for item in employees}
    team_store = _get_team_store()
    from bandi.company.team_registry import TeamRegistry

    registry_names = TeamRegistry().all()
    template_names: dict[str, str] = {}
    try:
        for template in team_store.list_templates():
            template_name = str(getattr(template, "name", "") or "").strip()
            if not template_name:
                continue
            template_names.setdefault(str(getattr(template, "template_id", "")), template_name)
            bound_team = str(getattr(template, "team_id", "") or "").strip()
            if bound_team:
                template_names.setdefault(bound_team, template_name)
    except Exception:  # noqa: BLE001 模板清单失败仅回退 team_id
        template_names = {}
    for entry in grouped.values():
        entry["role_count"] = len(entry["roles"])
        entry["employee_count"] = sum(
            1 for item in entry["roles"] if str(item["role_id"]) in employee_role_ids
        )
        name = str(registry_names.get(str(entry["team_id"]), "") or "").strip()
        if not name:
            name = template_names.get(str(entry["team_id"]), "")
        if name:
            entry["name"] = name
    return {"teams": sorted(grouped.values(), key=lambda item: str(item["team_id"]))}


@app.get("/api/company/org-chart")
async def company_org_chart() -> dict[str, Any]:
    """组织架构（角色层级 + 直属子级 + 在职员工数）。"""
    dispatcher = _org_api_dispatcher()
    roles = dispatcher.org_engine.list_roles()
    employees = dispatcher.org_engine.list_employees()
    by_role: dict[str, list[str]] = {}
    for e in employees:
        by_role.setdefault(e.role_id, []).append(e.employee_id)
    chart = [
        {
            **_org_role_to_dict(r),
            "subordinates": [x.id for x in roles if x.reports_to == r.id],
            "employees": by_role.get(r.id, []),
        }
        for r in roles
    ]
    return {"chart": chart}


@app.get("/api/company/work-items/{item_id}")
async def company_work_item_get(item_id: str) -> dict[str, Any]:
    """WorkItem 详情。"""
    dispatcher = _org_api_dispatcher()
    item = dispatcher.store.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"work item {item_id!r} not found")
    return _work_item_to_dict(item)


@app.get("/api/company/work-items/{item_id}/journal")
async def company_work_item_journal(item_id: str, limit: int = 200) -> dict[str, Any]:
    """功能：工作项 journal——阶段迁移/认领/完成的追加审计流"""
    dispatcher = _org_api_dispatcher()
    item = dispatcher.store.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"work item {item_id!r} not found")
    entries = dispatcher.store.list_journal(item_id, limit=limit)
    return {"work_item_id": item_id, "entries": entries}


@app.post("/api/company/work-items/{item_id}/review")
async def company_work_item_review(item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """评审决策注入（与 CLI `company review` 同源）——解除 AWAITING_* 滞留
    桌面端评审卡不再永久滞留（此前 review/retry 仅 CLI 可达）。"""
    verdict = str(payload.get("verdict", "")).strip()
    feedback = str(payload.get("feedback", "") or "")
    dispatcher = _org_api_dispatcher()
    from bandi.utils.errors import error_text

    try:
        dispatcher.review_work_item(item_id, verdict, feedback=feedback)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=error_text(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=error_text(e)) from e
    return {"work_item_id": item_id, "verdict": verdict, "recorded": True}


@app.post("/api/company/work-items/{item_id}/retry")
async def company_work_item_retry(item_id: str) -> dict[str, Any]:
    """重试 NEEDS_ATTENTION 工作项（与 CLI `company retry` 同源）。"""
    dispatcher = _org_api_dispatcher()
    from bandi.utils.errors import error_text

    try:
        dispatcher.retry_work_item(item_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=error_text(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=error_text(e)) from e
    return {"work_item_id": item_id, "queued": True}


@app.get("/api/company/talent/catalog")
async def company_talent_catalog() -> dict[str, Any]:
    """人才市场分类目录。"""
    dispatcher = _org_api_dispatcher()
    return {"catalog": dispatcher.talent.list_category_catalog()}


@app.get("/api/company/talent/templates")
async def company_talent_templates(categories: str = "") -> dict[str, Any]:
    """人才市场模板清单（?categories=a,b 过滤可选）。"""
    dispatcher = _org_api_dispatcher()
    cats = [c.strip() for c in categories.split(",") if c.strip()]
    if cats:
        templates = dispatcher.talent.list_templates_by_categories(categories=cats)
    else:
        templates = dispatcher.talent.list_templates()
    return {
        "templates": [
            t.to_dict() if hasattr(t, "to_dict") else _talent_template_dict(t) for t in templates
        ]
    }


def _talent_template_dict(t: Any) -> dict[str, Any]:
    base = {"name": getattr(t, "name", ""), "category": getattr(t, "category", "")}
    for f in ("description", "role_id", "domains"):
        v = getattr(t, f, None)
        if v is not None:
            base[f] = v
    return base


@app.get("/api/company/inbox")
async def company_inbox(role_id: str, project_id: str = "") -> list[dict[str, Any]]:
    """群聊/私信收件箱读取（communication.read_inbox）；起支持按项目读取。"""
    if project_id:
        from bandi.company.projects import ProjectError, ProjectLoadError

        try:
            record = _get_project_store().get_project(project_id)
        except ProjectLoadError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
        from bandi.company.comms import list_inbox

        messages = list_inbox(
            workspace=str(Path.home() / ".bandi" / "company"),
            project_id=project_id,
            session_id="default",
            role_id=role_id,
        )
    else:
        dispatcher = _org_api_dispatcher()
        messages = dispatcher.communication.read_inbox(role_id)
    out = []
    for m in messages:
        entry = {
            # CommsMessage 实际字段为 id/body（read_inbox 返回类型）——取错键致
            "message_id": str(getattr(m, "id", "")),
            "sender_role": str(getattr(m, "sender_role", "")),
            "content": str(getattr(m, "body", "")),
            "created_at": iso_local(getattr(m, "created_at", "")),
        }
        out.append(entry)
    return out


@app.get("/api/settings/sources")
async def settings_sources() -> dict[str, Any]:
    """配置来源分层诊断面（loaded_sources + 规则来源分布）。"""
    from bandi.core.config import BandiConfig

    cfg = BandiConfig.load()
    rule_sources: dict[str, int] = {}
    for rule in cfg.permissions.rules:
        src = str(rule.get("source") or "")
        rule_sources[src] = rule_sources.get(src, 0) + 1
    return {"sources": cfg.loaded_sources, "rule_sources": rule_sources}


@app.get("/api/system/info")
async def system_info() -> dict[str, Any]:
    """功能：关于页运行环境（版本/解释器/数据目录/运行时长，不含任何密钥）。"""
    from bandi.api import settings_ext

    return settings_ext.system_info(data_dir=_resolve_db_path().parent)


@app.get("/api/system/storage")
async def system_storage() -> dict[str, Any]:
    """功能：数据目录占用（各库大小 + 磁盘总量/已用/剩余）。"""
    from bandi.api import settings_ext

    return settings_ext.storage_info(data_dir=_resolve_db_path().parent)


@app.get("/api/settings/preferences")
async def get_preferences() -> dict[str, Any]:
    """功能：设置项现状（权限询问范围 / 保留天数 / 检索凭据是否已配置）。"""
    from bandi.api import settings_ext

    return settings_ext.current_preferences()


@app.put("/api/settings/preferences")
async def put_preferences(body: dict[str, Any]) -> dict[str, Any]:
    """功能：设置项写入——仅接受 permission_preset / retention_days / web_search。

    白名单外键 fail-closed 400；检索凭据落盘前 DPAPI 加密，出参只回「已配置」。
    """
    from bandi.api import settings_ext

    unknown = set(body) - {"permission_preset", "retention_days", "web_search"}
    if unknown:
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST, detail=f"UNKNOWN_FIELDS: {sorted(unknown)}"
        )
    try:
        return settings_ext.apply_preferences(
            preset=body.get("permission_preset"),
            retention_days=body.get("retention_days"),
            web_search=body.get("web_search"),
        )
    except settings_ext.PreferenceError as exc:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail=str(exc)) from exc


@app.get("/api/settings/permission-rules")
async def get_permission_rules() -> dict[str, Any]:
    """功能：权限规则表（allow/ask/deny + 来源）"""
    from bandi.api import settings_ext

    return settings_ext.list_permission_rules()


@app.put("/api/settings/permission-rules")
async def put_permission_rules(body: dict[str, Any]) -> dict[str, Any]:
    """功能：整表保存用户权限规则；非法条目 400"""
    from bandi.api import settings_ext

    try:
        return settings_ext.apply_permission_rules(body.get("rules"))
    except settings_ext.PreferenceError as exc:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail=str(exc)) from exc


@app.get("/api/settings/approval-grants")
async def get_approval_grants() -> dict[str, Any]:
    """功能：always-allow 列表（session/once + 到期时间）"""
    from bandi.tools.approval import get_approval_store

    return {"grants": get_approval_store().list_grants()}


@app.delete("/api/settings/approval-grants")
async def delete_approval_grant(body: dict[str, Any]) -> dict[str, Any]:
    """功能：撤销一条 always-allow（按会话键/工具/参数摘要/范围）"""
    from bandi.tools.approval import get_approval_store

    revoked = get_approval_store().revoke_grant(
        str(body.get("session_key") or ""),
        str(body.get("tool_name") or ""),
        str(body.get("params_digest") or ""),
        str(body.get("scope") or "session"),
    )
    return {"revoked": revoked}


@app.get("/api/settings/retention/preview")
async def retention_preview(days: int = 90) -> dict[str, Any]:
    """功能：清理前预览——与 delete_expired 同口径统计将被删除的会话数（零写入）。"""
    store = _get_store()
    return {"days": int(days), "sessions": store.count_expired(ttl_days=int(days))}


@app.post("/api/settings/retention/cleanup")
async def retention_cleanup(body: dict[str, Any]) -> dict[str, Any]:
    """功能：按保留天数清理会话——必须先归档再删除，且必须显式 confirm=true。"""
    if body.get("confirm") is not True:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="CONFIRM_REQUIRED")
    days = int(body.get("days") or 0)
    if days <= 0:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_DAYS")
    from bandi.memory.maintenance import run_session_maintenance

    store = _get_store()
    before = store.count_expired(ttl_days=days)
    report = run_session_maintenance(
        store,
        _resolve_db_path().parent / "archive",
        ttl_days=days,
        archive_after_days=days,
    )
    after = store.count_expired(ttl_days=days)
    # 功能：口径以「清理前后过期会话数差」为准——先归档后删除时 delete 计数可能为 0
    return {**report, "days": days, "deleted": max(0, before - after), "expired_before": before}


def _mcp_server_payload(server: Any, registered: list[str]) -> dict[str, Any]:
    # 功能：单台 MCP 服务器的设置面出参（配置 + 已注册工具数推断的运行态）
    prefix = str(getattr(server, "prefix", "") or getattr(server, "name", ""))
    names = [
        name for name in registered if name.startswith(prefix) or name.startswith(f"mcp__{prefix}")
    ]
    enabled = bool(getattr(server, "enabled", True))
    status = "disabled" if not enabled else ("connected" if names else "idle")
    return {
        "name": server.name,
        "pool": getattr(server, "pool", "") or "",
        "transport": server.transport,
        "command": server.command,
        "args": list(server.args),
        "url": server.url,
        "prefix": getattr(server, "prefix", "") or "",
        "enabled": enabled,
        "category": str(getattr(server, "category", "") or ""),
        "homepage": str(getattr(server, "homepage", "") or ""),
        "allowed_tools": getattr(server, "allowed_tools", None),
        "status": status,
        "tool_count": len(names),
        "tools": sorted(names),
    }


def _load_mcp_config() -> Any:
    from bandi.core.config import BandiConfig

    return BandiConfig.load()


@app.get("/api/computer/status")
async def computer_status() -> dict[str, Any]:
    """功能：桌面控制状态（开关 / 运行时 / 依赖 / 解释器 / 安装进度）。"""
    from bandi.tools.computer_use import runtime_status

    return await asyncio.to_thread(runtime_status)


@app.post("/api/computer/runtime/install")
async def computer_runtime_install(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """功能：安装桌面控制运行时（后台建 venv + 装依赖，进度由 status 轮询）。"""
    from bandi.tools.computer_use import start_install

    interpreter = str((body or {}).get("interpreter") or "").strip()
    return await start_install(interpreter)


@app.put("/api/computer/interpreter")
async def computer_set_interpreter(body: dict[str, Any]) -> dict[str, Any]:
    """功能：指定运行时解释器（不存在直接 400，不写坏配置）。"""
    from bandi.tools.computer_use.runtime import detect_interpreter

    path = str(body.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="MISSING_PATH")
    if not Path(path).exists():
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INTERPRETER_NOT_FOUND")
    cfg = BandiConfig.load()
    cfg.save(patch={"tools": {"computer_use": {"interpreter": path}}})
    return {"ok": True, "interpreter": detect_interpreter()}


@app.put("/api/computer/enabled")
async def computer_set_enabled(body: dict[str, Any]) -> dict[str, Any]:
    """功能：开关桌面控制——开启必须显式确认且运行时就绪（不给假开关）。"""
    from bandi.tools.computer_use import runtime_status

    if "enabled" not in body:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="MISSING_ENABLED")
    enabled = bool(body["enabled"])
    confirm = bool(body.get("confirm", False))
    if enabled and not confirm:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="CONFIRM_REQUIRED")
    if enabled:
        status = await asyncio.to_thread(runtime_status)
        if not status["deps_installed"]:
            raise HTTPException(
                status_code=HTTP_CONFLICT,
                detail="RUNTIME_NOT_READY",
            )
    cfg = BandiConfig.load()
    cfg.save(patch={"tools": {"computer_use": {"enabled": enabled}}})
    from bandi.tools.computer_use.safety import record_audit

    record_audit("computer_use_toggle", "ok", {"enabled": enabled, "confirm": confirm})
    status = await asyncio.to_thread(runtime_status)
    return {"ok": True, "enabled": enabled, "status": status}


@app.get("/api/mcp/servers")
async def mcp_servers() -> dict[str, Any]:
    """功能：MCP 服务器清单——配置项 + 当前进程已注册工具数（推断运行态）。"""
    from bandi.core.agent_runner import _MCP_BROKER_CACHE

    cfg = _load_mcp_config()
    registered: list[str] = []
    for broker in _MCP_BROKER_CACHE.values():
        registered = list(getattr(broker, "mcp_tool_names", []) or [])
        break
    tools_cfg = cfg.tools
    return {
        "servers": [_mcp_server_payload(server, registered) for server in tools_cfg.mcp_servers],
        "global": {
            "autostart": bool(tools_cfg.mcp_autostart),
            "lazy_exposure": bool(tools_cfg.mcp_lazy_exposure),
            "max_exposed_tools": int(tools_cfg.mcp_max_exposed_tools),
        },
    }


@app.put("/api/mcp/global")
async def put_mcp_global(body: dict[str, Any]) -> dict[str, Any]:
    """功能：MCP 全局开关（自动连接 / 懒暴露 / 单轮上限），只收三个键。"""
    allowed = {"autostart", "lazy_exposure", "max_exposed_tools"}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST, detail=f"UNKNOWN_FIELDS: {sorted(unknown)}"
        )
    patch: dict[str, Any] = {}
    if "autostart" in body:
        patch["mcp_autostart"] = bool(body["autostart"])
    if "lazy_exposure" in body:
        patch["mcp_lazy_exposure"] = bool(body["lazy_exposure"])
    if "max_exposed_tools" in body:
        value = int(body["max_exposed_tools"])
        if value < 0 or value > 50:
            raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_MAX_TOOLS")
        patch["mcp_max_exposed_tools"] = value
    if not patch:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="NO_FIELD")
    cfg = _load_mcp_config()
    cfg.save(patch={"tools": patch})
    return {"ok": True}


@app.get("/api/mcp/config")
async def get_mcp_config() -> dict[str, Any]:
    """功能：读取 mcp.json 编辑文档与路径。"""
    from bandi.core.mcp_file import mcp_config_path
    from bandi.resources.mcp_config import MCPConfigError, load_document

    try:
        document = load_document()
    except MCPConfigError as exc:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail=str(exc)) from exc
    return {"path": str(mcp_config_path()), "content": document}


@app.put("/api/mcp/config")
async def put_mcp_config(body: dict[str, Any]) -> dict[str, Any]:
    """功能：保存 mcp.json（校验 mcpServers 结构后原子落盘）。"""
    from bandi.resources.mcp_config import MCPConfigError, save_document

    try:
        path = save_document(body)
    except MCPConfigError as exc:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail=str(exc)) from exc
    servers = body.get("mcpServers") if isinstance(body, dict) else {}
    return {"ok": True, "path": str(path), "count": len(servers or {})}


@app.patch("/api/mcp/servers/{name}")
async def patch_mcp_server(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """功能：改单台服务器开关 / 白名单 / 分类 / 主页（白名单字段，其余 400）。"""
    from bandi.resources.mcp_config import MCPConfigError, load_servers, save_servers

    allowed = {"enabled", "allowed_tools", "category", "homepage"}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST, detail=f"UNKNOWN_FIELDS: {sorted(unknown)}"
        )
    try:
        servers = load_servers()
    except MCPConfigError as exc:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail=str(exc)) from exc
    target = next((item for item in servers if item.name == name), None)
    if target is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="MCP_SERVER_NOT_FOUND")
    if "allowed_tools" in body:
        value = body["allowed_tools"]
        if value is not None and not isinstance(value, list):
            raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="INVALID_ALLOWED_TOOLS")
        target.allowed_tools = value
    if "enabled" in body:
        target.enabled = bool(body["enabled"])
    for field in ("category", "homepage"):
        if field in body:
            setattr(target, field, str(body[field] or "").strip())
    try:
        save_servers(servers)
    except MCPConfigError as exc:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail=str(exc)) from exc
    return {"ok": True, "server": _mcp_server_payload(target, [])}


@app.post("/api/mcp/servers/{name}/test")
async def test_mcp_server(name: str) -> dict[str, Any]:
    """功能：试连单台服务器（10s 上限，只读不写配置），返回工具数或失败原因。"""
    from bandi.tools.mcp_client import MCPClient, StdioTransport, StreamableHTTPTransport

    cfg = _load_mcp_config()
    server = next((item for item in cfg.tools.mcp_servers if item.name == name), None)
    if server is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="MCP_SERVER_NOT_FOUND")
    transport = (
        (
            StdioTransport(server.command, server.args, env=server.env)
            if server.env
            else StdioTransport(server.command, server.args)
        )
        if server.transport == "stdio"
        else StreamableHTTPTransport(server.url)
    )
    client = MCPClient(server.name, transport)
    try:
        await asyncio.wait_for(client.connect(), timeout=10)
        tools = await asyncio.wait_for(client.list_tools(), timeout=10)
        return {
            "ok": True,
            "tool_count": len(tools),
            "error": "",
            "tools": [t.get("name", "") for t in tools],
        }
    except Exception as exc:  # noqa: BLE001 - 试连失败原因必须原样回给设置页
        return {"ok": False, "tool_count": 0, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            await client.disconnect()
        except Exception:
            logger.debug("MCP test client disconnect failed", exc_info=True)


def _subagent_task_dict(result: Any) -> dict[str, Any]:
    # 功能：完成快照序列化（API 面）
    return {
        "task_id": result.task_id,
        "session_id": result.session_id,
        "success": result.success,
        "summary": result.summary,
        "message": result.message,
        "total_tokens": result.total_tokens,
        "completed_at": iso_local(result.completed_at),
    }


@app.get("/api/subagent/tasks")
async def subagent_tasks_list() -> dict[str, Any]:
    """子代理任务面——活跃 task_id 列表 + 最近完成快照（FIFO 200）。"""
    from bandi.agent.subagent import list_active_tasks, list_completed_tasks

    return {
        "active": list_active_tasks(),
        "completed": [_subagent_task_dict(r) for r in list_completed_tasks()],
    }


@app.post("/api/subagent/tasks/{task_id}/cancel")
async def subagent_task_cancel(task_id: str) -> dict[str, Any]:
    """定向取消活跃子代理任务（不中断父对话）。"""
    from bandi.agent.subagent import cancel_active_task

    if not cancel_active_task(task_id):
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="SUBAGENT_TASK_NOT_FOUND")
    return {"cancelled": True, "task_id": task_id}


# 功能：资料库节点 API 路由挂载（独立模块，避免主文件继续膨胀）。
from bandi.api.knowledge_nodes_api import router as _knowledge_nodes_router  # noqa: E402

app.include_router(_knowledge_nodes_router)


# ==================== 前端 SPA 静态托管====================
# 功能：生产/桌面端由同一个 FastAPI 进程托管前端构建产物（frontend/dist），
# 使 electron 壳只需 spawn 一个 sidecar 就能同时提供 SPA + API + WS。
# 约束：必须在所有 API/WS 路由注册之后挂载（FastAPI 按注册顺序匹配）；
#       /api、/ws、/health、/ready 等不得被 SPA fallback 吞掉。
# 开关：BANDI_SERVE_SPA=0/false/no/off 强制关闭（dev 走 vite dev server 时无需托管）。
# 覆盖：BANDI_FRONTEND_DIST 显式指定 dist 目录（优先级最高）。

from fastapi.staticfiles import StaticFiles as _StaticFiles  # noqa: E402

_FRONTEND_DIST_ENV = "BANDI_FRONTEND_DIST"


def _resolve_frontend_dist() -> Path | None:
    """功能：按优先级解析前端构建产物目录，返回首个含 index.html 的候选。"""
    candidates: list[Path] = []
    override = os.environ.get(_FRONTEND_DIST_ENV, "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    home = Path(os.environ.get("BANDI_HOME", os.path.expanduser("~/.bandi"))).expanduser()
    # 桌面端「dist 外部替换」：用户数据目录优先，便于发版后只下发新 dist 而不重打壳
    candidates.append(home / "frontend" / "dist")
    # 仓库内开发态：<repo>/bandi/bandi/api/app.py → <repo>/frontend/dist
    candidates.append(Path(__file__).resolve().parents[3] / "frontend" / "dist")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


class _SPAStaticFiles(_StaticFiles):
    """功能：静态文件服务 + SPA fallback（未命中的非保留路径回 index.html）。"""

    def __init__(self, *args, known_paths: set[str] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._known_paths = known_paths or set()

    async def get_response(self, path: str, scope):  # 签名与 Starlette StaticFiles 保持一致
        normalized = "/" + path.replace("\\", "/").lstrip("/")
        head = normalized.lstrip("/").split("/", 1)[0]
        if head in _NON_SPA_PREFIXES:
            # 保留前缀不回落 HTML；已注册路由的方法不匹配返回 405，其余 404
            if scope.get("method") not in ("GET", "HEAD") and normalized in self._known_paths:
                raise HTTPException(status_code=405, detail="METHOD_NOT_ALLOWED")
            raise HTTPException(status_code=HTTP_NOT_FOUND, detail="NOT_FOUND")
        try:
            return await super().get_response(path, scope)
        except (_StarletteHTTPException, RuntimeError):
            # 深链（/work、/company 等）在浏览器刷新时必须回 index.html，否则 404
            return await super().get_response("index.html", scope)


def _mount_frontend_spa() -> Path | None:
    """功能：挂载 SPA 静态托管；返回实际使用的 dist 目录（未挂载返回 None）。"""
    flag = os.environ.get("BANDI_SERVE_SPA", "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return None
    dist = _resolve_frontend_dist()
    if dist is None:
        return None
    known_paths = {
        route.path for route in app.router.routes if isinstance(getattr(route, "path", None), str)
    }
    app.mount(
        "/",
        _SPAStaticFiles(directory=str(dist), html=True, known_paths=known_paths),
        name="frontend-spa",
    )
    return dist


SERVED_FRONTEND_DIST = _mount_frontend_spa()
