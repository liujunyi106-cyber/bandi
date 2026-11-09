# 功能：敏感信息掩码（脱敏 Key/Token）
from __future__ import annotations

from typing import Any

SENSITIVE_KEYS = {
    "password",
    "token",
    "secret",
    "key",
    "credential",
    "auth",
    "api_key",
    "access_key",
}


def mask_sensitive(value: str, max_visible: int = 4) -> str:
    if not value or len(value) <= max_visible:
        return "****"
    return value[:max_visible] + "****"


def _mask_value(value: Any) -> Any:
    if isinstance(value, dict):
        return mask_dict(value)
    if isinstance(value, list):
        return [_mask_value(v) for v in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    """敏感键匹配改精确/后缀边界——裸 "key" 子串匹配会误伤
    monkey_patch/key_value/hockey/turkey 等正常字段。"""
    import re

    lowered = key.lower()
    if lowered in SENSITIVE_KEYS:
        return True
    # 纯 key/token 前缀形态（key_value/key_type）歧义不误伤；
    # 但 secret_/password_/auth_ 前缀本身即敏感词
    if re.match(r"^(?:key|token)_", lowered):
        return False
    # 后缀/组合匹配：api_key / access_key / secret_key / auth_token / client_secret /
    # credentials（复数）等
    return bool(
        re.search(
            r"(?:^|_)(?:api_?|access_?|secret_?|private_?|client_?)?(?:key|token|secret|password|credential|credentials|auth)(?:$|_)",
            lowered,
        )
    )


def mask_dict(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in data.items():
        if _is_sensitive_key(k):
            # 敏感键的整个值掩码（含嵌套结构，防泄漏内部键名）
            result[k] = mask_sensitive(str(v)) if isinstance(v, str) else "****"
        else:
            result[k] = _mask_value(v)
    return result
