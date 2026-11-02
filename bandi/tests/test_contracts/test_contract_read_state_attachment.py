# 功能：契约——CE-23 工具层 read-state 精确恢复（压缩后文件附件按真实读取记录排序）。
# 钉行为：读取工具成功读取后记录读取状态（executor 共享 dict）；压缩附件生成时
# 优先按真实读取时间（最近优先）排序，未读过（仅被提及）的候选垫后；无记录时
# 保持既有 tool_calls 顺序（兼容）；读取失败不得留下记录（破坏者）。
from __future__ import annotations

import asyncio
from pathlib import Path

from bandi.core.message import Message, MessageRole, ToolCall
from bandi.memory.compression_pipeline import build_post_compact_file_attachment
from bandi.tools.executor import ToolExecutor
from bandi.tools.file_ops import FileReadTool
from bandi.tools.registry import ToolRegistry


def _removed_with_reads(paths: list[str]) -> list[Message]:
    """功能：构造被裁消息——依次含 read 工具调用（提及顺序即参数顺序）。"""
    return [
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id=f"c{i}", name="read", arguments={"path": path})],
        )
        for i, path in enumerate(paths)
    ]


# CE23-1：附件按真实读取时间排序（最近读取优先），未读过的候选垫后
def test_attachment_orders_by_real_read_time() -> None:
    removed = _removed_with_reads(["a.md", "b.md", "c.md"])
    read_state = {
        "a.md": {"read_at": "2026-09-15T10:00:00+00:00"},
        "b.md": {"read_at": "2026-09-15T12:00:00+00:00"},
        # c.md 仅被提及未读取
    }

    attachment = build_post_compact_file_attachment(removed, read_state=read_state)

    assert attachment is not None
    # 起附件改为「### 路径 + 真实内容片段」块，排序契约不变
    lines = [line[4:] for line in attachment.content.splitlines() if line.startswith("### ")]
    assert lines[0] == "b.md", "最近读取的文件必须排最前"
    assert lines[1] == "a.md"
    assert lines[-1] == "c.md", "仅提及未读取的候选必须垫后"


# CE23-2：无 read_state（None / 空）保持既有「最近触碰优先」顺序（兼容旧行为）
def test_attachment_without_read_state_keeps_legacy_order() -> None:
    removed = _removed_with_reads(["x.md", "y.md"])

    attachment = build_post_compact_file_attachment(removed, read_state=None)

    assert attachment is not None
    lines = [line[4:] for line in attachment.content.splitlines() if line.startswith("### ")]
    assert lines == ["y.md", "x.md"]


# CE23-3 破坏者：read_state 含被裁范围之外的文件——不得泄漏进附件
def test_attachment_ignores_paths_outside_removed_range() -> None:
    removed = _removed_with_reads(["kept.md"])
    read_state = {
        "kept.md": {"read_at": "2026-09-15T10:00:00+00:00"},
        "outside.md": {"read_at": "2026-09-15T11:00:00+00:00"},
    }

    attachment = build_post_compact_file_attachment(removed, read_state=read_state)

    assert attachment is not None
    assert "outside.md" not in attachment.content
    assert "### kept.md" in attachment.content


# CE23-4 端到端：真实 FileReadTool 经 executor 执行后写入共享 read_state
def test_file_read_tool_records_read_state(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_text("内容", encoding="utf-8")
    registry = ToolRegistry()
    registry.register_instance(FileReadTool())
    executor = ToolExecutor(registry=registry, project_root=str(tmp_path))
    executor.bind_session_meta({"session_id": "s-read"})

    (result,) = asyncio.run(
        executor.execute([ToolCall(id="r1", name="read", arguments={"path": str(target)})])
    )

    assert result.success is True
    assert str(target) in executor.read_state, "成功读取必须记录 read-state（原始 path 键）"
    assert executor.read_state[str(target)].get("read_at")


# CE23-5 破坏者：读取失败（不存在文件）不得留下记录
def test_failed_read_does_not_record(tmp_path: Path) -> None:
    missing = tmp_path / "missing.md"
    registry = ToolRegistry()
    registry.register_instance(FileReadTool())
    executor = ToolExecutor(registry=registry, project_root=str(tmp_path))
    executor.bind_session_meta({"session_id": "s-read-fail"})

    asyncio.run(
        executor.execute([ToolCall(id="r2", name="read", arguments={"path": str(missing)})])
    )

    assert str(missing) not in executor.read_state
