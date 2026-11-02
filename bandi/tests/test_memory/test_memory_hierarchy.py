# 功能：MemoryHierarchyManager 四层快照门面测试
import json

import pytest

from bandi.company.store import WorkItemStore
from bandi.memory.global_store import GlobalMemoryStore
from bandi.memory.markdown_memory import MarkdownMemoryStore
from bandi.memory.memory_manager import MemoryHierarchyManager
from bandi.memory.session.store import SessionStore


@pytest.fixture
def hierarchy(tmp_path):
    return MemoryHierarchyManager(
        global_store=GlobalMemoryStore(tmp_path / "global.db"),
        markdown_memory=MarkdownMemoryStore(tmp_path),
        work_item_store=WorkItemStore(str(tmp_path / "company.db")),
        session_store=SessionStore(tmp_path / "sessions.db"),
    )


class TestSaveLoadGlobal:
    def test_save_and_load_global_memory(self, hierarchy: MemoryHierarchyManager) -> None:
        hierarchy.save_memory("global", "产品定位：个人多 Agent 数字员工组织", allow_shared=True)
        assert hierarchy.load_memory("global") == "产品定位：个人多 Agent 数字员工组织"

    def test_load_empty_global_returns_empty(self, hierarchy: MemoryHierarchyManager) -> None:
        assert hierarchy.load_memory("global") == ""

    def test_append_global_memory(self, hierarchy: MemoryHierarchyManager) -> None:
        hierarchy.save_memory("global", "第一条", allow_shared=True)
        hierarchy.append_memory("global", "第二条", allow_shared=True)
        content = hierarchy.load_memory("global")
        assert "第一条" in content
        assert "第二条" in content


class TestSaveLoadProject:
    def test_save_and_load_project_memory(self, hierarchy: MemoryHierarchyManager) -> None:
        hierarchy.save_memory("project", "项目经验", project_id="proj1")
        assert hierarchy.load_memory("project", project_id="proj1") == "项目经验"

    def test_append_project_memory(self, hierarchy: MemoryHierarchyManager) -> None:
        hierarchy.save_memory("project", "基础", project_id="proj1")
        hierarchy.append_memory("project", "追加", project_id="proj1")
        content = hierarchy.load_memory("project", project_id="proj1")
        assert "基础" in content
        assert "追加" in content


class TestSaveLoadSessionEmployee:
    def test_save_and_load_session_memory(self, hierarchy: MemoryHierarchyManager) -> None:
        hierarchy.save_memory(
            "session", "会话过程记忆", project_id="proj1", session_id="s1", employee_id="emp1"
        )
        assert (
            hierarchy.load_memory(
                "session", project_id="proj1", session_id="s1", employee_id="emp1"
            )
            == "会话过程记忆"
        )

    def test_save_and_load_employee_memory(self, hierarchy: MemoryHierarchyManager) -> None:
        hierarchy.save_memory(
            "employee", "员工最终记忆", project_id="proj1", employee_id="emp1", role_id="选品专员"
        )
        assert (
            hierarchy.load_memory("employee", project_id="proj1", employee_id="emp1")
            == "员工最终记忆"
        )

    def test_load_missing_session_returns_empty(self, hierarchy: MemoryHierarchyManager) -> None:
        assert (
            hierarchy.load_memory("session", project_id="p", session_id="missing", employee_id="e")
            == ""
        )


class TestCollectContext:
    def test_collect_aggregates_all_scopes(self, hierarchy: MemoryHierarchyManager) -> None:
        hierarchy.save_memory("global", "全局：数字员工组织", allow_shared=True)
        hierarchy.save_memory("project", "项目：选品方法论", project_id="proj1")
        hierarchy.save_memory(
            "session", "会话：完成竞品分析", project_id="proj1", session_id="s1", employee_id="emp1"
        )
        hierarchy.save_memory(
            "employee", "员工：选品专员经验", project_id="proj1", employee_id="emp1"
        )
        context = hierarchy.collect_memory_context(
            project_id="proj1", session_id="s1", employee_id="emp1"
        )
        assert "全局：数字员工组织" in context
        assert "项目：选品方法论" in context
        assert "会话：完成竞品分析" in context
        assert "员工：选品专员经验" in context

    def test_collect_with_empty_scopes_returns_empty(
        self, hierarchy: MemoryHierarchyManager
    ) -> None:
        context = hierarchy.collect_memory_context(
            project_id="proj1", session_id="s1", employee_id="emp1"
        )
        assert context == ""

    def test_collect_missing_project_scope_skipped(self, hierarchy: MemoryHierarchyManager) -> None:
        hierarchy.save_memory("global", "只有全局", allow_shared=True)
        context = hierarchy.collect_memory_context(
            project_id="proj1", session_id="s1", employee_id="emp1"
        )
        assert "只有全局" in context

    def test_collect_empty_session_id_skips_session_scope(
        self, hierarchy: MemoryHierarchyManager
    ) -> None:
        """session_id 为空时不得检索 session 层（避免误读 employee 的 process 快照）。"""
        hierarchy.save_memory(
            "session", "会话记忆", project_id="proj1", session_id="", employee_id="emp1"
        )
        context = hierarchy.collect_memory_context(
            project_id="proj1", session_id="", employee_id="emp1"
        )
        assert "会话记忆" not in context, "无 session_id 时 session 层不得注入"

    def test_collect_budget_keeps_high_priority_scopes(
        self, hierarchy: MemoryHierarchyManager
    ) -> None:
        """预算不足时按 employee > session > project > global 优先级丢弃最低优先级层。"""
        from bandi.utils.token_counter import TokenCounter

        g_text = "全局：" + "x" * 200
        p_text = "项目：" + "y" * 200
        s_text = "会话：" + "z" * 200
        e_text = "员工：" + "w" * 200
        hierarchy.save_memory("global", g_text, allow_shared=True)
        hierarchy.save_memory("project", p_text, project_id="proj1")
        hierarchy.save_memory(
            "session", s_text, project_id="proj1", session_id="s1", employee_id="emp1"
        )
        hierarchy.save_memory("employee", e_text, project_id="proj1", employee_id="emp1")
        # 预算 = 恰好装下 员工+会话 两块（最高/次高优先级），保证装不下再加入 项目 块
        budget = TokenCounter.count(e_text) + TokenCounter.count(s_text)
        assert budget < TokenCounter.count(e_text) + TokenCounter.count(
            s_text
        ) + TokenCounter.count(p_text), "构造前提：预算不足以再装下项目块"
        context = hierarchy.collect_memory_context(
            project_id="proj1", session_id="s1", employee_id="emp1", max_tokens=budget
        )
        assert "员工" in context, "最高优先级（employee）必须保留"
        assert "全局" not in context, "最低优先级（global）应被预算裁剪"
        assert "项目" not in context or "会话" not in context, "中优先级层应按预算裁剪"

    def test_collect_budget_zero_returns_empty(self, hierarchy: MemoryHierarchyManager) -> None:
        """预算为 0 时返回空字符串（最高优先级层也放不下）。"""
        hierarchy.save_memory("employee", "员工记忆", project_id="proj1", employee_id="emp1")
        context = hierarchy.collect_memory_context(
            project_id="proj1", session_id="", employee_id="emp1", max_tokens=0
        )
        assert context == ""

    def test_collect_oversized_employee_keeps_lower_scopes(
        self, hierarchy: MemoryHierarchyManager
    ) -> None:
        """
        继续保留能放进预算的全局/项目/会话层。"""
        from bandi.utils.token_counter import TokenCounter

        g_text = "全局：" + "g" * 50
        p_text = "项目：" + "p" * 50
        s_text = "会话：" + "s" * 50
        e_text = "员工：" + "e" * 400
        hierarchy.save_memory("global", g_text, allow_shared=True)
        hierarchy.save_memory("project", p_text, project_id="proj1")
        hierarchy.save_memory(
            "session", s_text, project_id="proj1", session_id="s1", employee_id="emp1"
        )
        hierarchy.save_memory("employee", e_text, project_id="proj1", employee_id="emp1")
        # 预算 = 恰好装下低三层（全局+项目+会话），员工层单独必超预算
        budget = (
            TokenCounter.count(g_text) + TokenCounter.count(p_text) + TokenCounter.count(s_text)
        )
        assert budget < TokenCounter.count(e_text), "构造前提：员工层单独超预算"
        context = hierarchy.collect_memory_context(
            project_id="proj1", session_id="s1", employee_id="emp1", max_tokens=budget
        )
        assert context != "", "员工层超预算时其余层仍应保留"
        assert "员工" not in context, "超预算的员工层应被跳过"
        assert "全局" in context and "项目" in context and "会话" in context

    def test_collect_budget_covers_all_scopes(self, hierarchy: MemoryHierarchyManager) -> None:
        """预算充足时四层全量返回（向后兼容）。"""
        hierarchy.save_memory("global", "全局", allow_shared=True)
        hierarchy.save_memory("project", "项目", project_id="proj1")
        hierarchy.save_memory(
            "session", "会话", project_id="proj1", session_id="s1", employee_id="emp1"
        )
        hierarchy.save_memory("employee", "员工", project_id="proj1", employee_id="emp1")
        context = hierarchy.collect_memory_context(
            project_id="proj1", session_id="s1", employee_id="emp1", max_tokens=4096
        )
        assert "全局" in context
        assert "项目" in context
        assert "会话" in context
        assert "员工" in context


class TestPersistence:
    def test_snapshots_persist_across_instances(self, tmp_path) -> None:
        h1 = MemoryHierarchyManager(
            global_store=GlobalMemoryStore(tmp_path / "global.db"),
            markdown_memory=MarkdownMemoryStore(tmp_path),
            work_item_store=WorkItemStore(str(tmp_path / "company.db")),
            session_store=SessionStore(tmp_path / "sessions.db"),
        )
        h1.save_memory("global", "持久全局记忆", allow_shared=True)
        h1.save_memory("employee", "持久员工记忆", project_id="proj1", employee_id="emp1")
        h2 = MemoryHierarchyManager(
            global_store=GlobalMemoryStore(tmp_path / "global.db"),
            markdown_memory=MarkdownMemoryStore(tmp_path),
            work_item_store=WorkItemStore(str(tmp_path / "company.db")),
            session_store=SessionStore(tmp_path / "sessions.db"),
        )
        assert h2.load_memory("global") == "持久全局记忆"
        assert h2.load_memory("employee", project_id="proj1", employee_id="emp1") == "持久员工记忆"


class TestMultiProject:
    def test_project_memory_isolated_by_project_id(self, tmp_path) -> None:
        project_a = MarkdownMemoryStore(tmp_path / "stores", project_root=tmp_path / "proj_a")
        project_b = MarkdownMemoryStore(tmp_path / "stores", project_root=tmp_path / "proj_b")
        hierarchy = MemoryHierarchyManager(
            global_store=GlobalMemoryStore(tmp_path / "global.db"),
            markdown_memory=project_a,
            work_item_store=WorkItemStore(str(tmp_path / "company.db")),
            project_stores={"proj_a": project_a, "proj_b": project_b},
            session_store=SessionStore(tmp_path / "sessions.db"),
        )
        hierarchy.save_memory("project", "A 项目经验", project_id="proj_a")
        hierarchy.save_memory("project", "B 项目经验", project_id="proj_b")
        assert hierarchy.load_memory("project", project_id="proj_a") == "A 项目经验"
        assert hierarchy.load_memory("project", project_id="proj_b") == "B 项目经验"

    def test_project_memory_falls_back_to_default_store(
        self, hierarchy: MemoryHierarchyManager
    ) -> None:
        hierarchy.save_memory("project", "默认项目经验", project_id="unknown")
        assert hierarchy.load_memory("project", project_id="unknown") == "默认项目经验"


class TestMultiProjectStoreDir:
    """store_dir 模式下项目记忆按 project_id 派生独立目录，多项目互不串写。"""

    @pytest.fixture
    def store_dir_hierarchy(self, tmp_path) -> MemoryHierarchyManager:
        return MemoryHierarchyManager(
            global_store=GlobalMemoryStore(tmp_path / "global.db"),
            markdown_memory=MarkdownMemoryStore(tmp_path / "stores", project_root=tmp_path),
            work_item_store=WorkItemStore(str(tmp_path / "company.db")),
            session_store=SessionStore(tmp_path / "sessions.db"),
            store_dir=tmp_path / "stores",
        )

    def test_derived_project_stores_isolated(
        self, store_dir_hierarchy: MemoryHierarchyManager, tmp_path
    ) -> None:
        store_dir_hierarchy.save_memory("project", "A 项目经验", project_id="proj-a")
        store_dir_hierarchy.save_memory("project", "B 项目经验", project_id="proj-b")
        assert store_dir_hierarchy.load_memory("project", project_id="proj-a") == "A 项目经验"
        assert store_dir_hierarchy.load_memory("project", project_id="proj-b") == "B 项目经验"
        assert (tmp_path / "stores" / "projects" / "proj-a").is_dir()
        assert (tmp_path / "stores" / "projects" / "proj-b").is_dir()

    def test_derived_store_persists_across_instances(
        self, store_dir_hierarchy: MemoryHierarchyManager, tmp_path
    ) -> None:
        store_dir_hierarchy.save_memory("project", "持久项目记忆", project_id="proj-a")
        reopened = MemoryHierarchyManager(
            global_store=GlobalMemoryStore(tmp_path / "global.db"),
            markdown_memory=MarkdownMemoryStore(tmp_path / "stores", project_root=tmp_path),
            work_item_store=WorkItemStore(str(tmp_path / "company.db")),
            session_store=SessionStore(tmp_path / "sessions.db"),
            store_dir=tmp_path / "stores",
        )
        assert reopened.load_memory("project", project_id="proj-a") == "持久项目记忆"

    def test_unsafe_project_id_falls_back_to_default(
        self, store_dir_hierarchy: MemoryHierarchyManager, tmp_path
    ) -> None:
        store_dir_hierarchy.save_memory("project", "默认兜底", project_id="../escape")
        assert store_dir_hierarchy.load_memory("project", project_id="../escape") == "默认兜底"
        assert not (tmp_path / "stores" / "projects" / ".." / "escape").exists()

    def test_dotdot_path_traversal_blocked(
        self, store_dir_hierarchy: MemoryHierarchyManager, tmp_path
    ) -> None:
        store_dir_hierarchy.save_memory("project", "兜底", project_id="..")
        assert not (tmp_path / "stores" / "projects" / "..").exists()
        store_dir_hierarchy.save_memory("project", "兜底2", project_id="a/../../evil")
        assert not (tmp_path / "evil").exists()

    def test_dot_prefix_project_id_blocked(
        self, store_dir_hierarchy: MemoryHierarchyManager, tmp_path
    ) -> None:
        store_dir_hierarchy.save_memory("project", "兜底", project_id=".hidden")
        assert store_dir_hierarchy.load_memory("project", project_id=".hidden") == "兜底"

    def test_chinese_project_id_isolated_not_global(
        self, store_dir_hierarchy: MemoryHierarchyManager, tmp_path
    ) -> None:
        store_dir_hierarchy.save_memory("project", "中文项目经验", project_id="恒星贸易项目")
        assert (
            store_dir_hierarchy.load_memory("project", project_id="恒星贸易项目") == "中文项目经验"
        )
        dirs = [p.name for p in (tmp_path / "stores" / "projects").iterdir()]
        assert any("_" in d for d in dirs), "中文 project_id 必须替换为下划线派生目录"
        store_dir_hierarchy.save_memory("project", "另一项目经验", project_id="另一个项目")
        assert (
            store_dir_hierarchy.load_memory("project", project_id="恒星贸易项目") == "中文项目经验"
        )

    def test_long_project_id_truncated_to_64(
        self, store_dir_hierarchy: MemoryHierarchyManager, tmp_path
    ) -> None:
        long_id = "p" * 200
        store_dir_hierarchy.save_memory("project", "长ID经验", project_id=long_id)
        dirs = [p.name for p in (tmp_path / "stores" / "projects").iterdir()]
        assert len(dirs) == 1
        assert len(dirs[0]) <= 64, "派生目录段必须截断到 64"

    def test_safe_project_id_used_verbatim(
        self, store_dir_hierarchy: MemoryHierarchyManager, tmp_path
    ) -> None:
        store_dir_hierarchy.save_memory("project", "直用", project_id="proj-a_1")
        assert (tmp_path / "stores" / "projects" / "proj-a_1").is_dir()

    def test_windows_reserved_names_fall_back(
        self, store_dir_hierarchy: MemoryHierarchyManager, tmp_path
    ) -> None:
        """深度复审修复：Windows 保留名（CON/NUL/PRN/AUX/COM1-9/LPT1-9）与"""
        for bad in ("CON", "NUL", "PRN", "AUX", "COM1", "LPT9", "con", "nul"):
            store_dir_hierarchy.save_memory("project", "兜底", project_id=bad)
            assert store_dir_hierarchy.load_memory("project", project_id=bad) == "兜底"
        assert not (tmp_path / "stores" / "projects" / "CON").exists()

    def test_trailing_dot_space_project_id_falls_back(
        self, store_dir_hierarchy: MemoryHierarchyManager, tmp_path
    ) -> None:
        """深度复审修复：尾随点/空格目录段在 Windows 会被规范化（"proj." → "proj"）
        → 碰撞，一律回退默认 store。"""
        store_dir_hierarchy.save_memory("project", "尾随点", project_id="proj.")
        assert store_dir_hierarchy.load_memory("project", project_id="proj.") == "尾随点"
        store_dir_hierarchy.save_memory("project", "尾随空格", project_id="proj ")
        assert store_dir_hierarchy.load_memory("project", project_id="proj ") == "尾随空格"


class TestSessionRecordHygiene:
    """_ensure_session_record 不得在 SessionStore 建「记忆会话」假记录污染 list_sessions。"""

    def test_save_session_memory_does_not_create_fake_session(
        self, hierarchy: MemoryHierarchyManager
    ) -> None:
        hierarchy.save_memory(
            "session", "会话过程", project_id="p1", session_id="ghost", employee_id="e1"
        )
        sessions = hierarchy._session_store.list_sessions()
        assert sessions == [], "session 不在 SessionStore 时不得 create 假记录"
        assert hierarchy._session_store.get_session("ghost") is None

    def test_save_session_memory_updates_metadata_when_session_exists(
        self, hierarchy: MemoryHierarchyManager
    ) -> None:
        sid = hierarchy._session_store.create_session(mode="execute")
        hierarchy.save_memory(
            "session", "会话过程", project_id="p1", session_id=sid, employee_id="e1"
        )
        session = hierarchy._session_store.get_session(sid)
        assert session is not None
        assert "记忆会话" not in (session.get("title") or "")
        metadata = json.loads(session.get("metadata") or "{}")
        assert metadata.get("project_id") == "p1"
        assert metadata.get("employee_id") == "e1"

    def test_save_session_memory_preserves_existing_session_fields(
        self, hierarchy: MemoryHierarchyManager
    ) -> None:
        sid = hierarchy._session_store.create_session(title="用户真实会话", mode="execute")
        hierarchy.save_memory("session", "过程", project_id="p1", session_id=sid, employee_id="e1")
        session = hierarchy._session_store.get_session(sid)
        assert session["title"] == "用户真实会话"
        assert session["mode"] == "execute"

    def test_save_session_memory_merges_existing_metadata(
        self, hierarchy: MemoryHierarchyManager
    ) -> None:
        """快照保存不得覆盖 session 已有 metadata（如 artifact_hashes 增量去重键）。"""
        sid = hierarchy._session_store.create_session(mode="execute")
        hierarchy._session_store.update_session(
            sid, metadata=json.dumps({"artifact_hashes": {"a": "h1"}}, ensure_ascii=False)
        )
        hierarchy.save_memory("session", "过程", project_id="p1", session_id=sid, employee_id="e1")
        session = hierarchy._session_store.get_session(sid)
        metadata = json.loads(session.get("metadata") or "{}")
        assert metadata.get("artifact_hashes") == {"a": "h1"}, (
            "已有 metadata（artifact_hashes）必须保留，不得被快照字段覆盖"
        )
        assert metadata.get("project_id") == "p1"


class TestLoadSessionEmptyId:
    """load_memory("session") 空 session_id 必须返回空，不得退化读 employee 快照。"""

    def test_load_session_without_session_id_returns_empty(
        self, hierarchy: MemoryHierarchyManager
    ) -> None:
        hierarchy.save_memory(
            "session", "会话过程", project_id="p1", session_id="s1", employee_id="e1"
        )
        assert hierarchy.load_memory("session", project_id="p1", employee_id="e1") == ""
        assert (
            hierarchy.load_memory("session", project_id="p1", session_id="", employee_id="e1") == ""
        )

    def test_load_session_with_session_id_still_works(
        self, hierarchy: MemoryHierarchyManager
    ) -> None:
        hierarchy.save_memory(
            "session", "会话过程", project_id="p1", session_id="s1", employee_id="e1"
        )
        assert (
            hierarchy.load_memory("session", project_id="p1", session_id="s1", employee_id="e1")
            == "会话过程"
        )
