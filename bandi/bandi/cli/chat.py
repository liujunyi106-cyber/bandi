# 功能：ChatLoop — 全功能 CLI 交互循环（Agent 执行委托给 core/agent_runner.py）

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession as PTSessionType

from rich.console import Console
from rich.panel import Panel

from bandi.cli.chat_controller import ChatTurnController, QueuedChatInput
from bandi.cli.prompt_session import create_prompt_session
from bandi.cli.stream_handler import CLIStreamHandler
from bandi.cli.ui_state import UIStateStore
from bandi.cli.user_interaction import CLIUserInteraction
from bandi.core.abort_controller import AbortController

MODE_LABELS = {
    "execute": "[green]Execute[/green]",
    "plan": "[blue]Plan[/blue]",
}


class _InteractiveChatState:
    """集中管理 CLI 运行时状态。"""

    def __init__(self, restore: bool = False, ui_store: UIStateStore | None = None) -> None:
        self.ui_store = ui_store or UIStateStore()
        self.mode: str = "execute"
        self.model: str = ""
        self.session_id: str | None = None
        self.verbose: bool = False
        self.runtime_busy: bool = False
        self.project_id: str | None = None
        self.employee_id: str | None = None
        if restore:
            # 双模式归一：历史持久化值可能为已移除的 ask——兜底 execute
            restored = self.ui_store.mode or "execute"
            self.mode = restored if restored in ("execute", "plan") else "execute"
            self.model = self.ui_store.model or ""


class ChatLoop:
    def __init__(
        self,
        console: Console | None = None,
        verbose: bool = False,
        ui_store: UIStateStore | None = None,
        session_store: Any | None = None,
        llm_provider: Any | None = None,
        project_id: str | None = None,
        employee_id: str | None = None,
    ) -> None:
        self._console = console or Console()
        self._ui = CLIUserInteraction(self._console)
        self._state = _InteractiveChatState(restore=False, ui_store=ui_store)
        self._state.verbose = verbose
        self._state.project_id = project_id
        self._state.employee_id = employee_id
        self._controller = ChatTurnController()
        self._controller.set_executor(self._execute_turn)
        self._prompt_session: PTSessionType | None = None
        self._ontology_store: Any | None = None
        self._session_store: Any | None = session_store
        self._llm_provider: Any | None = llm_provider

    def _get_ontology_store(self) -> Any:
        """惰性构造共享 OntologyStore（跨轮复用连接，同一 CLI 进程内本体工具可见）。"""
        if self._ontology_store is None:
            from pathlib import Path

            from bandi.ontology.spec import open_ontology
            from bandi.ontology.triple_store import OntologyStore

            db_path = Path.home() / ".bandi" / "data" / "ontology.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._ontology_store = OntologyStore(open_ontology(str(db_path)))
        return self._ontology_store

    def _get_session_store(self) -> Any:
        if self._session_store is None:
            from pathlib import Path

            from bandi.memory.session.store import SessionStore

            db_path = Path.home() / ".bandi" / "data" / "bandi.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._session_store = SessionStore(str(db_path))
        return self._session_store

    async def run(self, session_id: str | None = None) -> None:
        self._state.session_id = session_id
        self._restore_persisted_state()
        self._prompt_session = create_prompt_session(
            self.SLASH_COMMANDS,
            self._controller,
            self._state.ui_store,
            lambda: self._state.mode,
            lambda: self._state.model,
        )
        self._show_welcome()
        self._kb_task = asyncio.create_task(self._keyboard_listener())
        while True:
            try:
                user_input = await self._prompt_session.prompt_async(self._prompt_text())
            except (EOFError, KeyboardInterrupt):
                self._kb_task.cancel()
                await self._controller.shutdown()
                self._console.print("\n[yellow]再见！[/yellow]")
                break
            if not user_input:
                continue
            if user_input.startswith("/"):
                should_exit = await self._handle_command(user_input)
                if should_exit:
                    self._kb_task.cancel()
                    await self._controller.shutdown()
                    break
                continue
            self._controller.submit(
                QueuedChatInput(
                    text=user_input,
                    mode=self._state.mode,
                )
            )

    def _restore_persisted_state(self) -> None:
        """从 ui_state.db 恢复持久化的 mode/model。"""
        from bandi.core.config import BandiConfig

        config = BandiConfig.load()
        restored = self._state.ui_store.mode or "execute"
        # 双模式归一：历史持久化值可能为已移除的 ask——兜底 execute
        self._state.mode = restored if restored in ("execute", "plan") else "execute"
        self._state.model = (
            self._state.ui_store.model
            or (config.llm.default_model if config.llm else "deepseek-v4.1-flash")
            or "deepseek-v4.1-flash"
        )

    def _show_welcome(self) -> None:
        self._console.print(
            Panel(
                "[bold]Bandi[/bold] — 个人多 Agent 数字员工组织\n"
                f"模式: {MODE_LABELS.get(self._state.mode, self._state.mode)}  "
                f"模型: [cyan]{self._state.model}[/cyan]\n"
                "Ctrl+P/A/E 切换模式 | 输入 /help 查看命令",
                title="欢迎",
                border_style="green",
            )
        )

    def _prompt_text(self) -> str:
        short_model = (
            self._state.model.split("/")[-1][:20]
            if "/" in self._state.model
            else self._state.model[:20]
        )
        busy_tag = " [red]⏳[/]" if self._controller.is_busy else ""
        q_tag = (
            f" [dim]#{self._controller.queue_length}[/]" if self._controller.queue_length else ""
        )
        return f"\n{MODE_LABELS.get(self._state.mode, self._state.mode)} [{short_model}]{busy_tag}{q_tag}> "

    # ── 键盘快捷键 Ctrl+P/A/E ─────────────────────────────────

    async def _keyboard_listener(self) -> None:
        """非阻塞快捷键监听。"""
        try:
            import msvcrt
        except ImportError:
            return

        while True:
            try:
                if msvcrt.kbhit():
                    key = msvcrt.getch()
                    if key == b"\x10":  # Ctrl+P
                        self._state.mode = "plan"
                        self._state.ui_store.mode = "plan"
                        self._console.print("\n[blue]  ↳ Plan 模式[/blue]")
                    elif key == b"\x05":  # Ctrl+E
                        self._state.mode = "execute"
                        self._state.ui_store.mode = "execute"
                        self._console.print("\n[green]  ↳ Execute 模式[/green]")
                await asyncio.sleep(0.1)
            except (EOFError, KeyboardInterrupt, AttributeError):
                break

    # ── 斜杠命令系统（含子命令支持）──────────────────────────────

    SLASH_COMMANDS: ClassVar[dict[str, tuple[str, str, tuple[str, ...]]]] = {
        "/help": ("显示此帮助", "帮助", ()),
        "/plan": ("切换 Plan 模式（只读）", "模式", ()),
        "/execute": ("切换 Execute 模式（完整权限）", "模式", ()),
        "/mode": ("显示当前模式", "模式", ()),
        "/model": ("管理模型：/model list | switch <name>", "模型", ("list", "switch")),
        "/clear": ("清除当前对话", "会话", ()),
        "/compact": ("压缩当前会话上下文（fork 新会话继续）", "会话", ()),
        "/status": ("显示会话/模型/Token 状态", "信息", ()),
        "/tools": ("列出可用工具", "信息", ()),
        "/verbose": ("切换详细输出", "设置", ()),
        "/quit": ("退出", "会话", ()),
    }

    SLASH_ALIASES: ClassVar[dict[str, str]] = {
        "q": "quit",
        "h": "help",
        "t": "tools",
        "s": "status",
    }

    async def _handle_command(self, cmd: str) -> bool:
        stripped = cmd.strip()
        if stripped == "/":
            return False
        import shlex

        try:
            parts = shlex.split(stripped)
        except ValueError:
            self._console.print("[red]命令格式错误，引号不匹配[/red]")
            return False
        raw_cmd = parts[0].lower()
        # 别名解析
        base_cmd = self.SLASH_ALIASES.get(raw_cmd.removeprefix("/"), raw_cmd)
        if not base_cmd.startswith("/"):
            base_cmd = "/" + base_cmd
        args = parts[1:]

        # 三级繁忙策略
        readonly_commands = {"/help", "/mode", "/status", "/tools", "/verbose"}
        if self._controller.is_busy and base_cmd not in readonly_commands:
            self._console.print(
                "[yellow]Agent 执行中，该命令不可用。使用 /help 查看只读命令。[/yellow]"
            )
            return False

        if base_cmd in ("/quit", "/exit", "/q"):
            return True

        handler = self._get_command_handler(base_cmd)
        if handler:
            result = handler(args)
            if asyncio.iscoroutine(result):
                await result
        else:
            self._console.print(f"[red]未知命令: {raw_cmd}[/red]")
            self._console.print(f"[dim]可用命令: {', '.join(sorted(self.SLASH_COMMANDS))}[/dim]")
        return False

    def _get_command_handler(self, cmd: str) -> Callable[[str], object] | None:
        handlers = {
            "/help": lambda a: self._show_help(),
            "/plan": lambda a: self._set_mode("plan"),
            "/execute": lambda a: self._set_mode("execute"),
            "/mode": lambda a: self._console.print(
                f"当前模式: {MODE_LABELS.get(self._state.mode, self._state.mode)}"
            ),
            "/model": self._handle_model_command,
            "/clear": lambda a: self._clear_session(),
            "/compact": lambda a: self._compact_session(),
            "/status": lambda a: self._show_status(),
            "/tools": lambda a: self._list_tools(),
            "/verbose": lambda a: self._toggle_verbose(),
        }
        return handlers.get(cmd)

    def _set_mode(self, mode: str) -> None:
        self._state.mode = mode
        self._state.ui_store.mode = mode
        labels = {
            "plan": "[blue]Plan 模式（只读工具）[/blue]",
            "execute": "[green]Execute 模式（完整权限）[/green]",
        }
        self._console.print(labels.get(mode, mode))

    def _show_help(self) -> None:
        lines = []
        for cmd, (desc, category, subcmds) in sorted(self.SLASH_COMMANDS.items()):
            line = f"  {cmd:<16} {desc}"
            if subcmds:
                line += f"\n  {'':17}[dim]子命令: {' | '.join(f'/{cmd.split()[0]} {s}' for s in subcmds)}[/]"
            lines.append(line)
        self._console.print(Panel("\n".join(lines), title="命令帮助", border_style="cyan"))

    async def _handle_model_command(self, args: list[str]) -> None:
        from bandi.core.config import BandiConfig

        config = BandiConfig.load()
        if not args or args[0] == "list":
            self._console.print(f"当前模型: [cyan]{self._state.model}[/cyan]")
            if config.llm:
                self._console.print(f"[dim]默认模型: {config.llm.default_model}[/dim]")
            self._console.print(
                "[dim]用法: /model switch <名称>  例: /model switch deepseek-v4.1-flash[/dim]"
            )
        elif args[0] == "switch" and len(args) >= 2:
            new_model = args[1]
            if config.llm:
                config.llm.default_model = new_model
                config.save(patch={"llm": {"default_model": new_model}})
                self._state.model = new_model
                self._state.ui_store.model = new_model
                self._console.print(f"[green]模型已切换至: {new_model}[/green]")
            else:
                self._console.print("[red]config.llm 未配置，请先运行 bandi init[/red]")
        else:
            self._console.print(f"[red]未知子命令: {args[0]}[/red]")

    def _clear_session(self) -> None:
        self._state.session_id = None
        self._console.print("[yellow]对话已清除[/yellow]")

    async def _compact_session(self) -> None:
        """
        原会话原样保留（SessionStore append-only 约束下的 fork 语义）。"""
        from bandi.core.config import BandiConfig
        from bandi.core.session_compact import compact_session
        from bandi.memory.compaction import COMPACT_BOUNDARY_HINT

        sid = self._state.session_id
        if not sid:
            self._console.print("[red]当前无活跃会话，无法压缩。[/red]")
            return
        store = self._get_session_store()
        if store.get_session(sid) is None:
            self._console.print(f"[red]会话 {sid} 不存在，无法压缩。[/red]")
            return

        config = BandiConfig.load()
        provider = self._llm_provider
        if provider is None:
            from bandi.agent.factory import build_provider

            provider = build_provider(config)
        result = await compact_session(store, sid, provider)
        if not result.success:
            hints = {
                "session not found": f"[red]会话 {sid} 不存在，无法压缩。[/red]",
                "too few messages": "[yellow]会话消息过少，无需压缩。[/yellow]",
            }
            self._console.print(
                hints.get(result.reason, "[yellow]压缩未产生有效缩减，会话保持不变。[/yellow]")
            )
            return

        self._state.session_id = result.new_session_id
        self._console.print(
            Panel(
                f"[green]压缩完成[/green] stage={result.stage}\n"
                f"消息: {result.before_messages} → {result.after_messages}    "
                f"Token(估): {result.before_tokens} → {result.after_tokens}\n"
                f"{COMPACT_BOUNDARY_HINT}\n"
                f"新会话: [cyan]{result.new_session_id}[/cyan]"
                + ("（含结构化摘要）" if result.has_summary else ""),
                title="/compact",
                border_style="green",
            )
        )

    async def _show_status(self) -> None:
        from bandi.core.config import BandiConfig

        config = BandiConfig.load()
        lines = [
            f"模式: {MODE_LABELS.get(self._state.mode, self._state.mode)}",
            f"模型: [cyan]{self._state.model}[/cyan]",
            f"会话: {self._state.session_id or '（新会话）'}",
            f"队列: {'[red]忙[/red]' if self._controller.is_busy else '[green]空闲[/green]'}",
            f"排队: {self._controller.queue_length} 条",
            f"API Key: {'[green]已配置[/green]' if config.llm and config.llm.api_key else '[red]未配置[/red]'}",
        ]
        self._console.print(Panel("\n".join(lines), title="状态", border_style="blue"))

    def _toggle_verbose(self) -> None:
        self._state.verbose = not self._state.verbose
        self._console.print(f"[green]详细输出: {'开' if self._state.verbose else '关'}[/green]")

    async def _list_tools(self) -> None:
        from bandi.agent.factory import build_tool_registry

        # 复用生产工具注册
        registry = build_tool_registry()
        schemas = registry.list_schemas()
        lines = []
        for s in schemas:
            fn = s.get("function", s)
            lines.append(
                f"  [cyan]{fn.get('name', '?'):<20}[/cyan] {fn.get('description', '')[:60]}"
            )
        self._console.print(
            Panel(
                "\n".join(lines) if lines else "  （无可用工具）",
                title=f"工具列表 ({len(schemas)})",
                border_style="blue",
            )
        )

    # ── 领域路由 ────────────────────────────────────────────────

    # ── Agent 执行 ──────────────────────────────────────────────

    async def _execute_turn(self, item: QueuedChatInput) -> None:
        """ChatTurnController 执行回调，委托给 core/agent_runner.py::run_agent_turn()。"""
        from bandi.core.agent_runner import run_agent_turn

        abort = AbortController()
        import signal

        signal.signal(signal.SIGINT, abort.signal_handler)

        handler = CLIStreamHandler(self._console, verbose=self._state.verbose)
        handler._start_time = time.time()
        from bandi.core.process_recorder import ProcessRecorder

        store = self._get_session_store()
        recorder = ProcessRecorder(handler, store, self._state.session_id or "")
        try:
            result = await run_agent_turn(
                user_input=item.text,
                mode=item.mode,
                session_id=self._state.session_id,
                session_store=store,
                stream_handler=recorder,
                abort_controller=abort,
                user_interaction=self._ui,
                triple_ontology_store=self._get_ontology_store(),
                project_id=self._state.project_id,
                employee_id=self._state.employee_id,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            abort.abort("user_interrupt")
            self._console.print("\n[yellow]⏹ Agent 执行已中断[/yellow]")
            await recorder.persist_partial(self._state.session_id or "")
            return
        except Exception:
            await recorder.persist_partial(self._state.session_id or "")
            raise
        finally:
            signal.signal(signal.SIGINT, signal.SIG_DFL)

        self._state.session_id = result.session_id
        await recorder.on_done(
            result.session_id,
            result.terminal_reason or "",
            result.total_tokens,
            result.turn_index,
        )
