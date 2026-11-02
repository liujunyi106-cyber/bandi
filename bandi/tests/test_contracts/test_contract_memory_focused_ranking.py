# 功能：契约——focused 检索加权。
# 钉行为：标题面（key/title）命中加权高于正文命中；警示词（清单/警告/坑/风险）加分；
# 无命中保持原输入顺序（稳定排序不扰动）。
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from bandi.context.sources import memory_source as ms
from bandi.core.message import Message, MessageRole


class _FakeManager:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def search_unified(self, query: str, **kwargs: Any) -> list[Any]:
        return self._entries


class _Entry:
    def __init__(self, key: str, value: str, category: str = "long_term") -> None:
        self.key = key
        self.value = value
        self.metadata = {"category": category}


def _collect(entries: list[_Entry], query: str, session_id: str) -> list[Any]:
    source = ms.MemoryContextSource(memory_manager=_FakeManager(entries))
    return asyncio.run(source.collect(Message(role=MessageRole.USER, content=query), session_id))


def _keys(items: list[Any]) -> list[str]:
    return [str(item.metadata.get("key")) for item in items]


def _isolate(tmp_path: Path, monkeypatch: Any) -> None:
    ms.SEEN_BY_SESSION.clear()
    monkeypatch.setattr(ms, "_seen_store_path", lambda: tmp_path / "seen_memory.json")
    with ms._SEEN_LOCK:
        ms.SEEN_BY_SESSION.clear()


# AR17-1：标题面（key）命中排前于纯正文命中
def test_heading_hit_ranks_before_body_hit(tmp_path: Path, monkeypatch: Any) -> None:
    _isolate(tmp_path, monkeypatch)
    entries = [
        _Entry("generic_note", "订单处理 的常规说明"),
        _Entry("订单处理", "无关正文"),
    ]

    items = _collect(entries, "订单处理", "s-focused-1")

    assert _keys(items)[0] == "订单处理", "标题面命中必须排前"


# AR17-2：同等正文命中下，警示词条目加分靠前
def test_warning_hint_boosts_rank(tmp_path: Path, monkeypatch: Any) -> None:
    _isolate(tmp_path, monkeypatch)
    entries = [
        _Entry("plain", "部署流程说明"),
        _Entry("risky", "部署流程说明；注意：这里有一个坑"),
    ]

    items = _collect(entries, "部署流程", "s-focused-2")

    assert _keys(items)[0] == "risky", "含警示词的条目必须加权靠前"


# AR17-3 破坏者：seen 降权优先于 focused 分数——已注入的高分条目仍排后
def test_seen_demotion_outranks_focused_score(tmp_path: Path, monkeypatch: Any) -> None:
    _isolate(tmp_path, monkeypatch)
    high_score = _Entry("订单处理", "无关正文")  # 标题命中 = 高分
    low_score = _Entry("generic", "订单处理 常规说明")  # 仅正文命中 = 低分
    with ms._SEEN_LOCK:
        # 模拟上一轮已注入高分条目（本轮未注入的低分条目不得被高分借 seen 翻盘）
        ms.SEEN_BY_SESSION["s-focused-3"] = {"long_term:订单处理": 1}

    items = _collect([high_score, low_score], "订单处理", "s-focused-3")

    assert _keys(items)[0] == "generic", "已注入条目必须降权（即使 focused 分更高）"


# AR17-4：查询无命中时不扰动原顺序（稳定）
def test_no_match_keeps_input_order(tmp_path: Path, monkeypatch: Any) -> None:
    _isolate(tmp_path, monkeypatch)
    entries = [
        _Entry("k1", "内容一"),
        _Entry("k2", "内容二"),
        _Entry("k3", "内容三"),
    ]

    items = _collect(entries, "完全无关查询zzz", "s-focused-4")

    assert _keys(items) == ["k1", "k2", "k3"]


# AR17-5：seen 持久化文件不产生真实串扰（隔离断言侧证）
def test_seen_state_isolated_to_tmp(tmp_path: Path, monkeypatch: Any) -> None:
    _isolate(tmp_path, monkeypatch)
    _collect([_Entry("k1", "内容一")], "内容", "s-focused-5")

    assert (tmp_path / "seen_memory.json").exists()
    data = json.loads((tmp_path / "seen_memory.json").read_text(encoding="utf-8"))
    assert "s-focused-5" in data


# AR17-6：中文整句（无空格）查询必须能区分相关/无关——防分词退化为整句成词（零区分度）
def test_chinese_sentence_query_discriminates(tmp_path: Path, monkeypatch: Any) -> None:
    _isolate(tmp_path, monkeypatch)
    entries = [
        _Entry("晚餐想法", "今天晚上吃什么"),
        _Entry("飞书渠道配置", "配置飞书渠道的步骤：建应用、配 webhook"),
    ]

    items = _collect(entries, "如何配置飞书渠道", "s-focused-6")

    assert _keys(items)[0] == "飞书渠道配置", "中文整句查询必须命中相关条目"


# AR17-7 破坏者：中文查询下仅有单字重合（无 2 字以上公共片段）的条目不得反超相关条目
def test_chinese_query_rejects_single_char_overlap(tmp_path: Path, monkeypatch: Any) -> None:
    _isolate(tmp_path, monkeypatch)
    entries = [
        _Entry("无关但有单字重合", "书山有路勤为径"),
        _Entry("飞书渠道配置", "配置飞书渠道的步骤"),
    ]

    items = _collect(entries, "飞书渠道", "s-focused-7")

    assert _keys(items)[0] == "飞书渠道配置", "单字重合不得构成有效命中"
