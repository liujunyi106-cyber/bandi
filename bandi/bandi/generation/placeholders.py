# 功能：确定性占位媒体编码器（stdlib-only：PNG/WAV/MP4）
from __future__ import annotations

import hashlib
import struct
import zlib


def color_from_prompt(prompt: str) -> tuple[int, int, int]:
    """从提示词派生稳定 RGB 三元组（偏中亮色，任何主题下可读）。"""
    digest = hashlib.sha256((prompt or "asset").encode("utf-8")).digest()
    r = 64 + digest[0] % 160
    g = 64 + digest[1] % 160
    b = 64 + digest[2] % 160
    return r, g, b


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def solid_png(
    width: int = 512, height: int = 512, rgb: tuple[int, int, int] = (90, 120, 200)
) -> bytes:
    """编码一张纯色 RGB PNG（zlib + CRC，真实可解析）。"""
    width = max(1, min(int(width), 2048))
    height = max(1, min(int(height), 2048))
    r, g, b = (max(0, min(int(c), 255)) for c in rgb)
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    row = b"\x00" + bytes((r, g, b)) * width
    raw = row * height
    idat = zlib.compress(raw, 9)
    return (
        signature + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")
    )


def _mp4_box(tag: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + tag + payload


def placeholder_mp4(seed: str = "") -> bytes:
    """最小结构合法 MP4（ftyp + mdat），正确 magic，可被 MIME 嗅探与下载。"""
    ftyp = _mp4_box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso2mp41")
    payload = hashlib.sha256((seed or "video").encode("utf-8")).digest()
    mdat = _mp4_box(b"mdat", payload)
    return ftyp + mdat


def silent_wav(seed: str = "", *, duration_sec: float = 1.0, sample_rate: int = 16000) -> bytes:
    """编码确定性小声量 16-bit 单声道 PCM WAV（结构合法可播放）。"""
    duration_sec = max(0.1, min(float(duration_sec), 30.0))
    sample_rate = max(8000, min(int(sample_rate), 48000))
    n_samples = int(duration_sec * sample_rate)
    digest = hashlib.sha256((seed or "audio").encode("utf-8")).digest()
    amplitude = 16 + digest[0] % 48
    frames = bytearray()
    for i in range(n_samples):
        sign = 1 if (i // 64) % 2 == 0 else -1
        frames += struct.pack("<h", sign * amplitude)

    byte_rate = sample_rate * 2
    data_size = len(frames)
    header = b"RIFF"
    header += struct.pack("<I", 36 + data_size)
    header += b"WAVE"
    header += b"fmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
    header += b"data"
    header += struct.pack("<I", data_size)
    return header + bytes(frames)


__all__ = ["color_from_prompt", "placeholder_mp4", "silent_wav", "solid_png"]
