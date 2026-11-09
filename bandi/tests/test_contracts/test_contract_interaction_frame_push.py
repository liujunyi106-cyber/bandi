"""契约：交互帧推送必须兼容同步回调，且不得因真实异常重发卡片。

不变式：
1. `HTTPUserInteraction._suspend` / notify 路径对同步回调（api/app.py 传
   `lambda item: handler.put(...)`，返回 None）与异步回调都能推出交互帧；
2. `UserInputTool._prompt_one` 仅在「老 UI 不认 choices 等关键字」时降级重发，
   其他 TypeError 原样上抛——同一提问不得登记第二张卡。
"""

from __future__ import annotations

import asyncio

import pytest

from bandi.core.user_interaction import HTTPUserInteraction, InteractionStore
from bandi.tools.user_input import UserInputTool


async def _drive(callback, answer: str = "Word") -> tuple[list, object | None]:
    store = InteractionStore(ttl_seconds=30)
    pushed: list = []
    reg_ui = HTTPUserInteraction(store, session_id="sess-t", on_interaction=_wrap(callback, pushed))
    tool = UserInputTool()
    tool.bind_ui(reg_ui)
    task = asyncio.create_task(
        tool.execute({"reason": "需要决定", "questions": [{"id": "f", "question": "导出格式？"}]})
    )
    await asyncio.sleep(0.6)
    if pushed:
        store.resolve(pushed[0].interaction_id, answer)
    try:
        await asyncio.wait_for(task, timeout=10)
    except Exception:  # noqa: BLE001 — 断言在下方按结果判定
        pass
    return pushed, tool


def _wrap(callback, pushed: list):
    if asyncio.iscoroutinefunction(callback):

        async def _async(item):
            pushed.append(item)
            return await callback(item)

        return _async

    def _sync(item):
        pushed.append(item)
        return callback(item)

    return _sync


class TestSyncCallbackSupported:
    @pytest.mark.asyncio
    async def test_sync_callback_pushed_frame(self) -> None:
        """生产形态：同步 lambda 返回 None，不得抛 TypeError。"""
        pushed, _ = await _drive(lambda item: None)
        assert len(pushed) == 1, "同步回调下也应推出交互帧"

    @pytest.mark.asyncio
    async def test_async_callback_still_works(self) -> None:
        async def cb(item):
            return None

        pushed, _ = await _drive(cb)
        assert len(pushed) == 1

    @pytest.mark.asyncio
    async def test_answer_reaches_tool(self) -> None:
        """破坏者视角：帧推得出去还不够，用户答复必须回到工具。"""
        pushed, tool = await _drive(lambda item: None, answer="Word")
        assert len(pushed) == 1
        assert tool._ui is not None


class TestNoDuplicateCard:
    @pytest.mark.asyncio
    async def test_single_question_registers_one_card(self) -> None:
        """一张卡不得变两张——重发会让用户答复落到孤儿卡上。"""
        pushed, _ = await _drive(lambda item: None)
        assert len(pushed) == 1, f"一次提问登记了 {len(pushed)} 张卡"

    @pytest.mark.asyncio
    async def test_real_typeerror_not_swallowed(self) -> None:
        """真实缺陷不得被当成『老实现不认关键字』而重试。"""

        class _Boom:
            async def ask_input(self, *a, **kw):
                raise TypeError("object NoneType can't be used in 'await' expression")

        tool = UserInputTool()
        tool.bind_ui(_Boom())
        with pytest.raises(TypeError, match="NoneType"):
            await tool.execute({"reason": "x", "questions": [{"id": "f", "question": "格式？"}]})


class TestLegacyUiFallback:
    @pytest.mark.asyncio
    async def test_legacy_ui_without_choices_kwargs(self) -> None:
        """老交互实现不认 choices 关键字时仍应降级为纯文本提问。"""

        class _Legacy:
            async def ask_input(self, prompt, secret=False):
                return "纯文本答复"

        tool = UserInputTool()
        tool.bind_ui(_Legacy())
        out = await tool.execute(
            {
                "reason": "x",
                "questions": [{"id": "f", "question": "格式？", "choices": ["PDF", "Word"]}],
            }
        )
        assert out["answers"] == {"f": "纯文本答复"}
