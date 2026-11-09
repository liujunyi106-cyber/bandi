# 功能：会话归档沉淀——LLM 摘要要点写入项目 AGENTS.md 的受管区块。
# 的项目文档自动更新）+ auto-memory 条目上限设计——取「显式触发 + 受管区块 + 条目上限」
# 防止无限追加膨胀；区块外内容严格保留（人类主权）。
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from bandi.company.projects import AGENTS_MD_FILENAME

logger = logging.getLogger(__name__)

ARCHIVE_BLOCK_START = "<!-- bandi:archives -->"
ARCHIVE_BLOCK_END = "<!-- /bandi:archives -->"
MAX_ARCHIVE_ENTRIES = 20
_TRANSCRIPT_HEAD_CHARS = 2500
_TRANSCRIPT_TAIL_CHARS = 3500
_ENTRY_MARKER = "### "

SUMMARY_PROMPT = (
    "你是项目知识管理员。把下面的会话沉淀为项目知识，严格按以下小标题输出：\n"
    "\n"
    "## 决策\n"
    "- （关键决策，每条一行）\n"
    "\n"
    "## 结论\n"
    "- （已确定的事实、参数、约定）\n"
    "\n"
    "## 待办\n"
    "- （未完成事项，没有则写「- （无）」）\n"
    "\n"
    "规则：\n"
    "- 三个小标题必须都出现，该节无内容时写「- （无）」；\n"
    "- 每条要点独占一行并以「- 」开头，不超过 40 字；\n"
    "- 只写决策、结论、待办，不要客套与过程描述；\n"
    "- 不得编造会话里没有出现过的结论或参数。\n"
    "\n"
    "会话标题：{title}\n\n会话内容：\n{transcript}"
)

ARCHIVE_SECTIONS = ("决策", "结论", "待办")
_SECTION_HEADING = "## "


# 预留：归档摘要小标题解析；启用时机 会话归档点读取（当前测试在用）
def parse_archive_points(body: str) -> dict[str, list[str]]:
    """功能：把摘要正文按固定小标题解析为 {小标题: [要点行]}（缺失的小标题返回空列表）。"""
    buckets: dict[str, list[str]] = {name: [] for name in ARCHIVE_SECTIONS}
    current = ""
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(_SECTION_HEADING):
            name = stripped[len(_SECTION_HEADING) :].strip()
            current = name if name in buckets else ""
            continue
        if current and stripped.startswith("- "):
            item = stripped[2:].strip()
            if item and item not in buckets[current]:
                buckets[current].append(item)
    return buckets


def _render_transcript(messages: list[dict[str, str]]) -> str:
    """功能：把会话消息渲染为纯文本（头尾截断，中性省略标记）。"""
    lines: list[str] = []
    for item in messages:
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        speaker = "用户" if role == "user" else "助手"
        lines.append(f"{speaker}: {content}")
    text = "\n".join(lines)
    if len(text) <= _TRANSCRIPT_HEAD_CHARS + _TRANSCRIPT_TAIL_CHARS:
        return text
    return text[:_TRANSCRIPT_HEAD_CHARS] + "\n…（中间省略）…\n" + text[-_TRANSCRIPT_TAIL_CHARS:]


def render_archive_entry(title: str, points: str, date_str: str | None = None) -> str:
    """功能：渲染单条归档条目（`### [日期] 标题` + 要点列表）。"""
    day = date_str or datetime.now().strftime("%Y-%m-%d")
    clean_title = str(title or "").strip() or "(无标题会话)"
    body = str(points or "").strip() or "- （摘要为空）"
    return f"{_ENTRY_MARKER}[{day}] {clean_title}\n\n{body}\n"


def _split_entries(block_body: str) -> list[str]:
    """功能：把受管区块正文按 `### ` 标记切分为条目列表（保持各条完整）。"""
    parts: list[str] = []
    current: list[str] | None = None
    for line in block_body.splitlines():
        if line.startswith(_ENTRY_MARKER):
            if current is not None:
                parts.append("\n".join(current).strip())
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        parts.append("\n".join(current).strip())
    return [part for part in parts if part]


def upsert_archive_block(existing: str, entry: str) -> str:
    """功能：把新条目插入受管区块（最新在前；超上限移除最旧；区块缺失则创建于文件末尾）。

    区块外内容严格保留；区块内只维护 bandi 归档条目。
    """
    text = existing or ""
    start = text.find(ARCHIVE_BLOCK_START)
    end = text.find(ARCHIVE_BLOCK_END)
    if start != -1 and end != -1 and end > start:
        body = text[start + len(ARCHIVE_BLOCK_START) : end]
        entries = [entry.strip(), *_split_entries(body)][:MAX_ARCHIVE_ENTRIES]
        rebuilt = "\n\n".join(entries) + "\n"
        return text[: start + len(ARCHIVE_BLOCK_START)] + "\n\n" + rebuilt + "\n" + text[end:]
    block = f"{ARCHIVE_BLOCK_START}\n\n{entry.strip()}\n\n{ARCHIVE_BLOCK_END}\n"
    prefix = text.rstrip()
    return block if not prefix else f"{prefix}\n\n{block}"


def _write_managed_archive(path: Path, title: str, points: str) -> bool:
    """功能：读-改-写受管归档区块（原子写，父目录自动创建）。"""
    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("归档读取失败: %s: %s", path, exc)
        return False
    updated = upsert_archive_block(existing, render_archive_entry(title, points))
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(updated, encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError) as exc:
        tmp.unlink(missing_ok=True)
        logger.warning("归档写入失败: %s: %s", path, exc)
        return False
    return True


def write_archive_entry(root_dir: str, title: str, points: str) -> bool:
    """功能：把归档条目写入项目 AGENTS.md（不存在则创建；原子写；失败返回 False）。"""
    root = str(root_dir or "").strip()
    if not root:
        return False
    base = Path(root).expanduser()
    if not base.is_dir():
        return False
    return _write_managed_archive(base / AGENTS_MD_FILENAME, title, points)


async def summarize_session(
    provider: Any, title: str, messages: list[dict[str, str]], session_id: str = ""
) -> str:
    """功能：LLM 摘要会话要点（3-5 条）；失败抛异常由调用方兜底。"""
    from bandi.llm.accounted import accounted_complete

    prompt = SUMMARY_PROMPT.format(
        title=title or "(无标题)", transcript=_render_transcript(messages)
    )
    response = await accounted_complete(
        provider,
        [{"role": "user", "content": prompt}],
        label="project_archive",
        session_id=session_id or "project-archive",
    )
    return str(getattr(response, "content", "") or "").strip()
