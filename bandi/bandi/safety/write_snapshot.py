# 功能：高危写操作执行前目标文件快照——轻量兜底（改坏可还原），不承担防护职责
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from bandi.safety.tool_risk import RiskLevel, predict_risk_from_arguments

logger = logging.getLogger(__name__)

# 单批快照文件数上限（轻量兜底，防异常超大参数把快照变成全盘复制）
_MAX_FILES_PER_BATCH = 10
# apply_patch 文本中的目标文件头
_PATCH_FILE_HEADER = re.compile(r"^\*\*\* (?:Update|Add) File: (.+)$", re.MULTILINE)


def default_snapshot_root() -> Path:
    """功能：快照根目录单一来源（Path.home 拼接，不走 "~" 字面量展开）。"""
    return Path.home().joinpath(".bandi", "snapshots")


def snapshot_worthy(tool_name: str, arguments: dict[str, Any] | None, is_read_only: bool) -> bool:
    """功能：判定该调用是否值得快照——非只读且风险达 MEDIUM 及以上（参数关键词可升级）。"""
    if is_read_only:
        return False
    return predict_risk_from_arguments(tool_name, arguments) is not RiskLevel.LOW


def extract_target_paths(
    arguments: dict[str, Any] | None, base_dir: str | None = None
) -> list[Path]:
    """功能：从参数中提取「已存在」的本地文件目标——绝对/相对路径 + apply_patch 文件头；
    不存在的路径（新建文件）无需快照。"""
    if not arguments:
        return []
    texts: list[str] = []
    _collect_strings(arguments, texts)
    candidates: list[str] = []
    for text in texts:
        for line_match in _PATCH_FILE_HEADER.finditer(text):
            candidates.append(line_match.group(1).strip())
        candidate = _path_like(text)
        if candidate:
            candidates.append(candidate)
    base = Path(base_dir) if base_dir else Path.cwd()
    targets: list[Path] = []
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute():
            path = base / path
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved not in targets:
            targets.append(resolved)
        if len(targets) >= _MAX_FILES_PER_BATCH:
            break
    return targets


def create_snapshot(
    tool_name: str,
    target_files: list[Path],
    snapshot_root: Path | None = None,
) -> Path | None:
    """
    无可快照文件返回 None；失败只告警不抛出（快照绝不阻断执行）。"""
    existing = [f for f in target_files if f.is_file()]
    if not existing:
        return None
    root = snapshot_root or default_snapshot_root()
    batch = root / f"{time.strftime('%Y%m%d-%H%M%S')}-{tool_name}"
    try:
        batch.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        for target in existing:
            key = f"{len(files):04d}_{target.name}"
            shutil.copy2(target, batch / key)
            files[key] = str(target)
        manifest = {
            "tool": tool_name,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "files": files,
        }
        (batch / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return batch
    except OSError:
        logger.warning("write snapshot failed for %s", tool_name, exc_info=True)
        _cleanup_partial(batch)
        return None


def restore_snapshot(batch_dir: Path, overwrite: bool = True) -> list[Path]:
    """功能：按 manifest 把批次内文件复制回原路径，返回已还原路径列表。

    预留 API：快照创建已接高危写执行链（`tools/executor.py`）；还原的
    UI 入口 / 失败回滚链待需求后接入（登记于技术债务清单）。
    """
    manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    restored: list[Path] = []
    for key, original in manifest["files"].items():
        destination = Path(original)
        if destination.exists() and not overwrite:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(batch_dir / key, destination)
        restored.append(destination)
    return restored


def _collect_strings(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_strings(item, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_strings(item, out, depth + 1)


def _path_like(text: str) -> str | None:
    """功能：从字符串中识别本地路径形态——盘符路径、~ 展开、file:// URI；
    其余（普通命令文本/URL）不当作路径。"""
    if not text or len(text) > 4096:
        return None
    if len(text) > 1 and text[1] == ":" and text[0].isalpha():
        return text
    if text.startswith("~"):
        return str(Path.home() / text[2:] if text[:2] == "~/" else Path.home() / text[1:])
    if "://" in text:
        if not text.lower().startswith("file://"):
            return None
        from urllib.parse import unquote, urlparse

        path = unquote(urlparse(text).path or "")
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path or None
    if text.startswith(("./", ".\\", "../")):
        return text
    return None


def _cleanup_partial(batch: Path) -> None:
    try:
        if batch.exists() and not any(batch.iterdir()):
            batch.rmdir()
    except OSError:
        pass
