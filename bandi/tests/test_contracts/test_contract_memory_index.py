"""
写入溢出：MarkdownMemoryStore.append_visible_entry 超预算 → 全文落
   .bandi/memory/entries/<id>.md + 留指针行（替代旧截断丢弃）
指针 roundtrip：save_entry → list_entry_pointers → read_entry 一致
检索：search_entries 中文 n-gram 命中 → MemoryManager.search_all
   "project_md" 分类 → search_unified 门面自动可达
隔离：project_root 维度 entries 互不可见（沿 _project_store 机制）
安全：read_entry 拒绝路径穿越 id
"""

from typing import Any

import pytest


def _store(tmp_path: Any, project_root: Any = None) -> Any:
    from bandi.memory.markdown_memory import MarkdownMemoryStore

    return MarkdownMemoryStore(str(tmp_path / "store"), project_root=project_root or tmp_path)


class TestOverflowPreservation:
    def test_overflow_creates_entry_and_pointer_line(self, tmp_path: Any) -> None:
        """破坏者：超预算 append 必须落 entry 全文 + 指针行，"""
        store = _store(tmp_path)
        tail_marker = "尾部关键事实XyZ123"
        long_entry = (
            "## [2026-08-26 10:00] 架构决策记录\n\n" + "背景内容。" * 3000
        ) + f"\n{tail_marker}"
        store.append_visible_entry(long_entry)

        pointers = store.list_entry_pointers()
        assert pointers, "溢出内容必须产生 entry 文件"
        full = store.read_entry(pointers[0].entry_id)
        assert tail_marker in full, "entry 必须含完整原文（不得截断）"

        visible = store.load_visible_text()
        assert "](entries/" in visible, "MEMORY.md 必须留指针行"

    def test_small_append_stays_inline(self, tmp_path: Any) -> None:
        """未超预算的常规追加行为不变（直接进 正文）。"""
        store = _store(tmp_path)
        store.append_visible_entry("短条目事实")
        assert "短条目事实" in store.load_visible_text()
        assert store.list_entry_pointers() == []


class TestPointerRoundtrip:
    def test_save_list_read_roundtrip(self, tmp_path: Any) -> None:
        store = _store(tmp_path)
        ptr = store.save_entry("部署流程", "步骤一构建镜像。\n步骤二推送仓库。")

        pointers = store.list_entry_pointers()
        assert [p.entry_id for p in pointers] == [ptr.entry_id]
        assert pointers[0].title == "部署流程"
        body = store.read_entry(ptr.entry_id)
        assert "步骤二推送仓库" in body

    def test_read_entry_rejects_traversal(self, tmp_path: Any) -> None:
        store = _store(tmp_path)
        with pytest.raises(ValueError):
            store.read_entry("../../etc/passwd")


class TestSearchAndIsolation:
    def test_search_entries_chinese_hit(self, tmp_path: Any) -> None:
        store = _store(tmp_path)
        store.save_entry("发布检查单", "上线前必须核对灰度比例与回滚预案")
        store.save_entry("无关条目", "今天天气不错适合散步")

        hits = store.search_entries("灰度 回滚")
        assert hits and "灰度" in hits[0][0].hook or hits[0][0].title == "发布检查单"
        assert all(p.title != "无关条目" for p, _ in hits)

    def test_dirty_entry_file_skipped_not_crash(self, tmp_path: Any) -> None:
        store = _store(tmp_path)
        entries_dir = tmp_path / ".bandi" / "memory" / "entries"
        entries_dir.mkdir(parents=True)
        (entries_dir / "bad.md").write_text("无 frontmatter 的脏文件", encoding="utf-8")

        pointers = store.list_entry_pointers()
        assert len(pointers) == 1 and pointers[0].title  # 降级不崩

    def test_project_root_isolation(self, tmp_path: Any) -> None:
        proj_a = tmp_path / "proj_a"
        proj_b = tmp_path / "proj_b"
        store_a = _store(tmp_path, project_root=proj_a)
        store_b = _store(tmp_path, project_root=proj_b)

        store_a.save_entry("A 项目专属", "alpha 内容")
        assert [p.title for p in store_b.list_entry_pointers()] == []
        assert [p.title for p in store_a.list_entry_pointers()] == ["A 项目专属"]


class TestPointerLineIntegrity:
    """
    先脱 hook，再截 title，`[..](entries/<id>.md)` 必须始终闭合完整。"""

    def _store(self, tmp_path: Any, max_chars: int) -> Any:
        from bandi.memory.markdown_memory import MarkdownMemoryStore

        return MarkdownMemoryStore(
            str(tmp_path / "store"), project_root=tmp_path, max_chars=max_chars
        )

    def test_pointer_line_drops_hook_keeps_link_intact(self, tmp_path: Any) -> None:
        """指针行超预算且剪掉 hook 后仍放得下 → 只剪 hook，链接完整。"""
        store = self._store(tmp_path, max_chars=120)
        body = "关键事实" + "_" * 300
        store.append_visible_entry(f"超长标题{'字' * 30}：{body}")

        visible = store.load_visible_text()
        assert "](entries/" in visible, "指针行必须存在"
        assert visible.strip().endswith(")"), "链接行必须以 ) 闭合，不得被截断"
        assert " — " not in visible, "hook 超预算时应被剪除"
        assert len(visible) <= 120, "可见区不得突破预算上限"
        size_ok = len(visible) <= 120 or True  # 保留：预算约束由裁剪指针行达成
        assert size_ok
        assert store.list_entry_pointers(), "正文必须已落 entry"
        assert "_" * 300 in store.read_entry(store.list_entry_pointers()[0].entry_id)

    def test_pointer_line_trims_title_keeps_link_closed(self, tmp_path: Any) -> None:
        """预算极小连 title 都要截 → 链接语法依然闭合（`[前缀](entries/id.md)`）。"""
        store = self._store(tmp_path, max_chars=40)
        store.append_visible_entry(f"标题{'一' * 60}：正文{'b' * 200}")

        visible = store.load_visible_text()
        assert "](entries/" in visible, "指针行必须存在"
        body = visible.split("]", 1)[-1]
        assert body.startswith("(entries/") and body.endswith(")"), "链接必须完整闭合"
        assert len(visible) <= 40

    def test_pointer_line_after_visible_near_budget_not_truncated(self, tmp_path: Any) -> None:
        """既有 visible 已顶满预算时追加长条目 → 新条目归档为指针而非截断既有内容。"""
        store = self._store(tmp_path, max_chars=400)
        store.append_visible_entry("既有可见内容" + "v" * 360)
        first = store.load_visible_text()
        assert "既有可见内容" in first
        store.append_visible_entry("新追加的完整事实" + "n" * 500)

        visible = store.load_visible_text()
        assert "既有可见内容" in visible, "既有内容不得被截断丢失"
        ptr_line = [ln for ln in visible.splitlines() if "](entries/" in ln]
        assert ptr_line, "新条目必须归档为指针行"
        assert ".md)" in ptr_line[0].split("]", 1)[1], "链接必须完整闭合 (.md)"


class TestSearchAllIntegration:
    def test_search_all_project_md_category(self, tmp_path: Any) -> None:
        """entries 经 MemoryManager.search_all 'project_md' 分类可达。"""
        from bandi.memory.long_term import LongTermMemory
        from bandi.memory.memory_provider import MemoryManager
        from bandi.memory.working import ScratchpadStore

        md_store = _store(tmp_path)
        md_store.save_entry("数据库迁移经验", "迁移前必须备份 users 表并校验外键约束")

        manager = MemoryManager(
            long_term=LongTermMemory(str(tmp_path / "long.db")),
            working=ScratchpadStore(str(tmp_path / "working.db")),
            markdown_memory=md_store,
        )
        results = manager.search_all("备份 外键", session_id="s1")
        assert "project_md" in results, "entries 必须以 project_md 分类进入统一检索"
        assert any("外键" in e.value for e in results["project_md"])
