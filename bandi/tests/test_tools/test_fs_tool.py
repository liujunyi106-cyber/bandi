# 功能：FsTool 基类测试 — 路径沙箱/敏感检测/编码检测/二进制判断/行号渲染

from __future__ import annotations

from pathlib import Path

import pytest

from bandi.tools.fs_tool import FsTool, _looks_binary


class _ConcreteFsTool(FsTool):
    name = "fs_tool_probe"
    description = "probe fs tool"
    category = "code"

    async def execute(self, params, context=None):
        return None


@pytest.fixture
def tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _ConcreteFsTool:
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    return _ConcreteFsTool()


class TestResolveSafePath:
    def test_relative_path_resolved_inside_workspace(self, tmp_path: Path, tool):
        target = tool._resolve_safe_path("sub/file.txt")
        assert target == (tmp_path / "sub/file.txt").resolve()

    def test_absolute_path_inside_workspace(self, tmp_path: Path, tool):
        f = tmp_path / "inside.txt"
        assert tool._resolve_safe_path(str(f)) == f.resolve()

    def test_absolute_path_outside_rejected(self, tmp_path: Path, tool):
        outside = tmp_path.parent / "escape.txt"
        with pytest.raises(ValueError, match="outside workspace"):
            tool._resolve_safe_path(str(outside))

    def test_traversal_rejected(self, tool):
        with pytest.raises(ValueError, match="outside workspace"):
            tool._resolve_safe_path("../escape.txt")


class TestIsSensitive:
    def test_dot_env_detected(self, tmp_path: Path, tool):
        assert tool._is_sensitive(tmp_path / ".env") is True

    def test_ssh_dir_detected(self, tmp_path: Path, tool):
        assert tool._is_sensitive(tmp_path / ".ssh" / "config") is True

    def test_pem_extension_detected(self, tmp_path: Path, tool):
        assert tool._is_sensitive(tmp_path / "server.pem") is True

    def test_normal_file_not_sensitive(self, tmp_path: Path, tool):
        assert tool._is_sensitive(tmp_path / "notes.md") is False


class TestEncoding:
    def test_utf8(self):
        assert FsTool.detect_encoding("你好".encode()) == "utf-8"

    def test_bom_decodes(self):
        enc = FsTool.detect_encoding(b"\xef\xbb\xbfhello")
        assert enc in ("utf-8", "utf-8-sig")

    def test_utf16(self):
        assert FsTool.detect_encoding("你好".encode("utf-16")) == "utf-16"

    def test_cp1252_fallback(self):
        assert FsTool.detect_encoding(b"\xe9") == "cp1252"


class TestLooksBinary:
    def test_text_not_binary(self):
        assert _looks_binary(b"hello world") is False

    def test_null_byte_is_binary(self):
        assert _looks_binary(b"\x00\x01\x02") is True

    def test_empty_not_binary(self):
        assert _looks_binary(b"") is False


# FileReadTool 改用 _render_read_output），其行为语义已迁移至
# tests/test_contracts/test_contract_read_enhanced.py（默认渲染/分页/越界报错）。


def test_resolve_rejects_symlink_pointing_inside_workspace(tmp_path, monkeypatch) -> None:
    """修复：symlink 指向 workspace **内部**也必须拒绝
    修复前 is_symlink 在 resolve() 之后判定恒 False（死检查），内部 symlink 放行。"""
    import bandi.tools.fs_tool as fs_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(fs_mod, "_WORKSPACE_ROOT", ws)
    secret = ws / "secret.txt"
    secret.write_text("inside", encoding="utf-8")
    link = ws / "link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("symlink 不可用")
    if not link.is_symlink():
        import pytest

        pytest.skip("symlink 静默退化（无特权环境），逃逸场景不可构造")
    try:
        fs_mod.resolve_within_workspace("link.txt")
    except ValueError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("指向 workspace 内部的 symlink 必须被拒绝")
