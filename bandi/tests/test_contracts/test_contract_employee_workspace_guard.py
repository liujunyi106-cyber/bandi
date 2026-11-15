# 功能：员工工作区免审批不得被变量路径/裸敏感文件/进程启动/递归删除绕过
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.tools.executor import ToolExecutor


@pytest.mark.parametrize(
    "command",
    [
        r"Set-Content -Path $env:APPDATA\evil.txt -Value x",
        r"Out-File -FilePath $HOME\.bandi\config.yaml",
        "Get-Content .env",
        "New-Item -ItemType File -Path id_rsa",
        "Start-Process calc",
        "Get-ChildItem -Recurse . | Remove-Item",
        "& Remove-Item -Recurse -Force .",
    ],
)
def test_workspace_auto_allow_rejects_dangerous_commands(tmp_path: Path, command: str) -> None:
    assert ToolExecutor._workspace_bash_allowed(None, {"command": command}, str(tmp_path)) is False


def test_workspace_auto_allow_keeps_safe_read(tmp_path: Path) -> None:
    assert (
        ToolExecutor._workspace_bash_allowed(
            None, {"command": "Get-Content README.md"}, str(tmp_path)
        )
        is True
    )
