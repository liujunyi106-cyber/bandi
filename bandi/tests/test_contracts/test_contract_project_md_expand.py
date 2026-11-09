"""
project_md 命中经 expand_entry 展开正文注入——搜索命中的 entry，MemoryManager
   的 search_all/search_unified 产出的 project_md 命中 value 必须含实质正文
read_entry 唯一生产调用点：MemoryManager.expand_entry 是 read_entry 的唯一
   生产入口（排除存储封装/测试）。破坏者：换第二处直接 read_entry 则契约暴露。
fail-closed：无 markdown store / 非法 entry_id / 读异常 → expand_entry 返回
   空串，search_unified 主链不炸且 project_md 值回退「标题 — 钩子」摘要。
预算钳制：超长 entry 正文注入只取片段（_PROJECT_MD_SNIPPET_CHARS），值不撑爆。
消费端装配：memory_source（MemoryContextSource.collect）与 prefetch
   （MemoryPrefetchProvider）经 search_unified 拿到的都是展开后正文——值内不再
   出现「指针-only」的钩子丢失内容。
"""

from __future__ import annotations

from typing import Any

from bandi.memory.memory_provider import MemoryManager

_SNIPPET = 300


def _manager(tmp_path: Any) -> tuple[MemoryManager, Any]:
    from bandi.memory.markdown_memory import MarkdownMemoryStore

    store = MarkdownMemoryStore(str(tmp_path / "store"), project_root=tmp_path)
    mm = MemoryManager(markdown_memory=store)
    return mm, store


def _search_project_md(mm: MemoryManager, query: str, limit: int = 5):
    return mm.search_all(query, limit=limit).get("project_md", [])


class TestBodyExpansion:
    def test_hit_value_contains_body_not_just_pointer(self, tmp_path: Any) -> None:
        """命中的 value 必须含 entry 正文关键词（而非仅 title—hook 指针）。"""
        mm, store = _manager(tmp_path)
        store.save_entry("上线检查", "灰度比例必须核对并准备回滚预案再发版")
        hits = _search_project_md(mm, "灰度 回滚")
        assert hits
        hit = hits[0]
        assert "灰度比例必须核对" in hit.value, f"value={hit.value!r} 应为展开正文而非指针"
        assert hit.metadata["entry_id"]

    def test_unified_path_also_exposes_body(self, tmp_path: Any) -> None:
        """消费端走 search_unified（memory_source/prefetch 唯一数据源）也拿到正文。"""
        mm, store = _manager(tmp_path)
        store.save_entry("数据库", "迁移前必须备份 users 表并校验外键约束")
        hits = mm.search_unified("备份 外键", limit=5)
        assert hits
        assert any("备份 users 表" in h.value for h in hits)

    def test_long_body_capped_to_snippet(self, tmp_path: Any) -> None:
        """超长正文注入只取片段，值长 ≤ 片段上限。"""
        mm, store = _manager(tmp_path)
        long_body = "核心内容 " + "X" * 1000 + " 收尾"
        store.save_entry("长文", long_body)
        hits = _search_project_md(mm, "核心内容")
        assert hits
        assert len(hits[0].value) <= _SNIPPET
        assert hits[0].value.startswith("核心内容")

    def test_match_by_body_only_brings_body(self, tmp_path: Any) -> None:
        """标题无关键词、正文命中时，value 同样为正文片段（倒排索引含正文）。"""
        mm, store = _manager(tmp_path)
        store.save_entry("无关键词标题", "正文里藏着灰度发布与回滚预案细节")
        hits = _search_project_md(mm, "灰度 回滚")
        assert hits
        assert "灰度发布与回滚预案" in hits[0].value


class TestFailClosed:
    def test_no_markdown_store_expand_empty(self, tmp_path: Any) -> None:
        """无 markdown store 时 expand_entry 返回空串，不抛异常。"""
        mm = MemoryManager()
        assert mm.expand_entry("20260101-abc12345") == ""

    def test_illegal_entry_id_expand_empty(self, tmp_path: Any) -> None:
        """非法 entry_id（路径穿越字符）expand_entry 净化 → read_entry 拒绝 → 空串。"""
        mm, store = _manager(tmp_path)
        store.save_entry("A", "正文")
        assert mm.expand_entry("../evil") == ""

    def test_missing_entry_expand_empty(self, tmp_path: Any) -> None:
        """不存在的 entry_id → read_entry 抛 FileNotFound → expand_entry 空串。"""
        mm, store = _manager(tmp_path)
        store.save_entry("A", "正文")
        assert mm.expand_entry("20260101-00000000") == ""

    def test_unified_chain_never_raises(self, tmp_path: Any) -> None:
        """检索链对任何输入稳——空 query/search 异常均空列表不炸。"""
        mm, store = _manager(tmp_path)
        store.save_entry("A", "正常正文")
        assert mm.search_unified("", limit=5) == []
        assert mm.search_unified("无关词XYZ不存在的", limit=5) == []


class TestReadEntrySingleCallPoint:
    def test_expand_entry_is_the_read_entry_facade(self, tmp_path: Any) -> None:
        """MemoryManager.expand_entry 委托 markdown.read_entry 返回全文。"""
        mm, store = _manager(tmp_path)
        store.save_entry("标题", "这是很长的一段正文 包含关键事实")
        # 命中的 entry_id 展开应返回完整正文（非片段）
        hits = _search_project_md(mm, "关键事实")
        assert hits
        full = mm.expand_entry(hits[0].metadata["entry_id"])
        assert "包含关键事实" in full
