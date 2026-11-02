# 功能：补丁应用工具（ApplyPatchTool：unified diff 解析 + 回退匹配 + 行尾/BOM 保真 + 原子写）
#
# ① **无回退容错**：hunk 的减/上下文块要求逐行完全相等，空白/缩进/引号稍有漂移即
# 「hunk(s) failed to apply」（弱模型产出的补丁尤其容易漂移）；
# ② **强制 LF**：新增行硬编码 "\n"，对 CRLF 文件会写出**混合行尾**；
# ③ **写回不保真**：解码用 errors="replace"（坏字节被替换成 U+FFFD 后写回 = 静默损坏）
# 现与 `edit` 共用 `tools/edit_match.py`（10 级回退链）与 `tools/fs_write.py`
# （BOM 保真解码 / 外部修改检测 / 原子写 / per-路径锁），消除两工具的行为分叉。
from __future__ import annotations

import asyncio
import re
from typing import Any

from bandi.tools.base import BaseTool, RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.edit_match import detect_line_ending, probe_match
from bandi.tools.errors import NonRetryableToolError
from bandi.tools.fs_write import (
    atomic_write,
    check_external_modification,
    decode_with_bom,
    file_lock,
)

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_HUNK_SLIDE_LIMIT = 5


def _strip_prefix(path: str) -> str:
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _parse_patch(patch_text: str) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    current_path: str = ""
    current_lines: list[str] = []
    for line in patch_text.splitlines(keepends=True):
        if line.startswith("--- "):
            if current_path and current_lines:
                files.append((current_path, "".join(current_lines)))
            current_path = _strip_prefix(line[4:].strip().rstrip("\n\r"))
            current_lines.clear()
        elif line.startswith("+++ "):
            continue
        else:
            current_lines.append(line)
    if current_path and current_lines:
        files.append((current_path, "".join(current_lines)))
    return files


def _hunk_blocks(hunk_text: str) -> tuple[list[str], list[str]]:
    """功能：hunk 正文 → (减/上下文行, 增/上下文行)。

    统一 diff 语义：`-` 只进 remove、`+` 只进 add、` `（上下文）两边都进；
    空行与 `\\ No newline at end of file` 等标记行两边都不进（与原实现一致）。
    行尾一律剥掉——重建时按**目标文件自身的行尾**拼接（②）。
    """
    remove: list[str] = []
    add: list[str] = []
    for hl in hunk_text.splitlines(keepends=True)[1:]:
        text = hl.rstrip("\n\r")
        if text.startswith("-"):
            remove.append(text[1:])
        elif text.startswith("+"):
            add.append(text[1:])
        elif text.startswith(" "):
            remove.append(text[1:])
            add.append(text[1:])
    return remove, add


def _join_lines(items: list[str], ending: str) -> str:
    # 功能：按目标文件行尾把行列表拼回文本（②：不再硬编码 "\n"）
    return "".join(item + ending for item in items)


def _line_offset(text: str, line_index: int) -> int:
    # 功能：第 line_index 行（0 基）在 text 中的字符偏移；越界返回全文长度
    if line_index <= 0:
        return 0
    return sum(len(raw) for raw in text.splitlines(keepends=True)[:line_index])


class ApplyPatchTool(BaseTool):  # 功能：解析 unified diff 格式并应用到文件
    name: str = "apply_patch"
    description: str = "Parse and apply a unified diff/patch to the workspace files"
    category: ToolCategory = ToolCategory.CODE
    risk_level: str = "medium"
    permissions_override: str = "ask"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["patch"],
        "additionalProperties": False,
        "properties": {
            "patch": {
                "type": "string",
                "description": "Unified diff content (e.g. from git diff or diff -u)",
            },
        },
    }
    is_concurrency_safe: bool = False
    needs_context: bool = True

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        patch_text = params["patch"]
        files = _parse_patch(patch_text)
        if not files:
            raise NonRetryableToolError(
                "apply_patch: no file headers (expecting '--- a/...' format)"
            )

        results: list[str] = []
        applied_total = 0
        failed_total = 0
        io_errors: list[str] = []
        for rel_path, hunks in files:
            from bandi.tools.fs_tool import (
                project_base,
                project_extra_roots,
                resolve_within_workspace,
                role_fs_scopes,
                sensitive_path_reason,
            )

            try:
                target = resolve_within_workspace(
                    rel_path,
                    extra_roots=project_extra_roots(context),
                    base=project_base(context),
                    role_fs_scopes=role_fs_scopes(context),
                )
            except ValueError as e:
                results.append(f"  {rel_path}: skipped ({e})")
                continue
            reason = sensitive_path_reason(target)
            if reason:
                results.append(f"  {rel_path}: skipped ({reason})")
                continue
            if not target.exists():
                results.append(f"  {rel_path}: not found")
                continue
            async with file_lock(target):
                try:
                    raw = target.read_bytes()
                    # 读后外部修改检测（有读取记录才生效）——检测到即失败，不静默覆盖
                    check_external_modification(context, target, raw)
                    text, encoding, bom = decode_with_bom(raw)
                except ValueError as e:
                    io_errors.append(f"  {rel_path}: {e}")
                    results.append(f"  {rel_path}: {e}")
                    continue
                except (OSError, PermissionError, UnicodeDecodeError, LookupError) as e:
                    io_errors.append(f"  {rel_path}: read error: {e}")
                    results.append(f"  {rel_path}: read error: {e}")
                    continue

                new_text, count, failed_count = _apply_hunks(text, hunks)
                failed_total += failed_count
                if count > 0 and failed_count == 0:
                    from bandi.agent.file_history import record_file_edit, record_file_written

                    record_file_edit(target, context)
                    try:
                        await asyncio.to_thread(
                            atomic_write, target, bom + new_text.encode(encoding)
                        )
                        record_file_written(target, context)
                        applied_total += count
                        results.append(f"  {rel_path}: {count} hunk(s) applied")
                    except (OSError, PermissionError, UnicodeEncodeError) as e:
                        io_errors.append(f"  {rel_path}: write error: {e}")
                        results.append(f"  {rel_path}: write error: {e}")
                else:
                    detail = (
                        f"{failed_count} hunk(s) failed to apply"
                        if failed_count > 0
                        else "no hunks applied"
                    )
                    results.append(f"  {rel_path}: {detail}")
        # 非空状态文本被 executor 当 success=True 吞掉，写盘失败不可观测
        if applied_total == 0 or io_errors or failed_total > 0:
            reason = (
                "no hunks applied"
                if applied_total == 0 and failed_total == 0
                else ("I/O error" if io_errors else f"{failed_total} hunk(s) failed to apply")
            )
            raise NonRetryableToolError(f"apply_patch: {reason}\n" + "\n".join(results))
        return "\n".join(results)


def _apply_hunks(text: str, hunks_text: str) -> tuple[str, int, int]:
    """功能：应用全部 hunk，返回 (结果文本, 成功数, 失败数)——调用方据失败数
    显式报错（失败 hunk 不得被静默跳过造成假成功）。"""
    total = 0
    failed = 0
    for hunk in re.split(r"(?=^@@ )", hunks_text, flags=re.MULTILINE):
        hunk = hunk.strip()
        if not hunk or not _HUNK_HEADER.match(hunk):
            continue
        text, ok = _apply_hunk(text, hunk)
        if ok:
            total += 1
        else:
            failed += 1
    return text, total, failed


def _apply_hunk(text: str, hunk_text: str) -> tuple[str, bool]:
    """功能：应用单个 hunk，返回 (结果文本, 是否成功)。

    定位策略（与 `edit` 不同，是**位置锚定**而非全局唯一性）：在 hunk 头标明的起始行
    附近 ±(`_HUNK_SLIDE_LIMIT`-1) 行内滑动，找第一处「减/上下文块按 10 级回退链命中」的
    位置（①：空白/缩进/引号/转义漂移均容忍）；替换跨度由**行数**决定（不依赖
    命中文本的字节长度，避免回退级别命中时切片错位）。新增行按文件自身行尾拼接（②）。
    """
    m = _HUNK_HEADER.match(hunk_text)
    start_idx = max(0, int(m.group(1)) - 1) if m else 0
    remove, add = _hunk_blocks(hunk_text)
    ending = detect_line_ending(text)

    if not remove:
        # 直接在 hunk 头标明的起始行插入（新增内容不依赖既有行匹配）
        if not add:
            return text, False
        insert_at = _line_offset(text, start_idx)
        block = _join_lines(add, ending)
        # 插入点不在行首（前一行无行尾符）时补一个行尾，防与上一行粘连
        if insert_at > 0 and text[insert_at - 1] not in ("\n", "\r"):
            block = ending + block
        return text[:insert_at] + block + text[insert_at:], True

    lines = text.splitlines(keepends=True)
    if not lines:
        return text, False
    remove_block = _join_lines(remove, ending)
    add_block = _join_lines(add, ending)
    for start in range(start_idx, start_idx + _HUNK_SLIDE_LIMIT):
        if start + len(remove) > len(lines):
            break
        candidate = "".join(lines[start : start + len(remove)])
        if probe_match(candidate, remove_block) is None:
            continue
        span_start = _line_offset(text, start)
        span_end = span_start + len(candidate)
        return text[:span_start] + add_block + text[span_end:], True
    return text, False
