# 功能：标记 + 配置族测试 — MarkdownProcessor/HTMLProcessor/ConfigFile/ArchiveManager

from __future__ import annotations

import zipfile

import pytest

from bandi.tools.doc.archive_manager import ArchiveManagerTool
from bandi.tools.doc.config_file_tool import ConfigFileTool
from bandi.tools.doc.html_processor import HTMLProcessorTool
from bandi.tools.doc.markdown_processor import MarkdownProcessorTool


@pytest.fixture
def tool(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    return tmp_path


class TestMarkdownProcessor:
    @pytest.mark.asyncio
    async def test_read_parses_structure(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.md"
        f.write_text(
            "# Title\n\n## Section A\n\n[link](https://example.com)\n\n```python\nprint(1)\n```\n\n---\nauthor: Bob\n---\n\n正文",
            encoding="utf-8",
        )
        result = await MarkdownProcessorTool().execute({"operation": "read", "file_path": str(f)})
        assert result["success"] is True
        assert result["headings"][0]["text"] == "Title"
        assert result["headings"][1]["level"] == 2
        assert result["links"][0]["url"] == "https://example.com"
        assert result["code_blocks"][0]["language"] == "python"
        assert result["word_count"] >= 1

    @pytest.mark.asyncio
    async def test_create_with_sections_and_metadata(self, tool, tmp_path) -> None:
        f = tmp_path / "out.md"
        result = await MarkdownProcessorTool().execute(
            {
                "operation": "create",
                "file_path": str(f),
                "title": "Plan",
                "metadata": {"author": "Alice", "tags": ["a", "b"]},
                "sections": [{"heading": "Goal", "level": 2, "content": "Do it"}],
            }
        )
        assert result["success"] is True
        text = f.read_text(encoding="utf-8")
        assert "# Plan" in text
        assert "author: Alice" in text
        assert "## Goal" in text
        assert result["has_frontmatter"] is True

    @pytest.mark.asyncio
    async def test_append_and_prepend_separators(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.md"
        f.write_text("## Existing\n", encoding="utf-8")
        tool_ = MarkdownProcessorTool()
        await tool_.execute({"operation": "append", "file_path": str(f), "content": "appended"})
        await tool_.execute({"operation": "prepend", "file_path": str(f), "content": "prepended"})
        text = f.read_text(encoding="utf-8")
        assert "appended" in text and "prepended" in text

    @pytest.mark.asyncio
    async def test_insert_replace_delete_section(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.md"
        f.write_text("## A\n\nalpha\n\n## B\n\nbeta\n", encoding="utf-8")
        tool_ = MarkdownProcessorTool()
        r1 = await tool_.execute(
            {
                "operation": "insert_section",
                "file_path": str(f),
                "section_title": "A",
                "content": "inserted",
            }
        )
        assert r1["success"] is True
        assert "inserted" in f.read_text(encoding="utf-8")
        r2 = await tool_.execute(
            {
                "operation": "replace_section",
                "file_path": str(f),
                "section_title": "A",
                "content": "new-alpha",
            }
        )
        assert r2["success"] is True
        text = f.read_text(encoding="utf-8")
        assert "new-alpha" in text and "\nalpha\n" not in text
        r3 = await tool_.execute(
            {"operation": "delete_section", "file_path": str(f), "section_title": "B"}
        )
        assert r3["success"] is True
        assert "## B" not in f.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_extract_and_generate_toc(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.md"
        f.write_text("# Top\n\n## Sub\n\n### Deep\n", encoding="utf-8")
        tool_ = MarkdownProcessorTool()
        r1 = await tool_.execute({"operation": "extract_toc", "file_path": str(f)})
        assert r1["success"] is True
        assert r1["toc"][0]["text"] == "Top"
        assert r1["toc"][0]["children"][0]["text"] == "Sub"
        r2 = await tool_.execute({"operation": "generate_toc", "file_path": str(f), "max_depth": 2})
        assert r2["success"] is True
        assert "- [Sub](#sub)" in r2["toc_markdown"]
        assert "### Deep" not in r2["toc_markdown"]

    @pytest.mark.asyncio
    async def test_extract_code_blocks_with_language_filter(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.md"
        f.write_text("```python\nx = 1\n```\n\n```bash\nls\n```\n", encoding="utf-8")
        r = await MarkdownProcessorTool().execute(
            {"operation": "extract_code_blocks", "file_path": str(f), "language": "python"}
        )
        assert r["success"] is True
        assert r["count"] == 1
        assert r["code_blocks"][0]["code"] == "x = 1"

    @pytest.mark.asyncio
    async def test_build_table_and_list(self, tool, tmp_path) -> None:
        tool_ = MarkdownProcessorTool()
        r1 = await tool_.execute(
            {
                "operation": "build_table",
                "headers": ["a", "b"],
                "rows": [["1", "2"]],
                "align": "right",
            }
        )
        assert r1["success"] is True
        assert "| a" in r1["table_markdown"]
        r2 = await tool_.execute(
            {
                "operation": "build_list",
                "items": [{"text": "x", "children": ["x1"]}, "y"],
                "ordered": True,
            }
        )
        assert r2["success"] is True
        assert "1. x" in r2["list_markdown"]
        assert "2. y" in r2["list_markdown"]

    @pytest.mark.asyncio
    async def test_merge_format_convert(self, tool, tmp_path) -> None:
        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        out = tmp_path / "merged.md"
        a.write_text("# A\n", encoding="utf-8")
        b.write_text("# B\n", encoding="utf-8")
        tool_ = MarkdownProcessorTool()
        r = await tool_.execute(
            {"operation": "merge", "file_path": str(out), "merge_files": [str(a), str(b)]}
        )
        assert r["success"] is True
        assert "---" in out.read_text(encoding="utf-8")
        r2 = await tool_.execute(
            {"operation": "convert", "file_path": str(a), "output_format": "html"}
        )
        assert r2["success"] is True
        assert "<h1>A</h1>" in r2["content"]
        r3 = await tool_.execute({"operation": "format", "file_path": str(b)})
        assert r3["success"] is True

    @pytest.mark.asyncio
    async def test_template_meeting_and_changelog(self, tool, tmp_path) -> None:
        tool_ = MarkdownProcessorTool()
        r1 = await tool_.execute(
            {
                "operation": "template",
                "template_name": "meeting",
                "template_data": {"attendees": ["A"], "action_items": ["ship it"]},
            }
        )
        assert r1["success"] is True
        assert "- [ ] ship it" in r1["content"]
        assert "## Attendees" in r1["content"]
        r2 = await tool_.execute({"operation": "template", "template_name": "unknown"})
        assert r2["success"] is False

    @pytest.mark.asyncio
    async def test_errors(self, tool, tmp_path) -> None:
        tool_ = MarkdownProcessorTool()
        assert (await tool_.execute({"operation": "nope", "file_path": "x"}))["success"] is False
        assert (await tool_.execute({"operation": "read", "file_path": "missing.md"}))[
            "success"
        ] is False
        assert (await tool_.execute({"operation": "write", "file_path": "x"}))["success"] is False


class TestHTMLProcessor:
    _HTML = """<!DOCTYPE html><html><head><title>Page Title</title>
<meta name="description" content="desc here">
<meta property="og:title" content="OG">
<meta charset="utf-8"></head>
<body><h1>Hello</h1><p>Some <b>bold</b> text.</p>
<a href="https://example.com">ext</a>
<a href="/internal">int</a>
<table><thead><tr><th>name</th><th>age</th></tr></thead>
<tbody><tr><td>Alice</td><td>30</td></tr></tbody></table>
<script>var x = 1;</script></body></html>"""

    @pytest.mark.asyncio
    async def test_read(self, tool, tmp_path) -> None:
        f = tmp_path / "page.html"
        f.write_text(self._HTML, encoding="utf-8")
        r = await HTMLProcessorTool().execute({"operation": "read", "file_path": str(f)})
        assert r["success"] is True
        assert r["title"] == "Page Title"
        assert "Some bold text." in r["text"]
        assert "var x = 1" not in r["text"]
        assert r["word_count"] > 0

    @pytest.mark.asyncio
    async def test_extract_links(self, tool, tmp_path) -> None:
        f = tmp_path / "page.html"
        f.write_text(self._HTML, encoding="utf-8")
        r = await HTMLProcessorTool().execute({"operation": "extract_links", "file_path": str(f)})
        assert r["success"] is True
        assert r["count"] == 2
        ext = next(link for link in r["links"] if link["href"] == "https://example.com")
        assert ext["is_external"] is True
        assert ext["text"] == "ext"

    @pytest.mark.asyncio
    async def test_extract_tables_with_index(self, tool, tmp_path) -> None:
        f = tmp_path / "page.html"
        f.write_text(self._HTML, encoding="utf-8")
        tool_ = HTMLProcessorTool()
        r = await tool_.execute({"operation": "extract_tables", "file_path": str(f)})
        assert r["success"] is True
        assert r["count"] == 1
        assert r["tables"][0]["headers"] == ["name", "age"]
        assert r["tables"][0]["rows"][0]["name"] == "Alice"
        bad = await tool_.execute(
            {"operation": "extract_tables", "file_path": str(f), "table_index": 5}
        )
        assert bad["success"] is False

    @pytest.mark.asyncio
    async def test_extract_metadata(self, tool, tmp_path) -> None:
        f = tmp_path / "page.html"
        f.write_text(self._HTML, encoding="utf-8")
        r = await HTMLProcessorTool().execute(
            {"operation": "extract_metadata", "file_path": str(f)}
        )
        assert r["success"] is True
        assert r["description"] == "desc here"
        assert r["og_tags"]["og:title"] == "OG"
        assert r["charset"] == "utf-8"

    @pytest.mark.asyncio
    async def test_convert_markdown_and_plain(self, tool, tmp_path) -> None:
        f = tmp_path / "page.html"
        f.write_text(self._HTML, encoding="utf-8")
        tool_ = HTMLProcessorTool()
        r1 = await tool_.execute(
            {"operation": "convert", "file_path": str(f), "output_format": "markdown"}
        )
        assert r1["success"] is True
        assert "| name | age |" in r1["content"]
        r2 = await tool_.execute(
            {"operation": "convert", "file_path": str(f), "output_format": "plain_text"}
        )
        assert r2["success"] is True
        assert "|" not in r2["content"]
        out = tmp_path / "conv.md"
        r3 = await tool_.execute(
            {
                "operation": "convert",
                "file_path": str(f),
                "output_format": "markdown",
                "output_path": str(out),
            }
        )
        assert r3["success"] is True and out.exists()

    @pytest.mark.asyncio
    async def test_errors(self, tool, tmp_path) -> None:
        tool_ = HTMLProcessorTool()
        assert (await tool_.execute({"operation": "read", "file_path": "missing.html"}))[
            "success"
        ] is False
        assert (
            await tool_.execute(
                {"operation": "convert", "file_path": "missing.html", "output_format": "markdown"}
            )
        )["success"] is False


class TestConfigFile:
    @pytest.mark.asyncio
    async def test_read_json_yaml_toml(self, tool, tmp_path) -> None:
        tool_ = ConfigFileTool()
        j = tmp_path / "c.json"
        j.write_text('{"db": {"host": "localhost", "ports": [80, 443]}}', encoding="utf-8")
        r = await tool_.execute({"operation": "read", "file_path": str(j)})
        assert r["success"] is True
        assert r["data"]["db"]["host"] == "localhost"
        assert r["key_count"] == 3
        y = tmp_path / "c.yaml"
        y.write_text("db:\n  host: localhost\n", encoding="utf-8")
        assert (await tool_.execute({"operation": "read", "file_path": str(y)}))["format"] == "yaml"
        t = tmp_path / "c.toml"
        t.write_text('db = { host = "localhost" }\n', encoding="utf-8")
        assert (await tool_.execute({"operation": "read", "file_path": str(t)}))["data"]["db"][
            "host"
        ] == "localhost"

    @pytest.mark.asyncio
    async def test_write_and_query(self, tool, tmp_path) -> None:
        tool_ = ConfigFileTool()
        f = tmp_path / "out.json"
        r1 = await tool_.execute(
            {"operation": "write", "file_path": str(f), "data": {"a": {"b": 1}}}
        )
        assert r1["success"] is True and f.exists()
        r2 = await tool_.execute({"operation": "query", "file_path": str(f), "path": "a.b"})
        assert r2["value"] == 1
        r3 = await tool_.execute({"operation": "query", "file_path": str(f), "path": "a.missing"})
        assert r3["value"] is None

    @pytest.mark.asyncio
    async def test_write_requires_format_without_extension(self, tool, tmp_path) -> None:
        f = tmp_path / "conf"
        r = await ConfigFileTool().execute(
            {"operation": "write", "file_path": str(f), "data": {"x": 1}}
        )
        assert r["success"] is False
        r2 = await ConfigFileTool().execute(
            {"operation": "write", "file_path": str(f), "data": {"x": 1}, "format": "json"}
        )
        assert r2["success"] is True

    @pytest.mark.asyncio
    async def test_merge_and_convert(self, tool, tmp_path) -> None:
        tool_ = ConfigFileTool()
        base = tmp_path / "base.json"
        overlay = tmp_path / "overlay.json"
        base.write_text('{"a": 1, "nested": {"x": 1}}', encoding="utf-8")
        overlay.write_text('{"b": 2, "nested": {"y": 2}}', encoding="utf-8")
        r = await tool_.execute(
            {"operation": "merge", "base_path": str(base), "overlay_path": str(overlay)}
        )
        assert r["success"] is True
        assert r["data"] == {"a": 1, "b": 2, "nested": {"x": 1, "y": 2}}
        out = tmp_path / "merged.yaml"
        r2 = await tool_.execute(
            {
                "operation": "merge",
                "base_path": str(base),
                "overlay_path": str(overlay),
                "output_path": str(out),
            }
        )
        assert r2["success"] is True and out.exists()
        r3 = await tool_.execute(
            {"operation": "convert", "file_path": str(base), "output_format": "yaml"}
        )
        assert r3["success"] is True
        assert "a: 1" in r3["content"]

    @pytest.mark.asyncio
    async def test_toml_write_manual_dump(self, tool, tmp_path) -> None:
        f = tmp_path / "out.toml"
        r = await ConfigFileTool().execute(
            {
                "operation": "write",
                "file_path": str(f),
                "data": {"name": "app", "port": 8080, "db": {"host": "h"}},
            }
        )
        assert r["success"] is True
        text = f.read_text(encoding="utf-8")
        assert 'name = "app"' in text
        assert "port = 8080" in text
        assert "[db]" in text
        r2 = await ConfigFileTool().execute({"operation": "read", "file_path": str(f)})
        assert r2["data"]["name"] == "app"

    @pytest.mark.asyncio
    async def test_errors(self, tool, tmp_path) -> None:
        tool_ = ConfigFileTool()
        assert (await tool_.execute({"operation": "read", "file_path": "missing.json"}))[
            "success"
        ] is False
        assert (await tool_.execute({"operation": "query", "file_path": "x"}))["success"] is False
        bad = tmp_path / "bad.json"
        bad.write_text("{invalid", encoding="utf-8")
        assert (await tool_.execute({"operation": "read", "file_path": str(bad)}))[
            "success"
        ] is False


class TestConfigFileTomliW:
    _DATA = {
        "name": "app",
        "port": 8080,
        "enabled": True,
        "db": {"host": "localhost", "ports": [80, 443], "opts": {"timeout": 30}},
    }

    @pytest.mark.asyncio
    async def test_toml_write_tomli_w_roundtrip(self, tool, tmp_path) -> None:
        import tomllib

        f = tmp_path / "out.toml"
        r = await ConfigFileTool().execute(
            {"operation": "write", "file_path": str(f), "data": self._DATA}
        )
        assert r["success"] is True
        parsed = tomllib.loads(f.read_text(encoding="utf-8"))
        assert parsed == self._DATA

    @pytest.mark.asyncio
    async def test_convert_json_to_toml_tomli_w(self, tool, tmp_path) -> None:
        import json
        import tomllib

        j = tmp_path / "c.json"
        j.write_text(json.dumps(self._DATA), encoding="utf-8")
        r = await ConfigFileTool().execute(
            {"operation": "convert", "file_path": str(j), "output_format": "toml"}
        )
        assert r["success"] is True
        assert tomllib.loads(r["content"]) == self._DATA

    @pytest.mark.asyncio
    async def test_manual_dump_fallback_when_tomli_w_missing(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        import bandi.tools.doc.config_file_tool as mod

        real_import = builtins.__import__

        def _block_tomli_w(name, *args, **kwargs):
            if name == "tomli_w":
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _block_tomli_w)
        out = mod._dump_toml({"name": "app", "port": 8080, "db": {"host": "h"}})
        assert 'name = "app"' in out
        assert "[db]" in out
        assert "port = 8080" in out


class TestArchiveManager:
    @pytest.mark.asyncio
    async def test_create_list_info_zip(self, tool, tmp_path) -> None:
        data = tmp_path / "data.txt"
        data.write_text("hello", encoding="utf-8")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "inner.txt").write_text("inner", encoding="utf-8")
        arch = tmp_path / "bundle.zip"
        tool_ = ArchiveManagerTool()
        r = await tool_.execute(
            {"operation": "create", "archive_path": str(arch), "files": [str(data), str(sub)]}
        )
        assert r["success"] is True
        assert r["count"] == 2
        r2 = await tool_.execute({"operation": "list", "archive_path": str(arch)})
        assert r2["success"] is True
        names = {e["name"] for e in r2["entries"]}
        assert "data.txt" in names
        assert any("inner.txt" in n for n in names)
        r3 = await tool_.execute({"operation": "info", "archive_path": str(arch)})
        assert r3["success"] is True
        assert r3["file_count"] >= 2
        assert 0.0 <= r3["compression_ratio"] <= 1.0

    @pytest.mark.asyncio
    async def test_extract_zip_selected_and_all(self, tool, tmp_path) -> None:
        data = tmp_path / "data.txt"
        data.write_text("hello", encoding="utf-8")
        arch = tmp_path / "bundle.zip"
        tool_ = ArchiveManagerTool()
        await tool_.execute(
            {"operation": "create", "archive_path": str(arch), "files": [str(data)]}
        )
        out_dir = tmp_path / "out"
        r1 = await tool_.execute(
            {
                "operation": "extract",
                "archive_path": str(arch),
                "output_dir": str(out_dir),
                "files": ["data.txt"],
            }
        )
        assert r1["success"] is True
        assert (out_dir / "data.txt").exists()
        r2 = await tool_.execute(
            {"operation": "extract", "archive_path": str(arch), "output_dir": str(out_dir)}
        )
        assert r2["success"] is True and r2["count"] == 1

    @pytest.mark.asyncio
    async def test_tar_gz_roundtrip(self, tool, tmp_path) -> None:
        data = tmp_path / "a.txt"
        data.write_text("alpha", encoding="utf-8")
        arch = tmp_path / "bundle.tar.gz"
        tool_ = ArchiveManagerTool()
        r = await tool_.execute(
            {"operation": "create", "archive_path": str(arch), "files": [str(data)]}
        )
        assert r["success"] is True and r["format"] == "tar.gz"
        out_dir = tmp_path / "out"
        r2 = await tool_.execute(
            {"operation": "extract", "archive_path": str(arch), "output_dir": str(out_dir)}
        )
        assert r2["success"] is True
        assert (out_dir / "a.txt").read_text(encoding="utf-8") == "alpha"

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, tool, tmp_path) -> None:
        evil = tmp_path / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../../escape.txt", "pwned")
        r = await ArchiveManagerTool().execute(
            {"operation": "extract", "archive_path": str(evil), "output_dir": str(tmp_path / "out")}
        )
        assert r["success"] is False
        assert not (tmp_path.parent / "escape.txt").exists()

    @pytest.mark.asyncio
    async def test_errors(self, tool, tmp_path) -> None:
        tool_ = ArchiveManagerTool()
        assert (await tool_.execute({"operation": "list", "archive_path": "missing.zip"}))[
            "success"
        ] is False
        assert (await tool_.execute({"operation": "create", "archive_path": "x.zip"}))[
            "success"
        ] is False
        bad = tmp_path / "bad.zip"
        bad.write_text("not a zip", encoding="utf-8")
        assert (await tool_.execute({"operation": "list", "archive_path": str(bad)}))[
            "success"
        ] is False
