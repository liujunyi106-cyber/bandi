# 功能：P0-2 post-compact 回注真实文件内容（而非仅路径）契约。
from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.core.message import Message, MessageRole, ToolCall
from bandi.memory.compression_pipeline import (
    POST_COMPACT_MAX_FILE_CHARS,
    build_post_compact_file_attachment,
)


def _read_pair(path: str, content: str, call_id: str = "tc1") -> list[Message]:
    return [
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id=call_id, name="read", arguments={"path": path})],
        ),
        Message(
            role=MessageRole.TOOL,
            content=content,
            tool_call_id=call_id,
            name="read",
        ),
    ]


def test_post_compact_reinjects_real_content_from_removed_tool_message() -> None:
    """契约：压缩回注的是被移除 read 结果的真实正文，不是「内容已不在上下文中」。"""
    body = "def main():\n    print('hello from a.txt')\n"
    attachment = build_post_compact_file_attachment(_read_pair("a.txt", body))

    assert attachment is not None
    text = attachment.content or ""
    assert "def main()" in text and "hello from a.txt" in text, "必须回注真实正文"
    assert "a.txt" in text, "必须保留文件路径锚点"
    assert "内容已不在上下文中" not in text, "旧口径文案不得残留"


def test_post_compact_falls_back_to_disk_when_tool_content_missing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """契约：read 结果已被移出上下文时，仅允许从工作区内的 resolved_path 重读片段。"""
    monkeypatch.setattr("bandi.tools.fs_tool.workspace_root", lambda: tmp_path)
    target = tmp_path / "b.txt"
    target.write_text("REAL DISK CONTENT for b.txt\n", encoding="utf-8")
    removed = [
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="tc9", name="read", arguments={"path": "b.txt"})],
        )
        # 故意不提供 TOOL 消息（模拟更早被裁）
    ]
    read_state = {"b.txt": {"resolved_path": str(target), "read_at": "2026-10-24T00:00:00"}}

    attachment = build_post_compact_file_attachment(removed, read_state=read_state)

    assert attachment is not None
    text = attachment.content or ""
    assert "REAL DISK CONTENT" in text, "磁盘兜底必须回注真实内容"


def test_post_compact_caps_content_per_file() -> None:
    """契约：单文件回注受预算约束，超长内容带截断提示。"""
    body = "X" * (POST_COMPACT_MAX_FILE_CHARS * 3)
    attachment = build_post_compact_file_attachment(_read_pair("big.txt", body))

    assert attachment is not None
    text = attachment.content or ""
    assert text.count("X") == POST_COMPACT_MAX_FILE_CHARS
    assert "片段已截断" in text
