# 功能：桌面操作脚本（在 Computer Use 独立 venv 内运行；只依赖 mss/pyautogui）
from __future__ import annotations

import json
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

_SYSTEM = platform.system()


def _enable_dpi_awareness() -> bool:
    # 功能：声明 DPI 感知——坐标与截图统一到物理像素（否则缩放屏上点击会偏）
    if _SYSTEM != "Windows":
        return False
    try:
        import ctypes

        # 2 = PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return True
    except Exception:  # noqa: BLE001 - 老系统回落到 system aware
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
            return True
        except Exception:  # noqa: BLE001
            return False


DPI_AWARE = _enable_dpi_awareness()


def _scale_factor() -> float:
    # 功能：系统缩放比（物理像素 / 逻辑像素，如 125% ⇒ 1.25）；取不到按 1.0
    if _SYSTEM != "Windows":
        return 1.0
    try:
        import ctypes

        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        if dpi <= 0:
            dpi = 96
        return round(max(0.5, min(4.0, dpi / 96.0)), 3)
    except Exception:  # noqa: BLE001 - 老系统取不到 DPI 时按 100%
        return 1.0


def _import(name: str):
    # 功能：延迟导入（缺依赖时给明确错误而不是半截行为）
    module = __import__(name)
    return module


def _win_title(hwnd) -> str:
    # 功能：取窗口标题（失败空串）
    import ctypes

    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value or ""


def _root_title(hwnd) -> str:
    # 功能：取顶层窗口标题（子控件回溯到根）
    if not hwnd:
        return ""
    import ctypes

    root = ctypes.windll.user32.GetAncestor(hwnd, 2) or hwnd
    return _win_title(root)


def _foreground_title() -> str:
    # 功能：当前前台窗口标题（非 Windows 返回空串）
    if _SYSTEM != "Windows":
        return ""
    import ctypes

    return _win_title(ctypes.windll.user32.GetForegroundWindow())


def _title_at(x: int, y: int) -> str:
    # 功能：屏幕坐标处的窗口标题——回显「实际点到的是哪个窗口」
    if _SYSTEM != "Windows":
        return ""
    import ctypes
    import ctypes.wintypes

    point = ctypes.wintypes.POINT(int(x), int(y))
    hwnd = ctypes.windll.user32.WindowFromPoint(point)
    return _root_title(hwnd)


def _virtual_monitor(sct) -> dict:
    # 功能：虚拟屏幕（多屏合并）几何——monitors[0]
    return dict(sct.monitors[0])


def _screenshot(params: dict) -> dict:
    mss = _import("mss")
    out_dir = Path(params.get("out_dir") or (Path.home() / ".bandi" / "tmp" / "computer"))
    out_dir.mkdir(parents=True, exist_ok=True)
    region = params.get("region")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    path = out_dir / f"screen-{stamp}.png"
    with mss.mss() as sct:
        monitor = _virtual_monitor(sct)
        if region:
            box = {
                "left": int(region.get("x", 0)),
                "top": int(region.get("y", 0)),
                "width": int(region.get("width", monitor["width"])),
                "height": int(region.get("height", monitor["height"])),
            }
        else:
            box = monitor
        shot = sct.grab(box)
        mss.tools.to_png(shot.rgb, shot.size, output=str(path))
        monitors = [dict(m) for m in sct.monitors[1:]]
    scale = _scale_factor()
    return {
        "ok": True,
        "path": str(path),
        "width": shot.width,
        "height": shot.height,
        "origin": {"x": box["left"], "y": box["top"]},
        "monitors": monitors,
        "dpi_aware": DPI_AWARE,
        # 功能：显式坐标系——物理像素为准；逻辑坐标 × scale = 物理坐标
        "coordinate_space": "physical",
        "scale": scale,
        "screen_logical": {
            "width": int(round(monitor["width"] / scale)),
            "height": int(round(monitor["height"] / scale)),
        },
        "foreground_window": _foreground_title(),
        "hint": (
            "截图与点击都用物理像素：点击坐标 = origin + 截图像素；"
            f"若坐标来自系统显示设置（逻辑坐标），先乘以 scale={scale}"
        ),
    }


def _click(params: dict) -> dict:
    pyautogui = _import("pyautogui")
    x = int(params["x"])
    y = int(params["y"])
    button = str(params.get("button") or "left")
    clicks = int(params.get("clicks") or 1)
    if button not in {"left", "right", "middle"}:
        return {"ok": False, "error": f"非法按钮：{button}"}
    if clicks < 1 or clicks > 3:
        return {"ok": False, "error": "clicks 必须在 1~3"}
    target = _title_at(x, y)
    pyautogui.moveTo(x, y, duration=float(params.get("duration") or 0.1))
    pyautogui.click(button=button, clicks=clicks, interval=0.08)
    return {
        "ok": True,
        "x": x,
        "y": y,
        "button": button,
        "clicks": clicks,
        "window": target,
        "dpi_aware": DPI_AWARE,
    }


def _type(params: dict) -> dict:
    pyautogui = _import("pyautogui")
    text = str(params.get("text") or "")
    if not text:
        return {"ok": False, "error": "text 不能为空"}
    interval = float(params.get("interval") or 0.02)
    ascii_only = text.isascii()
    if ascii_only:
        pyautogui.write(text, interval=interval)
        mode = "keystrokes"
    else:
        # 功能：非 ASCII 走剪贴板粘贴（pyautogui.write 只支持单键字符）
        pyperclip = _import("pyperclip")
        pyperclip.copy(text)
        time.sleep(0.05)
        pyautogui.hotkey("command" if _SYSTEM == "Darwin" else "ctrl", "v")
        mode = "clipboard"
    if params.get("press_enter"):
        pyautogui.press("enter")
    return {
        "ok": True,
        "length": len(text),
        "mode": mode,
        "window": _foreground_title(),
        "dpi_aware": DPI_AWARE,
    }


def _scroll(params: dict) -> dict:
    pyautogui = _import("pyautogui")
    amount = int(params.get("amount") or 0)
    if amount == 0:
        return {"ok": False, "error": "amount 不能为 0（正数向上、负数向下）"}
    x, y = params.get("x"), params.get("y")
    if x is not None and y is not None:
        pyautogui.scroll(amount, int(x), int(y))
    else:
        pyautogui.scroll(amount)
    return {"ok": True, "amount": amount, "window": _foreground_title()}


def _info(_params: dict) -> dict:
    pyautogui = _import("pyautogui")
    mss = _import("mss")
    with mss.mss() as sct:
        monitors = [dict(m) for m in sct.monitors]
    scale = _scale_factor()
    return {
        "ok": True,
        "platform": _SYSTEM,
        "screen": {"width": int(pyautogui.size()[0]), "height": int(pyautogui.size()[1])},
        "coordinate_space": "physical",
        "scale": scale,
        "monitors": monitors,
        "dpi_aware": DPI_AWARE,
        "python": f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}",
    }


_ACTIONS = {
    "screenshot": _screenshot,
    "click": _click,
    "type": _type,
    "scroll": _scroll,
    "info": _info,
}


def main() -> int:
    # 功能：从 stdin 读 {action, params}，执行后把结果以 JSON 打到 stdout 最后一行
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw or "{}")
        action = str(payload.get("action") or "")
        handler = _ACTIONS.get(action)
        if handler is None:
            print(json.dumps({"ok": False, "error": f"未知动作：{action}"}, ensure_ascii=False))
            return 2
        result = handler(dict(payload.get("params") or {}))
    except Exception as exc:  # noqa: BLE001 - 任何异常都必须以结构化结果返回
        print(
            json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
