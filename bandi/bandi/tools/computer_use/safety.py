# 功能：安全边界——敏感窗口识别、行为审计、运行时路径
from __future__ import annotations

import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 功能：命中即拒绝的窗口标题片段（密码/银行/支付/凭据管理器）
SENSITIVE_WINDOW_PATTERNS: tuple[str, ...] = (
    "1password",
    "bitwarden",
    "lastpass",
    "keepass",
    "password",
    "密码",
    "密钥",
    "凭据",
    "银行",
    "网银",
    "支付",
    "支付宝",
    "微信支付",
    "wallet",
    "keychain",
    "login",
    "登录",
)


def _foreground_title_windows() -> str:
    # 功能：取前台窗口标题（仅 Windows；其余平台返回空串 = 不做窗口判定）
    import ctypes

    user32 = ctypes.windll.user32
    handle = user32.GetForegroundWindow()
    if not handle:
        return ""
    length = user32.GetWindowTextLengthW(handle)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, length + 1)
    return buffer.value or ""


def foreground_window_title() -> str:
    # 功能：跨平台取前台窗口标题（失败一律空串，不抛异常）
    if platform.system() != "Windows":
        return ""
    try:
        return _foreground_title_windows()
    except Exception:  # noqa: BLE001 - 取标题失败不得阻断工具执行
        return ""


def match_sensitive(title: str) -> str:
    # 功能：返回命中的敏感片段（未命中空串）
    lowered = (title or "").lower()
    if not lowered:
        return ""
    for pattern in SENSITIVE_WINDOW_PATTERNS:
        if pattern in lowered:
            return pattern
    return ""


def audit_path() -> Path:
    # 功能：桌面控制审计文件路径（随 Path.home 派生，测试可隔离）
    return Path.home() / ".bandi" / "logs" / "computer-audit.jsonl"


def record_audit(action: str, outcome: str, detail: dict[str, Any] | None = None) -> None:
    # 功能：追加一行审计（fsync 落盘；写失败不阻断主链）
    path = audit_path()
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "pid": os.getpid(),
        "action": action,
        "outcome": outcome,
        "detail": detail or {},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass


def screen_geometry() -> dict[str, Any]:
    # 功能：屏幕几何（无第三方依赖的兜底实现，供未装运行时时的状态展示）
    system = platform.system()
    if system == "Windows":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            return {
                "width": int(user32.GetSystemMetrics(0)),
                "height": int(user32.GetSystemMetrics(1)),
                "virtual": True,
            }
        except Exception:  # noqa: BLE001 - 几何取不到不影响状态面
            return {}
    if system == "Darwin":
        try:
            out = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            return {"raw": out.stdout[:400]}
        except (OSError, subprocess.SubprocessError):
            return {}
    return {}
