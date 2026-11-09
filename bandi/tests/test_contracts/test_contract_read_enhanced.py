# 功能：契约——Read 增强。
# 钉行为：默认行号渲染 + 三态尾注（EOF / 行数帽 / 50KB 字节帽）/ 单行 2000 字符截断 /
# offset 越界报错（空文件 offset=1 合法）/ 二进制扩展名黑名单 + NUL + 不可打印占比 /
from __future__ import annotations

import re
from pathlib import Path

import pytest

from bandi.tools.base import ToolPermissionContext
from bandi.tools.file_ops import FileReadTool


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    return tmp_path


# ──────────────────────────────────────────────────────────────────────
# A. 输出渲染族
# ──────────────────────────────────────────────────────────────────────


class TestReadRender:
    @pytest.mark.asyncio
    async def test_default_render_has_line_numbers_and_eof_note(self, workspace: Path) -> None:
        fp = workspace / "a.txt"
        fp.write_text("第一行\nsecond line\nthird\n", encoding="utf-8")
        result = await FileReadTool().execute({"path": str(fp)})
        assert "     1|第一行" in result
        assert "     3|third" in result
        assert "(End of file - total 3 lines)" in result

    @pytest.mark.asyncio
    async def test_default_limit_caps_at_2000_lines(self, workspace: Path) -> None:
        fp = workspace / "big.txt"
        fp.write_text("\n".join(f"line-{i}" for i in range(1, 2006)), encoding="utf-8")
        result = await FileReadTool().execute({"path": str(fp)})
        assert "  2000|line-2000" in result
        assert "line-2001" not in result
        assert "(Showing lines 1-2000 of 2005. Use offset=2001 to continue.)" in result

    @pytest.mark.asyncio
    async def test_byte_cap_stops_before_50kb(self, workspace: Path) -> None:
        fp = workspace / "wide.txt"
        fp.write_text("\n".join("x" * 58 for _ in range(1200)), encoding="utf-8")
        result = await FileReadTool().execute({"path": str(fp)})
        assert "Output capped at 50 KB" in result
        match = re.search(r"Use offset=(\d+) to continue", result)
        assert match is not None
        assert int(match.group(1)) > 1

    @pytest.mark.asyncio
    async def test_byte_cap_resume_reads_whole_file(self, workspace: Path) -> None:
        # 破坏者：字节帽截断后按提示续读，两段拼接必须覆盖文件尾部
        fp = workspace / "resume.txt"
        lines = [f"row-{i:04d}-" + "y" * 40 for i in range(1200)]
        lines[-1] = "FINAL-TAIL-MARKER"
        fp.write_text("\n".join(lines), encoding="utf-8")

        first = await FileReadTool().execute({"path": str(fp)})
        assert "Output capped at 50 KB" in first
        next_offset = int(re.search(r"Use offset=(\d+) to continue", first).group(1))

        second = await FileReadTool().execute({"path": str(fp), "offset": next_offset})
        assert "FINAL-TAIL-MARKER" in second

    @pytest.mark.asyncio
    async def test_single_line_truncated_at_2000_chars(self, workspace: Path) -> None:
        fp = workspace / "longline.txt"
        fp.write_text("A" * 3000, encoding="utf-8")
        result = await FileReadTool().execute({"path": str(fp)})
        assert "... (line truncated to 2000 chars)" in result
        body = next(line for line in result.splitlines() if "     1|" in line)
        assert body.count("A") == 2000

    @pytest.mark.asyncio
    async def test_explicit_offset_and_limit_pagination_note(self, workspace: Path) -> None:
        fp = workspace / "page.txt"
        fp.write_text("\n".join(f"l{i}" for i in range(1, 6)), encoding="utf-8")
        result = await FileReadTool().execute({"path": str(fp), "offset": 2, "limit": 2})
        assert "     2|l2" in result
        assert "     3|l3" in result
        assert "l1" not in result and "l4" not in result
        assert "(Showing lines 2-3 of 5. Use offset=4 to continue.)" in result

    @pytest.mark.asyncio
    async def test_offset_out_of_range_raises(self, workspace: Path) -> None:
        fp = workspace / "small.txt"
        fp.write_text("only\none\nline\n", encoding="utf-8")
        with pytest.raises(ValueError, match="out of range"):
            await FileReadTool().execute({"path": str(fp), "offset": 10})

    @pytest.mark.asyncio
    async def test_empty_file_with_offset_one_is_valid(self, workspace: Path) -> None:
        fp = workspace / "empty.txt"
        fp.write_bytes(b"")
        result = await FileReadTool().execute({"path": str(fp), "offset": 1})
        assert "(End of file - total 0 lines)" in result

    @pytest.mark.asyncio
    async def test_offset_zero_rejected(self, workspace: Path) -> None:
        fp = workspace / "a.txt"
        fp.write_text("x\n", encoding="utf-8")
        with pytest.raises(ValueError, match="offset must be >= 1"):
            await FileReadTool().execute({"path": str(fp), "offset": 0})

    @pytest.mark.asyncio
    async def test_explicit_large_limit_not_clamped_to_2000(self, workspace: Path) -> None:
        fp = workspace / "deep.txt"
        fp.write_text("\n".join(f"r{i}" for i in range(1, 2501)), encoding="utf-8")
        result = await FileReadTool().execute({"path": str(fp), "limit": 3000})
        assert "  2500|r2500" in result
        assert "(End of file - total 2500 lines)" in result

    @pytest.mark.asyncio
    async def test_form_feed_and_cr_do_not_shift_line_numbers(self, workspace: Path) -> None:
        # 破坏者：行分割必须只按 CR/LF——splitlines 会把 \x0c/\x85 等当换行造成行号漂移
        fp = workspace / "tricky.txt"
        fp.write_bytes(b"first\x0csecond\nthird\rfourth\n")
        result = await FileReadTool().execute({"path": str(fp)})
        assert "     1|first\x0csecond" in result
        assert "     2|third" in result
        assert "     3|fourth" in result
        assert "(End of file - total 3 lines)" in result


# ──────────────────────────────────────────────────────────────────────
# B. 二进制 / 目录族
# ──────────────────────────────────────────────────────────────────────


class TestReadBinaryAndDirectory:
    @pytest.mark.asyncio
    async def test_binary_extension_rejected(self, workspace: Path) -> None:
        fp = workspace / "photo.png"
        fp.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        with pytest.raises(ValueError, match="binary"):
            await FileReadTool().execute({"path": str(fp)})

    @pytest.mark.asyncio
    async def test_nul_byte_rejected(self, workspace: Path) -> None:
        fp = workspace / "blob.bin"
        fp.write_bytes(b"abc\x00def")
        with pytest.raises(ValueError, match="binary"):
            await FileReadTool().execute({"path": str(fp)})

    @pytest.mark.asyncio
    async def test_nonprintable_ratio_rejected(self, workspace: Path) -> None:
        fp = workspace / "ctrl.dat"
        fp.write_bytes(b"\x01" * 200)
        with pytest.raises(ValueError, match="binary"):
            await FileReadTool().execute({"path": str(fp)})

    @pytest.mark.asyncio
    async def test_directory_mode_lists_entries_sorted(self, workspace: Path) -> None:
        (workspace / "b.txt").write_text("b", encoding="utf-8")
        (workspace / "a.txt").write_text("a", encoding="utf-8")
        (workspace / "sub").mkdir()
        result = await FileReadTool().execute({"path": str(workspace)})
        assert "a.txt" in result and "b.txt" in result
        assert "sub/" in result
        assert "(3 entries)" in result
        assert result.index("a.txt") < result.index("b.txt"), "目录条目必须排序"

    @pytest.mark.asyncio
    async def test_directory_pagination(self, workspace: Path) -> None:
        for i in range(25):
            (workspace / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
        result = await FileReadTool().execute({"path": str(workspace), "limit": 10})
        assert "(Showing 1-10 of 25 entries. Use offset=11 to continue.)" in result

    @pytest.mark.asyncio
    async def test_gbk_chinese_not_treated_as_binary(self, workspace: Path) -> None:
        fp = workspace / "gbk.txt"
        fp.write_bytes("中文内容测试".encode("gbk"))
        result = await FileReadTool().execute({"path": str(fp)})
        assert "中文内容测试" in result


# ──────────────────────────────────────────────────────────────────────
# C. Did-you-mean
# ──────────────────────────────────────────────────────────────────────


class TestReadDidYouMean:
    @pytest.mark.asyncio
    async def test_file_not_found_suggests_similar(self, workspace: Path) -> None:
        (workspace / "alpha.py").write_text("x = 1\n", encoding="utf-8")
        with pytest.raises(FileNotFoundError) as excinfo:
            await FileReadTool().execute({"path": str(workspace / "apha.py")})
        message = str(excinfo.value)
        assert "Did you mean" in message
        assert "alpha.py" in message

    @pytest.mark.asyncio
    async def test_file_not_found_without_suggestion(self, workspace: Path) -> None:
        with pytest.raises(FileNotFoundError) as excinfo:
            await FileReadTool().execute({"path": str(workspace / "missing.txt")})
        assert "Did you mean" not in str(excinfo.value)


# ──────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────


class TestReadCompatibility:
    @pytest.mark.asyncio
    async def test_read_state_still_recorded(self, workspace: Path) -> None:
        fp = workspace / "state.txt"
        fp.write_text("hello", encoding="utf-8")
        read_state: dict = {}
        ctx = ToolPermissionContext(extra={"read_state": read_state})
        await FileReadTool().execute({"path": str(fp)}, context=ctx)
        record = read_state[str(fp)]
        assert record["resolved_path"] == str(fp.resolve())
        assert record["partial"] is False

    @pytest.mark.asyncio
    async def test_chinese_content_line_numbers(self, workspace: Path) -> None:
        fp = workspace / "cn.md"
        fp.write_text("# 标题\n\n中文正文段落。\n", encoding="utf-8")
        result = await FileReadTool().execute({"path": str(fp)})
        assert "     1|# 标题" in result
        assert "     3|中文正文段落。" in result
        assert "(End of file - total 3 lines)" in result
