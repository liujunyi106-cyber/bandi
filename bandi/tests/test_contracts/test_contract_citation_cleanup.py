# 功能：模型自写来源清单清理契约——只删尾部引用块，不误删正文
from __future__ import annotations

import pytest

from bandi.knowledge.citations import strip_generated_citation_block


@pytest.mark.parametrize(
    "header",
    ["引用来源：", "参考来源：", "资料来源：", "**引用来源**：", "### 引用来源"],
)
def test_removes_trailing_citation_block(header: str) -> None:
    content = (
        "产品保修期为两年，电池保修六个月。\n\n"
        f"{header}\n[1] warranty.md（知识库条目「保修政策」，chunk_id: kchunk:abc）"
    )

    cleaned = strip_generated_citation_block(content)

    assert cleaned == "产品保修期为两年，电池保修六个月。"
    assert "chunk_id" not in cleaned


def test_keeps_source_word_inside_normal_prose() -> None:
    content = "这个结论来自多个来源：访谈、日志和工单，需要进一步核对。"

    assert strip_generated_citation_block(content) == content


def test_keeps_content_without_citation_block() -> None:
    content = "第一段。\n\n第二段。"

    assert strip_generated_citation_block(content) == content


def test_does_not_remove_whole_reply() -> None:
    content = "引用来源：[1] warranty.md（chunk_id: kchunk:abc）"

    assert strip_generated_citation_block(content) == content


def test_does_not_strip_header_inside_code_fence() -> None:
    content = "示例：\n\n```text\n引用来源：[1] warranty.md\n```"

    assert strip_generated_citation_block(content) == content


def test_keeps_single_line_generic_source() -> None:
    content = "结论。\n\n来源：https://example.com/a"

    assert strip_generated_citation_block(content) == content


def test_keeps_generic_source_with_single_file_line() -> None:
    content = "结论。\n\n来源：详见 docs/config.json"

    assert strip_generated_citation_block(content) == content


def test_strips_multi_bullet_generic_source() -> None:
    content = "结论。\n\n来源：\n- https://example.com/a\n- https://example.com/b"

    assert strip_generated_citation_block(content) == "结论。"
