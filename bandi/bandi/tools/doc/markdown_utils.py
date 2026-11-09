# 功能：Markdown 纯函数工具——解析/格式化/HTML 转换/表格列表构建/模板渲染（无 I/O，供 MarkdownProcessorTool 编排）

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+)$")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_CODE_FENCE = re.compile(r"^```([^\n`]*)\r?\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TRAILING_SPACES = re.compile(r"[ \t]+$", re.MULTILINE)
_MULTIPLE_BLANK = re.compile(r"\n{3,}")

_TEMPLATES = {
    "story": ["title", "meta", "body"],
    "report": ["title", "meta", "summary", "sections", "conclusion"],
    "notes": ["title", "meta", "body"],
    "article": ["title", "meta", "abstract", "sections", "references"],
    "meeting": ["title", "meta", "attendees", "agenda", "notes", "action_items"],
    "readme": ["title", "badges", "description", "installation", "usage", "api", "license"],
    "changelog": ["title", "entries"],
}


def parse_headings(lines: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, line in enumerate(lines, start=1):
        m = _HEADING_LINE.match(line)
        if m:
            out.append({"level": len(m.group(1)), "text": m.group(2).strip(), "line": i})
    return out


def parse_links(text: str) -> list[dict[str, str]]:
    return [{"text": t, "url": u} for t, u in _LINK.findall(text)]


def parse_images(text: str) -> list[dict[str, str]]:
    return [{"alt": a, "src": s} for a, s in _IMAGE.findall(text)]


def parse_code_blocks(text: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    for m in _CODE_FENCE.finditer(text):
        lang = (m.group(1) or "").strip()
        code = m.group(2)
        if code.endswith("\n"):
            code = code[:-1]
        blocks.append({"language": lang, "code": code})
    return blocks


def code_blocks_with_lines(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in _CODE_FENCE.finditer(text):
        lang = (m.group(1) or "").strip()
        code = m.group(2)
        if code.endswith("\n"):
            code = code[:-1]
        out.append(
            {"language": lang, "code": code, "line_number": text[: m.start()].count("\n") + 1}
        )
    return out


def parse_frontmatter(text: str) -> dict[str, str] | None:
    m = _FRONTMATTER.match(text)
    if not m:
        return None
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm


def nest_toc(headings: list[dict[str, Any]], max_depth: int) -> list[dict[str, Any]]:
    root: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []
    for h in headings:
        if h["level"] > max_depth:
            continue
        node: dict[str, Any] = {
            "level": h["level"],
            "text": h["text"],
            "line": h["line"],
            "children": [],
        }
        while stack and stack[-1][0] >= h["level"]:
            stack.pop()
        if not stack:
            root.append(node)
        else:
            stack[-1][1]["children"].append(node)
        stack.append((h["level"], node))
    return root


def _inline_markdown_to_html(text: str) -> str:
    s = re.sub(r"`([^`]+)`", lambda m: f"<code>{html.escape(m.group(1))}</code>", text)
    s = re.sub(r"\*\*([^*]+)\*\*", lambda m: f"<strong>{html.escape(m.group(1))}</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", lambda m: f"<em>{html.escape(m.group(1))}</em>", s)
    s = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>',
        s,
    )
    return s


def markdown_to_html(md: str) -> str:
    lines = md.splitlines()
    html_parts: list[str] = []
    para_buf: list[str] = []
    in_code = False
    code_buf: list[str] = []
    code_lang = ""

    def flush_para() -> None:
        if para_buf:
            html_parts.append(f"<p>{_inline_markdown_to_html(' '.join(para_buf))}</p>")
            para_buf.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_para()
            if not in_code:
                in_code = True
                code_lang = stripped[3:].strip()
                code_buf = []
            else:
                in_code = False
                code = "\n".join(code_buf)
                lang_attr = f' class="language-{html.escape(code_lang)}"' if code_lang else ""
                html_parts.append(f"<pre><code{lang_attr}>{html.escape(code)}</code></pre>")
            continue
        if in_code:
            code_buf.append(line)
            continue
        hm = _HEADING_LINE.match(line)
        if hm:
            flush_para()
            html_parts.append(
                f"<h{len(hm.group(1))}>{_inline_markdown_to_html(hm.group(2).strip())}</h{len(hm.group(1))}>"
            )
            continue
        if not stripped:
            flush_para()
            continue
        para_buf.append(stripped)
    flush_para()
    return "\n".join(html_parts)


def markdown_to_plain(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    in_code = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            out.append(line)
            continue
        s = line
        s = re.sub(r"^#{1,6}\s+", "", s)
        s = re.sub(r"`([^`]+)`", r"\1", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", s)
        s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1", s)
        out.append(s)
    return "\n".join(out)


def format_markdown(text: str) -> str:
    result = _TRAILING_SPACES.sub("", text)
    result = _MULTIPLE_BLANK.sub("\n\n", result)
    lines = result.splitlines()
    formatted: list[str] = []
    for i, line in enumerate(lines):
        formatted.append(line)
        if _HEADING_LINE.match(line) and i + 1 < len(lines) and lines[i + 1].strip():
            formatted.append("")
    result = "\n".join(formatted)
    if not result.endswith("\n"):
        result += "\n"
    return result


def find_section_range(lines: list[str], section_title: str) -> tuple[int, int] | None:
    start_idx: int | None = None
    start_level = 0
    for i, line in enumerate(lines):
        m = _HEADING_LINE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            if start_idx is None:
                if title.lower() == section_title.lower():
                    start_idx = i
                    start_level = level
            elif level <= start_level:
                return (start_idx, i)
    if start_idx is not None:
        return (start_idx, len(lines))
    return None


def build_table_md(headers: list[str], rows: list[list[str]], align: str | None = None) -> str:
    if not headers:
        return ""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(cell))

    def pad(val: str, width: int) -> str:
        return val.ljust(width)

    header_line = "| " + " | ".join(pad(h, col_widths[i]) for i, h in enumerate(headers)) + " |"
    if align == "center":
        sep_line = (
            "| "
            + " | ".join(":" + "-" * (w - 2) + ":" if w > 2 else ":-:" for w in col_widths)
            + " |"
        )
    elif align == "right":
        sep_line = "| " + " | ".join("-" * (w - 1) + ":" for w in col_widths) + " |"
    else:
        sep_line = "| " + " | ".join("-" * w for w in col_widths) + " |"
    body_lines: list[str] = []
    for row in rows:
        cells = []
        for i in range(len(headers)):
            val = row[i] if i < len(row) else ""
            cells.append(pad(val, col_widths[i]))
        body_lines.append("| " + " | ".join(cells) + " |")
    return "\n".join([header_line, sep_line] + body_lines)


def build_list_md(items: list[Any], ordered: bool = False, indent: int = 0) -> str:
    lines: list[str] = []
    prefix_space = "  " * indent
    for i, item in enumerate(items, start=1):
        if isinstance(item, dict):
            text = item.get("text", str(item))
            children = item.get("children", [])
        elif isinstance(item, (list, tuple)) and len(item) >= 1:
            text = str(item[0])
            children = item[1] if len(item) > 1 and isinstance(item[1], list) else []
        else:
            text = str(item)
            children = []
        marker = f"{i}." if ordered else "-"
        lines.append(f"{prefix_space}{marker} {text}")
        if children:
            lines.append(build_list_md(children, ordered=ordered, indent=indent + 1))
    return "\n".join(lines)


def _render_document(params: dict[str, Any]) -> str:
    parts: list[str] = []
    metadata = params.get("metadata")
    if metadata:
        parts.append(_render_frontmatter(metadata))
    parts.append(f"# {params.get('title', 'Untitled')}")
    body = params.get("content")
    sections = params.get("sections") or []
    if body and not sections:
        parts.append(body)
    else:
        for sec in sections:
            heading = sec.get("heading", "")
            level = sec.get("level", 2)
            parts.append(f"{'#' * level} {heading}")
            if sec.get("content"):
                parts.append(sec["content"])
    return "\n\n".join(parts) + "\n"


def _render_frontmatter(metadata: dict[str, Any]) -> str:
    fm_lines = ["---"]
    for k, v in metadata.items():
        if isinstance(v, list):
            fm_lines.append(f"{k}:")
            for item in v:
                fm_lines.append(f"  - {item}")
        else:
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    return "\n".join(fm_lines)


def _render_template(template_name: str, params: dict[str, Any]) -> str:
    data = params.get("template_data") or {}
    title = params.get("title") or data.get("title", "Untitled")
    metadata = params.get("metadata") or data.get("metadata")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts: list[str] = []

    if metadata or template_name in ("report", "article", "meeting"):
        fm = dict(metadata or {})
        if "date" not in fm:
            fm["date"] = now
        if "title" not in fm:
            fm["title"] = title
        parts.append(_render_frontmatter(fm))
    parts.append(f"# {title}")

    if template_name == "story":
        parts.append(data.get("body") or data.get("content") or "*Write your story here...*")
    elif template_name == "report":
        parts.append("## Summary\n")
        parts.append(data.get("summary", "*Executive summary goes here.*"))
        for sec in data.get("sections") or []:
            if isinstance(sec, dict):
                parts.append(f"## {sec.get('heading', 'Section')}\n")
                parts.append(sec.get("content") or "*Content goes here.*")
            else:
                parts.append(f"## {sec}\n")
        parts.append("## Conclusion\n")
        parts.append(data.get("conclusion", "*Conclusion goes here.*"))
    elif template_name == "notes":
        body = data.get("body") or data.get("content")
        parts.append(body if body else "## Notes\n\n- ")
    elif template_name == "article":
        abstract = data.get("abstract")
        if abstract:
            parts.append("## Abstract\n")
            parts.append(f"> {abstract}")
        for sec in data.get("sections") or []:
            if isinstance(sec, dict):
                parts.append(f"## {sec.get('heading', 'Section')}\n")
                parts.append(sec.get("content") or "")
            else:
                parts.append(f"## {sec}\n")
        refs = data.get("references") or []
        if refs:
            parts.append("## References\n")
            for i, ref in enumerate(refs, 1):
                parts.append(f"{i}. {ref}")
    elif template_name == "meeting":
        attendees = data.get("attendees") or []
        if attendees:
            parts.append("## Attendees\n")
            parts.append("\n".join(f"- {a}" for a in attendees))
        agenda = data.get("agenda") or []
        if agenda:
            parts.append("## Agenda\n")
            parts.append("\n".join(f"{i}. {item}" for i, item in enumerate(agenda, 1)))
        parts.append("## Discussion Notes\n")
        parts.append(data.get("notes") or data.get("content") or "*Notes here...*")
        action_items = data.get("action_items") or []
        parts.append("## Action Items\n")
        parts.append(
            "\n".join(f"- [ ] {item}" for item in action_items) if action_items else "- [ ] "
        )
    elif template_name == "readme":
        parts.append(data.get("description") or "*Project description*")
        parts.append("## Installation\n")
        parts.append(f"```bash\n{data.get('installation', '# Installation steps')}\n```")
        parts.append("## Usage\n")
        parts.append(data.get("usage") or "*Usage examples*")
        if data.get("api"):
            parts.append("## API\n")
            parts.append(data["api"])
        parts.append("## License\n")
        parts.append(data.get("license", "MIT"))
    elif template_name == "changelog":
        entries = data.get("entries") or []
        if entries:
            for entry in entries:
                parts.append(
                    f"## [{entry.get('version', '0.0.0')}] - {entry.get('date', now.split(' ')[0])}\n"
                )
                for change in entry.get("changes") or []:
                    parts.append(f"### {change.get('type', 'Changed')}\n")
                    parts.append("\n".join(f"- {item}" for item in change.get("items") or []))
        else:
            parts.append(f"## [Unreleased] - {now.split(' ')[0]}\n")
            parts.append("### Added\n\n- ")
    return "\n\n".join(parts) + "\n"
