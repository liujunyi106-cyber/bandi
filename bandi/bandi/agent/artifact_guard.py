# 功能：产物守卫——零落盘假完成兜底。完成态校验声称产物可读回且落在
# project_root 内；缺失产出演化为校正指令，驱动补齐轮而非静默通过。
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def _norm(root: Path, rel: str) -> Path:
    return (root / rel).resolve()


def correction_prompt(missing: list[str]) -> str:
    # 功能：生成校正轮指令——点名缺失产物并要求用 write 补齐
    quoted = ", ".join(f"「{name}」" for name in missing)
    return (
        f"你声称任务已经完成，但以下产物在项目目录中缺失：{quoted}。"
        "请务必使用 write 工具补全这些文件，补全后逐一核对可读回，再简短汇报。"
    )


class ArtifactGuard:
    # 功能：以 project_root 为沙箱基准的产物可读回守卫
    def __init__(self, project_root: str | Path) -> None:
        self._root = Path(project_root).resolve()

    def is_within(self, abs_path: str | Path) -> bool:
        # 功能：绝对路径（含符号链接解析后）是否位于项目根内
        candidate = Path(abs_path).resolve()
        return candidate == self._root or self._root in candidate.parents

    def missing(self, expected: Sequence[str]) -> list[str]:
        # 功能：返回不可读回或越界（逃逸）的产物相对路径列表
        return [rel for rel in expected if not self._is_safe_artifact(rel)]

    def _is_safe_artifact(self, rel: str) -> bool:
        resolved = _norm(self._root, rel)
        return self.is_within(resolved) and resolved.is_file()
