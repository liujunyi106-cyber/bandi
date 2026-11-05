# 功能：markdown entries 倒排索引契约——检索语义不变式(候选剪枝,评分仍原子串匹配)
"""
  倒排完备性：search_entries 的返回结果必须与「全表 title+hook+body 子串
     匹配」的朴素实现逐条一致（score + 排序顺序）——倒排只做候选剪枝
     不得改变检索语义（审查项 #11/#9，#9 倒排索引 + 稳定 ranking）
  规模加速：候选剪枝成立——含查询 token 的 entry 才被评分，不含的
  稳定 ranking：同 relevantce 的 entry 按 created/entry_id 稳定序（与
     _build_index 排序一致），不因倒排引入随机序
  roundtrip 不变：倒排后 save_entry/list/read_entry 行为不变
破坏者断言：若倒排漏建 gram 导致候选缺失（漏命中文命中），契约失败——
"""

from __future__ import annotations

from typing import Any

import pytest

from bandi.memory.episodic import tokenize_cn


def _store(tmp_path: Any) -> Any:
    from bandi.memory.markdown_memory import MarkdownMemoryStore

    return MarkdownMemoryStore(str(tmp_path / "store"), project_root=tmp_path)


def _naive_search(store: Any, query: str, limit: int):
    """朴素基准：直接对 title+hook+body 做子串匹配（倒排的目标等价物）。

    数据源走公共 API（list_entry_pointers + read_entry），不依赖内部 _index 结构。
    """
    q = (query or "").strip()
    if not q:
        return []
    tokens = tokenize_cn(q) or [q]
    scored = []
    for ptr in store.list_entry_pointers():
        body = store.read_entry(ptr.entry_id)
        haystack = f"{ptr.title} {ptr.hook} {body}"
        hit = sum(1 for t in tokens if t in haystack)
        if hit == 0:
            continue
        scored.append((ptr, hit / len(tokens)))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    if limit <= 0:
        return []
    return scored[:limit]


def _seed_entries(store: Any) -> Any:
    store.save_entry("发布检查单", "上线前必须核对灰度比例与回滚预案")
    store.save_entry("数据库迁移", "迁移前必须备份 users 表并校验外键约束")
    store.save_entry("部署流程", "步骤一构建镜像推送仓库")
    store.save_entry("无关天气", "今天天气不错适合散步")
    return store


class TestInvertedCompleteness:
    @pytest.mark.parametrize(
        "query",
        [
            "灰度 回滚",
            "备份 外键",
            "构建镜像",
            "迁移 备份",
            "发布",
            "不存在词XYZ",
            "天气 散步",
        ],
    )
    def test_result_matches_naive_semantics(self, tmp_path: Any, query: str) -> None:
        """倒排结果必须与朴素子串匹配召回一致（集合与顺序不变式）——
        HB-P1后 FTS 命中时分数由 bm25 相关度承载（不再恒 1.0）
        契约钉「不丢/不错排」而非具体分数口径。"""
        store = _seed_entries(_store(tmp_path))
        got = store.search_entries(query, limit=10)
        want = _naive_search(store, query, limit=10)
        assert [p.entry_id for p, _s in got] == [p.entry_id for p, _s in want], (
            f"命中集合与顺序必须与朴素匹配一致（query={query!r}）"
        )
        scores = [s for _p, s in got]
        assert scores == sorted(scores, reverse=True), "分数必须递减"

    def test_multi_term_no_entry_found_return_empty(self, tmp_path: Any) -> None:
        store = _store(tmp_path)
        store.save_entry("A", "仅一个完全不相关条目")
        assert store.search_entries("灰度 回滚", limit=5) == []
        assert store.search_entries("", limit=5) == []

    def test_limit_zero_is_empty(self, tmp_path: Any) -> None:
        store = _seed_entries(_store(tmp_path))
        assert store.search_entries("灰度", limit=0) == []


class TestCandidatePruning:
    def test_only_relevant_entries_are_candidates(self, tmp_path: Any) -> None:
        """
        hit_count==0,候选外的 entry 不得出现。"""
        store = _store(tmp_path)
        store.save_entry("相关一", "灰度发布黄金比例")
        store.save_entry("相关二", "灰度回滚预案演练")
        irrelevant = store.save_entry("无关", "天气晴朗绿树成荫")

        hits = store.search_entries("灰度", limit=10)
        ids = [p.entry_id for p, _ in hits]
        assert irrelevant.entry_id not in ids
        assert len(ids) == 2

    def test_inverted_index_built_from_body_not_just_title(self, tmp_path: Any) -> None:
        """倒排必须索引 title+hook+body 全文（不只 title）,否则正文命中丢失。"""
        store = _store(tmp_path)
        store.save_entry("标题无关键词", "正文藏着灰度发布与回滚预案")
        hits = store.search_entries("灰度 回滚", limit=5)
        assert hits and "正文藏着" in hits[0][0].hook


class TestStableRanking:
    def test_same_score_order_is_deterministic(self, tmp_path: Any) -> None:
        """相同相关度的 entry 顺序稳定（created/entry_id 降序）。"""
        store = _store(tmp_path)
        store.save_entry("E1", "灰度发布预案")
        store.save_entry("E2", "灰度发布预案")
        r1 = store.search_entries("灰度发布预案", limit=10)
        r2 = store.search_entries("灰度发布预案", limit=10)
        assert [p.entry_id for p, _ in r1] == [p.entry_id for p, _ in r2]


class TestRoundtripUnchanged:
    def test_save_list_read_unchanged(self, tmp_path: Any) -> None:
        """倒排后写入/列表/读取行为不变。"""
        store = _store(tmp_path)
        ptr = store.save_entry("部署", "构建推送")
        assert [p.entry_id for p in store.list_entry_pointers()] == [ptr.entry_id]
        assert "构建推送" in store.read_entry(ptr.entry_id)
