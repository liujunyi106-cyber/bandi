# 功能：FsTool — 文件系统工具统一基类（沙箱校验 + 敏感路径检测 + 编码检测 + 行号渲染）

from __future__ import annotations

import asyncio
import codecs
import os
from pathlib import Path
from typing import Any

from bandi.tools.base import BaseTool, ToolPermissionContext
from bandi.tools.edit_match import levenshtein_distance

_SENSITIVE_PATHS: list[str] = [
    ".env",
    ".git",
    ".ssh",
    ".gnupg",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "credentials",
    "secrets",
    ".gitconfig",
    ".netrc",
    ".npmrc",
]

_SENSITIVE_EXTENSIONS: tuple[str, ...] = (".pem", ".key", ".pfx", ".p12", ".keystore")

_SENSITIVE_DIRS_LOWER: set[str] = {p.lower() for p in _SENSITIVE_PATHS}

_CONTROL_PLANE_FILES: frozenset[str] = frozenset(
    {
        "mcp.json",
        "approvals.json",
        "llm_profiles.json",
        "models_cache.json",
        "seen_memory.json",
        "preferences.md",
    }
)
_CONTROL_PLANE_DIRS: frozenset[str] = frozenset(
    {
        "agents",
        "company",
        "data",
        "logs",
        "memory",
        "skills",
        "snapshots",
        "file-history",
        "idempotency",
        "tmp",
    }
)


def control_plane_path_reason(target: Path) -> str | None:
    # 功能：拒绝工具读写应用控制面/数据面（~/.bandi 下，workspace 除外）。
    try:
        root = (Path.home() / ".bandi").resolve()
        relative = target.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    if not relative.parts:
        return "control-plane root rejected"
    if relative.parts[0] == "workspace":
        return None
    top = relative.parts[0]
    if top in _CONTROL_PLANE_DIRS:
        return f"control-plane path rejected: {top}"
    if len(relative.parts) == 1:
        name = relative.name.lower()
        if name in _CONTROL_PLANE_FILES or name.startswith("config."):
            return f"control-plane file rejected: {name}"
    return None


def sensitive_path_reason(target: Path) -> str | None:
    # 功能：敏感路径唯一判断（六-）——全部路径工具共享此函数
    # 命中返回拒绝原因（含 sensitive 字样），未命中返回 None
    if not str(target):
        return None
    control_reason = control_plane_path_reason(target)
    if control_reason:
        return control_reason
    for part in target.parts:
        if part.lower() in _SENSITIVE_DIRS_LOWER:
            return f"sensitive path rejected: {part}"
    if target.suffix.lower() in _SENSITIVE_EXTENSIONS:
        return f"sensitive file type rejected: {target.suffix}"
    return None


_MAX_FILE_SIZE: int = 10 * 1024 * 1024

_BINARY_PEEK_BYTES: int = 8000

_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".zip",
        ".tar",
        ".gz",
        ".exe",
        ".dll",
        ".so",
        ".class",
        ".jar",
        ".war",
        ".7z",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        ".bin",
        ".dat",
        ".obj",
        ".o",
        ".a",
        ".lib",
        ".wasm",
        ".pyc",
        ".pyo",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".ico",
        ".pdf",
    }
)


def workspace_root() -> Path:
    # 功能：返回工作区根目录（优先 BANDI_WORKSPACE_ROOT，默认 ~/.bandi/workspace）——
    # 目录创建收敛到本函数（fs 工具族 + bash/collab/git 的唯一来源），失败不抛。
    root = Path(os.environ.get("BANDI_WORKSPACE_ROOT") or Path.home() / ".bandi" / "workspace")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:  # 只读环境等：留待调用方按「路径不存在」报错
        pass
    return root


_WORKSPACE_ROOT: Path = workspace_root()


def _looks_binary(raw: bytes, path: str | Path | None = None) -> bool:
    # 功能：二进制判定——扩展名黑名单 + NUL 字节 + 不可打印占比 >30%（样本 8000B）
    if path is not None and Path(path).suffix.lower() in _BINARY_EXTENSIONS:
        return True
    sample = raw[:_BINARY_PEEK_BYTES]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    non_printable = sum(1 for byte in sample if byte < 9 or 13 < byte < 32)
    return non_printable / len(sample) > 0.3


def user_scope_roots() -> list[str]:
    """功能：用户日常可访问的目录根——应用数据目录 ~/.bandi（系统提示引导的偏好/
    记忆文件落在这里）与桌面/文档/下载。

    放行这些根，使「桌面上有什么」「我的偏好是什么」这类最高频意图在普通会话与
    项目会话中都可用；系统目录（Windows/Program Files 等）仍不放行。
    env BANDI_FS_USER_ROOTS（os.pathsep 分隔）可追加自定义根——桌面端装到自定义
    盘符、或OneDrive 重定向桌面时用它兜底。
    """
    roots: list[str] = []
    try:
        home = Path.home()
    except (OSError, RuntimeError):  # 无家目录环境（服务账号）——只留 env 追加根
        home = None
    if home is not None:
        for candidate in (
            home / ".bandi",
            home / "Desktop",
            home / "Documents",
            home / "Downloads",
        ):
            if candidate.is_dir():
                roots.append(str(candidate.resolve()))
    for entry in (os.environ.get("BANDI_FS_USER_ROOTS") or "").split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        try:
            roots.append(str(Path(entry).expanduser().resolve()))
        except (OSError, ValueError):
            continue
    return roots


def resolve_within_workspace(
    path_str: str,
    extra_roots: list[str | Path] | None = None,
    base: str | Path | None = None,
    role_fs_scopes: list[str] | None = None,
    scope_base: str | Path | None = None,
) -> Path:
    """
    extra_roots：额外放行的根目录（如项目根，允许 agent Edit 项目内 .bandi/memory/）。
    相对路径必须落在项目内而非全局 workspace；缺省回落 _WORKSPACE_ROOT）。
    role_fs_scopes（.6）：角色级目录白名单——非空时目标必须同时落在任一
    scope 根内（相对 scope 以 scope_base 为基准，缺省 workspace），把角色信息
    边界从工具面下沉到落盘路径；空/None 回落现行为。"""
    raw = Path(path_str).expanduser()  # `~` 须先展开——否则被当字面目录名拼进 workspace
    root = _WORKSPACE_ROOT.resolve()
    base_dir = (Path(base).resolve() if base else root) if not raw.is_absolute() else None
    # symlink 必须在 resolve() **前**对未解析路径判定——resolve 跟随链接后
    unresolved = (base_dir or root) / raw if not raw.is_absolute() else raw
    target = unresolved.resolve()
    allowed = [root]
    for extra in [*(extra_roots or []), *user_scope_roots()]:
        try:
            allowed.append(Path(extra).resolve())
        except (OSError, ValueError):
            continue
    if not any(target.is_relative_to(a) for a in allowed):
        raise ValueError(f"path outside workspace: {path_str}")
    if role_fs_scopes and not _scope_allows(target, role_fs_scopes, scope_base):
        raise ValueError(f"path outside role fs scopes: {path_str}")
    if unresolved.is_symlink():
        raise ValueError(f"symlink not allowed: {path_str}")
    return target


def _scope_allows(target: Path, scopes: list[str], scope_base: str | Path | None) -> bool:
    """功能：目标是否落在任一角色 scope 根内——非法/空白 scope 条目跳过（fail-closed：
    无任何合法命中即拒绝；空白条目不得解析为基准根而静默放行整个沙箱）。"""
    base_dir = Path(scope_base).resolve() if scope_base else _WORKSPACE_ROOT.resolve()
    for entry in scopes:
        raw_entry = str(entry).strip()
        if not raw_entry:
            continue
        try:
            scope_path = Path(raw_entry)
            scoped_root = (
                (base_dir / scope_path).resolve()
                if not scope_path.is_absolute()
                else scope_path.resolve()
            )
        except (OSError, ValueError):
            continue
        if target.is_relative_to(scoped_root):
            return True
    return False


def project_extra_roots(context: Any) -> list[str] | None:
    """功能：从 ToolPermissionContext.extra 提取 project_root 沙箱附加根——
    未注入 context/project_root 时返回 None（回落全局 workspace）。"""
    extra = getattr(context, "extra", None) or {}
    project_root = extra.get("project_root")
    return [project_root] if project_root else None


def role_fs_scopes(context: Any) -> list[str] | None:
    """功能：从 ToolPermissionContext.extra 提取角色 fs_scopes（9.6）——
    未注入返回 None（无声明回落现行为）。"""
    extra = getattr(context, "extra", None) or {}
    scopes = extra.get("role_fs_scopes")
    if not scopes:
        return None
    return [str(s) for s in scopes]


def _safe_path_segment(value: str) -> str:
    """身份标识 → 安全路径段（防 `::`/`..`/分隔符注入穿越）。"""
    import re as _re

    text = _re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "").strip())
    return "" if text in (".", "..") else text


def employee_workspace_root(
    project_root: str | None, project_id: str, employee_id: str
) -> Path | None:
    """员工独立工作区——项目根下 `.employees/<employee_id>`。

    有项目根时落在项目内（协作/交接仍可访问项目文件），无项目根时落
    `workspace_root/<project_id>/<employee_id>`。返回创建好的绝对路径；
    身份缺失/建目录失败返回 None（回落既有 project_root 行为）。
    bash 默认 cwd / 浏览器 profile 以它为基准，实现员工间产物与登录态隔离。
    """
    emp = _safe_path_segment(employee_id)
    if not emp:
        return None
    pid = _safe_path_segment(project_id)
    if project_root:
        root = Path(project_root).expanduser().resolve() / ".employees" / emp
    else:
        base = workspace_root()
        root = (base / (pid or "default") / emp) if pid else (base / "employees" / emp)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return root


def project_base(context: Any) -> str | None:
    """功能：project_root 会话中相对路径的解析基准（BUG-J 补强）。"""
    roots = project_extra_roots(context)
    return roots[0] if roots else None


def similar_path_suggestions(target: str | Path, *, limit: int = 3) -> list[str]:
    # 功能：同目录相似文件名建议（basename 双向包含 + 编辑距离，≤limit 条）——
    path = Path(target)
    try:
        names = sorted(entry.name for entry in path.parent.iterdir() if entry.is_file())
    except OSError:
        return []
    lowered = path.name.lower()
    stem = path.stem.lower()
    if not stem:
        return []
    hits: list[str] = []
    for name in names:
        low = name.lower()
        if low == lowered:
            continue
        if stem in low or low in lowered:
            hits.append(name)
        elif levenshtein_distance(stem, Path(name).stem.lower()) <= max(1, len(stem) // 3):
            hits.append(name)
        if len(hits) >= limit:
            break
    return hits


class FsTool(BaseTool):  # 功能：文件系统工具基类 — 统一沙箱/敏感/编码/行号逻辑
    def _resolve_safe_path(
        self, path_str: str, context: ToolPermissionContext | None = None
    ) -> Path:
        # .6：角色 fs_scopes 窄化（无声明回落现行为）
        return resolve_within_workspace(
            path_str,
            extra_roots=project_extra_roots(context),
            base=project_base(context),
            role_fs_scopes=role_fs_scopes(context),
            scope_base=project_base(context),
        )

    def _is_sensitive(self, path: Path) -> bool:
        return sensitive_path_reason(path) is not None

    def _resolve_file(self, path_str: str, *, must_exist: bool = True) -> Path:
        if not path_str:
            raise ValueError("file_path is required")
        path = self._resolve_safe_path(path_str)
        if self._is_sensitive(path):
            raise ValueError(f"sensitive path rejected: {path_str}")
        if must_exist and not path.is_file():
            raise ValueError(f"file not found: {path_str}")
        return path

    def _resolve_output(self, path_str: str, context: ToolPermissionContext | None = None) -> Path:
        # 功能：project_root 会话敏感的路径解析（沙箱 + 敏感检测），带 context 透传
        if not path_str:
            raise ValueError("output path is required")
        path = self._resolve_safe_path(path_str, context)
        if self._is_sensitive(path):
            raise ValueError(f"sensitive path rejected: {path_str}")
        return path

    @staticmethod
    def looks_binary(raw: bytes, path: str | Path | None = None) -> bool:
        return _looks_binary(raw, path)

    @staticmethod
    def detect_encoding(raw: bytes) -> str:
        # 中文）吞掉（解码恒"成功"），故 utf-16 仅在 BOM 命中时返回
        if raw.startswith(codecs.BOM_UTF8):
            return "utf-8-sig"
        if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
            return "utf-16"
        for enc in ("utf-8", "gb18030", "cp1252"):
            try:
                raw.decode(enc)
                return enc
            except UnicodeDecodeError:
                continue
        return "utf-8"

    async def _read_bytes(self, path: Path) -> bytes:
        try:
            if path.stat().st_size > _MAX_FILE_SIZE:
                raise ValueError(f"file exceeds max size ({_MAX_FILE_SIZE} bytes)")
        except OSError as e:
            raise ValueError(f"cannot stat file: {path}") from e
        return await asyncio.to_thread(path.read_bytes)
