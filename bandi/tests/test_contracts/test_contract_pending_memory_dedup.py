"""功能：待批准队列必须按「正文」去重，不能按条目 id 去重。

真机现象（`~/.bandi/memory/pending/memory` 287 条，审批界面显示 261 条）：

    同一句「帮我搜一下最近 OpenAI 有没有发布新模型」出现 12 次
    item.id = consolidated:ep:b84623c8
              consolidated:ep:78e61f73
              consolidated:ep:5dd55fce   ← 12 个 id 全不同
    content  = 全部相同

根因：去重键取 `(target, item.id)`。巩固晋升把同一段内容写成多条情节，
每条情节晋升时生成 `consolidated:ep:<episode>` 形式的**新** id，于是
`_supersede_duplicates` 认为「都是不同条目」，一句重复话堆十几条。

契约钉住：正文相同即同一条待批准写入，与条目 id 无关。
"""

from __future__ import annotations

from bandi.memory.staging import _payload_item_key


def _payload(content: str, item_id: str, target: str = "long_term") -> dict:
    return {"target": target, "item": {"id": item_id, "content": content}}


def test_same_content_different_ids_share_one_key() -> None:
    """功能：正文相同、id 不同 → 同一个去重键（这正是真机 12 条重复的形态）。"""
    keys = {
        _payload_item_key(_payload("帮我搜一下最近 OpenAI 有没有发布新模型。", f"consolidated:ep:{i:08x}"))
        for i in range(12)
    }
    assert len(keys) == 1, f"12 条同内容产生 {len(keys)} 个键——去重仍按 id 走"


def test_different_content_keeps_distinct_keys() -> None:
    """功能：正文不同必须保留为不同记录（不能过度合并丢信息）。"""
    first = _payload_item_key(_payload("用户偏好含税口径。", "id-1"))
    second = _payload_item_key(_payload("用户偏好英文报告。", "id-2"))
    assert first != second


def test_same_content_different_target_not_merged() -> None:
    """功能：写入目标不同不得合并（长期记忆与技能是两回事）。"""
    long_term = _payload_item_key(_payload("同一句正文", "id-1", target="long_term"))
    skill = _payload_item_key(_payload("同一句正文", "id-1", target="skill"))
    assert long_term != skill


def test_whitespace_around_content_does_not_split() -> None:
    """功能：正文首尾空白不应制造两个键（模型输出常带前后换行）。"""
    first = _payload_item_key(_payload("  结论：按含税口径   ", "id-1"))
    second = _payload_item_key(_payload("结论：按含税口径", "id-2"))
    assert first == second


def test_skill_name_still_wins() -> None:
    """功能：带技能名的记录仍按技能名去重（既有行为不得回归）。"""
    key = _payload_item_key({"skill_name": "pdf", "item": {"content": "x"}})
    assert key == ("skill", "pdf")


def test_falls_back_to_id_when_content_missing() -> None:
    """功能：正文缺失时回落到 id，不能因此把记录判成无法去重。"""
    key = _payload_item_key({"target": "long_term", "item": {"id": "ep-42"}})
    assert key == ("long_term", "ep-42")


def test_unparseable_payload_returns_none() -> None:
    """功能：payload 非法时返回 None（调用方据此跳过去重，而不是崩溃）。"""
    assert _payload_item_key("not a dict") is None
    assert _payload_item_key({"item": "not a dict"}) is None
