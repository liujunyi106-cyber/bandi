# 功能：MarkdownMemoryStore 测试（项目内 ）
import pytest

from bandi.memory.markdown_memory import MarkdownMemoryStore


@pytest.fixture
def store(tmp_path) -> MarkdownMemoryStore:
    return MarkdownMemoryStore(tmp_path)


class TestReadWrite:
    def test_save_and_load_visible_text(self, store: MarkdownMemoryStore) -> None:
        store.save_visible_text("# 项目记忆\n\n选品团队经验")
        assert store.load_visible_text() == "# 项目记忆\n\n选品团队经验"

    def test_load_missing_file_returns_empty(self, store: MarkdownMemoryStore) -> None:
        assert store.load_visible_text() == ""

    def test_append_visible_entry_adds_timestamped_section(
        self, store: MarkdownMemoryStore
    ) -> None:
        store.append_visible_entry("学会了批量选品")
        content = store.load_visible_text()
        assert "## [" in content
        assert "学会了批量选品" in content

    def test_append_is_append_not_overwrite(self, store: MarkdownMemoryStore) -> None:
        store.save_visible_text("第一条")
        store.append_visible_entry("第二条")
        content = store.load_visible_text()
        assert "第一条" in content
        assert "第二条" in content
        assert content.index("第一条") < content.index("第二条")

    def test_ensure_memory_file_creates_bootstrap(self, store: MarkdownMemoryStore) -> None:
        path = store.ensure_memory_file()
        assert path.exists()
        assert store.load_visible_text() != ""


class TestPathSafety:
    def test_project_root_escape_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            MarkdownMemoryStore(tmp_path, project_root=tmp_path / ".." / "escape")

    def test_writes_inside_project_root(self, tmp_path) -> None:
        project_root = tmp_path / "proj"
        store = MarkdownMemoryStore(tmp_path / "store", project_root=project_root)
        store.save_visible_text("内容")
        assert (project_root / ".bandi" / "memory" / "MEMORY.md").exists()

    def test_relative_project_root_ok(self, tmp_path) -> None:
        store = MarkdownMemoryStore(tmp_path, project_root=tmp_path)
        store.save_visible_text("ok")
        assert store.load_visible_text() == "ok"


class TestClamping:
    def test_visible_text_clamped_to_max_chars(self, store: MarkdownMemoryStore) -> None:
        """预算 = 索引体量（12000 chars）——定位为索引而非正文仓库。"""
        from bandi.memory.markdown_memory import PROJECT_MEMORY_MAX_CHARS

        store.save_visible_text("x" * (PROJECT_MEMORY_MAX_CHARS * 3))
        assert len(store.load_visible_text()) <= PROJECT_MEMORY_MAX_CHARS

    def test_max_chars_configurable_soft_budget(self, tmp_path) -> None:
        """审查项9：MAX_CHARS 由模块常量改为实例可配置软预算——超出预算
        的内容路由 entries 保留（不静默丢数据），预算阈值可按项目调。"""
        store = MarkdownMemoryStore(tmp_path, max_chars=120)
        store.save_visible_text("# 项目记忆\n\n" + "x" * 4000)
        visible = store.load_visible_text()
        assert len(visible) <= 120, "可见区必须落在配置预算内"
        # 溢出内容全文落入 entries——指针可读回，未丢数据
        pointers = store.list_entry_pointers()
        assert pointers, "超预算内容必须落 entries 索引而非静默截断"
        body = store.read_entry(pointers[0].entry_id)
        assert "x" in body and len(body.strip("x\n")), "溢出全文保留于 entry"


class TestSearchEntriesLimit:
    def test_search_entries_zero_limit_returns_empty(self, store: MarkdownMemoryStore) -> None:
        """审查项9：search_entries limit<=0 必须返回空——不得因 max(1, limit)
        兜底翻转成返回 1 条（语义缺陷：limit=0 应语义为「不返回结果」）。"""
        store.save_entry("选品", "蓝鲸选品 回滚 灰度的经验")
        store.save_entry("部署", "回滚预案演练")
        assert store.search_entries("蓝鲸", limit=0) == [], (
            "limit=0 必须返回空（旧 max(1,0)→1 兜底翻转语义）"
        )
        assert store.search_entries("蓝鲸", limit=-1) == [], "负 limit 同样返回空"

    def test_search_entries_positive_limit_still_topk(self, store: MarkdownMemoryStore) -> None:
        """审查项9（回归）：正 limit 仍按相关度 top-k，不改原有行为。"""
        store.save_entry("选品", "蓝鲸选品 回滚 灰度的经验")
        store.save_entry("部署", "回滚预案演练")
        hits = store.search_entries("蓝鲸", limit=1)
        assert len(hits) == 1
        assert hits[0][0].title == "选品"


class TestStructuredBlock:
    def test_strip_structured_block(self, store: MarkdownMemoryStore) -> None:
        raw = "可见内容\n<!-- BANDI_STRUCTURED:BEGIN -->\n```yaml\nsecret: 1\n```\n<!-- BANDI_STRUCTURED:END -->"
        store.save_visible_text(raw)
        visible = store.load_visible_text()
        assert "secret" not in visible
        assert "可见内容" in visible

    def test_merge_markdown_idempotent(self, store: MarkdownMemoryStore) -> None:
        store.save_visible_text("已有内容")
        store.merge_markdown("已有内容")
        assert store.load_visible_text() == "已有内容"

    def test_save_visible_text_preserves_structured_block(self, store: MarkdownMemoryStore) -> None:
        raw = (
            "可见A\n"
            "<!-- BANDI_STRUCTURED:BEGIN -->\n"
            "```yaml\nsecret: 1\n```\n"
            "<!-- BANDI_STRUCTURED:END -->"
        )
        store.save_visible_text(raw)
        store.save_visible_text("可见B")
        full = store.memory_file_path.read_text(encoding="utf-8")
        assert "可见B" in full
        assert "BANDI_STRUCTURED:BEGIN" in full
        assert "secret: 1" in full
        assert store.load_visible_text() == "可见B"

    def test_append_visible_entry_preserves_structured_block(
        self, store: MarkdownMemoryStore
    ) -> None:
        raw = "可见A\n<!-- BANDI_STRUCTURED:BEGIN -->\nsecret: 1\n<!-- BANDI_STRUCTURED:END -->"
        store.save_visible_text(raw)
        store.append_visible_entry("新增条目")
        full = store.memory_file_path.read_text(encoding="utf-8")
        assert "新增条目" in full
        assert "secret: 1" in full
        visible = store.load_visible_text()
        assert "secret" not in visible

    def test_append_oversized_keeps_structured_block_intact(
        self, store: MarkdownMemoryStore
    ) -> None:
        """append 超长内容截断时不得切断 structured 块的 BEGIN/END 标记。"""
        raw = "可见A\n<!-- BANDI_STRUCTURED:BEGIN -->\nsecret: 1\n<!-- BANDI_STRUCTURED:END -->"
        store.save_visible_text(raw)
        store.append_visible_entry("x" * 4000)
        full = store.memory_file_path.read_text(encoding="utf-8")
        assert "BANDI_STRUCTURED:BEGIN" in full, "structured BEGIN 标记必须完整保留"
        assert "BANDI_STRUCTURED:END" in full, "structured END 标记必须完整保留"
        assert "secret: 1" in full, "structured 内容不得被截断丢弃"
        assert store.load_visible_text(), "可见部分仍可读取"
