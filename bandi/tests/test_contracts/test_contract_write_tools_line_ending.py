#
# 背景（技术债 ）：一批写工具以默认文本模式落盘（`open("w")` / `Path.write_text`）
# 在 Windows 上会把内容里的 "\n" 静默改写成 "\r\n" ⇒ 用户拿到与输入不一致的文件
# （Git 上表现为整文件行尾变化：diff 噪声 + 潜在冲突）。
#
# 覆盖两层：
# ① **行为**：`write` 工具（覆盖 / 追加 / 自带 CRLF）落盘后字节必须与输入一致；
# ② **族级源码守卫**：这 6 个模块里**任何**文本写站点都必须带 `newline=""`——
# 新增写站点若漏掉，本契约变红（防「修了这次、下次又漏」）。
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from bandi.tools.base import ToolPermissionContext
from bandi.tools.file_ops import FileWriteTool

_REPO_ROOT = Path(__file__).resolve().parents[2]

# 债单点名的 6 个写工具。新增同类模块时应一并纳入本表。
_WRITE_MODULES = (
    "bandi/tools/file_ops.py",
    "bandi/tools/util/folder_tool.py",
    "bandi/tools/doc/markdown_processor.py",
    "bandi/tools/doc/html_processor.py",
    "bandi/tools/doc/config_file_tool.py",
    "bandi/tools/doc/text_processor.py",
)

_TEXT_MODE_OPEN = re.compile(r"\.open\(\s*(['\"])([rwxa]\+?b?)\1")
# 真正的「文件写」调用：排除 `_write_text(`（自建 helper）与 `write_text(` 的定义行
_WRITE_TEXT_CALL = re.compile(r"(?<![_\w])write_text\(")


# ── ① 行为：write 工具落盘字节保真 ─────────────────────────────────────────


@pytest.fixture()
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # 隔离工作区（与既有 c3_tools 契约同口径：工作区根 + HOME 双 patch）
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", workspace)
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return workspace


def _write(ws: Path, **params: object) -> None:
    payload = {"path": str(ws / "f.txt"), **params}
    asyncio.run(FileWriteTool().execute(payload, ToolPermissionContext()))


def test_write_tool_overwrite_keeps_lf(ws: Path) -> None:
    """契约①：LF 内容经 `write`（覆盖）落盘后仍是 LF。"""
    _write(ws, content="a\nb\n")
    assert (ws / "f.txt").read_bytes() == b"a\nb\n"


def test_write_tool_append_keeps_lf(ws: Path) -> None:
    """契约①：追加模式同样不得转换行尾（两段 LF 拼起来还是 LF）。"""
    _write(ws, content="a\n", mode="write")
    _write(ws, content="b\n", mode="append")
    assert (ws / "f.txt").read_bytes() == b"a\nb\n"


def test_write_tool_crlf_input_preserved(ws: Path) -> None:
    """契约①（反向）：输入自带 CRLF 时原样保留，**不得**二次转换。"""
    _write(ws, content="a\r\nb\r\n")
    assert (ws / "f.txt").read_bytes() == b"a\r\nb\r\n"


# ── ② 族级源码守卫：所有文本写站点必须 newline="" ──────────────────────────


def _scan_text_write_sites(root: Path, modules: tuple[str, ...]) -> list[str]:
    """功能：扫出「文本写落盘但未显式 newline=""」的站点（返回可读清单）。"""
    offenders: list[str] = []
    for rel in modules:
        for lineno, line in enumerate((root / rel).read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]  # 去注释，避免注释里的示例文本误判
            opened = _TEXT_MODE_OPEN.search(code)
            if (
                opened is not None
                and any(ch in opened.group(2) for ch in "wax")
                and "b" not in opened.group(2)
                and "newline=" not in code
            ):
                offenders.append(f"{rel}:{lineno} open({opened.group(2)!r}) 缺 newline=")
            if _WRITE_TEXT_CALL.search(code) and "newline=" not in code:
                offenders.append(f"{rel}:{lineno} write_text 缺 newline=")
    return offenders


def test_no_text_write_site_converts_line_endings() -> None:
    """契约②（族级）：6 个写工具的**每一处**文本写落盘都必须显式 `newline=""`。

    为什么用源码扫描而非逐个行为用例：这些工具的写入口分散在十几个操作分支里
    （`text_processor` 一个文件就有 7 处），逐个构造调用易漏；源码守卫覆盖**全部**
    现有站点，且**新增站点漏写即变红**——这正是"修一处漏一处"的防复发手段。
    """
    offenders = _scan_text_write_sites(_REPO_ROOT, _WRITE_MODULES)
    assert not offenders, (
        "文本写落盘必须显式 newline=''（否则 LF 被静默换成平台行尾）：\n" + "\n".join(offenders)
    )


def test_guard_detects_missing_newline(tmp_path: Path) -> None:
    """契约②（破坏者）：把守卫指向一份含违规站点的样本，必须能抓出来。

    没有这条，上面的扫描可能因「正则写错 / 路径写错」而**永远真空通过**（假绿）。
    """
    fake = tmp_path / "bandi" / "tools"
    fake.mkdir(parents=True)
    (fake / "file_ops.py").write_text(
        "def f(p):\n    p.write_text('x', encoding='utf-8')\n    with p.open('w') as h:\n        h.write('y')\n",
        encoding="utf-8",
        newline="",
    )
    offenders = _scan_text_write_sites(tmp_path, ("bandi/tools/file_ops.py",))
    assert len(offenders) == 2, (
        f"守卫必须抓出两条违规（write_text 与 open('w')），实际: {offenders}"
    )


def test_guard_accepts_compliant_site(tmp_path: Path) -> None:
    """契约②（反向）：合规站点（带 newline=""）不得被误报。"""
    fake = tmp_path / "bandi" / "tools"
    fake.mkdir(parents=True)
    (fake / "file_ops.py").write_text(
        "def f(p):\n    p.write_text('x', encoding='utf-8', newline='')\n"
        "    with p.open('w', newline='') as h:\n        h.write('y')\n",
        encoding="utf-8",
        newline="",
    )
    assert _scan_text_write_sites(tmp_path, ("bandi/tools/file_ops.py",)) == []


def test_write_tool_import_is_real() -> None:
    """契约②前提：守卫覆盖的模块表必须**真的**包含 write 工具（防表被写空/写错）。"""
    assert "bandi/tools/file_ops.py" in _WRITE_MODULES
    assert FileWriteTool.name == "write"
    assert (_REPO_ROOT / "bandi/tools/file_ops.py").is_file()
