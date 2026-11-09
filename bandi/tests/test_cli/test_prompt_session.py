from __future__ import annotations

from unittest.mock import MagicMock

from prompt_toolkit.document import Document

from bandi.cli.prompt_session import _bottom_toolbar, _OPCSlashCompleter, create_prompt_session


class TestOPCSlashCompleter:
    def setup_method(self) -> None:
        self.commands = {
            "/help": ("Show help", "general", ()),
            "/mode": ("Change mode", "mode", ("execute", "plan", "review")),
            "/exit": ("Exit", "general", ()),
        }
        self.completer = _OPCSlashCompleter(self.commands)

    def test_non_slash_no_completions(self) -> None:
        doc = Document(text="hello")
        completions = list(self.completer.get_completions(doc, None))
        assert completions == []

    def test_slash_prefix_matches(self) -> None:
        doc = Document(text="/h")
        completions = list(self.completer.get_completions(doc, None))
        names = [c.text for c in completions]
        assert any("/help" in n for n in names)

    def test_exact_match_no_completions(self) -> None:
        doc = Document(text="/help")
        completions = list(self.completer.get_completions(doc, None))
        names = [c.text for c in completions]
        assert all("/help" not in n for n in names)

    def test_subcommand_completions(self) -> None:
        doc = Document(text="/m")
        completions = list(self.completer.get_completions(doc, None))
        names = [c.text for c in completions]
        assert any("execute" in n for n in names)
        assert any("plan" in n for n in names)

    def test_no_match(self) -> None:
        doc = Document(text="/zzzzz")
        completions = list(self.completer.get_completions(doc, None))
        assert completions == []


class TestBottomToolbar:
    def test_basic_mode_model(self) -> None:
        controller = MagicMock()
        controller.is_busy = False
        controller.queue_length = 0
        result = _bottom_toolbar("execute", "deepseek-chat", controller)
        assert "execute" in result

    def test_busy_indicator(self) -> None:
        controller = MagicMock()
        controller.is_busy = True
        controller.queue_length = 0
        result = _bottom_toolbar("execute", "deepseek-chat", controller)
        assert "busy" in result

    def test_queue_indicator(self) -> None:
        controller = MagicMock()
        controller.is_busy = False
        controller.queue_length = 3
        result = _bottom_toolbar("execute", "deepseek-chat", controller)
        assert "#3" in result

    def test_model_name_truncated(self) -> None:
        controller = MagicMock()
        controller.is_busy = False
        controller.queue_length = 0
        result = _bottom_toolbar("execute", "models/deepseek-chat-v3", controller)
        assert "deepseek-chat-v3" in result
        assert "models/" not in result


class TestCreatePromptSession:
    def test_creates_session(self) -> None:
        from unittest.mock import MagicMock, patch

        with patch("bandi.cli.prompt_session.PTSession") as mock_pt:
            mock_pt.return_value = "session-obj"
            commands = {"/help": ("Show help", "general", ())}
            controller = MagicMock()
            ui_store = MagicMock()
            ui_store._path = MagicMock()
            ui_store._path.parent = MagicMock()
            session = create_prompt_session(
                commands, controller, ui_store, lambda: "execute", lambda: "model"
            )
        assert session == "session-obj"
