# 功能：对话附件存储（上传落盘 + 读回）——项目附件目录
# `~/.bandi/company/projects/<pid>/uploads/`（company 缺省 `~/.bandi/company/uploads/`）；
# 落盘名 `<upload_id>__<安全原名>`；大小上限 fail-closed + 文件名安全化（防目录逃逸）。
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from bandi.core.exceptions import BandiError
from bandi.skills.path_guard import validate_path_segment

DEFAULT_ATTACHMENTS_BASE = "~/.bandi/company"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_FILENAME_CHARS = 120
_UNSAFE_NAME_RE = re.compile(r"[^\w.\-]+", re.UNICODE)


class AttachmentError(BandiError):
    """附件操作失败（参数非法 / 落盘读写失败）。"""


class AttachmentNotFoundError(AttachmentError):
    """附件不存在。"""


class AttachmentTooLargeError(AttachmentError):
    """附件超过大小上限。"""


def _safe_filename(name: str) -> str:
    """功能：原始文件名安全化——去目录成分 + 白名单字符（含中文），空回退 attachment。"""
    base = str(name or "").replace("\\", "/").split("/")[-1].strip()
    cleaned = _UNSAFE_NAME_RE.sub("_", base).strip("._") or "attachment"
    return cleaned[:MAX_FILENAME_CHARS]


@dataclass(frozen=True)
class AttachmentRecord:
    """功能：附件落盘记录（upload_id 为前端引用 id）。"""

    upload_id: str
    name: str
    size: int
    project_id: str
    path: str


class AttachmentStore:
    """功能：附件本地库（项目附件目录 + 原子写 + 大小上限 + 文件名安全化）。"""

    def __init__(self, base_dir: str = DEFAULT_ATTACHMENTS_BASE) -> None:
        self.base_dir = base_dir

    def _base(self) -> Path:
        return Path(self.base_dir).expanduser()

    def uploads_dir(self, project_id: str = "company") -> Path:
        """功能：项目附件目录（company = 全局附件目录；其余 = projects/<pid>/uploads）。"""
        pid = str(project_id or "company").strip() or "company"
        if pid == "company":
            return self._base() / "uploads"
        safe_pid = validate_path_segment(pid, label="project_id", error_cls=AttachmentError)
        return self._base() / "projects" / safe_pid / "uploads"

    def save(self, data: bytes, *, filename: str, project_id: str = "company") -> AttachmentRecord:
        """功能：落盘附件（tmp + replace 原子写）；超过 MAX_UPLOAD_BYTES 显式拒绝。"""
        payload = bytes(data or b"")
        if len(payload) > MAX_UPLOAD_BYTES:
            raise AttachmentTooLargeError(
                f"附件超过大小上限（{MAX_UPLOAD_BYTES} 字节）: {len(payload)}"
            )
        directory = self.uploads_dir(project_id)
        upload_id = uuid.uuid4().hex[:12]
        safe_name = _safe_filename(filename)
        target = directory / f"{upload_id}__{safe_name}"
        tmp = directory / f".{upload_id}.tmp"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(payload)
            tmp.replace(target)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise AttachmentError(f"附件落盘失败: {target}: {exc}") from exc
        return AttachmentRecord(
            upload_id=upload_id,
            name=safe_name,
            size=len(payload),
            project_id=str(project_id or "company").strip() or "company",
            path=str(target),
        )

    def resolve(self, upload_id: str, *, project_id: str = "company") -> Path:
        """功能：upload_id → 落盘路径（无效 id / 不存在显式失败）。"""
        uid = validate_path_segment(upload_id, label="upload_id", error_cls=AttachmentError)
        directory = self.uploads_dir(project_id)
        matches = sorted(directory.glob(f"{uid}__*")) if directory.is_dir() else []
        if not matches:
            raise AttachmentNotFoundError(f"附件不存在: {uid}")
        return matches[0]

    def read(self, upload_id: str, *, project_id: str = "company") -> bytes:
        """功能：读回附件字节（不存在的显式失败）。"""
        return self.resolve(upload_id, project_id=project_id).read_bytes()

    @staticmethod
    def display_name(path: Path) -> str:
        """功能：从落盘名解析展示名（`<upload_id>__<name>` → `<name>`）。"""
        return path.name.split("__", 1)[-1]


# 功能：文本类附件内容提取
MAX_ATTACHMENT_TEXT_BYTES = 20 * 1024
TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".log",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".sql",
        ".html",
        ".htm",
        ".xml",
        ".sh",
        ".ps1",
        ".bat",
    }
)
_TRUNCATION_MARKER = "\n…（内容已截断）"


def extract_attachment_text(
    path: str | Path, *, max_bytes: int = MAX_ATTACHMENT_TEXT_BYTES
) -> str | None:
    """功能：文本类附件读取（扩展名白名单；超长截断带标记；非文本/读失败返回 None）。"""
    target = Path(path)
    if target.suffix.lower() not in TEXT_EXTENSIONS:
        return None
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    return f"{text}{_TRUNCATION_MARKER}" if truncated else text
