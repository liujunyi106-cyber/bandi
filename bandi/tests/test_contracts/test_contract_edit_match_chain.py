# 功能：契约——Edit 合体匹配链（opencode 10 级回退 + cc-haha 引号归一/外部修改双保险）。
# 钉行为：多级容错命中（命中原文切片上替换）/ 命中准入拒绝（不成比例、多匹配、空串
# 恒等）/ 引号风格保持 / 行尾与 BOM 字节保真 / 外部修改双保险（mtime + 内容哈希
from __future__ import annotations

import asyncio
import codecs
import hashlib
from pathlib import Path

import pytest

from bandi.tools.base import ToolPermissionContext
from bandi.tools.edit_match import (
    MultipleMatchesError,
    OldStringNotFoundError,
    block_anchor_replacer,
    context_aware_replacer,
    convert_line_ending,
    detect_line_ending,
    indentation_flexible_replacer,
    is_disproportionate_match,
    normalize_line_endings,
    replace_with_fallback,
)
from bandi.tools.file_edit import FileEditTool
from bandi.tools.file_ops import FileReadTool


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    return tmp_path


def _read_state(*, path: Path, mtime: float, sha256: str, partial: bool = False) -> dict:
    """功能：构造 read_state 记录（Edit 外部修改检测消费形态）。"""
    return {
        str(path): {
            "read_at": "2026-09-18T00:00:00+00:00",
            "mtime": mtime,
            "size": path.stat().st_size,
            "resolved_path": str(path.resolve()),
            "content_sha256": sha256,
            "partial": partial,
        }
    }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ──────────────────────────────────────────────────────────────────────
# A. 匹配链回退族（纯函数）
# ──────────────────────────────────────────────────────────────────────


class TestMatchChainFallback:
    def test_exact_match_uses_simple_tier(self) -> None:
        match = replace_with_fallback("alpha\nbeta\n", "beta", "gamma")
        assert match.tier == "simple"
        assert match.text == "alpha\ngamma\n"
        assert match.count == 1
        assert match.actual_old == "beta"

    def test_line_trimmed_fallback_tolerates_indent_drift(self) -> None:
        content = "def f():\n    return 1\n"
        old = "def f():\n  return 1"
        match = replace_with_fallback(content, old, "def f():\n    return 2")
        assert match.tier == "line_trimmed"
        assert match.text == "def f():\n    return 2\n"
        assert match.actual_old == "def f():\n    return 1"

    def test_quote_normalization_fallback_preserves_curly_style(self) -> None:
        content = "他说“你好”然后走了\n"
        old = '他说"你好"然后走了'
        new = '他说"再见"然后走了'
        match = replace_with_fallback(content, old, new)
        assert match.tier == "quote"
        assert match.text == "他说“再见”然后走了\n"
        assert match.actual_old == "他说“你好”然后走了"

    def test_whitespace_normalization_fallback(self) -> None:
        content = "value\t=\tcompute( 1 )\n"
        old = "value = compute( 1 )"
        match = replace_with_fallback(content, old, "value = compute(2)")
        assert match.tier == "whitespace"
        assert match.text == "value = compute(2)\n"

    def test_escape_normalization_fallback(self) -> None:
        content = "first\nsecond\n"
        old = "first\\nsecond"
        match = replace_with_fallback(content, old, "merged")
        assert match.tier == "escape"
        assert match.text == "merged\n"

    def test_block_anchor_fallback_for_fuzzy_middle_lines(self) -> None:
        content = (
            "def calculate_total(items):\n"
            "    total = 0\n"
            "    for item in items:\n"
            "        total += item.price\n"
            "    return total\n"
        )
        old = (
            "def calculate_total(items):\n"
            "    total = 0\n"
            "    for item in values:\n"
            "        total += item.cost\n"
            "    return total"
        )
        new = "def calculate_total(entries):\n    return sum(e.cost for e in entries)"
        match = replace_with_fallback(content, old, new)
        assert match.tier == "block_anchor"
        assert match.text == new + "\n"

    def test_context_aware_fallback_when_anchor_similarity_low(self) -> None:
        content = "start()\nalpha=1\nzzzz\nyyyy\ndelta=4\nend()\n"
        old = "start()\nalpha=1\nbeta=2\ngamma=3\ndelta=4\nend()"
        match = replace_with_fallback(content, old, "replaced_block()")
        assert match.tier == "context_aware"
        assert match.text == "replaced_block()\n"

    def test_indentation_flexible_replacer_yields_dedented_block(self) -> None:
        content = "\t\talpha\n\t\tbeta\n"
        find = "    alpha\n    beta"
        candidates = list(indentation_flexible_replacer(content, find))
        assert candidates == ["\t\talpha\n\t\tbeta"]

    def test_block_anchor_replacer_requires_three_lines(self) -> None:
        assert list(block_anchor_replacer("a\nb\n", "a\nb")) == []
        assert list(context_aware_replacer("a\nb\n", "a\nb")) == []

    def test_replace_all_replaces_every_occurrence(self) -> None:
        match = replace_with_fallback("a\nb\na\n", "a", "x", replace_all=True)
        assert match.count == 2
        assert match.text == "x\nb\nx\n"


class TestMatchChainGuards:
    def test_chain_rejects_empty_old_string(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            replace_with_fallback("content", "", "x")

    def test_chain_rejects_identical_old_new(self) -> None:
        with pytest.raises(ValueError, match="identical"):
            replace_with_fallback("content", "a", "a")

    def test_chain_rejects_multiple_matches_without_replace_all(self) -> None:
        with pytest.raises(MultipleMatchesError, match="multiple matches"):
            replace_with_fallback("x\nx\n", "x", "y")

    def test_chain_not_found_message_mentions_old_string(self) -> None:
        with pytest.raises(OldStringNotFoundError, match="old_string not found"):
            replace_with_fallback("hello", "missing", "x")

    def test_fuzzy_tier_cannot_bypass_uniqueness_guard(self) -> None:
        # 破坏者：两处缩进相同的模糊候选——任一级都不得静默替换第一处
        content = "    keep\n    keep\n"
        with pytest.raises(MultipleMatchesError):
            replace_with_fallback(content, "keep", "x")

    def test_disproportionate_match_guard(self) -> None:
        assert is_disproportionate_match("a\nb\nc\nd", "a\nb") is False
        assert is_disproportionate_match("a\nb\nc\nd\ne", "a\nb") is True
        long_search = ("y" * 600) + "\nend"
        assert is_disproportionate_match(long_search, "short\nend") is True

    def test_line_ending_utilities_round_trip(self) -> None:
        crlf = "a\r\nb\r\n"
        assert detect_line_ending(crlf) == "\r\n"
        assert detect_line_ending("a\nb\n") == "\n"
        assert normalize_line_endings(crlf) == "a\nb\n"
        assert convert_line_ending("a\nb", "\r\n") == "a\r\nb"


# ──────────────────────────────────────────────────────────────────────
# B. 工具层：行尾 / BOM / 中文真实数据形态
# ──────────────────────────────────────────────────────────────────────


class TestEditToolByteFidelity:
    @pytest.mark.asyncio
    async def test_lf_file_stays_lf(self, workspace: Path) -> None:
        fp = workspace / "lf.txt"
        fp.write_bytes(b"line1\nline2\n")
        await FileEditTool().execute(
            {"file_path": str(fp), "old_string": "line1", "new_string": "changed"}
        )
        assert fp.read_bytes() == b"changed\nline2\n"

    @pytest.mark.asyncio
    async def test_crlf_file_round_trips_with_lf_old_string(self, workspace: Path) -> None:
        fp = workspace / "crlf.txt"
        fp.write_bytes(b"line1\r\nline2\r\n")
        result = await FileEditTool().execute(
            {
                "file_path": str(fp),
                "old_string": "line1\nline2",
                "new_string": "changed1\nchanged2",
            }
        )
        assert "replaced" in result
        assert fp.read_bytes() == b"changed1\r\nchanged2\r\n"

    @pytest.mark.asyncio
    async def test_utf8_bom_preserved(self, workspace: Path) -> None:
        fp = workspace / "bom.txt"
        fp.write_bytes(codecs.BOM_UTF8 + "你好世界".encode())
        await FileEditTool().execute(
            {"file_path": str(fp), "old_string": "你好", "new_string": "再见"}
        )
        data = fp.read_bytes()
        assert data.startswith(codecs.BOM_UTF8), "UTF-8 BOM 必须保留"
        assert data[len(codecs.BOM_UTF8) :].decode("utf-8") == "再见世界"

    @pytest.mark.asyncio
    async def test_utf16_bom_preserved(self, workspace: Path) -> None:
        fp = workspace / "u16.txt"
        fp.write_bytes("alpha".encode("utf-16"))
        await FileEditTool().execute(
            {"file_path": str(fp), "old_string": "alpha", "new_string": "beta"}
        )
        assert fp.read_bytes().decode("utf-16") == "beta"

    @pytest.mark.asyncio
    async def test_tool_quote_fallback_in_chinese_doc(self, workspace: Path) -> None:
        fp = workspace / "doc.md"
        fp.write_text("他说“你好”然后走了\n", encoding="utf-8")
        result = await FileEditTool().execute(
            {
                "file_path": str(fp),
                "old_string": '他说"你好"然后走了',
                "new_string": '他说"再见"然后走了',
            }
        )
        assert "replaced" in result
        assert fp.read_text(encoding="utf-8") == "他说“再见”然后走了\n"


# ──────────────────────────────────────────────────────────────────────
# C. 工具层守卫（空串 / 恒等 / 多匹配 / 文件不存在建议）
# ──────────────────────────────────────────────────────────────────────


class TestEditToolGuards:
    @pytest.mark.asyncio
    async def test_identical_strings_rejected(self, workspace: Path) -> None:
        fp = workspace / "same.txt"
        fp.write_text("hello\n", encoding="utf-8")
        with pytest.raises(ValueError, match="identical"):
            await FileEditTool().execute(
                {"file_path": str(fp), "old_string": "hello", "new_string": "hello"}
            )

    @pytest.mark.asyncio
    async def test_empty_old_string_rejected_for_nonempty_file(self, workspace: Path) -> None:
        fp = workspace / "nonempty.txt"
        fp.write_text("content\n", encoding="utf-8")
        with pytest.raises(ValueError, match="empty"):
            await FileEditTool().execute(
                {"file_path": str(fp), "old_string": "", "new_string": "prefix"}
            )
        assert fp.read_text(encoding="utf-8") == "content\n"

    @pytest.mark.asyncio
    async def test_empty_old_string_writes_empty_file(self, workspace: Path) -> None:
        fp = workspace / "empty.txt"
        fp.write_bytes(b"")
        result = await FileEditTool().execute(
            {"file_path": str(fp), "old_string": "", "new_string": "hello\n"}
        )
        assert "replaced" in result
        assert fp.read_text(encoding="utf-8") == "hello\n"

    @pytest.mark.asyncio
    async def test_multiple_matches_rejected_at_tool_level(self, workspace: Path) -> None:
        fp = workspace / "multi.txt"
        fp.write_text("x\nx\n", encoding="utf-8")
        with pytest.raises(ValueError, match="matches"):
            await FileEditTool().execute(
                {"file_path": str(fp), "old_string": "x", "new_string": "y"}
            )
        assert fp.read_text(encoding="utf-8") == "x\nx\n", "多匹配拒绝时文件不得被改写"

    @pytest.mark.asyncio
    async def test_file_not_found_with_similar_suggestion(self, workspace: Path) -> None:
        (workspace / "alpha.py").write_text("x = 1\n", encoding="utf-8")
        with pytest.raises(FileNotFoundError) as excinfo:
            await FileEditTool().execute(
                {"file_path": str(workspace / "apha.py"), "old_string": "x", "new_string": "y"}
            )
        assert "Did you mean" in str(excinfo.value)
        assert "alpha.py" in str(excinfo.value)


# ──────────────────────────────────────────────────────────────────────
# D. 外部修改双保险（read_state 消费）
# ──────────────────────────────────────────────────────────────────────


class TestEditToolExternalModification:
    @pytest.mark.asyncio
    async def test_external_modification_rejected(self, workspace: Path) -> None:
        fp = workspace / "ext.txt"
        fp.write_text("original content\n", encoding="utf-8")
        stale = _read_state(
            path=fp,
            mtime=fp.stat().st_mtime - 10,
            sha256=_sha256(b"original content\n"),
        )
        fp.write_text("changed by linter\n", encoding="utf-8")
        ctx = ToolPermissionContext(extra={"read_state": stale})
        with pytest.raises(ValueError, match="modified since read"):
            await FileEditTool().execute(
                {"file_path": str(fp), "old_string": "changed", "new_string": "x"}, context=ctx
            )
        assert fp.read_text(encoding="utf-8") == "changed by linter\n"

    @pytest.mark.asyncio
    async def test_touch_only_mtime_same_content_allowed(self, workspace: Path) -> None:
        fp = workspace / "touch.txt"
        fp.write_text("same content\n", encoding="utf-8")
        digest = _sha256(fp.read_bytes())
        record = _read_state(path=fp, mtime=fp.stat().st_mtime - 10, sha256=digest)
        ctx = ToolPermissionContext(extra={"read_state": record})
        result = await FileEditTool().execute(
            {"file_path": str(fp), "old_string": "same", "new_string": "blessed"}, context=ctx
        )
        assert "replaced" in result
        assert fp.read_text(encoding="utf-8") == "blessed content\n"

    @pytest.mark.asyncio
    async def test_no_read_record_allows_edit(self, workspace: Path) -> None:
        # 设计偏离 无读取记录时不强制先读（兼容现状），有记录才启用双保险
        fp = workspace / "norec.txt"
        fp.write_text("hello\n", encoding="utf-8")
        ctx = ToolPermissionContext(extra={"read_state": {}})
        result = await FileEditTool().execute(
            {"file_path": str(fp), "old_string": "hello", "new_string": "bye"}, context=ctx
        )
        assert "replaced" in result

    @pytest.mark.asyncio
    async def test_partial_read_record_then_modified_rejected(self, workspace: Path) -> None:
        fp = workspace / "partial.txt"
        fp.write_text("unchanged content\n", encoding="utf-8")
        digest = _sha256(fp.read_bytes())
        record = _read_state(path=fp, mtime=fp.stat().st_mtime - 10, sha256=digest, partial=True)
        ctx = ToolPermissionContext(extra={"read_state": record})
        with pytest.raises(ValueError, match="modified since read"):
            await FileEditTool().execute(
                {"file_path": str(fp), "old_string": "unchanged", "new_string": "x"}, context=ctx
            )

    @pytest.mark.asyncio
    async def test_latest_read_record_wins_for_same_file(self, workspace: Path) -> None:
        # 破坏者：同一文件两条读取记录（不同参数原文键）——外部修改检测必须取
        # 最新一次读取的基准；取旧记录会把「已重读最新内容」误报为 modified since read
        fp = workspace / "doc.txt"
        fp.write_text("v1 content\n", encoding="utf-8")
        read_state: dict = {}
        ctx = ToolPermissionContext(extra={"read_state": read_state})
        await FileReadTool().execute({"path": "doc.txt"}, context=ctx)
        fp.write_text("v2 content\n", encoding="utf-8")
        await FileReadTool().execute({"path": str(fp)}, context=ctx)

        result = await FileEditTool().execute(
            {"file_path": "doc.txt", "old_string": "v2 content", "new_string": "v3 content"},
            context=ctx,
        )
        assert "replaced" in result
        assert fp.read_text(encoding="utf-8") == "v3 content\n"

    @pytest.mark.asyncio
    async def test_read_tool_records_extended_read_state(self, workspace: Path) -> None:
        fp = workspace / "r.txt"
        fp.write_text("abc", encoding="utf-8")
        read_state: dict = {}
        ctx = ToolPermissionContext(extra={"read_state": read_state})
        await FileReadTool().execute({"path": str(fp)}, context=ctx)
        record = read_state[str(fp)]
        assert record["resolved_path"] == str(fp.resolve())
        assert record["content_sha256"] == _sha256(b"abc")
        assert record["partial"] is False

    @pytest.mark.asyncio
    async def test_partial_read_marks_partial(self, workspace: Path) -> None:
        fp = workspace / "p.txt"
        fp.write_text("line1\nline2\n", encoding="utf-8")
        read_state: dict = {}
        ctx = ToolPermissionContext(extra={"read_state": read_state})
        await FileReadTool().execute({"path": str(fp), "offset": 1, "limit": 1}, context=ctx)
        assert read_state[str(fp)]["partial"] is True


# ──────────────────────────────────────────────────────────────────────
# E. 并发串行 + 原子写
# ──────────────────────────────────────────────────────────────────────


class TestEditToolConcurrencyAndAtomicity:
    @pytest.mark.asyncio
    async def test_concurrent_edits_are_serialized(self, workspace: Path) -> None:
        fp = workspace / "concurrent.txt"
        fp.write_bytes(b"AAA\nBBB\n")
        tool = FileEditTool()
        await asyncio.gather(
            tool.execute({"file_path": str(fp), "old_string": "AAA", "new_string": "XXX"}),
            tool.execute({"file_path": str(fp), "old_string": "BBB", "new_string": "YYY"}),
        )
        assert fp.read_bytes() == b"XXX\nYYY\n", "并发编辑必须串行化，不得丢失更新"

    @pytest.mark.asyncio
    async def test_atomic_write_failure_cleans_tmp(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fp = workspace / "atomic.txt"
        fp.write_bytes(b"hello\n")

        def boom(src: object, dst: object) -> None:
            raise OSError("replace failed")

        # apply_patch 共用），故补丁目标同步指向实现站点。
        monkeypatch.setattr("bandi.tools.fs_write.os.replace", boom)
        with pytest.raises(OSError):
            await FileEditTool().execute(
                {"file_path": str(fp), "old_string": "hello", "new_string": "bye"}
            )
        leftovers = [p.name for p in workspace.iterdir() if p.name != "atomic.txt"]
        assert leftovers == [], f"原子写失败必须清理临时文件，实际残留: {leftovers}"
        assert fp.read_bytes() == b"hello\n"


# ──────────────────────────────────────────────────────────────────────
# F. 端到端（DoD-12）：executor 生产链路 read → edit
# ──────────────────────────────────────────────────────────────────────


class _AutoApprove:
    # 功能：E2E 用同步交互桩（模拟 CLI 在场，审批门零介入 + ask 层放行）
    async def ask_permission(self, tool_name: str, args: dict, risk: str) -> bool:
        return True

    async def ask_confirmation(self, prompt: str, timeout: float = 30.0) -> bool:
        return True

    async def ask_input(self, prompt: str, *, secret: bool = False) -> str:
        return ""

    async def show_notification(self, message: str, level: str = "info") -> None:
        return None


class TestEditToolEndToEnd:
    @pytest.mark.asyncio
    async def test_executor_read_then_edit_full_chain(self, workspace: Path) -> None:
        from bandi.core.message import ToolCall
        from bandi.tools.approval import reset_approval_store
        from bandi.tools.executor import ToolExecutor
        from bandi.tools.permissions_pipeline import PermissionsPipeline
        from bandi.tools.registry import ToolRegistry

        reset_approval_store()
        registry = ToolRegistry()
        registry.register_instance(FileReadTool())
        registry.register_instance(FileEditTool())
        pipeline = PermissionsPipeline()
        pipeline.set_user_interaction(_AutoApprove())
        executor = ToolExecutor(
            registry=registry, project_root=str(workspace), permissions_pipeline=pipeline
        )
        executor.bind_session_meta({"session_id": "s-ct01-e2e"})

        fp = workspace / "flow.txt"
        fp.write_text("老内容\n", encoding="utf-8")
        results = await executor.execute(
            [
                ToolCall(id="c1", name="read", arguments={"path": str(fp)}),
                ToolCall(
                    id="c2",
                    name="edit",
                    arguments={
                        "file_path": str(fp),
                        "old_string": "老内容",
                        "new_string": "新内容",
                    },
                ),
            ]
        )
        assert all(r.success for r in results), [r.error for r in results]
        assert fp.read_text(encoding="utf-8") == "新内容\n"
        record = executor.read_state[str(fp)]
        assert record["resolved_path"] == str(fp.resolve())
        assert record["partial"] is False
