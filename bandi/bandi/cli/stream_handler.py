# 功能：CLIStreamHandler — 流式输出处理 + 自适应 drain 双模式
# 实现 core StreamHandler Protocol，供 run_agent_turn 调用

from __future__ import annotations

import logging
import time
from typing import Any

from rich.console import Console
from rich.markdown import Markdown

logger = logging.getLogger(__name__)


class CLIStreamHandler:
    """Rich 实现的 StreamHandler，注册到 EventBus 进行事件驱动渲染。

    drain 模式：
      smooth — 每次排空 1 行，平滑输出
      catch_up — 当队列 >= 8 行时切换，批量排空到 2 行后恢复
    """

    def __init__(self, console: Console | None = None, verbose: bool = False) -> None:
        self._console = console or Console()
        self._verbose = verbose
        self._token_buffer: list[str] = []
        self._tool_count = 0
        self._start_time: float = 0.0
        self._drain_mode: str = "smooth"
        self._drain_count = 0
        self._session_id: str = ""

    # ── 自适应 drain 控制 ──────────────────────────────────────

    def _should_flush(self, content: str) -> bool:
        if self._drain_mode == "catch_up":
            self._drain_count += 1
            if self._drain_count > 2:
                self._drain_mode = "smooth"
                self._drain_count = 0
            return True
        self._drain_count += 1
        if self._drain_count >= 8:
            self._drain_mode = "catch_up"
            self._drain_count = 0
            return True
        return len(self._token_buffer) >= 5 or content.endswith(
            ("\n", ".", "!", "?", "。", "！", "？", "：")
        )

    # ── StreamHandler Protocol 实现 ────────────────────────────

    async def on_content_delta(self, session_id: str, delta: str) -> None:
        self._token_buffer.append(delta)
        if self._should_flush(delta):
            text = "".join(self._token_buffer)
            self._token_buffer.clear()
            self._console.print(text, end="", highlight=False)

    async def on_thinking(self, session_id: str, delta: str) -> None:
        """功能：CLI 省略 CoT 实时渲染（思维链由前端过程块消费）；协议完整性占位。"""

    async def on_tool_call_start(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        tool_call_id: str = "",
    ) -> None:
        self._tool_count += 1
        self._console.print(f"\n  [bold cyan]> tool:[/] [white]{tool_name}[/]")

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
        if success:
            self._console.print(f"  [green]  ok[/] [dim]({duration_ms}ms)[/] {output_preview}")
        else:
            self._console.print(f"  [red]  err[/] {output_preview[:200]}")

    async def on_tool_progress(
        self, session_id: str, tool_name: str, progress_type: str, data: dict[str, Any]
    ) -> None:
        step = data.get("step", "")
        self._console.print(
            f"  [cyan]    {progress_type}[/][dim]{(' ' + str(step)) if step else ''}[/]"
        )

    async def on_skill_injected(self, session_id: str, skills: list[str]) -> None:
        """功能：CLI 省略 Skill 使用组渲染（前端过程块消费）；协议完整性占位。"""

    async def on_note(self, session_id: str, note_type: str) -> None:
        """功能：CLI 省略过程消息 note 行渲染（前端过程块消费）；协议完整性占位。"""

    async def on_memory_written(self, session_id: str, channels: list[str]) -> None:
        """功能：CLI 省略记忆写入行渲染（前端过程块消费）；协议完整性占位。"""

    async def on_task_ledger(self, session_id: str, items: list[dict[str, Any]]) -> None:
        """功能：CLI 省略会话任务条渲染（前端消费）；协议完整性占位。"""

    async def on_done(
        self, session_id: str, terminal_reason: str, total_tokens: int, turn_index: int
    ) -> None:
        if self._token_buffer:
            self._console.print("".join(self._token_buffer), end="")
            self._token_buffer.clear()
        elapsed = time.time() - self._start_time
        parts = [f"{self._tool_count} tool call(s)", f"{total_tokens} tokens", f"{elapsed:.1f}s"]
        if terminal_reason and terminal_reason != "COMPLETED":
            parts.append(terminal_reason)
        self._console.print(f"\n  [dim]{' | '.join(parts)}[/]")

    async def on_error(self, session_id: str, error: str) -> None:
        self._console.print(f"\n  [red]✗ err: {error}[/red]")

    async def render_markdown(self, text: str) -> None:
        """Rich Markdown 渲染，含异常 fallback。"""
        try:
            md = Markdown(text)
            self._console.print(md)
        except Exception as e:  # noqa: BLE001
            logger.debug("markdown render failed, fallback to plain: %s", e)
            self._console.print(text)
