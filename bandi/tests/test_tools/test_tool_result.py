from bandi.tools.base import ToolResult


class TestToolResult:
    def test_ok_default(self):
        r = ToolResult.ok("done")
        assert r.is_ok
        assert r.output == "done"
        assert r.error is None

    def test_fail(self):
        r = ToolResult.fail("something went wrong")
        assert not r.is_ok
        assert r.error == "something went wrong"

    def test_ok_with_metadata(self):
        r = ToolResult.ok("output", {"key": "value"})
        assert r.metadata == {"key": "value"}

    def test_default_is_ok(self):
        r = ToolResult()
        assert r.is_ok
