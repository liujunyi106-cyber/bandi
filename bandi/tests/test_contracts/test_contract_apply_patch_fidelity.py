#
# ① **无回退容错**：hunk 的减/上下文块要求逐行完全相等，空白/缩进/引号稍漂移即整体失败
# （弱模型产出的补丁尤其容易漂移）；
# ② **强制 LF**：新增行硬编码 "\n" ⇒ CRLF 文件被写出**混合行尾**；
# ③ **写回不保真**：`errors="replace"` 解码（坏字节静默变 U+FFFD 后写回 = 静默损坏）
#
# 本契约用**真实工具 + 真实文件系统**（不造假对象），每条修复都配**破坏者用例**
# （该失败必须仍失败、该保持原样必须原样），防止"为容错而过度宽松"。
from __future__ import annotations

import asyncio
import codecs
from pathlib import Path

import pytest

from bandi.tools.apply_patch import ApplyPatchTool
from bandi.tools.base import ToolPermissionContext
from bandi.tools.errors import NonRetryableToolError
from bandi.tools.file_edit import FileEditTool


@pytest.fixture()
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # 隔离工作区（与既有 c3_tools 契约同口径：工作区根 + HOME 双 patch）
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", workspace)
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return workspace


def _diff(name: str, hunk: str) -> str:
    return f"--- a/{name}\n+++ b/{name}\n{hunk}"


def _apply(name: str, hunk: str, context: ToolPermissionContext | None = None) -> str:
    return asyncio.run(ApplyPatchTool().execute({"patch": _diff(name, hunk)}, context))


# ── ① 回退容错（①）───────────────────────────────────────────────────


def test_fallback_tolerates_indentation_drift(ws: Path) -> None:
    """契约①：hunk 的减行与文件实际缩进不一致时，仍须按回退链命中并应用。

    修复前：逐行完全相等比较 ⇒ 整体 `hunk(s) failed to apply`。
    """
    (ws / "a.txt").write_bytes(b"def f():\n    return 1\n")
    result = _apply("a.txt", "@@ -1,2 +1,2 @@\n def f():\n-return 1\n+    return 2\n")
    assert (ws / "a.txt").read_bytes() == b"def f():\n    return 2\n"
    assert "applied" in result


def test_fallback_tolerates_quote_style_drift(ws: Path) -> None:
    """契约①：文件用弯引号、补丁用直引号时，须按引号归一命中（与 edit 同链）。"""
    (ws / "q.txt").write_bytes("say \u201chi\u201d now\n".encode())
    _apply("q.txt", '@@ -1,1 +1,1 @@\n-say "hi" now\n+say bye\n')
    assert (ws / "q.txt").read_bytes().decode() == "say bye\n"


def test_unrelated_content_still_rejected(ws: Path) -> None:
    """契约①（破坏者）：内容毫不相干时**必须仍失败**且文件原样——容错不等于乱改。"""
    original = b"totally different\n"
    (ws / "b.txt").write_bytes(original)
    with pytest.raises(NonRetryableToolError):
        _apply("b.txt", "@@ -1,1 +1,1 @@\n-nothing like this at all\n+new\n")
    assert (ws / "b.txt").read_bytes() == original


def test_slide_within_limit_hits_offset_target(ws: Path) -> None:
    """契约①：hunk 头行号有偏差时，在 ±(滑动上限-1) 行内滑动定位（容错真实行号漂移）。"""
    (ws / "s.txt").write_bytes(b"one\ntwo\nthree\nfour\nfive\n")
    _apply("s.txt", "@@ -1,1 +1,1 @@\n-three\n+THREE\n")
    assert (ws / "s.txt").read_bytes() == b"one\ntwo\nTHREE\nfour\nfive\n"


def test_slide_beyond_limit_rejected(ws: Path) -> None:
    """契约①（破坏者）：偏差超过滑动上限时**不得**去匹配远处的同内容——保留位置锚定语义。"""
    original = b"1\n2\n3\n4\n5\nfar\n"
    (ws / "s2.txt").write_bytes(original)
    with pytest.raises(NonRetryableToolError):
        _apply("s2.txt", "@@ -1,1 +1,1 @@\n-far\n+FAR\n")
    assert (ws / "s2.txt").read_bytes() == original


def test_pure_addition_hunk_still_works(ws: Path) -> None:
    """契约①（兼容）：纯新增 hunk（只有 + 行）仍按 hunk 头起始行插入。"""
    (ws / "add.txt").write_bytes(b"a\nb\n")
    _apply("add.txt", "@@ -3,0 +3,1 @@\n+inserted\n")
    assert (ws / "add.txt").read_bytes() == b"a\nb\ninserted\n"


def test_parity_with_edit_tool(ws: Path) -> None:
    """契约①：与 `edit`**同一套容错**——同一漂移输入下两工具结果必须一致。

    破坏者价值：若哪天有人把 apply_patch 的匹配改回「逐行完全相等」，本用例即变红。
    """
    drift = "def f():\n    return 1\n"
    a = ws / "p1.txt"
    b = ws / "p2.txt"
    a.write_bytes(drift.encode())
    b.write_bytes(drift.encode())
    _apply("p1.txt", "@@ -1,2 +1,2 @@\n def f():\n-return 1\n+    return 2\n")
    asyncio.run(
        FileEditTool().execute(
            {
                "file_path": str(b),
                "old_string": "def f():\nreturn 1",
                "new_string": "def f():\n    return 2",
            }
        )
    )
    assert a.read_bytes() == b.read_bytes() == b"def f():\n    return 2\n"


# ── ② 行尾保真（②）──────────────────────────────────────────────────


def test_crlf_file_stays_crlf(ws: Path) -> None:
    """契约②：CRLF 文件的补丁应用后**必须全 CRLF**（修复前新增行是 "\n" ⇒ 混合行尾）。"""
    (ws / "crlf.txt").write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    _apply("crlf.txt", "@@ -2,1 +2,1 @@\n-beta\n+BETA\n")
    raw = (ws / "crlf.txt").read_bytes()
    assert raw == b"alpha\r\nBETA\r\ngamma\r\n"
    assert raw.count(b"\n") == raw.count(b"\r\n"), "不得出现混合行尾"


def test_lf_file_stays_lf(ws: Path) -> None:
    """契约②（反向）：LF 文件不得被写成 CRLF。"""
    (ws / "lf.txt").write_bytes(b"a\nb\n")
    _apply("lf.txt", "@@ -2,1 +2,1 @@\n-b\n+B\n")
    assert (ws / "lf.txt").read_bytes() == b"a\nB\n"


# ── ③ 编码 / BOM 保真（③）────────────────────────────────────────────


def test_utf8_bom_preserved(ws: Path) -> None:
    """契约③：带 UTF-8 BOM 的文件写回后 BOM 必须还在（修复前 BOM 丢失）。"""
    (ws / "bom.txt").write_bytes(codecs.BOM_UTF8 + b"hello\n")
    _apply("bom.txt", "@@ -1,1 +1,1 @@\n-hello\n+hi\n")
    raw = (ws / "bom.txt").read_bytes()
    assert raw.startswith(codecs.BOM_UTF8), "BOM 必须原样保留"
    assert raw[len(codecs.BOM_UTF8) :] == b"hi\n"


def test_gbk_content_preserved(ws: Path) -> None:
    """契约③：GBK 文件按原编码写回，中文行不得被替换成 U+FFFD。"""
    (ws / "gbk.txt").write_bytes("你好世界\n".encode("gbk"))
    _apply("gbk.txt", "@@ -1,1 +1,1 @@\n-你好世界\n+再见世界\n")
    assert (ws / "gbk.txt").read_bytes().decode("gbk") == "再见世界\n"


# ── ④ 写前保护（③）─────────────────────────────────────────────────


def test_external_modification_rejected(ws: Path) -> None:
    """契约④：读后文件被外部改动（mtime 前进 + 内容哈希不符）⇒ 拒绝写入，文件原样。

    修复前：无此检测 ⇒ 直接用模型手里的旧内容覆盖（静默丢失他人的改动）。
    """
    f = ws / "ext.txt"
    f.write_bytes(b"hello\n")
    stale = {
        "k": {
            "resolved_path": str(f),
            "mtime": 1.0,
            "read_at": "T00:00:00Z",
            "content_sha256": "deadbeef",
            "partial": False,
        }
    }
    with pytest.raises(NonRetryableToolError) as exc:
        _apply(
            "ext.txt",
            "@@ -1,1 +1,1 @@\n-hello\n+bye\n",
            ToolPermissionContext(extra={"read_state": stale}),
        )
    assert "modified since read" in str(exc.value)
    assert f.read_bytes() == b"hello\n", "外部改动检测命中时不得落盘"


def test_atomic_write_failure_keeps_file_and_cleans_tmp(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约④：原子替换失败 ⇒ 文件保持原内容、无残留临时文件（与 edit 同一实现）。"""
    f = ws / "at.txt"
    f.write_bytes(b"hello\n")

    def boom(src: object, dst: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("bandi.tools.fs_write.os.replace", boom)
    with pytest.raises(NonRetryableToolError):
        _apply("at.txt", "@@ -1,1 +1,1 @@\n-hello\n+bye\n")
    assert f.read_bytes() == b"hello\n"
    assert [p.name for p in ws.iterdir() if p.name != "at.txt"] == []
