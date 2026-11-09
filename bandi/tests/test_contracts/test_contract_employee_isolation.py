# 功能：员工执行隔离契约——每员工独立 workspace + 独立浏览器 profile。
from __future__ import annotations

from pathlib import Path

from bandi.tools.fs_tool import employee_workspace_root
from bandi.tools.web.browser_pool import browser_profile_dir


def test_employee_workspace_is_per_employee_under_project(tmp_path: Path) -> None:
    """契约：员工工作区落在项目根下 .employees/<employee_id>，身份段被清洗。"""
    ws_a = employee_workspace_root(str(tmp_path), "proj-1", "emp::42")
    ws_b = employee_workspace_root(str(tmp_path), "proj-1", "emp::43")

    assert ws_a is not None and ws_b is not None
    assert ws_a == (tmp_path / ".employees" / "emp__42").resolve()
    assert ws_a.is_dir() and ws_b.is_dir()
    assert ws_a != ws_b, "不同员工必须落到不同工作区"


def test_employee_workspace_without_project_uses_workspace_root(
    tmp_path: Path, monkeypatch
) -> None:
    """契约：无项目根时落 workspace_root/<project_id>/<employee_id>。"""
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    ws = employee_workspace_root(None, "p1", "e1")

    assert ws == (tmp_path / "ws" / "p1" / "e1").resolve()
    assert ws.is_dir()


def test_employee_workspace_rejects_empty_identity() -> None:
    """破坏者：无 employee_id 不得凭空造目录（返回 None 回落既有行为）。"""
    assert employee_workspace_root("/tmp", "p1", "") is None
    assert employee_workspace_root("/tmp", "p1", "..") is None


def test_browser_profile_dir_is_per_employee(tmp_path: Path) -> None:
    """契约：员工浏览器 profile 落各自工作区；非员工会话返回 None（临时会话）。"""
    extra_a = {"employee_id": "e1", "employee_workspace": str(tmp_path / "ws-a")}
    extra_b = {"employee_id": "e2", "employee_workspace": str(tmp_path / "ws-b")}

    dir_a = browser_profile_dir(extra_a)
    dir_b = browser_profile_dir(extra_b)

    assert dir_a is not None and dir_b is not None
    assert dir_a != dir_b, "不同员工必须使用不同浏览器 profile"
    assert Path(dir_a).is_dir() and Path(dir_b).is_dir()
    # 非员工（个人助理）保持临时会话
    assert browser_profile_dir({"employee_id": "", "employee_workspace": str(tmp_path)}) is None
    assert browser_profile_dir({}) is None


def test_employee_read_resolves_relative_to_employee_workspace(tmp_path: Path) -> None:
    """契约：read/write 与 bash 同基准——相对路径先落员工工作区，不落全局 workspace。"""
    import asyncio

    from bandi.tools.base import ToolPermissionContext
    from bandi.tools.file_ops import FileReadTool, FileWriteTool

    employee_ws = tmp_path / "employee"
    employee_ws.mkdir()
    (employee_ws / "MEMORY.md").write_text("employee-memory", encoding="utf-8")
    context = ToolPermissionContext(
        extra={"employee_workspace": str(employee_ws), "project_root": str(tmp_path)}
    )
    read = asyncio.run(FileReadTool().execute({"path": "MEMORY.md"}, context))
    assert "employee-memory" in read
    asyncio.run(FileWriteTool().execute({"path": "out.txt", "content": "ok"}, context))
    assert (employee_ws / "out.txt").read_text(encoding="utf-8") == "ok"
