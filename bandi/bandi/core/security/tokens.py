# 功能：签名令牌签发/校验（+ TTL，零第三方依赖）
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import time
from typing import Any

from bandi.core.security.secret_protect import unprotect

# 令牌版本前缀（格式不兼容变更时递增）
TOKEN_VERSION = "v1"

# 缺省有效期与上限（秒）
DEFAULT_TTL_SECONDS = 3600
MAX_TTL_SECONDS = 86400

_AUTH_SECRET_ENV = "BANDI_AUTH_SECRET"
_API_KEY_ENV = "BANDI_API_KEY"


def resolve_token_secret() -> str:
    # 功能：解析令牌签名密钥——BANDI_AUTH_SECRET 优先，缺省从 BANDI_API_KEY 派生，都无则禁用（空串）
    secret = os.environ.get(_AUTH_SECRET_ENV, "").strip()
    if secret:
        return secret
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if api_key:
        # 安全：API key 可能以 dpapi: 密文存放——先用解密后明文派生，与 HTTP/WS
        # 鉴权使用的明文 key 同源（密文派生会让签名令牌与静态 key 两套口径）
        api_key = unprotect(api_key)
        return hashlib.sha256(("bandi-token:" + api_key).encode("utf-8")).hexdigest()
    return ""


def _b64encode(data: bytes) -> str:
    # 功能：URL 安全 base64 编码（去 padding）
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    # 功能：URL 安全 base64 严格解码（容忍缺失 padding；非法字符抛 ValueError）
    padding = "=" * (-len(text) % 4)
    return base64.b64decode(text + padding, altchars=b"-_", validate=True)


def _sign(signing_input: str, secret: str) -> str:
    # 功能：计算签名段（十六进制）
    return hmac.new(
        secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def issue_token(
    subject: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS, secret: str, now: float | None = None
) -> str:
    # 功能：签发 v1.<payload_b64>.<hmac> 令牌（payload 含 sub/iat/exp/nonce；ttl 收敛到 [1, 上限]）
    if not secret:
        raise ValueError("token signing secret is not configured")
    issued_at = int(time.time() if now is None else float(now))
    ttl = max(1, min(int(ttl_seconds), MAX_TTL_SECONDS))
    payload = {
        "sub": str(subject),
        "iat": issued_at,
        "exp": issued_at + ttl,
        "nonce": secrets.token_hex(8),
    }
    payload_b64 = _b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{TOKEN_VERSION}.{payload_b64}"
    return f"{signing_input}.{_sign(signing_input, secret)}"


def _is_finite_number(value: Any) -> bool:
    # 功能：判断值是否为有限数值（bool 不算；NaN/Infinity 拒绝）
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def verify_token(token: str, *, secret: str, now: float | None = None) -> dict[str, Any] | None:
    # 功能：校验令牌（v1 前缀/严格 base64/恒定时间比较/sub、nonce、有限 exp/iat/过期）；非法返回 None
    if not secret or not token:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        return None
    signing_input = f"{parts[0]}.{parts[1]}"
    expected = _sign(signing_input, secret)
    try:
        if not hmac.compare_digest(expected, parts[2]):
            return None
    except TypeError:  # 签名段含非 ASCII 字符（攻击者输入）——直接拒绝
        return None
    try:
        payload = json.loads(_b64decode(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        return None
    if not isinstance(payload.get("nonce"), str) or not payload["nonce"]:
        return None
    exp = payload.get("exp")
    iat = payload.get("iat")
    # NaN/Infinity 会被 json.loads 放行——`current >= nan` 恒 False 使令牌永不过期
    if not _is_finite_number(exp) or not _is_finite_number(iat):
        return None
    current = time.time() if now is None else float(now)
    if current >= float(exp):
        return None
    return payload
