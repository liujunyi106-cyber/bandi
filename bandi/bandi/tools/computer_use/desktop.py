# 功能：Computer Use 四个桌面工具（截屏/点击/打字/滚动）
from __future__ import annotations

import asyncio
from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.computer_use.runtime import ComputerRuntimeError, run_action
from bandi.tools.computer_use.safety import (
    foreground_window_title,
    match_sensitive,
    record_audit,
)

_DISABLED_HINT = "桌面控制在设置 → Computer Use 中处于关闭状态；请先在设置里启用并完成环境安装。"


def _config() -> Any:
    # 功能：实时读配置（关掉开关后下一次调用立即失效）
    from bandi.core.config import BandiConfig

    return BandiConfig.load().tools.computer_use


class _ComputerTool(BaseTool):
    # 功能：桌面工具公共门禁——开关、敏感窗口、审计、运行时异常归一
    category = ToolCategory.UTIL
    tags = ["computer", "util"]
    capability_tags = ["dangerous"]
    risk_level = "high"
    dedup_scope = "none"
    feature_group = "computer_use"
    action: str = ""

    def _guard(self) -> dict[str, Any] | None:
        # 功能：返回拒绝结果（None = 放行）
        cfg = _config()
        if not bool(getattr(cfg, "enabled", False)):
            record_audit(self.name, "denied", {"reason": "disabled"})
            return {"ok": False, "error": "COMPUTER_USE_DISABLED", "hint": _DISABLED_HINT}
        title = foreground_window_title()
        hit = "" if bool(getattr(cfg, "allow_sensitive_windows", False)) else match_sensitive(title)
        if hit:
            record_audit(self.name, "denied", {"reason": "sensitive_window", "pattern": hit})
            return {
                "ok": False,
                "error": "SENSITIVE_WINDOW",
                "window_title": title,
                "hint": f"前台窗口命中敏感词「{hit}」，已拒绝执行；请先切换到目标窗口。",
            }
        return None

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str | dict[str, Any]:
        blocked = self._guard()
        if blocked is not None:
            return blocked
        payload = {key: value for key, value in params.items() if value is not None}
        try:
            result = await asyncio.to_thread(run_action, self.action, payload)
        except ComputerRuntimeError as exc:
            record_audit(self.name, "runtime_error", {"error": str(exc)})
            return {
                "ok": False,
                "error": "RUNTIME_NOT_READY",
                "detail": str(exc),
                "hint": "请在设置 → Computer Use 中点击「安装环境」并等待就绪。",
            }
        record_audit(self.name, "ok", {"params": _summarize_params(self.action, params)})
        return result


def _summarize_params(action: str, params: dict[str, Any]) -> dict[str, Any]:
    # 功能：审计里只留可复核的关键字段（不落输入正文）
    if action == "type":
        text = str(params.get("text") or "")
        return {
            "length": len(text),
            "preview": text[:20],
            "press_enter": bool(params.get("press_enter")),
        }
    return {
        key: params[key]
        for key in ("x", "y", "button", "clicks", "amount", "region")
        if key in params
    }


class ComputerScreenshotTool(_ComputerTool):
    name = "computer_screenshot"
    user_facing_name = "桌面截图"
    action = "screenshot"
    # 功能：截屏是只读动作——风险级 medium 不进默认询问集合（high/critical），
    # 否则模型每一步都要为「看一眼」弹一次权限卡，实际用不起来（对齐 Claude 的截图不拦）。
    # 代价：不弹卡 = 不经过用户确认就能读屏，故敏感窗口拒绝与审计仍强制执行（见 _guard）。
    risk_level = "medium"
    description = (
        "截取当前桌面屏幕并保存为 PNG 文件。返回文件路径、像素尺寸、坐标原点和缩放比例 scale；"
        "截图覆盖所有显示器的拼接区域，像素 (0,0) 对应返回的 origin。"
        "坐标一律用**物理像素**：点击坐标 = origin + 截图像素；"
        "若你手上是逻辑坐标（如系统显示设置里的分辨率），先乘以 scale。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "region": {
                "type": "object",
                "description": "可选：只截取某区域（屏幕绝对坐标）",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
                "required": ["x", "y", "width", "height"],
                "additionalProperties": False,
            }
        },
        "additionalProperties": False,
    }


class ComputerClickTool(_ComputerTool):
    name = "computer_click"
    user_facing_name = "桌面点击"
    action = "click"
    description = (
        "在屏幕坐标 (x, y) 处点击鼠标。坐标使用电脑屏幕绝对坐标（不是截图相对坐标，"
        "需加上截图返回的 origin）。支持左/右/中键与 1~3 次点击。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "屏幕绝对横坐标"},
            "y": {"type": "integer", "description": "屏幕绝对纵坐标"},
            "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
            "clicks": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
        },
        "required": ["x", "y"],
        "additionalProperties": False,
    }


class ComputerTypeTool(_ComputerTool):
    name = "computer_type"
    user_facing_name = "桌面打字"
    action = "type"
    description = (
        "向当前焦点窗口输入文本。ASCII 逐键输入，中文等非 ASCII 字符走剪贴板粘贴。"
        "执行前请先用 computer_click 把焦点切到目标输入框。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要输入的文本"},
            "press_enter": {"type": "boolean", "default": False, "description": "输入后是否按回车"},
        },
        "required": ["text"],
        "additionalProperties": False,
    }


class ComputerScrollTool(_ComputerTool):
    name = "computer_scroll"
    user_facing_name = "桌面滚动"
    action = "scroll"
    description = "在当前焦点窗口滚动滚轮：正数向上、负数向下，单位为滚轮格。"
    parameters = {
        "type": "object",
        "properties": {
            "amount": {"type": "integer", "description": "滚动格数（正上负下）"},
            "x": {"type": "integer", "description": "可选：滚动位置横坐标"},
            "y": {"type": "integer", "description": "可选：滚动位置纵坐标"},
        },
        "required": ["amount"],
        "additionalProperties": False,
    }


COMPUTER_TOOLS: tuple[type[BaseTool], ...] = (
    ComputerScreenshotTool,
    ComputerClickTool,
    ComputerTypeTool,
    ComputerScrollTool,
)
