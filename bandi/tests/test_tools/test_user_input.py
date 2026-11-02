from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from bandi.tools.user_input import UserInputTool


class TestUserInputTool:
    @pytest.mark.asyncio
    async def test_with_questions(self) -> None:
        inputs = iter(["Alice", "yes"])
        tool = UserInputTool(input_fn=lambda _: next(inputs))
        result = await tool.execute(
            {
                "reason": "need info",
                "questions": [
                    {"id": "name", "question": "what is your name?"},
                    {"id": "confirm", "question": "confirm?"},
                ],
            }
        )
        assert result["reason"] == "need info"
        assert result["answers"]["name"] == "Alice"
        assert result["answers"]["confirm"] == "yes"

    @pytest.mark.asyncio
    async def test_no_questions(self) -> None:
        tool = UserInputTool()
        result = await tool.execute({"reason": "just checking"})
        assert result["answers"] == {}

    @pytest.mark.asyncio
    async def test_empty_input_fn(self) -> None:
        tool = UserInputTool(input_fn=lambda _: "")
        result = await tool.execute(
            {
                "reason": "testing",
                "questions": [{"id": "q1", "question": "say something"}],
            }
        )
        assert result["answers"]["q1"] == ""

    @pytest.mark.asyncio
    async def test_reason_only(self) -> None:
        tool = UserInputTool()
        result = await tool.execute({"reason": "blocked"})
        assert result["reason"] == "blocked"
        assert result["answers"] == {}

    @pytest.mark.asyncio
    async def test_input_fn_exception(self) -> None:
        tool = UserInputTool(input_fn=MagicMock(side_effect=RuntimeError("fail")))
        with pytest.raises(RuntimeError, match="fail"):
            await tool.execute(
                {
                    "reason": "test",
                    "questions": [{"id": "q1", "question": "say something"}],
                }
            )

    @pytest.mark.asyncio
    async def test_cancelled_during_input(self) -> None:
        with patch(
            "bandi.tools.user_input.asyncio.to_thread",
            side_effect=asyncio.CancelledError(),
        ):
            tool = UserInputTool(input_fn=lambda _: "ok")
            with pytest.raises(asyncio.CancelledError):
                await tool.execute(
                    {
                        "reason": "test",
                        "questions": [{"id": "q1", "question": "say something"}],
                    }
                )

    @pytest.mark.asyncio
    async def test_timeout_during_input(self) -> None:
        async def _blocking(*_a, **_kw):
            await asyncio.sleep(999)

        with patch("bandi.tools.user_input.asyncio.to_thread", side_effect=_blocking):
            tool = UserInputTool(input_fn=lambda _: "ok")
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    tool.execute(
                        {
                            "reason": "test",
                            "questions": [{"id": "q1", "question": "say something"}],
                        }
                    ),
                    timeout=0.05,
                )


class _AsyncUI:
    """async Protocol 实现：记录提问并按注入答复返回。"""

    def __init__(self, answers: list[str] | None = None) -> None:
        self._answers = iter(answers or ["ui-answer"])
        self.prompts: list[str] = []

    async def ask_input(self, prompt: str, *, secret: bool = False) -> str:
        self.prompts.append(prompt)
        return next(self._answers)

    async def ask_permission(self, tool_name: str, args: dict, risk: str) -> bool:
        return True

    async def ask_confirmation(self, prompt: str, timeout: float = 30.0) -> bool:
        return True

    async def show_notification(self, message: str, level: str = "info") -> None:
        pass


class TestUserInputToolAsyncChannel:
    @pytest.mark.asyncio
    async def test_ui_injected_channel_preferred(self) -> None:
        """注入 async UI（HTTP/CLI 通道）优先于本地 input_fn——headless 不挂死。"""
        ui = _AsyncUI(answers=["来自交互通道"])
        tool = UserInputTool(input_fn=lambda _: "来自stdin", ui=ui)
        result = await tool.execute(
            {
                "reason": "need info",
                "questions": [{"id": "q1", "question": "what?"}],
            }
        )
        assert result["answers"]["q1"] == "来自交互通道"
        assert ui.prompts and "what?" in ui.prompts[0]

    @pytest.mark.asyncio
    async def test_no_channel_raises_fail_closed(self) -> None:
        """无 ui 无 input_fn（headless 空态）→ 显式抛错而非 input() 挂死。"""
        tool = UserInputTool()
        with pytest.raises(RuntimeError, match="no interactive channel"):
            await tool.execute(
                {
                    "reason": "test",
                    "questions": [{"id": "q1", "question": "say something"}],
                }
            )

    @pytest.mark.asyncio
    async def test_multi_question_ui_roundtrip(self) -> None:
        ui = _AsyncUI(answers=["Alice", "yes"])
        tool = UserInputTool(ui=ui)
        result = await tool.execute(
            {
                "reason": "need info",
                "questions": [
                    {"id": "name", "question": "your name?"},
                    {"id": "ok", "question": "proceed?"},
                ],
            }
        )
        assert result["answers"]["name"] == "Alice"
        assert result["answers"]["ok"] == "yes"
        assert len(ui.prompts) == 2
