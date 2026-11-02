# 功能：Office + 文本工具测试 — WordReader/ExcelReader/CSVProcessor/TextFileProcessor

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bandi.tools.doc.csv_processor import CSVProcessorTool
from bandi.tools.doc.excel_reader import ExcelReaderTool
from bandi.tools.doc.text_processor import TextFileProcessorTool
from bandi.tools.doc.word_reader import WordReaderTool


@pytest.fixture
def tool(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    return tmp_path


def _find_soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    win_default = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")
    return str(win_default) if win_default.is_file() else None


def _make_docx(path) -> None:
    from docx import Document

    doc = Document()
    doc.add_heading("Test Heading", level=1)
    doc.add_paragraph("Hello paragraph one.")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "A1"
    table.cell(0, 1).text = "B1"
    table.cell(1, 0).text = "A2"
    table.cell(1, 1).text = "B2"
    doc.save(str(path))


def _make_xlsx(path, sheet_name: str = "Data") -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(["name", "age", "city"])
    ws.append(["Alice", 30, "Beijing"])
    ws.append(["Bob", 25, "Shanghai"])
    ws2 = wb.create_sheet("Second")
    ws2.append(["x", "y"])
    ws2.append([1, 2])
    wb.save(str(path))


class TestWordReader:
    @pytest.mark.asyncio
    async def test_read_docx(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.docx"
        _make_docx(f)
        reader = WordReaderTool()
        result = await reader.execute({"file_path": str(f)})
        assert result["success"] is True
        assert result["heading_count"] == 1
        assert "Hello paragraph one." in result["text"]
        assert result["table_count"] == 1
        assert result["content"][0]["type"] == "heading"

    @pytest.mark.asyncio
    async def test_read_docx_no_tables(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.docx"
        _make_docx(f)
        reader = WordReaderTool()
        result = await reader.execute({"file_path": str(f), "extract_tables": False})
        assert result["success"] is True
        assert "tables" not in result

    @pytest.mark.asyncio
    async def test_missing_file(self, tool) -> None:
        reader = WordReaderTool()
        result = await reader.execute({"file_path": "nope.docx"})
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_unsupported_suffix(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.rtf"
        f.write_text("x", encoding="utf-8")
        reader = WordReaderTool()
        result = await reader.execute({"file_path": str(f)})
        assert result["success"] is False
        assert "unsupported" in result["error"]

    @pytest.mark.asyncio
    async def test_sensitive_path(self, tool) -> None:
        reader = WordReaderTool()
        result = await reader.execute({"file_path": ".ssh/id.docx"})
        assert result["success"] is False
        assert "sensitive" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.heavy
    async def test_legacy_doc_missing_dependency(self, tool, tmp_path) -> None:
        f = tmp_path / "legacy.doc"
        f.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 8)
        reader = WordReaderTool()
        result = await reader.execute({"file_path": str(f)})
        assert result["success"] is False
        assert "pyantiword" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.heavy
    @pytest.mark.external_tool
    async def test_legacy_doc_libreoffice_real(self, tool, tmp_path) -> None:
        soffice = _find_soffice()
        if not soffice:
            pytest.skip("LibreOffice not installed")
        docx = tmp_path / "legacy_src.docx"
        _make_docx(docx)
        outdir = tmp_path / "lo_out"
        outdir.mkdir()
        subprocess.run(
            [
                soffice,
                "--headless",
                f"-env:UserInstallation=file:///{(tmp_path / 'lo_profile').as_posix()}",
                "--convert-to",
                "doc",
                "--outdir",
                str(outdir),
                str(docx),
            ],
            capture_output=True,
            timeout=180,
            check=True,
        )
        doc_file = outdir / "legacy_src.doc"
        assert doc_file.exists()
        reader = WordReaderTool()
        result = await reader.execute({"file_path": str(doc_file)})
        assert result["success"] is True
        assert result["metadata"]["extraction_source"] == "libreoffice"
        assert "Hello paragraph one." in result["text"]


class TestExcelReader:
    @pytest.mark.asyncio
    async def test_read_records(self, tool, tmp_path) -> None:
        f = tmp_path / "data.xlsx"
        _make_xlsx(f)
        reader = ExcelReaderTool()
        result = await reader.execute({"file_path": str(f)})
        assert result["success"] is True
        assert result["headers"] == ["name", "age", "city"]
        assert result["row_count"] == 2
        assert result["data"][0]["name"] == "Alice"
        assert result["sheet_names"] == ["Data", "Second"]

    @pytest.mark.asyncio
    async def test_sheet_by_name(self, tool, tmp_path) -> None:
        f = tmp_path / "data.xlsx"
        _make_xlsx(f)
        reader = ExcelReaderTool()
        result = await reader.execute({"file_path": str(f), "sheet_name": "Second"})
        assert result["success"] is True
        assert result["headers"] == ["x", "y"]
        assert result["row_count"] == 1

    @pytest.mark.asyncio
    async def test_sheet_by_index(self, tool, tmp_path) -> None:
        f = tmp_path / "data.xlsx"
        _make_xlsx(f)
        reader = ExcelReaderTool()
        result = await reader.execute({"file_path": str(f), "sheet_index": 1})
        assert result["success"] is True
        assert result["sheet_name"] == "Second"

    @pytest.mark.asyncio
    async def test_bad_sheet_name(self, tool, tmp_path) -> None:
        f = tmp_path / "data.xlsx"
        _make_xlsx(f)
        reader = ExcelReaderTool()
        result = await reader.execute({"file_path": str(f), "sheet_name": "Nope"})
        assert result["success"] is False
        assert "sheet not found" in result["error"]

    @pytest.mark.asyncio
    async def test_output_dict(self, tool, tmp_path) -> None:
        f = tmp_path / "data.xlsx"
        _make_xlsx(f)
        reader = ExcelReaderTool()
        result = await reader.execute({"file_path": str(f), "output_format": "dict"})
        assert result["success"] is True
        assert result["data"]["name"] == ["Alice", "Bob"]

    @pytest.mark.asyncio
    async def test_unsupported_format(self, tool, tmp_path) -> None:
        f = tmp_path / "old.xls"
        f.write_bytes(b"fake")
        reader = ExcelReaderTool()
        result = await reader.execute({"file_path": str(f)})
        assert result["success"] is False
        assert "unsupported" in result["error"]


class TestCSVProcessor:
    @pytest.mark.asyncio
    async def test_read(self, tool, tmp_path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("name,age\nAlice,30\nBob,25", encoding="utf-8")
        processor = CSVProcessorTool()
        result = await processor.execute({"operation": "read", "file_path": str(f)})
        assert result["success"] is True
        assert result["headers"] == ["name", "age"]
        assert result["row_count"] == 2

    @pytest.mark.asyncio
    async def test_read_tab_delimiter(self, tool, tmp_path) -> None:
        f = tmp_path / "data.tsv"
        f.write_text("name\tage\nAlice\t30", encoding="utf-8")
        processor = CSVProcessorTool()
        result = await processor.execute({"operation": "read", "file_path": str(f)})
        assert result["success"] is True
        assert result["headers"] == ["name", "age"]

    @pytest.mark.asyncio
    async def test_query_filter(self, tool, tmp_path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("name,age\nAlice,30\nBob,25", encoding="utf-8")
        processor = CSVProcessorTool()
        result = await processor.execute(
            {
                "operation": "query",
                "file_path": str(f),
                "filter_column": "name",
                "filter_value": "Alice",
            }
        )
        assert result["success"] is True
        assert result["row_count"] == 1
        assert result["rows"][0]["age"] == "30"

    @pytest.mark.asyncio
    async def test_query_sort(self, tool, tmp_path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("name,age,group\nAlice,30,x\nBob,25,x\nCarol,35,y", encoding="utf-8")
        processor = CSVProcessorTool()
        result = await processor.execute(
            {
                "operation": "query",
                "file_path": str(f),
                "filter_column": "group",
                "filter_value": "x",
                "sort_column": "age",
                "sort_order": "desc",
            }
        )
        assert result["success"] is True
        ages = [r["age"] for r in result["rows"]]
        assert ages == ["30", "25"]

    @pytest.mark.asyncio
    async def test_query_unknown_column(self, tool, tmp_path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("name,age\nAlice,30", encoding="utf-8")
        processor = CSVProcessorTool()
        result = await processor.execute(
            {
                "operation": "query",
                "file_path": str(f),
                "filter_column": "nope",
                "filter_value": "x",
            }
        )
        assert result["success"] is False
        assert "unknown column" in result["error"]

    @pytest.mark.asyncio
    async def test_write(self, tool, tmp_path) -> None:
        out = tmp_path / "out.csv"
        processor = CSVProcessorTool()
        result = await processor.execute(
            {
                "operation": "write",
                "file_path": str(out),
                "data": [{"a": 1, "b": 2}, {"a": 3, "b": 4}],
            }
        )
        assert result["success"] is True
        assert result["row_count"] == 2
        assert out.read_text(encoding="utf-8").startswith("a,b")

    @pytest.mark.asyncio
    async def test_stats(self, tool, tmp_path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("name,age\nAlice,30\nBob,25", encoding="utf-8")
        processor = CSVProcessorTool()
        result = await processor.execute({"operation": "stats", "file_path": str(f)})
        assert result["success"] is True
        assert result["columns"]["age"]["inferred_type"] == "numeric"
        assert result["columns"]["age"]["min"] == 25.0
        assert result["columns"]["name"]["inferred_type"] == "string"

    @pytest.mark.asyncio
    async def test_convert_json(self, tool, tmp_path) -> None:
        f = tmp_path / "data.csv"
        out = tmp_path / "data.json"
        f.write_text("name,age\nAlice,30", encoding="utf-8")
        processor = CSVProcessorTool()
        result = await processor.execute(
            {"operation": "convert", "file_path": str(f), "output_path": str(out)}
        )
        assert result["success"] is True
        assert result["row_count"] == 1
        assert '"name"' in out.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_unknown_operation(self, tool) -> None:
        processor = CSVProcessorTool()
        result = await processor.execute({"operation": "bogus"})
        assert result["success"] is False
        assert "unknown operation" in result["error"]


class TestTextProcessor:
    @pytest.mark.asyncio
    async def test_read(self, tool, tmp_path) -> None:
        f = tmp_path / "note.txt"
        f.write_text("line one\nline two", encoding="utf-8")
        processor = TextFileProcessorTool()
        result = await processor.execute({"operation": "read", "file_path": str(f)})
        assert result["success"] is True
        assert "line one" in result["text"]
        assert result["encoding"] == "utf-8"

    @pytest.mark.asyncio
    async def test_write(self, tool, tmp_path) -> None:
        out = tmp_path / "new.txt"
        processor = TextFileProcessorTool()
        result = await processor.execute(
            {"operation": "write", "file_path": str(out), "data": "hello"}
        )
        assert result["success"] is True
        assert out.read_text(encoding="utf-8") == "hello"

    @pytest.mark.asyncio
    async def test_search_with_context(self, tool, tmp_path) -> None:
        f = tmp_path / "log.txt"
        f.write_text("start\nneedle here\nend", encoding="utf-8")
        processor = TextFileProcessorTool()
        result = await processor.execute(
            {"operation": "search", "file_path": str(f), "pattern": "needle"}
        )
        assert result["success"] is True
        assert result["match_count"] == 1
        assert len(result["matches"][0]["context"]) == 3

    @pytest.mark.asyncio
    async def test_replace(self, tool, tmp_path) -> None:
        f = tmp_path / "note.txt"
        f.write_text("foo foo bar", encoding="utf-8")
        processor = TextFileProcessorTool()
        result = await processor.execute(
            {"operation": "replace", "file_path": str(f), "pattern": "foo", "replacement": "baz"}
        )
        assert result["success"] is True
        assert result["replacements_made"] == 2
        assert f.read_text(encoding="utf-8") == "baz baz bar"

    @pytest.mark.asyncio
    async def test_transform_uppercase(self, tool, tmp_path) -> None:
        f = tmp_path / "note.txt"
        f.write_text("hello", encoding="utf-8")
        processor = TextFileProcessorTool()
        result = await processor.execute(
            {"operation": "transform", "file_path": str(f), "transform_type": "uppercase"}
        )
        assert result["success"] is True
        assert f.read_text(encoding="utf-8") == "HELLO"

    @pytest.mark.asyncio
    async def test_transform_sort_numeric(self, tool, tmp_path) -> None:
        f = tmp_path / "nums.txt"
        f.write_text("10\n2\n1", encoding="utf-8")
        processor = TextFileProcessorTool()
        await processor.execute(
            {
                "operation": "transform",
                "file_path": str(f),
                "transform_type": "sort_lines",
                "sort_key": "numeric",
            }
        )
        assert f.read_text(encoding="utf-8") == "1\n2\n10"

    @pytest.mark.asyncio
    async def test_extract_line_range(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.txt"
        f.write_text("a\nb\nc\nd", encoding="utf-8")
        processor = TextFileProcessorTool()
        result = await processor.execute(
            {"operation": "extract", "file_path": str(f), "start_line": 2, "end_line": 3}
        )
        assert result["success"] is True
        assert result["extracted"] == "b\nc"

    @pytest.mark.asyncio
    async def test_extract_markers(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.txt"
        f.write_text("pre\nSTART\ninside\nEND\npost", encoding="utf-8")
        processor = TextFileProcessorTool()
        result = await processor.execute(
            {
                "operation": "extract",
                "file_path": str(f),
                "start_marker": "START",
                "end_marker": "END",
            }
        )
        assert result["success"] is True
        assert "inside" in result["extracted"]
        assert "pre" not in result["extracted"]

    @pytest.mark.asyncio
    async def test_split_chunk_lines(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.txt"
        f.write_text("1\n2\n3\n4", encoding="utf-8")
        processor = TextFileProcessorTool()
        result = await processor.execute(
            {"operation": "split", "file_path": str(f), "chunk_lines": 2}
        )
        assert result["success"] is True
        assert result["parts_count"] == 2

    @pytest.mark.asyncio
    async def test_join(self, tool, tmp_path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        out = tmp_path / "merged.txt"
        a.write_text("A", encoding="utf-8")
        b.write_text("B", encoding="utf-8")
        processor = TextFileProcessorTool()
        result = await processor.execute(
            {
                "operation": "join",
                "file_path": str(out),
                "source_files": [str(a), str(b)],
                "separator": "\n",
            }
        )
        assert result["success"] is True
        assert out.read_text(encoding="utf-8") == "A\nB\n"

    @pytest.mark.asyncio
    async def test_stats(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.txt"
        f.write_text("one two\nthree", encoding="utf-8")
        processor = TextFileProcessorTool()
        result = await processor.execute({"operation": "stats", "file_path": str(f)})
        assert result["success"] is True
        assert result["line_count"] == 2
        assert result["word_count"] == 3

    @pytest.mark.asyncio
    async def test_head_tail(self, tool, tmp_path) -> None:
        f = tmp_path / "doc.txt"
        f.write_text("1\n2\n3\n4\n5", encoding="utf-8")
        processor = TextFileProcessorTool()
        head = await processor.execute({"operation": "head", "file_path": str(f), "lines": 2})
        tail = await processor.execute({"operation": "tail", "file_path": str(f), "lines": 2})
        assert head["lines"] == ["1", "2"]
        assert tail["lines"] == ["4", "5"]

    @pytest.mark.asyncio
    async def test_diff(self, tool, tmp_path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("same\ndiffers", encoding="utf-8")
        b.write_text("same\nchanged", encoding="utf-8")
        processor = TextFileProcessorTool()
        result = await processor.execute(
            {"operation": "diff", "file_path": str(a), "file_path_2": str(b)}
        )
        assert result["success"] is True
        assert result["additions"] == 1
        assert result["deletions"] == 1

    @pytest.mark.asyncio
    async def test_detect_encoding(self, tool, tmp_path) -> None:
        f = tmp_path / "cn.txt"
        f.write_bytes("你好世界".encode("gbk"))
        processor = TextFileProcessorTool()
        result = await processor.execute({"operation": "detect_encoding", "file_path": str(f)})
        assert result["success"] is True
        assert (
            result["encoding"] == "utf-8"
            or result["encoding"] in result["strict_decode_candidates"]
        )

    @pytest.mark.asyncio
    async def test_try_decodings(self, tool, tmp_path) -> None:
        f = tmp_path / "enc.txt"
        f.write_bytes(b"abc")
        processor = TextFileProcessorTool()
        result = await processor.execute(
            {"operation": "try_decodings", "file_path": str(f), "encodings": ["utf-8", "ascii"]}
        )
        assert result["success"] is True
        assert result["results"][0]["strict_ok"] is True

    @pytest.mark.asyncio
    async def test_binary_rejected(self, tool, tmp_path) -> None:
        f = tmp_path / "bin.dat"
        f.write_bytes(b"\x00\x01\x02")
        processor = TextFileProcessorTool()
        result = await processor.execute({"operation": "read", "file_path": str(f)})
        assert result["success"] is False
        assert "binary" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_operation(self, tool) -> None:
        processor = TextFileProcessorTool()
        result = await processor.execute({})
        assert result["success"] is False
