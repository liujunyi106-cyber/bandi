# 功能：API key 静态加密（Windows ；非 Windows 或调用失败原样返回不阻塞）
from __future__ import annotations

import base64
import binascii
import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger(__name__)

DPAPI_PREFIX = "dpapi:"

# 禁止 弹出任何 UI（服务/CLI 场景不能阻塞在对话框上）
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    # 功能：DATA_BLOB 结构（长度 + 字节指针）
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_available() -> bool:
    # 功能：探测 Windows 是否可用（非 Windows 平台返回 False）
    return hasattr(ctypes, "windll")


def _blob_for(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    # 功能：构造输入 DATA_BLOB 并保持底层缓冲引用（防 GC 释放）
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _call_dpapi(data: bytes, *, decrypt: bool) -> bytes | None:
    # 功能：调用 CryptProtectData/CryptUnprotectData（失败返回 None，不抛出）
    if not _dpapi_available():
        return None
    windll = ctypes.windll
    blob_in, _buffer = _blob_for(data)
    blob_out = _DataBlob()
    api = windll.crypt32.CryptUnprotectData if decrypt else windll.crypt32.CryptProtectData
    ok = api(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        return None
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        windll.kernel32.LocalFree(blob_out.pbData)


def _protect_bytes(data: bytes) -> bytes | None:
    # 功能：加密字节
    return _call_dpapi(data, decrypt=False)


def _unprotect_bytes(data: bytes) -> bytes | None:
    # 功能：解密字节
    return _call_dpapi(data, decrypt=True)


def is_protected(text: str) -> bool:
    # 功能：判断值是否为 dpapi: 前缀的受保护密文
    return bool(text) and text.startswith(DPAPI_PREFIX)


def protect(text: str) -> str:
    # 功能：加密明文为 dpapi:<base64>；空值/已保护值/不可用或失败时原样返回并告警
    if not text or is_protected(text):
        return text
    encrypted = _protect_bytes(text.encode("utf-8"))
    if encrypted is None:
        logger.warning("DPAPI protect unavailable or failed; value kept unchanged")
        return text
    return DPAPI_PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect(text: str) -> str:
    # 功能：解密 dpapi:<base64>；非保护值零行为，解密失败保留原值并告警（fail-safe）
    if not is_protected(text):
        return text
    try:
        encrypted = base64.b64decode(text[len(DPAPI_PREFIX) :], validate=True)
    except (binascii.Error, ValueError):
        logger.warning("DPAPI value is not valid base64; keeping raw value")
        return text
    decrypted = _unprotect_bytes(encrypted)
    if decrypted is None:
        logger.warning("DPAPI decrypt failed; keeping raw value")
        return text
    try:
        return decrypted.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("DPAPI decrypted payload is not valid utf-8; keeping raw value")
        return text
