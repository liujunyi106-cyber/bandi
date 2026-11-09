from bandi.core.message import ToolResult
from bandi.tools.cache import ToolResultCache


class TestToolResultCache:
    def test_set_and_get(self) -> None:
        cache = ToolResultCache()
        key = cache.build_key("test_tool", {"arg1": "val1"})
        result = ToolResult(
            tool_call_id="call_1",
            tool_name="test_tool",
            success=True,
            output="result_data",
        )
        cache.set(key, result, ttl=30.0)
        cached = cache.get(key)
        assert cached is not None
        assert cached.output == "result_data"
        assert cached.tool_name == "test_tool"

    def test_cache_miss(self) -> None:
        cache = ToolResultCache()
        key = cache.build_key("unknown_tool", {})
        cached = cache.get(key)
        assert cached is None

    def test_ttl_expiration(self) -> None:
        cache = ToolResultCache()
        key = cache.build_key("ttl_tool", {})
        result = ToolResult(tool_call_id="call_1", tool_name="ttl_tool", success=True, output="x")
        cache.set(key, result, ttl=-1.0)
        cached = cache.get(key)
        assert cached is None

    def test_lru_eviction(self) -> None:
        cache = ToolResultCache(maxsize=2)
        keys = []
        for i in range(3):
            k = cache.build_key(f"tool_{i}", {"i": i})
            keys.append(k)
            cache.set(
                k,
                ToolResult(
                    tool_call_id=f"call_{i}",
                    tool_name=f"tool_{i}",
                    success=True,
                    output=str(i),
                ),
                ttl=60.0,
            )
        assert cache.get(keys[0]) is None
        assert cache.get(keys[1]) is not None
        assert cache.get(keys[2]) is not None

    def test_build_key_consistency(self) -> None:
        k1 = ToolResultCache.build_key("tool", {"b": 2, "a": 1})
        k2 = ToolResultCache.build_key("tool", {"a": 1, "b": 2})
        assert k1 == k2

    def test_build_key_different_tools(self) -> None:
        k1 = ToolResultCache.build_key("tool_a", {"x": 1})
        k2 = ToolResultCache.build_key("tool_b", {"x": 1})
        assert k1 != k2

    def test_invalidate(self) -> None:
        cache = ToolResultCache()
        key = cache.build_key("my_tool", {"x": 1})
        cache.set(
            key,
            ToolResult(tool_call_id="call_1", tool_name="my_tool", success=True, output="x"),
        )
        assert cache.has(key) is True
        cache.invalidate(key)
        assert cache.has(key) is False

    def test_clear(self) -> None:
        cache = ToolResultCache()
        k1 = cache.build_key("a", {})
        k2 = cache.build_key("b", {})
        cache.set(k1, ToolResult(tool_call_id="c1", tool_name="a", success=True, output="x"))
        cache.set(k2, ToolResult(tool_call_id="c2", tool_name="b", success=True, output="y"))
        cache.clear()
        assert cache.size == 0

    def test_size_with_ttl_check(self) -> None:
        cache = ToolResultCache()
        key = cache.build_key("tool", {})
        cache.set(
            key,
            ToolResult(tool_call_id="c1", tool_name="tool", success=True, output="x"),
            ttl=60.0,
        )
        assert cache.size == 1

    def test_has(self) -> None:
        cache = ToolResultCache()
        key = cache.build_key("tool", {})
        assert cache.has(key) is False
        cache.set(
            key,
            ToolResult(tool_call_id="c1", tool_name="tool", success=True, output="x"),
            ttl=60.0,
        )
        assert cache.has(key) is True

    def test_invalidate_tool_removes_all_entries_for_tool(self) -> None:
        cache = ToolResultCache()
        k1 = cache.build_key("read", {"path": "/a"})
        k2 = cache.build_key("read", {"path": "/b"})
        k3 = cache.build_key("write", {"path": "/c"})
        for k, name in ((k1, "read"), (k2, "read"), (k3, "write")):
            cache.set(
                k,
                ToolResult(tool_call_id="c1", tool_name=name, success=True, output="x"),
                ttl=60.0,
            )
        cache.invalidate_tool("read")
        assert cache.has(k1) is False
        assert cache.has(k2) is False
        assert cache.has(k3) is True
