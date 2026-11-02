from __future__ import annotations

from pathlib import Path

import pytest

from bandi.tools.apply_patch import ApplyPatchTool, _apply_hunk, _parse_patch

_WORKSPACE_ROOT = Path.home() / ".bandi" / "workspace"


@pytest.fixture(autouse=True)
def _sandboxed_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tests.test_tools.test_apply_patch as ap_mod

    monkeypatch.setattr(ap_mod, "_WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))


class TestParsePatch:
    def test_parse_single_file(self) -> None:
        patch = """--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 a
-b
+c
 d"""
        files = _parse_patch(patch)
        assert len(files) == 1
        assert files[0][0] == "foo.py"

    def test_parse_multiple_files(self) -> None:
        patch = """--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-x
+y
--- a/b.py
+++ b/b.py
@@ -1 +1 @@
-old
+new"""
        files = _parse_patch(patch)
        assert len(files) == 2
        assert files[0][0] == "a.py"
        assert files[1][0] == "b.py"

    def test_parse_invalid_no_header(self) -> None:
        files = _parse_patch("some random text")
        assert files == []


class TestApplyHunk:
    def test_replace_line(self) -> None:
        text = "keep\nold\nkeep\n"
        hunk = "@@ -1,3 +1,3 @@\n keep\n-old\n+new\n keep\n"
        result, ok = _apply_hunk(text, hunk)
        assert ok
        assert result == "keep\nnew\nkeep\n"

    def test_add_line(self) -> None:
        text = "a\nb\n"
        hunk = "@@ -1,2 +1,3 @@\n a\n b\n+new\n"
        result, ok = _apply_hunk(text, hunk)
        assert ok
        assert result == "a\nb\nnew\n"

    def test_remove_line(self) -> None:
        text = "a\ndel\nb\n"
        hunk = "@@ -1,3 +1,2 @@\n a\n-del\n b\n"
        result, ok = _apply_hunk(text, hunk)
        assert ok
        assert result == "a\nb\n"

    def test_no_match_returns_false(self) -> None:
        text = "x\ny\nz\n"
        hunk = "@@ -1,3 +1,3 @@\n a\n-b\n c\n"
        result, ok = _apply_hunk(text, hunk)
        assert not ok
        assert result == text

    def test_empty_hunk(self) -> None:
        text = "a\n"
        hunk = "@@ -1,1 +1,1 @@\n"
        _result, ok = _apply_hunk(text, hunk)
        assert not ok


class TestApplyHunks:
    def test_multiple_hunks_comma_headers(self) -> None:
        from bandi.tools.apply_patch import _apply_hunks

        text = "a\nb\nc\nd\n"
        patch = "@@ -1,2 +1,2 @@\n a\n-b\n+b1\n@@ -3,2 +3,2 @@\n c\n-d\n+d1\n"
        result, total, failed = _apply_hunks(text, patch)
        assert total == 2
        assert failed == 0
        assert result == "a\nb1\nc\nd1\n"

    def test_multiple_hunks_no_comma_header(self) -> None:
        """回归：无逗号 hunk 头（@@ -1 +1 @@）在多 hunk 补丁中必须被正确切分。"""
        from bandi.tools.apply_patch import _apply_hunks

        text = "a\nb\nc\n"
        patch = "@@ -1,3 +1,3 @@\n a\n-b\n+b1\n c\n@@ -2 +2 @@\n-b1\n+b2\n"
        result, total, failed = _apply_hunks(text, patch)
        assert total == 2
        assert failed == 0
        assert result == "a\nb2\nc\n"


class TestApplyPatchTool:
    @pytest.mark.asyncio
    async def test_apply_success(self) -> None:
        target = _WORKSPACE_ROOT / "_test_patch_target.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("a\nb\nc\n")
        tool = ApplyPatchTool()
        result = await tool.execute(
            {
                "patch": f"--- a/{target.name}\n+++ b/{target.name}\n@@ -1,3 +1,3 @@\n a\n-b\n+c\n c\n",
            }
        )
        assert "applied" in result
        assert target.read_text() == "a\nc\nc\n"

    @pytest.mark.asyncio
    async def test_missing_file(self) -> None:
        from bandi.tools.errors import NonRetryableToolError

        tool = ApplyPatchTool()
        with pytest.raises(NonRetryableToolError) as exc_info:
            await tool.execute(
                {
                    "patch": "--- a/nonexistent.txt\n+++ b/nonexistent.txt\n@@ -1 +1 @@\n-x\n+y\n",
                }
            )
        assert "not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_invalid_patch(self) -> None:
        from bandi.tools.errors import NonRetryableToolError

        tool = ApplyPatchTool()
        with pytest.raises(NonRetryableToolError) as exc_info:
            await tool.execute({"patch": "not a patch"})
        assert "no file headers" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_sensitive_file_skipped(self) -> None:
        """apply_patch 不得改写敏感路径（.env 等）——3.6 起
        0 hunk 应用按确定性失败抛出。"""
        from bandi.tools.errors import NonRetryableToolError

        env = _WORKSPACE_ROOT / ".env"
        env.write_text("API_KEY=secret")
        tool = ApplyPatchTool()
        with pytest.raises(NonRetryableToolError) as exc_info:
            await tool.execute(
                {
                    "patch": "--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-API_KEY=secret\n+API_KEY=hacked\n",
                }
            )
        assert "sensitive" in str(exc_info.value).lower(), f"敏感文件必须跳过: {exc_info.value}"
        assert env.read_text("utf-8") == "API_KEY=secret", "敏感文件不得被改写"
