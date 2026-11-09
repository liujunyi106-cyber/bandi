# 功能：契约——只读文件回合不自动沉淀长期记忆
#
# 行为不变式（钉行为，不钉实现）：
# 1. read/grep/glob 文件问答的回复（达阈值长度）不得自动写长期记忆（复现：
#      问「README 开头写了啥」→ 整段摘要被写进 long_term）
#   2. 混合工具回合（含写操作）不受只读闸门影响
#   3. 用户显式「记住」的文件回合仍写入（用户输入为源）
# 4. 用户输入含可记内容时无工具轮照常写入

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from bandi.core.agent_runner import _persist_memory

_LONG_REPLY = "结论：该文件描述了评测工作区，代号银杉、特殊标记青柠-42。" * 6


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        tool_name=name,
        success=True,
        output="content",
        error=None,
        duration_ms=5,
        tool_call_id=f"c-{name}",
    )


def _ctx(
    *,
    tool_results: list[SimpleNamespace] | None,
    raw_input: str = "我项目里有个 README.md，帮我看看开头都写了啥？",
    final_message: str = _LONG_REPLY,
) -> SimpleNamespace:
    return SimpleNamespace(
        tool_results=tool_results,
        final_message=final_message,
        current_content=None,
        raw_input=raw_input,
        turn_index=7,
        terminal_reason=SimpleNamespace(name="COMPLETED"),
        messages=[],
    )


def _manager() -> MagicMock:
    manager = MagicMock()
    manager.long_term.check_conflict.return_value = False
    return manager


class TestReadonlyFileTurnNotAutoWritten:
    def test_readonly_file_reply_not_written(self) -> None:
        """核心不变式：文件内容复述不得自动写长期记忆。"""
        manager = _manager()
        asyncio.run(
            _persist_memory(_ctx(tool_results=[_tool("read")]), manager, session_id="s-file")
        )
        assert not manager.long_term.store.called, "文件复述不得自动写长期记忆"

    def test_grep_glob_only_turn_not_written(self) -> None:
        manager = _manager()
        asyncio.run(
            _persist_memory(
                _ctx(tool_results=[_tool("grep"), _tool("glob")]), manager, session_id="s-gg"
            )
        )
        assert not manager.long_term.store.called

    def test_mixed_tool_turn_still_writes(self) -> None:
        """破坏者视角：含写操作的回合不受只读闸门影响。"""
        manager = _manager()
        asyncio.run(
            _persist_memory(
                _ctx(
                    tool_results=[_tool("read"), _tool("write")],
                    raw_input="记住：我项目里有个 README.md（共 3 节）",
                ),
                manager,
                session_id="s-mix",
            )
        )
        assert manager.long_term.store.called

    def test_explicit_remember_on_file_turn_still_writes(self) -> None:
        """显式「记住」优先于只读闸门（用户输入为源）。"""
        manager = _manager()
        asyncio.run(
            _persist_memory(
                _ctx(
                    tool_results=[_tool("read")],
                    raw_input="记住：我项目的 README 代号是银杉。",
                ),
                manager,
                session_id="s-exp",
            )
        )
        assert manager.long_term.store.called, "显式记住不得被只读闸门拦下"

    def test_plain_long_reply_still_writes(self) -> None:
        """既有回退路径不回归：用户输入含可记内容时无工具轮照常写入。"""
        manager = _manager()
        asyncio.run(
            _persist_memory(
                _ctx(
                    tool_results=None,
                    raw_input="记住：我项目里有个 README.md（共 3 节）",
                ),
                manager,
                session_id="s-plain",
            )
        )
        assert manager.long_term.store.called, "用户输入含可记内容时无工具轮照常写入"
