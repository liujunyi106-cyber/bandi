from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from bandi.company._utils import path_within
from bandi.company.work_item_identity import projection_id_for_task

DATA_ACQUISITION_PROJECTION_ID = "data_acquisition"
ACQUISITION_SPECIALIST_ROLE_ID = "acquisition_specialist"
ACQUISITION_SHELL_PREFIXES = {"curl", "wget", "yt-dlp", "aria2c", "ffmpeg"}
MEDIA_BINARY_ASSET_KEYWORDS = (
    "video",
    "trailer",
    "footage",
    "clip",
    "素材",
    "片段",
    "audio",
    "music",
    "subtitle",
    "srt",
    "bilibili",
    "youtube",
    "mp4",
    "wav",
)
_SHELL_CONTROL_TOKENS = {"&&", "||", ";", "|", "&", ">", ">>", "<", "<<"}
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def task_projection_id(task: Any | None) -> str:
    if task is None:
        return ""
    return projection_id_for_task(task).lower()


def task_role_id(task: Any | None) -> str:
    if task is None:
        return ""
    metadata = dict(getattr(task, "metadata", {}) or {})
    assigned = str(getattr(task, "assignee_role", "") or "").strip().lower()
    if assigned:
        return assigned
    return str(metadata.get("work_item_role_id", "") or "").strip().lower()


def is_acquisition_specialist_projection(
    *,
    task: Any | None = None,
    projection_id: str = "",
    role_id: str = "",
) -> bool:
    resolved_projection = str(projection_id or task_projection_id(task) or "").strip().lower()
    resolved_role = str(role_id or task_role_id(task) or "").strip().lower()
    return (
        resolved_projection == DATA_ACQUISITION_PROJECTION_ID
        and resolved_role == ACQUISITION_SPECIALIST_ROLE_ID
    )


def workspace_root_for_task(task: Any | None) -> Path | None:
    if task is None:
        return None
    metadata = dict(getattr(task, "metadata", {}) or {})
    manifest = dict(metadata.get("workspace_manifest", {}) or {})
    root = str(manifest.get("root_path", "") or metadata.get("target_output_dir", "") or "").strip()
    if not root:
        return None
    try:
        return Path(root).resolve()
    except (OSError, ValueError):
        return None


def reserved_path_for_task(task: Any | None, key: str) -> Path | None:
    if task is None:
        return None
    manifest = dict(dict(getattr(task, "metadata", {}) or {}).get("workspace_manifest", {}) or {})
    reserved = dict(manifest.get("reserved_paths", {}) or {})
    raw = str(reserved.get(key, "") or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except (OSError, ValueError):
        return None


def _default_resolved_path(
    task: Any | None, reserved_key: str | None, file_name: str, relative_path: str
) -> str:
    if reserved_key:
        reserved = reserved_path_for_task(task, reserved_key)
        if reserved is not None:
            return str((reserved / file_name).resolve())
    root = workspace_root_for_task(task)
    if root is not None:
        return str((root / relative_path).resolve())
    return relative_path


def _normalize_item_text(value: Any) -> str:
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(value).strip()
    if isinstance(value, (list, tuple, set)):
        normalized = [_normalize_item_text(item) for item in value]
        return " ".join(item for item in normalized if item)
    return str(value or "").strip()


def _has_media_keyword(text: str) -> bool:
    """ASCII 关键词词边界匹配（video 不命中 videos），中文关键词子串匹配。"""
    lower = text.lower()
    for keyword in MEDIA_BINARY_ASSET_KEYWORDS:
        if keyword.isascii():
            if re.search(rf"\b{re.escape(keyword)}\b", lower):
                return True
        elif keyword in lower:
            return True
    return False


def load_json_file(path: str) -> object | None:
    raw = str(path or "").strip()
    if not raw:
        return None
    try:
        return json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _manifest_entries(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("entries", "items", "downloads", "download_manifest"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [dict(item) for item in nested if isinstance(item, dict)]
    return []


def load_download_manifest_entries(path: str) -> list[dict[str, Any]]:
    return _manifest_entries(load_json_file(path))


def _path_within(candidate: Path, root: Path) -> bool:
    return path_within(candidate, root)


def is_acquisition_shell_command(command: str) -> bool:
    tokens = command.strip().split()
    if not tokens:
        return False
    return tokens[0] in ACQUISITION_SHELL_PREFIXES


def is_download_command(command: str) -> bool:
    if not is_acquisition_shell_command(command):
        return False
    for arg in command.split():
        if _HTTP_URL_RE.match(arg.strip("\"'")):
            return True
    return False


# 功能：判定命令是否为媒体采集（下载目标为媒体文件）
def is_media_acquisition(command: str) -> bool:
    if not is_download_command(command):
        return False
    return _has_media_keyword(command)


def is_projection_scoped_acquisition(command: str, projection_id: str) -> bool:
    """仅判断命令是否为指定 projection 的采集命令（不含完整命令+scope 检查）。

    与 :func:`is_projection_scoped_acquisition_shell_command` 分工：前者只查
    projection_id 匹配，后者做完整命令段拆分 + scope 判定。
    """
    if not is_acquisition_shell_command(command):
        return False
    return projection_id.lower() == DATA_ACQUISITION_PROJECTION_ID


def _is_fd_redirect(tokens: list[str], index: int) -> bool:
    """判断 token 是否属于 fd 重定向（2>/&>/>&）的一部分，是则不触发分段。

    shlex 会把 `2>` 拆成 `2`,`>`，把 `&>`/`>&` 合并为一个 token；这里统一
    识别这三种形态，重定向目标保留在同一段内以便纳入越界检查。
    """
    token = tokens[index]
    if token in {">", ">>"}:
        prev = tokens[index - 1] if index > 0 else ""
        nxt = tokens[index + 1] if index + 1 < len(tokens) else ""
        return prev.isdigit() or prev == "&" or nxt == "&"
    if token == "&":
        prev = tokens[index - 1] if index > 0 else ""
        nxt = tokens[index + 1] if index + 1 < len(tokens) else ""
        return prev == ">" or nxt == ">"
    return False


def split_shell_command_segments(command: str) -> list[list[str]]:
    text = str(command or "").replace("\r\n", "\n").replace("\n", " ; ").strip()
    if not text:
        return []
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
    segments: list[list[str]] = []
    current: list[str] = []
    for index, token in enumerate(tokens):
        if token in _SHELL_CONTROL_TOKENS and not _is_fd_redirect(tokens, index):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _resolve_output_path(
    token: str, *, working_directory: str, target_output_dir: str
) -> Path | None:
    raw = str(token or "").strip()
    if not raw or raw == "-":
        return None
    try:
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate.resolve()
        base = Path(working_directory or target_output_dir or ".").resolve()
        return (base / candidate).resolve()
    except (OSError, ValueError):
        return None


def _extract_redirect_targets(
    tokens: list[str], *, working_directory: str, target_output_dir: str
) -> list[Path]:
    """提取重定向写目标（> / >> / 2> / &> / >&），供越界检查。

    纯 fd 复制（2>&1 / >&1）目标为数字，跳过；`-` 开头视为选项跳过。
    """
    targets: list[Path] = []
    for index, token in enumerate(tokens):
        if token not in {">", ">>", ">&", "&>", "&>>"}:
            continue
        nxt = index + 1
        if nxt < len(tokens) and tokens[nxt] == "&":
            nxt += 1
        if nxt >= len(tokens):
            continue
        target = tokens[nxt]
        if target.isdigit() or target.startswith("-"):
            continue
        resolved = _resolve_output_path(
            target,
            working_directory=working_directory,
            target_output_dir=target_output_dir,
        )
        if resolved is not None:
            targets.append(resolved)
    return targets


def _extract_output_targets(
    prefix: str,
    tokens: list[str],
    *,
    working_directory: str,
    target_output_dir: str,
) -> list[Path]:
    targets: list[Path] = []

    def _append(token: str) -> None:
        resolved = _resolve_output_path(
            token,
            working_directory=working_directory,
            target_output_dir=target_output_dir,
        )
        if resolved is not None:
            targets.append(resolved)

    if prefix == "curl":
        output_dir = ""
        for index, token in enumerate(tokens[:-1]):
            if token == "--output-dir":
                output_dir = tokens[index + 1]
            elif token in {"-o", "--output"}:
                _append(
                    str(Path(output_dir) / tokens[index + 1]) if output_dir else tokens[index + 1]
                )
            elif token in {"-O", "--remote-name"}:
                for url in tokens[index + 1 :]:
                    if _HTTP_URL_RE.match(url):
                        target_dir = output_dir or working_directory or target_output_dir
                        _append(str(Path(target_dir) / Path(url).name))
        if output_dir:
            _append(output_dir)
        if not targets:
            _append(working_directory or target_output_dir)
        return targets

    if prefix == "wget":
        directory_prefix = ""
        file_name = ""
        for index, token in enumerate(tokens[:-1]):
            if token in {"-O", "--output-document"}:
                file_name = tokens[index + 1]
            elif token in {"-P", "--directory-prefix"}:
                directory_prefix = tokens[index + 1]
        if file_name:
            _append(str(Path(directory_prefix) / file_name) if directory_prefix else file_name)
        elif directory_prefix:
            _append(directory_prefix)
        else:
            _append(working_directory or target_output_dir)
        return targets

    if prefix == "yt-dlp":
        directory_prefix = ""
        output_template = ""
        for index, token in enumerate(tokens[:-1]):
            if token in {"-o", "--output"}:
                output_template = tokens[index + 1]
            elif token in {"-P", "--paths"}:
                raw_paths = str(tokens[index + 1] or "")
                if re.match(r"^[A-Za-z]:[\\/]", raw_paths):
                    directory_prefix = raw_paths
                elif ":" in raw_paths:
                    directory_prefix = raw_paths.split(":", 1)[1]
                else:
                    directory_prefix = raw_paths
        if output_template:
            _append(
                str(Path(directory_prefix) / output_template)
                if directory_prefix
                else output_template
            )
        elif directory_prefix:
            _append(directory_prefix)
        else:
            _append(working_directory or target_output_dir)
        return targets

    if prefix == "aria2c":
        lower_tokens = [token.lower() for token in tokens]
        directory_prefix = ""
        file_name = ""
        for index, token in enumerate(lower_tokens[:-1]):
            if token in {"-d", "--dir"}:
                directory_prefix = tokens[index + 1]
            elif token in {"-o", "--out"}:
                file_name = tokens[index + 1]
        if file_name:
            _append(str(Path(directory_prefix) / file_name) if directory_prefix else file_name)
        elif directory_prefix:
            _append(directory_prefix)
        else:
            _append(working_directory or target_output_dir)
        return targets

    if prefix == "ffmpeg":
        last_input_index = 0
        input_positions: set[int] = set()
        for index, token in enumerate(tokens):
            if token == "-i" and index + 1 < len(tokens):
                last_input_index = index + 1
                input_positions.add(index + 1)
        for index, token in enumerate(tokens[last_input_index:], start=last_input_index):
            if index in input_positions:
                continue
            if token.startswith("-") or _HTTP_URL_RE.match(token):
                continue
            _append(token)
        if not targets:
            _append(working_directory or target_output_dir)
        return targets

    return targets


def is_projection_scoped_acquisition_shell_command(
    *,
    command: str,
    task: Any | None = None,
    projection_id: str = "",
    role_id: str = "",
    working_directory: str = "",
    target_output_dir: str = "",
) -> bool:
    if not is_acquisition_specialist_projection(
        task=task, projection_id=projection_id, role_id=role_id
    ):
        return False
    root = str(target_output_dir or (workspace_root_for_task(task) or "")).strip()
    if not root:
        return False
    try:
        workspace_root = Path(root).resolve()
    except (OSError, ValueError):
        return False
    segments = split_shell_command_segments(command)
    if len(segments) != 1:
        return False
    tokens = segments[0]
    if not tokens:
        return False
    prefix = str(tokens[0] or "").strip().lower()
    if prefix not in ACQUISITION_SHELL_PREFIXES:
        return False
    urls = [token for token in tokens if "://" in token]
    if urls and any(not _HTTP_URL_RE.match(url) for url in urls):
        return False
    targets = _extract_output_targets(
        prefix,
        tokens,
        working_directory=working_directory,
        target_output_dir=str(workspace_root),
    )
    targets.extend(
        _extract_redirect_targets(
            tokens,
            working_directory=working_directory,
            target_output_dir=str(workspace_root),
        )
    )
    if not targets:
        try:
            cwd = Path(working_directory or workspace_root).resolve()
        except (OSError, ValueError):
            return False
        return _path_within(cwd, workspace_root)
    return all(_path_within(target, workspace_root) for target in targets)
