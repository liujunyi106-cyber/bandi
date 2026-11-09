# 功能：text_processor 编码探测契约（charset_normalizer 能力如实上报，不得硬编码）
from __future__ import annotations

import importlib.util

import pytest

from bandi.tools.doc.text_processor import TextFileProcessorTool


@pytest.mark.asyncio
async def test_detect_encoding_reports_live_charset_normalizer_availability(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    target = tmp_path / "t.txt"
    target.write_text("hello 中文", encoding="utf-8")
    tool = TextFileProcessorTool()
    result = await tool.execute({"operation": "detect_encoding", "file_path": str(target)})
    assert result["success"] is True
    expected = importlib.util.find_spec("charset_normalizer") is not None
    assert result["charset_normalizer_available"] is expected, (
        "能力字段必须与真实环境探测一致（原实现硬编码 False）"
    )


@pytest.mark.asyncio
async def test_append_preserves_gbk_encoding(tmp_path, monkeypatch) -> None:
    """契约（数据完整性·破坏者）：GBK 文件 append 后必须保持 GBK 可读回——
    修复前检测链误判（无 BOM 的 utf-16 猜测抢吞）且写回硬编码 utf-8
    文件永久损坏（写回编码必须用读取时检测到的编码）。"""
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    target = tmp_path / "gbk.txt"
    target.write_bytes("第一行\n".encode("gbk"))

    tool = TextFileProcessorTool()
    result = await tool.execute({"operation": "append", "file_path": str(target), "data": "第二行"})
    assert result.get("success") is True, result
    content = target.read_bytes().decode("gbk")  # 必须仍可按 GBK 解码
    assert "第一行" in content and "第二行" in content, f"GBK 内容损坏: {content!r}"


@pytest.mark.asyncio
async def test_transform_preserves_gbk_encoding(tmp_path, monkeypatch) -> None:
    """契约（同族）：GBK 文件 transform 后同样保持原编码（replace/transform
    等全部写回路径统一用检测编码）。"""
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    target = tmp_path / "gbk2.txt"
    target.write_bytes("中文内容abc\n".encode("gbk"))

    tool = TextFileProcessorTool()
    result = await tool.execute(
        {"operation": "transform", "file_path": str(target), "transform_type": "uppercase"}
    )
    assert result.get("success") is True, result
    content = target.read_bytes().decode("gbk")
    assert "中文内容ABC" in content, f"GBK 内容损坏: {content!r}"
