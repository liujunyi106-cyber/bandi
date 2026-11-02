# 功能：工具结果摘要三层——规则摘要（shell/read/其他）+
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from bandi.utils.token_counter import format_omission_marker

logger = logging.getLogger(__name__)

SUMMARY_PREFIX = "[tool:summary]"
TOOL_HISTORY_PREFIX = "[tool:history]"
MICRO_CLEARED_PLACEHOLDER = "[Old result cleared]"
PERSISTED_EXCERPT_PREFIX = "[tool:persisted]"
PERSISTED_PATH_MARKER = "[full output persisted:"

# 落盘摘录的头尾保留量：规则摘要会丢掉正文中段以外的事实，摘录改为保留
# 真实头/尾（与 executor 侧落盘口径一致），中段由路径回读。
_PERSIST_HEAD_CHARS = 900
_PERSIST_TAIL_CHARS = 400

_SHELL_TOOLS = frozenset({"bash", "python_exec", "shell_exec"})
_READ_TOOLS = frozenset({"read", "grep", "glob", "web_search", "web_fetch"})
_LLM_WHITELIST = frozenset({"bash", "python_exec", "read", "grep", "web_fetch"})

SUMMARY_MAX_CHARS = 1200
_HEAD_MAX_CHARS = 600
_ERROR_LINE_MAX_CHARS = 200
_TAIL_LINES = 30
_HEAD_LINES = 12
_LLM_MIN_CHARS = 8000
_LLM_INPUT_CAP = 24000
_ERROR_RE = re.compile(r"(?i)error|exception|traceback|failed")

_LLM_PROMPT = (
    "把下面的工具输出压缩为简洁的事实摘要：保留关键文件路径、错误串、数值与结论，"
    "去除重复与修饰。只输出摘要正文（不超过 400 字），不要复述指令。\n"
    "工具：{tool_name}\n输出：\n{content}"
)


def is_already_shaped(content: str) -> bool:
    # 功能：幂等护栏——已摘要/已占位/已落盘回传/空内容不再二次处理
    text = (content or "").strip()
    if not text:
        return True
    if text == MICRO_CLEARED_PLACEHOLDER:
        return True
    if text.startswith(SUMMARY_PREFIX) or text.startswith(TOOL_HISTORY_PREFIX):
        return True
    return "[full output persisted:" in text


def _format_summary(tool_name: str, core: str) -> str:
    return f"{SUMMARY_PREFIX} tool={tool_name}\ncore: {core.strip()}"


def _apply_cap(text: str) -> str:
    # 功能：兜底截断——省略标注带规模且计入帽（只有「…」= 静默丢内容）
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    omitted = len(text)
    for _ in range(3):
        marker = format_omission_marker(omitted)
        keep = max(SUMMARY_MAX_CHARS - len(marker), 0)
        omitted = len(text) - keep
    marker = format_omission_marker(omitted)
    keep = max(SUMMARY_MAX_CHARS - len(marker), 0)
    return text[:keep] + marker


def _rule_summary_shell(content: str, tool_name: str) -> str:
    # 功能：shell 规则——error 行前 5 条（每条 ≤200 字符）+ tail 30 行
    lines = content.splitlines()
    error_lines = [ln for ln in lines if _ERROR_RE.search(ln)]
    tail = lines[-_TAIL_LINES:]
    parts: list[str] = []
    if error_lines:
        parts.append(
            "errors: " + "; ".join(ln.strip()[:_ERROR_LINE_MAX_CHARS] for ln in error_lines[:5])
        )
    parts.append("tail:\n" + "\n".join(tail))
    return _apply_cap(_format_summary(tool_name, "\n".join(parts)))


def _rule_summary_read(content: str, tool_name: str) -> str:
    # 功能：read 规则——head 12 行（≤600 字符）+ (N lines, M chars) 统计
    lines = content.splitlines()
    head = "\n".join(lines[:_HEAD_LINES])
    if len(head) > _HEAD_MAX_CHARS:
        head = head[:_HEAD_MAX_CHARS] + "…"
    body = f"{head}\n… ({len(lines)} lines, {len(content):,} chars)"
    return _apply_cap(_format_summary(tool_name, body))


def _rule_summary_generic(content: str, tool_name: str) -> str:
    # 功能：其他工具——首段 + 字符数注记（预算内预留前缀与注记空间，保证注记不被帽吃掉）
    note = f"… ({len(content):,} chars)"
    prefix_len = len(_format_summary(tool_name, ""))
    keep = max(SUMMARY_MAX_CHARS - prefix_len - len(note), 0)
    body = content[:keep]
    if len(content) > keep:
        body += note
    return _format_summary(tool_name, body)


def rule_summary(content: str, tool_name: str) -> str:
    # 功能：规则摘要分派（同步纯函数，prune 与 executor 共用）
    name = str(tool_name or "tool").strip().lower()
    if name in _SHELL_TOOLS:
        return _rule_summary_shell(content, name)
    if name in _READ_TOOLS:
        return _rule_summary_read(content, name)
    return _rule_summary_generic(content, name)


def persisted_excerpt(content: str, tool_name: str, path: str) -> str:
    # 功能：全文落盘后的上下文摘录——保留真实头/尾 + 全文路径，不做规则摘要
    text = content or ""
    name = str(tool_name or "tool").strip().lower() or "tool"
    lines = text.count("\n") + 1
    head = text[:_PERSIST_HEAD_CHARS]
    tail = (
        text[-_PERSIST_TAIL_CHARS:] if len(text) > _PERSIST_HEAD_CHARS + _PERSIST_TAIL_CHARS else ""
    )
    parts = [f"{PERSISTED_EXCERPT_PREFIX} tool={name} chars={len(text):,} lines={lines}", head]
    if tail:
        parts.append("…")
        parts.append(tail)
    parts.append(f"{PERSISTED_PATH_MARKER} {path}]")
    return "\n".join(parts)


async def summarize_tool_output(
    content: str,
    tool_name: str,
    *,
    llm: Callable[[str, str], Awaitable[str]] | None = None,
    session_id: str = "",
) -> str:
    # 功能：摘要编排——规则先行；白名单 + 超阈值 + llm 注入时尝试 LLM，失败/空输出回退规则
    name = str(tool_name or "tool").strip().lower()
    if llm is not None and name in _LLM_WHITELIST and len(content) > _LLM_MIN_CHARS:
        prompt = _LLM_PROMPT.format(tool_name=name, content=content[:_LLM_INPUT_CAP])
        try:
            text = (await llm(prompt, session_id)).strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("tool summary LLM failed for %s: %s", name, exc)
            text = ""
        if text:
            return _apply_cap(_format_summary(name, text))
    return rule_summary(content, name)
