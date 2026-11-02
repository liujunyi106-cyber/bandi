"""
契约族：敏感路径统一守卫（六-）。

背景：敏感路径判断分散——fs_tool 内联 `_is_sensitive`、apply_patch/file_edit
import 私有常量各自判断、glob/grep 完全无判断。读取密钥/敏感目录的路径能力
工具（glob/grep）可绕过敏感名单。

本契约钉的行为族（全部生产调用路径）：
- `tools/fs_tool.py::sensitive_path_reason`（唯一判断函数——共享单一来源）
- `tools/fs_tool.py`（read/write/list 等经 _is_sensitive）
- `tools/file_edit.py` / `tools/apply_patch.py`（编辑类）
- `tools/glob_tool.py` / `tools/grep_tool.py`（读类新增守卫）

断言「最终产物」：同一敏感路径（.ssh 目录 / .key 文件）在全部路径工具上
行为一致——拒绝且原因指向敏感路径；非敏感路径放行。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bandi.tools.fs_tool import sensitive_path_reason

_SENSITIVE_KEY = Path("C:/prod/server.pem")
_SENSITIVE_DIR = Path("C:/home/u/.ssh/id_rsa")
_PLAIN = Path("C:/proj/src/main.py")


def test_contract_sensitive_reason_uniform_semantics() -> None:
    """共享函数语义：敏感文件后缀/敏感目录命中给出原因，普通路径 None。"""
    assert sensitive_path_reason(_SENSITIVE_KEY) is not None, "pem 必须命中"
    assert sensitive_path_reason(_SENSITIVE_DIR) is not None, ".ssh 目录必须命中"
    assert sensitive_path_reason(_PLAIN) is None, "普通路径必须放行"
    assert sensitive_path_reason(Path("")) is None, "空路径无判断意义"


def test_contract_fs_tools_reject_sensitive_uniformly(tmp_path: Path) -> None:
    """read/write/glob/grep 对同一敏感路径行为一致（拒绝）。"""
    from bandi.tools.file_ops import FileReadTool
    from bandi.tools.glob_tool import GlobTool
    from bandi.tools.grep_tool import GrepTool

    key = tmp_path / "server.key"
    key.write_text("secret", encoding="utf-8")

    from bandi.tools.base import ToolPermissionContext

    ctx = ToolPermissionContext(
        user_id="u", role="r", mode="execute", extra={"project_root": str(tmp_path)}
    )

    read_tool = FileReadTool()
    with pytest.raises((ValueError, PermissionError), match="sensitive"):
        asyncio.run(read_tool.execute({"path": str(key)}, context=ctx))

    glob_tool = GlobTool()
    glob_out = asyncio.run(
        glob_tool.execute({"path": str(key.parent), "pattern": "*.key"}, context=ctx)
    )
    assert "server.key" not in glob_out, "glob 结果必须剔除敏感文件（读也敏感）"

    grep_tool = GrepTool()
    grep_out = asyncio.run(
        grep_tool.execute(
            {"path": str(key.parent), "pattern": "secret", "include": "*.key"},
            context=ctx,
        )
    )
    assert "server.key" not in grep_out, "grep 结果必须剔除敏感文件（读也敏感）"
