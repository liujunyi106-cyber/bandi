# 功能：gen 工具族契约族——渲染行为/全格式产物可读回/模板与主题生命周期/feature gate/装配
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from bandi.tools.gen.checklist_tool import ChecklistGenerateTool
from bandi.tools.gen.document_tool import DocumentGenerateTool
from bandi.tools.gen.excel_generator import ExcelGenerateTool
from bandi.tools.gen.render import parse_md_blocks, render_document, render_slides
from bandi.tools.gen.slides_tool import SlidesGenerateTool
from bandi.tools.gen.store import (
    JsonStore,
    contrast_ratio,
    derive_theme_payload,
    hex_to_rgb,
    lint_theme,
    rgb_to_hex,
    validate_store_name,
)
from bandi.tools.gen.template_filler import TemplateFillerTool
from bandi.tools.gen.template_tool import DocumentTemplateTool
from bandi.tools.gen.theme_tool import ThemeDesignerTool


def _docx_text(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs)


def _pdf_text(data: bytes) -> list[str]:
    import pymupdf as fitz

    with fitz.open(stream=data, filetype="pdf") as doc:
        return [doc.load_page(i).get_text() for i in range(doc.page_count)]


def _pptx_slides(data: bytes) -> list[str]:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    titles: list[str] = []
    for slide in prs.slides:
        title = slide.shapes.title.text if slide.shapes.title is not None else ""
        titles.append(title)
    return titles


# ---------- store / 颜色纯函数（行为族：推导确定性与对比度语义） ----------


def test_hex_rgb_roundtrip() -> None:
    assert rgb_to_hex(hex_to_rgb("#3366CC")) == "#3366CC"
    with pytest.raises(ValueError):
        hex_to_rgb("xyz")


def test_contrast_ratio_black_on_white_is_21() -> None:
    assert abs(contrast_ratio("#000000", "#FFFFFF") - 21.0) < 0.02


def test_derive_theme_light_is_deterministic_and_contrast_safe() -> None:
    t = derive_theme_payload(kind="document", primary="#3366CC", mode="light")
    again = derive_theme_payload(kind="document", primary="#3366CC", mode="light")
    assert t == again
    assert t["kind"] == "document"
    assert t["colors"]["background"] == "#FFFFFF"
    assert lint_theme(t) == []


def test_derive_theme_dark_accent_derived() -> None:
    t = derive_theme_payload(kind="deck", primary="#E63946", mode="dark")
    assert t["mode"] == "dark"
    assert t["colors"]["background"] == "#0F1115"
    assert t["deck"]["dark"] is True


def test_derive_theme_invalid_color_mode_rejected() -> None:
    with pytest.raises(ValueError):
        derive_theme_payload(kind="document", primary="notacolor", mode="light")
    with pytest.raises(ValueError):
        derive_theme_payload(kind="document", primary="#3366CC", mode="neon")


def test_lint_theme_flags_low_contrast() -> None:
    bad = {
        "colors": {
            "text": "#FFFFFF",
            "text_light": "#FFFFFF",
            "background": "#FFFFFF",
            "surface": "#FFFFFF",
        }
    }
    warnings = lint_theme(bad)
    assert len(warnings) >= 2


def test_store_roundtrip_overwrite_guard_and_name_validation(tmp_path: Path) -> None:
    store = JsonStore(base_dir=tmp_path)
    assert store.save("templates", "q3", {"content": "x"}) is True
    assert store.load("templates", "q3") == {"content": "x"}
    assert store.list("templates") == [{"content": "x", "name": "q3"}]
    assert store.delete("templates", "q3") is True
    assert store.delete("templates", "q3") is False
    assert store.load("templates", "q3") is None
    with pytest.raises(ValueError):
        store.save("templates", "Q3!", {"x": 1})
    with pytest.raises(ValueError):
        validate_store_name("../evil")


# ---------- 渲染纯函数（行为族：块解析与各格式产物可读回） ----------


def test_parse_md_blocks_detects_all_block_types() -> None:
    md = (
        "# 标题\n"
        "段落文字 **加粗**\n"
        "- 项一\n- 项二\n"
        "1. 第一\n"
        "```python\nprint(1)\n```\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
        "> 引用\n"
        "---\n"
    )
    blocks = parse_md_blocks(md)
    kinds = [b["type"] for b in blocks]
    assert kinds == [
        "heading",
        "para",
        "bullet",
        "bullet",
        "bullet",
        "code",
        "table",
        "quote",
        "hr",
    ]
    table = blocks[6]
    assert table["headers"] == ["a", "b"]
    assert table["rows"] == [["1", "2"]]
    assert blocks[0]["text"] == "标题"
    assert "加粗" in blocks[1]["text"]


def test_render_document_markdown_passthrough() -> None:
    md = "# 标题\n\n正文\n"
    assert render_document(md, "markdown").decode("utf-8") == md


def test_render_document_html_contains_content() -> None:
    data = render_document("# 周报\n\n完成三项", "html")
    text = data.decode("utf-8")
    assert "<!DOCTYPE html>" in text
    assert "周报" in text and "完成三项" in text


def test_render_document_docx_readable_back() -> None:
    data = render_document("# 标题\n\n第一段\n- 项一\n| a | b |\n|---|---|\n| 1 | 2 |", "docx")
    text = _docx_text(data)
    assert "标题" in text and "第一段" in text and "项一" in text


def test_render_document_pdf_readable_back() -> None:
    data = render_document("# 集成测试\n\n中国内容段落", "pdf")
    pages = _pdf_text(data)
    assert len(pages) >= 1
    assert "集成测试" in pages[0]


def test_render_slides_markdown_pages_separated() -> None:
    data = render_slides(
        [{"title": "第一页", "body": "要点"}, {"title": "第二页", "takeaway": "结论"}],
        "markdown",
    )
    text = data.decode("utf-8")
    assert text.count("---") == 1
    assert "第一页" in text and "结论" in text


def test_render_slides_html_and_pptx_and_pdf_back() -> None:
    slides = [{"title": "页一", "body": "要点A"}, {"title": "页二", "body": "要点B"}]
    html = render_slides(slides, "html").decode("utf-8")
    assert html.count("<section>") == 2 and "页一" in html
    assert _pptx_slides(render_slides(slides, "pptx")) == ["页一", "页二"]
    assert len(_pdf_text(render_slides(slides, "pdf"))) == 2


def test_render_unsupported_formats_rejected() -> None:
    with pytest.raises(ValueError):
        render_document("# x", "exe")
    with pytest.raises(ValueError):
        render_slides([{"title": "x"}], "exe")
    with pytest.raises(ValueError):
        render_slides([], "markdown")


# ---------- document_generate / excel_generate / slides_generate 工具 ----------


@pytest.mark.asyncio
async def test_document_generate_writes_and_reads_back(tmp_path: Path) -> None:
    tool = DocumentGenerateTool()
    out = tmp_path / "report.md"
    result = await tool.execute(
        {"content": "# 中文标题\n\n市场数据分析段落", "output_path": str(out)}, _ctx(tmp_path)
    )
    assert result["success"] is True
    content = out.read_text(encoding="utf-8")
    assert "中文标题" in content and "市场数据分析段落" in content


@pytest.mark.asyncio
async def test_document_generate_html_docx_pdf(tmp_path: Path) -> None:
    tool = DocumentGenerateTool()
    base = "# 报告标题\n\n正文内容"
    for fmt in ("html", "docx", "pdf"):
        ext = {"html": ".html", "docx": ".docx", "pdf": ".pdf"}[fmt]
        out = tmp_path / f"out{ext}"
        result = await tool.execute({"content": base, "output_path": str(out)}, _ctx(tmp_path))
        assert result["success"] is True and result["format"] == fmt
    assert "报告标题" in (tmp_path / "out.html").read_text(encoding="utf-8")
    assert "报告标题" in _docx_text((tmp_path / "out.docx").read_bytes())
    assert "报告标题" in _pdf_text((tmp_path / "out.pdf").read_bytes())[0]


@pytest.mark.asyncio
async def test_document_generate_unknown_format_rejected(tmp_path: Path) -> None:
    tool = DocumentGenerateTool()
    result = await tool.execute(
        {"content": "# x", "output_path": str(tmp_path / "x.exe")}, _ctx(tmp_path)
    )
    assert result["success"] is False


@pytest.mark.asyncio
async def test_excel_generate_multi_sheet_readable(tmp_path: Path) -> None:
    import openpyxl

    tool = ExcelGenerateTool()
    out = tmp_path / "book.xlsx"
    result = await tool.execute(
        {
            "sheets": [
                {
                    "name": "销售",
                    "headers": ["产品", "金额"],
                    "rows": [["笔记本", 1200], ["显示器", 800]],
                },
                {"name": "汇总", "rows": [["合计", 2000]]},
            ],
            "output_path": str(out),
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True and result["sheets"] == 2
    wb = openpyxl.load_workbook(str(out), read_only=True)
    assert wb.sheetnames == ["销售", "汇总"]
    ws = wb["销售"]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0] == ("产品", "金额")
    assert rows[1] == ("笔记本", 1200)


@pytest.mark.asyncio
async def test_slides_generate_pptx_md_readable(tmp_path: Path) -> None:
    tool = SlidesGenerateTool()
    out = tmp_path / "deck.pptx"
    result = await tool.execute(
        {
            "slides": [{"title": "开篇", "body": "背景"}, {"title": "结尾", "takeaway": "行动"}],
            "output_path": str(out),
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True and result["slides"] == 2
    assert _pptx_slides(out.read_bytes()) == ["开篇", "结尾"]
    md = tmp_path / "deck.md"
    await tool.execute(
        {"slides": [{"title": "开篇", "body": "背景"}], "output_path": str(md)}, _ctx(tmp_path)
    )
    assert "开篇" in md.read_text(encoding="utf-8")


# ---------- checklist_generate ----------


@pytest.mark.asyncio
async def test_checklist_generate_status_and_progress(tmp_path: Path) -> None:
    tool = ChecklistGenerateTool()
    out = tmp_path / "todo.md"
    result = await tool.execute(
        {
            "title": "上线清单",
            "items": [
                {"text": "备份", "status": "completed", "priority": "high"},
                {"text": "发布", "status": "pending", "assignee": "张三"},
            ],
            "output_path": str(out),
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True and result["completed"] == 1 and result["total"] == 2
    md = out.read_text(encoding="utf-8")
    assert "- [x]" in md and "- [ ]" in md
    assert "进度：1/2" in md and "50%" in md
    assert "上线清单" in md


@pytest.mark.asyncio
async def test_checklist_generate_group_by_status(tmp_path: Path) -> None:
    tool = ChecklistGenerateTool()
    out = tmp_path / "g.md"
    result = await tool.execute(
        {
            "items": [
                {"text": "a", "status": "completed"},
                {"text": "b", "status": "pending"},
                {"text": "c", "status": "completed"},
            ],
            "group_by": "status",
            "output_path": str(out),
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True
    md = out.read_text(encoding="utf-8")
    assert "### completed" in md and "### pending" in md
    assert "67%" in md


@pytest.mark.asyncio
async def test_checklist_generate_empty_items_rejected(tmp_path: Path) -> None:
    tool = ChecklistGenerateTool()
    result = await tool.execute(
        {"items": [], "output_path": str(tmp_path / "x.md")}, _ctx(tmp_path)
    )
    assert result["success"] is False


@pytest.mark.asyncio
async def test_checklist_generate_docx_pdf(tmp_path: Path) -> None:
    tool = ChecklistGenerateTool()
    for fmt, ext in (("docx", ".docx"), ("pdf", ".pdf")):
        out = tmp_path / f"c{ext}"
        result = await tool.execute(
            {"items": [{"text": "任务", "status": "completed"}], "output_path": str(out)},
            _ctx(tmp_path),
        )
        assert result["success"] is True and result["format"] == fmt
    assert "任务" in _docx_text((tmp_path / "c.docx").read_bytes())
    assert "任务" in _pdf_text((tmp_path / "c.pdf").read_bytes())[0]


# ---------- template_filler 行为族 ----------


@pytest.mark.asyncio
async def test_template_filler_vars_and_loop(tmp_path: Path) -> None:
    tool = TemplateFillerTool()
    out = tmp_path / "invitation.md"
    result = await tool.execute(
        {
            "template_string": "会议：{{ meeting_name }}\n{% for p in participants %}- {{ p }}\n{% endfor %}",
            "variables": {"meeting_name": "产品评审", "participants": ["张三", "李四"]},
            "output_path": str(out),
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True
    md = out.read_text(encoding="utf-8")
    assert "产品评审" in md and "- 张三" in md and "- 李四" in md


@pytest.mark.asyncio
async def test_template_filler_filters_and_strict_undefined(tmp_path: Path) -> None:
    tool = TemplateFillerTool()
    out = tmp_path / "r.txt"
    ok = await tool.execute(
        {
            "template_string": "{{ price | format_currency(symbol='¥') }} {{ name | slugify }}",
            "variables": {"price": 1234.5, "name": "Hello World"},
            "output_path": str(out),
        },
        _ctx(tmp_path),
    )
    assert ok["success"] is True
    assert out.read_text(encoding="utf-8") == "¥1,234.50 hello-world"
    err = await tool.execute(
        {
            "template_string": "{{ missing_var }}",
            "variables": {},
            "strict_mode": True,
            "output_path": str(tmp_path / "e.txt"),
        },
        _ctx(tmp_path),
    )
    assert err["success"] is False


@pytest.mark.asyncio
async def test_template_filler_yaml_data_file(tmp_path: Path) -> None:
    data = tmp_path / "vars.yaml"
    data.write_text("name: 王五\nitems: [1, 2]", encoding="utf-8")
    tool = TemplateFillerTool()
    result = await tool.execute(
        {
            "template_string": "{{ name }}:{% for i in items %}{{ i }}{% endfor %}",
            "data_file": str(data),
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True
    assert result["rendered"] == "王五:12"


@pytest.mark.asyncio
async def test_template_filler_no_output_returns_text() -> None:
    tool = TemplateFillerTool()
    result = await tool.execute({"template_string": "你好 {{ who }}", "variables": {"who": "世界"}})
    assert result["success"] is True and result["rendered"] == "你好 世界"
    assert "output_path" not in result


# ---------- document_template 生命周期（save/list/get/preview/generate） ----------


def _tpl_store(tmp_path: Path) -> JsonStore:
    return JsonStore(base_dir=tmp_path)


def _ctx(tmp_path: Path) -> Any:
    """功能：注入 project_root 的项目上下文——gen 写盘工具的沙箱放行（BUG-J 工程协议）。"""
    from bandi.tools.base import ToolPermissionContext

    return ToolPermissionContext(
        user_id="unit-test",
        role="tester",
        mode="execute",
        extra={"project_root": str(tmp_path)},
    )


@pytest.mark.asyncio
async def test_template_document_lifecycle(tmp_path: Path) -> None:
    tool = DocumentTemplateTool(store=_tpl_store(tmp_path))
    saved = await tool.execute(
        {
            "action": "save",
            "name": "weekly",
            "kind": "document",
            "description": "周报模板",
            "content": "# {{ week }} 周报\n\n完成项：{{ done }}",
            "variables": [{"name": "week", "required": True}, {"name": "done"}],
        }
    )
    assert saved["success"] is True
    assert (await tool.execute({"action": "list"}))["templates"][0]["name"] == "weekly"
    got = await tool.execute({"action": "get", "name": "weekly"})
    assert got["success"] and got["template"]["kind"] == "document"
    preview = await tool.execute(
        {"action": "preview", "name": "weekly", "values": {"week": "第3周", "done": "三项"}}
    )
    assert preview["success"] is True
    rendered = preview["rendered"]["content"]
    assert "第3周" in rendered and "三项" in rendered
    out = tmp_path / "report.md"
    gen = await tool.execute(
        {
            "action": "generate",
            "name": "weekly",
            "values": {"week": "第4周", "done": "两项"},
            "output_path": str(out),
        },
        _ctx(tmp_path),
    )
    assert gen["success"] is True
    assert "第4周" in out.read_text(encoding="utf-8")
    deleted = await tool.execute({"action": "delete", "name": "weekly"})
    assert deleted["success"] is True and deleted["deleted"] is True


@pytest.mark.asyncio
async def test_template_deck_generate_pptx(tmp_path: Path) -> None:
    tool = DocumentTemplateTool(store=_tpl_store(tmp_path))
    await tool.execute(
        {
            "action": "save",
            "name": "pitch",
            "kind": "deck",
            "slides": [{"title": "{{ company }} 方案", "body": "核心 {{ point }}"}],
        }
    )
    out = tmp_path / "pitch.pptx"
    gen = await tool.execute(
        {
            "action": "generate",
            "name": "pitch",
            "values": {"company": "Bandi", "point": "AI 员工"},
            "output_path": str(out),
        },
        _ctx(tmp_path),
    )
    assert gen["success"] is True
    assert _pptx_slides(out.read_bytes()) == ["Bandi 方案"]


@pytest.mark.asyncio
async def test_template_generate_requires_output_and_unknown_action(tmp_path: Path) -> None:
    tool = DocumentTemplateTool(store=_tpl_store(tmp_path))
    await tool.execute({"action": "save", "name": "d1", "kind": "document", "content": "{{ x }}"})
    no_out = await tool.execute({"action": "generate", "name": "d1", "values": {"x": "1"}})
    assert no_out["success"] is False
    missing = await tool.execute({"action": "get", "name": "nope"})
    assert missing["success"] is False
    bad_action = await tool.execute({"action": "explode"})
    assert bad_action["success"] is False


# ---------- theme_designer 生命周期 ----------


@pytest.mark.asyncio
async def test_theme_create_dry_run_no_persist(tmp_path: Path) -> None:
    tool = ThemeDesignerTool(store=_tpl_store(tmp_path))
    result = await tool.execute(
        {"action": "create", "name": "brand", "primary": "#3366CC", "mode": "dark", "dry_run": True}
    )
    assert result["success"] is True and result["saved"] is False
    assert result["payload"]["colors"]["background"] == "#0F1115"
    assert result["lint_warnings"] == []
    assert (await tool.execute({"action": "get", "name": "brand"}))["success"] is False


@pytest.mark.asyncio
async def test_theme_save_overrides_and_lint(tmp_path: Path) -> None:
    tool = ThemeDesignerTool(store=_tpl_store(tmp_path))
    bad = await tool.execute(
        {
            "action": "save",
            "name": "lowcontrast",
            "payload": {
                "colors": {
                    "text": "#FFFFFF",
                    "background": "#FFFFFF",
                    "surface": "#FFFFFF",
                    "text_light": "#FFFFFF",
                }
            },
        }
    )
    assert bad["success"] is True
    assert len(bad["lint_warnings"]) >= 2
    create = await tool.execute(
        {"action": "create", "name": "brand", "primary": "#E63946", "mode": "light"}
    )
    assert create["success"] is True and create["saved"] is True
    got = await tool.execute({"action": "get", "name": "brand"})
    assert got["success"] is True and got["theme"]["colors"]["primary"] == "#E63946"
    listing = await tool.execute({"action": "list"})
    assert {t["name"] for t in listing["themes"]} == {"lowcontrast", "brand"}
    deleted = await tool.execute({"action": "delete", "name": "brand"})
    assert deleted["success"] is True and deleted["deleted"] is True


@pytest.mark.asyncio
async def test_theme_create_requires_primary(tmp_path: Path) -> None:
    tool = ThemeDesignerTool(store=_tpl_store(tmp_path))
    result = await tool.execute({"action": "create", "name": "x", "mode": "light"})
    assert result["success"] is False


# ---------- 装配：全 7 工具注册 + feature gate fail-closed ----------


def test_all_gen_tools_registered_with_gate() -> None:
    from bandi.agent.factory import build_tool_registry
    from bandi.tools.base import ToolCategory

    registry = build_tool_registry(feature_flags={"gen": True})
    names = {t.name for t in registry.list_tools()}
    for expected in (
        "document_generate",
        "slides_generate",
        "excel_generate",
        "checklist_generate",
        "template_filler",
        "document_template",
        "theme_designer",
    ):
        assert expected in names
        tool = registry.get(expected)
        assert tool is not None
        assert tool.category == ToolCategory.GEN
        assert tool.feature_group == "gen"
        assert tool.to_tool_meta()["capability_tags"] == ["file_write"]

    closed = build_tool_registry(feature_flags={"gen": False})
    for expected in (
        "document_generate",
        "slides_generate",
        "excel_generate",
        "checklist_generate",
        "template_filler",
        "document_template",
        "theme_designer",
    ):
        assert closed.get(expected) is None


@pytest.mark.asyncio
async def test_registry_wired_tools_write_final_artifacts(tmp_path: Path) -> None:
    """端到端：生产装配点取工具执行，真实写盘并读回最终产物（DoD-12 主链路）。"""
    from bandi.agent.factory import build_tool_registry

    registry = build_tool_registry(feature_flags={"gen": True})
    ctx = _ctx(tmp_path)
    doc = registry.get("document_generate")
    out_doc = tmp_path / "e2e.docx"
    result = await doc.execute(
        {"content": "# 端到端报告\n\n装配链路写入", "output_path": str(out_doc)}, ctx
    )
    assert result["success"] is True
    assert "端到端报告" in _docx_text(out_doc.read_bytes())
    deck = registry.get("slides_generate")
    out_deck = tmp_path / "e2e.pdf"
    result = await deck.execute(
        {"slides": [{"title": "桌面页", "body": "内容"}], "output_path": str(out_deck)}, ctx
    )
    assert result["success"] is True
    assert "桌面页" in _pdf_text(out_deck.read_bytes())[0]
    out_tbl = tmp_path / "e2e_table.pdf"
    result = await doc.execute(
        {
            "content": "# 表格\n\n| 字段 | 值 |\n|------|-----|\n| 请求 | 已受理 |\n",
            "output_path": str(out_tbl),
        },
        ctx,
    )
    assert result["success"] is True
    text = "\n".join(_pdf_text(out_tbl.read_bytes()))
    assert "字段" in text and "已受理" in text
    assert "**【" not in text and "【" not in text


def test_gen_schema_rejects_unknown_params() -> None:
    tool = DocumentGenerateTool()
    errors = tool.validate_params({"content": "x", "output_path": "o.md", "nope": 1})
    assert any("nope" in e for e in errors)
    assert tool.validate_params({"content": "x", "output_path": "o.md"}) == []


@pytest.mark.asyncio
async def test_gen_document_output_path_traversal_rejected(tmp_path: Path) -> None:
    """破坏者：output_path 用 ../ 越出 project_root 沙箱必须拒绝，不得落盘到外部。"""
    tool = DocumentGenerateTool()
    result = await tool.execute({"content": "# x", "output_path": "../escape.md"}, _ctx(tmp_path))
    assert result["success"] is False, f"越界路径必须拒绝，实际: {result}"
    assert "outside workspace" in str(result.get("error", "")).lower()
    assert not (tmp_path.parent / "escape.md").exists()


@pytest.mark.asyncio
async def test_gen_pdf_table_header_clean_readback(tmp_path: Path) -> None:
    """PDF 表格表头必须干净（无 markdown 星号/全角括号噪声），且可读回。"""
    tool = DocumentGenerateTool()
    out = tmp_path / "t.pdf"
    md = "# 表格标题\n\n| 列一 | 列二 |\n|------|------|\n| 甲 | 乙 |\n"
    result = await tool.execute({"content": md, "output_path": str(out)}, _ctx(tmp_path))
    assert result["success"] is True, f"PDF 写入失败: {result}"
    text = "\n".join(_pdf_text(out.read_bytes()))
    assert "列一" in text and "甲" in text, f"表格内容应可读回，实际: {text!r}"
    assert "**【" not in text and "【" not in text, f"表头不得含 markdown 噪声，实际: {text!r}"
    assert "*" not in text, f"表头不得含星号噪声，实际: {text!r}"


@pytest.mark.asyncio
async def test_template_filler_url_scheme_whitelist() -> None:
    """破坏者：template_source=url 只允许 http/https——file:// 等任意 scheme 必须拒绝
    （防本地任意文件读取/SSRF，绕过 FsTool 沙箱）。"""
    tool = TemplateFillerTool()
    for bad in ("file:///etc/hosts", "javascript:alert(1)", "ftp://example.com/x.j2"):
        result = await tool.execute({"template_source": "url", "template_url": bad})
        assert result["success"] is False, f"scheme {bad!r} 必须拒绝，实际: {result}"
        assert "scheme" in str(result.get("error", "")).lower()
