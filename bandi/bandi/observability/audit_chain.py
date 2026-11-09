# 功能：审计链 完整性（可选密钥；缺省禁用，向后兼容纯哈希链）
from __future__ import annotations

import hashlib
import hmac
import os

AUDIT_HMAC_KEY_ENV = "BANDI_AUDIT_HMAC_KEY"


def resolve_audit_hmac_key() -> str:
    # 功能：解析审计链 密钥（未配置返回空串 = 禁用）
    return os.environ.get(AUDIT_HMAC_KEY_ENV, "").strip()


def compute_entry_hmac(raw: str, key: str) -> str:
    # 功能：对条目规范 JSON 计算 十六进制摘要
    return hmac.new(key.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_entry_hmac(raw: str, stored: object, key: str) -> bool:
    # 功能：恒定时间校验条目 （空值/非字符串直接拒绝，坏数据不抛异常）
    if not isinstance(stored, str) or not stored:
        return False
    return hmac.compare_digest(compute_entry_hmac(raw, key), stored)
