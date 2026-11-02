# 功能：资料库文件落盘——原子写、sha256、单份 .bak、mtime 冲突检测。

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from pathlib import Path

BACKUP_INTERVAL_SEC = 600
ASSET_MAX_BYTES = 20 * 1024 * 1024
ASSET_NAME_RE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


class NodeFileStore:
    # 功能：按 node_id 分目录存放源文件；所有写入先落临时文件再原子替换。
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def node_dir(self, node_id: str) -> Path:
        return self._root / node_id

    def path_for(self, node_id: str, name: str) -> Path:
        return self.node_dir(node_id) / name

    def write(self, node_id: str, name: str, data: bytes) -> tuple[Path, str, float]:
        # 功能：原子写入并返回 (路径, sha256, mtime)；同名覆盖前按节流写 .bak。
        target = self.path_for(node_id, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._maybe_backup(target)
        tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return target, hashlib.sha256(data).hexdigest(), target.stat().st_mtime

    def rename(self, node_id: str, old_name: str, new_name: str) -> None:
        # 功能：重命名节点目录内的源文件与 .bak，保持节点 id 不变。
        if old_name == new_name:
            return
        source = self.path_for(node_id, old_name)
        if source.exists():
            os.replace(source, self.path_for(node_id, new_name))
        old_backup = self.node_dir(node_id) / f"{old_name}.bak"
        if old_backup.exists():
            os.replace(old_backup, self.backup_path(node_id, new_name))

    def read(self, node_id: str, name: str) -> bytes:
        return self.path_for(node_id, name).read_bytes()

    def mtime(self, node_id: str, name: str) -> float:
        path = self.path_for(node_id, name)
        return path.stat().st_mtime if path.exists() else 0.0

    def backup_path(self, node_id: str, name: str) -> Path:
        return self.node_dir(node_id) / f"{name}.bak"

    def delete(self, node_id: str) -> None:
        shutil.rmtree(self.node_dir(node_id), ignore_errors=True)

    def save_asset(self, node_id: str, name: str, data: bytes) -> str:
        # 功能：保存块资产（图片/视频/附件）到 node/assets——返回清洗后的文件名。
        safe = safe_asset_name(name)
        directory = self.node_dir(node_id) / "assets"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / safe).write_bytes(data)
        return safe

    def asset_path(self, node_id: str, name: str) -> Path:
        # 功能：资产绝对路径——只允许单层文件名，拒绝路径穿越。
        return self.node_dir(node_id) / "assets" / safe_asset_name(name)

    def _maybe_backup(self, target: Path) -> None:
        # 功能：.bak 每 10 分钟最多写一次，避免自动保存把磁盘写放大。
        if not target.exists():
            return
        backup = target.with_name(f"{target.name}.bak")
        if backup.exists() and time.time() - backup.stat().st_mtime < BACKUP_INTERVAL_SEC:
            return
        backup.write_bytes(target.read_bytes())


def safe_asset_name(name: str) -> str:
    # 功能：资产名清洗——去目录成分/控制字符，空名回落 asset，限长 120。
    base = Path(str(name or "")).name.strip().replace("\\", "_").replace("/", "_")
    cleaned = ASSET_NAME_RE.sub("_", base).strip("._") or "asset"
    return cleaned[:120]
