"""功能：文件追加必须统一行尾，避免同一文件混用 CRLF 与 LF。

真机现象（`bandi/tmp_daily_count.txt`，自动化任务每日追加一行）：

    2026-11-18 10:12 .py 文件数 502
    2026-11-19 09:01 .py 文件数 503
    2026-11-21 13:57 .py 文件数 503

内容正确（UTF-8 无 BOM），但字节层面 `CRLF: 1 + 裸 LF: 2`——同一文件混两种行尾。
根因：append 用 `newline=""` 让 Python **不做换行转换**，模型把换行写成 `\\r\\n`
时被原样落盘，而历史内容是 `\\n`。后果：逐行比对/去重/日志采集会把一行当两行。

契约钉住归一化函数本身：CRLF 与裸 CR 一律转 LF，正文不受影响。
"""

from __future__ import annotations

from bandi.tools.file_ops import normalize_newlines


def test_crlf_becomes_lf() -> None:
    """功能：CRLF 输入必须落成单个 LF。"""
    assert normalize_newlines("第一行\r\n第二行\r\n") == "第一行\n第二行\n"


def test_bare_cr_becomes_lf() -> None:
    """功能：裸 CR（老式 Mac 行尾）也必须归一，防漏一种形态。"""
    assert normalize_newlines("甲\r乙") == "甲\n乙"


def test_mixed_endings_collapse() -> None:
    """功能：混合行尾归一后按行切分，行数必须等于真实行数。"""
    text = normalize_newlines("a\r\nb\nc\rd\r\n")
    assert text.count("\r") == 0
    assert text.splitlines() == ["a", "b", "c", "d"]


def test_plain_lf_is_untouched() -> None:
    """功能：已是 LF 的内容必须原样返回（防过度改写）。"""
    original = "2026-11-21 13:57 .py 文件数 503\n"
    assert normalize_newlines(original) == original


def test_no_trailing_newline_is_preserved() -> None:
    """功能：不补也不删尾行——给什么行尾形态就保持什么。"""
    assert normalize_newlines("没有换行") == "没有换行"
    assert normalize_newlines("带一\r\n") == "带一\n"


def test_chinese_content_is_not_mangled() -> None:
    """功能：归一化不得改动正文（只碰行尾），UTF-8 往返无损。"""
    text = normalize_newlines("2026-11-21 13:57 .py 文件数 503\r\n")
    assert text == "2026-11-21 13:57 .py 文件数 503\n"
    assert text.encode("utf-8").decode("utf-8") == text
