# 功能：PromptSession — prompt-toolkit 增强输入（自动补全 + bottom toolbar）

from __future__ import annotations

from prompt_toolkit import PromptSession as PTSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

from bandi.cli.chat_controller import ChatTurnController
from bandi.cli.ui_state import UIStateStore

STYLE = Style.from_dict(
    {
        "status.mode": "bold",
        "status.model": "cyan",
        "status.busy": "red",
        "status.queue": "ansibrightblack",
        "status.field": "ansibrightblack",
    }
)


class _OPCSlashCompleter(Completer):
    """斜杠命令自动补全。"""

    def __init__(self, commands: dict[str, tuple[str, str, tuple[str, ...]]]) -> None:
        self._commands = commands

    def get_completions(self, document, _complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        word = text[1:].lower()
        for cmd, (desc, _cat, subcmds) in self._commands.items():
            name = cmd.removeprefix("/")
            if name.startswith(word) and name != word:
                yield Completion(
                    f"/{name}",
                    start_position=-len(text),
                    display=f"/{name}",
                    display_meta=desc[:40],
                )
                if subcmds:
                    for sc in subcmds:
                        sc_text = f"/{name} {sc}"
                        sc_desc = f"/{name} {sc}  ({desc})"
                        yield Completion(
                            sc_text,
                            start_position=-len(text),
                            display=sc_text,
                            display_meta=sc_desc[:60],
                        )


def _bottom_toolbar(mode: str, model: str, controller: ChatTurnController) -> str:
    """底部状态栏。"""
    parts = [
        f"[status.mode]{mode}[/]",
        f"[status.model]{model.split('/')[-1][:24]}[/]",
    ]
    if controller.is_busy:
        parts.append("[status.busy]⏳ busy[/]")
    if controller.queue_length:
        parts.append(f"[status.queue]#{controller.queue_length}[/]")
    return " | ".join(parts)


def create_prompt_session(
    commands: dict[str, tuple[str, str, tuple[str, ...]]],
    controller: ChatTurnController,
    ui_store: UIStateStore,
    mode_getter,
    model_getter,
) -> PTSession:
    """创建 prompt-toolkit PromptSession 替代裸 input()。"""
    history_path = str(ui_store._path.parent / ".bandi_history")
    kb = KeyBindings()

    session = PTSession(
        completer=_OPCSlashCompleter(commands),
        history=FileHistory(history_path),
        key_bindings=kb,
        style=STYLE,
        bottom_toolbar=lambda: _bottom_toolbar(mode_getter(), model_getter(), controller),
        vi_mode=False,
        complete_while_typing=True,
    )
    return session
