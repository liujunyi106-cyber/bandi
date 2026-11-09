# 功能：CLIUserInteraction — rich 实现的用户交互层，实现 core UserInteraction Protocol（async）
# 桌面端实现同 Protocol 即可接入

from __future__ import annotations

import asyncio
from typing import Any

from rich.console import Console
from rich.prompt import Prompt


class CLIUserInteraction:
    """Rich 实现的 UserInteraction。桌面端实现同签名的类即可替换。

    阻塞式终端输入（Prompt.ask）经 asyncio.to_thread 包出 async 接口——
    供 AgentLoop 工具执行路径在事件循环内安全调用。
    """

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    async def ask_permission(self, tool_name: str, args: dict[str, Any], risk: str) -> bool:
        self._console.print(f"[yellow]⚠ 权限请求[/yellow] 工具: {tool_name}  风险: {risk}")
        result = await asyncio.to_thread(
            Prompt.ask, "允许执行？", choices=["y", "n", "a"], default="n"
        )
        return result in ("y", "a")

    async def ask_permission_with_feedback(
        self, tool_name: str, args: dict[str, Any], risk: str
    ) -> tuple[bool, str]:
        """拒绝时可附理由（回灌模型，CorrectedError 等价物）。"""
        self._console.print(f"[yellow]⚠ 权限请求[/yellow] 工具: {tool_name}  风险: {risk}")
        result = await asyncio.to_thread(
            Prompt.ask, "允许执行？", choices=["y", "n", "a"], default="n"
        )
        if result in ("y", "a"):
            return True, ""
        feedback = await asyncio.to_thread(Prompt.ask, "拒绝理由（可选，回车跳过）", default="")
        return False, str(feedback or "").strip()

    async def ask_confirmation(self, prompt: str, timeout: float = 30.0) -> bool:
        self._console.print(f"[yellow]?[/yellow] {prompt}")
        result = await asyncio.to_thread(Prompt.ask, "确认？", choices=["y", "n"], default="n")
        return result == "y"

    async def ask_input(self, prompt: str, secret: bool = False) -> str:
        return await asyncio.to_thread(Prompt.ask, prompt, password=secret)

    async def show_notification(self, message: str, level: str = "info") -> None:
        style = {"info": "green", "warning": "yellow", "error": "red"}.get(level, "green")
        self._console.print(f"[{style}]{message}[/{style}]")
