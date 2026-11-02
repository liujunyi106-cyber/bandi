from __future__ import annotations

from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.registry import ToolRegistry


class _MockTool(BaseTool):
    name = "bash"
    description = "run shell commands"
    category = ToolCategory.CODE

    async def execute(self, params, context=None):
        return None


class _MockReadTool(BaseTool):
    name = "read"
    description = "read files"
    category = ToolCategory.CODE
    aliases = ("cat",)

    async def execute(self, params, context=None):
        return None


class _MockWebTool(BaseTool):
    name = "web_search"
    description = "search web"
    category = ToolCategory.WEB

    async def execute(self, params, context=None):
        return None


def make_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register_instance(_MockTool())
    r.register_instance(_MockReadTool())
    r.register_instance(_MockWebTool())
    return r


class TestScopedCategories:
    def test_code_scope_includes_bash_and_read(self):
        r = make_registry()
        scoped = r.scoped_tags(["code"])
        names = {t.name for t in scoped.list_tools()}
        assert "bash" in names
        assert "read" in names
        assert "web_search" not in names

    def test_web_scope_includes_web_search_only(self):
        r = make_registry()
        scoped = r.scoped_tags(["web"])
        names = {t.name for t in scoped.list_tools()}
        assert "web_search" in names
        assert "bash" not in names

    def test_multiple_categories_or_logic(self):
        r = make_registry()
        scoped = r.scoped_tags(["code", "web"])
        names = {t.name for t in scoped.list_tools()}
        assert {"bash", "read", "web_search"} <= names

    def test_scoped_get_filters(self):
        r = make_registry()
        scoped = r.scoped_tags(["web"])
        assert scoped.get("web_search") is not None
        assert scoped.get("bash") is None


class TestScopedRole:
    def test_admin_sees_all(self):
        r = make_registry()
        scoped = r.scoped_role("admin")
        names = {t.name for t in scoped.list_tools()}
        assert "bash" in names
        assert "read" in names
        assert "web_search" in names

    def test_readonly_sees_only_web(self):
        r = make_registry()
        scoped = r.scoped_role("readonly")
        names = {t.name for t in scoped.list_tools()}
        assert "web_search" in names
        assert "bash" not in names

    def test_member_sees_web_but_not_code(self):
        r = make_registry()
        scoped = r.scoped_role("member")
        names = {t.name for t in scoped.list_tools()}
        assert "web_search" in names
        assert "bash" not in names

    def test_unknown_role_falls_back_to_util(self):
        r = make_registry()
        scoped = r.scoped_role("nonexistent")
        assert len(scoped.list_tools()) == 0


class TestScopedSchemas:
    def test_scoped_schemas_filters(self):
        r = make_registry()
        scoped = r.scoped_tags(["code"])
        schemas = scoped.list_schemas()
        names = {s["function"]["name"] for s in schemas}
        assert "bash" in names
        assert "web_search" not in names


class TestAliases:
    def test_get_by_alias(self):
        r = make_registry()
        assert r.get("read") is not None
        assert r.get("cat").name == "read"

    def test_get_unknown_returns_none(self):
        r = make_registry()
        assert r.get("nonexistent") is None

    def test_get_respects_enabled(self):
        r = make_registry()
        r._tools["read"].enabled = False
        assert r.get("read") is None
        assert r.get("cat") is None


class TestSchemaCache:
    def test_schema_cache_refreshes_after_registration(self):
        r = make_registry()
        before = {s["function"]["name"] for s in r.list_schemas()}
        assert "web_search" in before

        class _MockTodoTool(BaseTool):
            name = "todo_read"
            description = "read todos"
            category = ToolCategory.UTIL

            async def execute(self, params, context=None):
                return None

        r.register_instance(_MockTodoTool())
        after = {s["function"]["name"] for s in r.list_schemas()}
        assert "todo_read" in after

    def test_schema_cache_hits_same_generation(self):
        r = make_registry()
        first = r.list_schemas(mode=None, allowed_tools=["bash", "read"])
        second = r.list_schemas(mode=None, allowed_tools=["bash", "read"])
        assert first == second

    def test_allowed_tools_filters(self):
        r = make_registry()
        schemas = r.list_schemas(allowed_tools=["bash"])
        names = [s["function"]["name"] for s in schemas]
        assert names == ["bash"]

    def test_generation_bump_clears_cache(self):
        r = make_registry()
        r.list_schemas()
        assert len(r._schemas_cache) >= 1

        class _MockTodoTool(BaseTool):
            name = "todo_read"
            description = "read todos"
            category = ToolCategory.UTIL

            async def execute(self, params, context=None):
                return None

        r.register_instance(_MockTodoTool())
        assert r._schemas_cache == {}, "generation bump 后缓存未清空"
