# 功能：契约——压缩纪要双通道注入去重：messages 已含「[上下文摘要]」
# SYSTEM 时，「## 会话记忆」段的压缩纪要是同源重复注入——剔除；
# 无摘要时保留（二次压缩裁掉摘要后纪要是唯一找回途径）。
import asyncio
from unittest.mock import Mock

from bandi.core.message import Message
from bandi.memory.compaction import SUMMARY_MESSAGE_PREFIX
from bandi.perception.context_loader import ContextLoader

_NOTES = "本会话正文内容\n[压缩纪要 turn 2]\n摘要内容ABC\n\n[压缩纪要 turn 3]\n摘要内容DEF"


class _FakeEntryStore:
    def __init__(self, entries: list[dict] | None = None) -> None:
        self._entries = entries or []

    def get_session(self, sid: str) -> dict:
        return {"title": "t"}

    def get_branch(self, sid: str) -> list[dict]:
        return self._entries


def _loader(store: object, memory_context: str) -> ContextLoader:
    hierarchy = Mock()
    hierarchy.collect_memory_context.return_value = memory_context
    return ContextLoader(store, hierarchy=hierarchy, project_id="p1", employee_id="e1")


def _load(loader: ContextLoader) -> str:
    bundle = asyncio.run(loader.load(Message(role="user", content="hi"), "s1"))
    return bundle.memory_context


def test_notes_kept_without_summary_in_history() -> None:
    """契约（对照）：历史无摘要 SYSTEM 时纪要保留（跨轮唯一找回途径）。"""
    loader = _loader(_FakeEntryStore(), _NOTES)
    assert "压缩纪要" in _load(loader)


def test_notes_stripped_when_summary_present() -> None:
    """契约（核心）：历史含「[上下文摘要]」SYSTEM 时纪要行剔除
    （红：现状双通道重复注入）。"""
    entries = [
        {
            "entry_type": "message",
            "role": "system",
            "content": f"{SUMMARY_MESSAGE_PREFIX}已压缩的会话内容",
        }
    ]
    loader = _loader(_FakeEntryStore(entries), _NOTES)
    out = _load(loader)
    assert "压缩纪要" not in out, f"重复注入未去重：{out!r}"
    assert "本会话正文内容" in out, "正文不得误删"
