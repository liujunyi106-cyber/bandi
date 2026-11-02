# 功能：入口行数上限契约
# 契约原则：入口文件行数超限必须收敛（指针行淘汰 + append 溢出 entries 化）；正文不丢数据。
from __future__ import annotations

import bandi.memory.markdown_memory as mm_mod
from bandi.memory.markdown_memory import MarkdownMemoryStore


def _store(tmp_path) -> MarkdownMemoryStore:
    store = MarkdownMemoryStore(tmp_path)
    store.ensure_memory_file()
    return store


def test_entrypoint_pointer_lines_pruned_to_cap(tmp_path, monkeypatch) -> None:
    """指针行超限按最旧优先淘汰至行数上限，最新保留。"""
    monkeypatch.setattr(mm_mod, "MAX_ENTRYPOINT_LINES", 6)
    store = _store(tmp_path)
    lines = ["# 标题", ""] + [f"- [条目{i}](entries/2025010{i}-aaaa.md) — hook" for i in range(8)]
    store.save_visible_text("\n".join(lines))
    visible = store.load_visible_text()
    assert len(visible.splitlines()) <= 6, (
        f"行数必须收敛到上限（实际 {len(visible.splitlines())}）：{visible!r}"
    )
    assert "条目0" not in visible, "最旧指针优先淘汰"
    assert "条目7" in visible, "最新指针必须保留"


def test_append_line_overflow_falls_back_to_entries(tmp_path, monkeypatch) -> None:
    """append 行数超限 → 内容落 entries（不丢数据），入口行数收敛。"""
    monkeypatch.setattr(mm_mod, "MAX_ENTRYPOINT_LINES", 5)
    store = _store(tmp_path)
    for i in range(6):
        store.append_visible_entry(f"第{i}段内容")
    visible = store.load_visible_text()
    assert len(visible.splitlines()) <= 5, (
        f"append 行数超限必须收敛（实际 {len(visible.splitlines())}）：{visible!r}"
    )
    assert list(store.entries_dir.glob("*.md")), "溢出内容必须落 entries"


def test_append_within_cap_keeps_inline_section(tmp_path, monkeypatch) -> None:
    """回归：上限内 append 仍直写段落（不进 entries）。"""
    monkeypatch.setattr(mm_mod, "MAX_ENTRYPOINT_LINES", 200)
    store = _store(tmp_path)
    store.append_visible_entry("短记录")
    visible = store.load_visible_text()
    assert "短记录" in visible
    assert "(entries/" not in visible
