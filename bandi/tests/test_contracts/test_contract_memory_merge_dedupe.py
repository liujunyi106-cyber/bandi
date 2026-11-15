"""契约：记忆合并去重必须消掉纯排版变体。

缺陷：`_try_add` 只做完全子串比对，于是「青龙·关联·部署端口 7421」与
「青龙·关联·部署端口7421」被当成两条不同陈述并列写进同一条合并记忆。
中文里空白不承载语义，压掉空白后相同的即是同一条陈述。
"""

from __future__ import annotations

from bandi.memory.long_term import MemoryItem
from bandi.memory.merge import _merge_group_content, _squeeze


def _item(item_id: str, content: str) -> MemoryItem:
    return MemoryItem(item_id=item_id, content=content, importance=0.5)


class TestSqueeze:
    def test_removes_all_whitespace(self) -> None:
        assert _squeeze("部署端口 7421") == _squeeze("部署端口7421")

    def test_keeps_cjk_and_digits(self) -> None:
        assert _squeeze("明远 科技·关联") == "明远科技·关联"

    def test_empty_safe(self) -> None:
        assert _squeeze("") == ""
        assert _squeeze(None) == ""


class TestMergeDedupe:
    def test_space_only_variant_not_appended(self) -> None:
        """核心回归：只差空白的两条不得并列。"""
        merged = _merge_group_content(
            [
                _item("a", "青龙·关联·部署端口 7421"),
                _item("b", "青龙·关联·部署端口7421"),
            ]
        )
        lines = merged.splitlines()
        assert len(lines) == 1, f"纯排版变体被当两条写入了：{lines}"

    def test_genuinely_different_statements_both_kept(self) -> None:
        """破坏者视角：内容真的不同就不能被误吞。"""
        merged = _merge_group_content(
            [
                _item("a", "青龙·关联·部署端口 7421"),
                _item("b", "青龙·关联·部署端口 8421"),
            ]
        )
        assert len(merged.splitlines()) == 2, f"不同陈述被误吞：{merged!r}"

    def test_whitespace_inside_same_line_still_deduped(self) -> None:
        merged = _merge_group_content(
            [
                _item("a", "青 龙·关联·端口"),
                _item("b", "青龙·关联·端 口"),
            ]
        )
        assert len(merged.splitlines()) == 1, f"分段空白变体未被去重：{merged!r}"

    def test_canonical_variant_preserved_for_review(self) -> None:
        """去重只消重复，不该改写保留下来的那条内容。"""
        merged = _merge_group_content(
            [
                _item("a", "青龙·关联·部署端口 7421"),
                _item("b", "青龙·关联·部署端口7421"),
            ]
        )
        assert merged.strip() == "青龙·关联·部署端口 7421"
