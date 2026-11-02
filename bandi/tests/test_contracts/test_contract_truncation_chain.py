# 功能：契约——截断体系修复批（审计：落盘读回死路/折叠毁出路/
# 失败不落盘/无 TTL/文档静默截断）。
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from bandi.core.message import Message, MessageRole


class TestTruncateHeadTail:
    def _executor(self, monkeypatch: Any, tmp_path: Path) -> Any:
        from bandi.tools.executor import ToolExecutor

        monkeypatch.setattr(
            "bandi.tools.executor.ToolExecutor.__init__", lambda self, *a, **k: None
        )
        ex = ToolExecutor()
        ex._max_output_chars = 100
        ex._temp_dir = tmp_path / "tmp"
        ex._project_root = None
        return ex

    def test_keeps_head_and_tail(self, monkeypatch: Any, tmp_path: Path) -> None:
        ex = self._executor(monkeypatch, tmp_path)
        text = "HEAD" + ("x" * 500) + "TAIL_END_ROOT_CAUSE"
        truncated, full = ex._truncate_output(text)
        assert truncated is not None
        assert truncated.startswith("HEAD")
        assert truncated.rstrip().endswith("TAIL_END_ROOT_CAUSE"), (
            f"尾部（根因/错误信息）必须保留：{truncated[-60:]!r}"
        )
        assert "chars omitted" in truncated
        assert full == text


class TestPersistOutputReadable:
    def test_persists_into_project_root_when_set(self, monkeypatch: Any, tmp_path: Path) -> None:
        from bandi.core.message import ToolCall
        from bandi.tools.executor import ToolExecutor

        monkeypatch.setattr(
            "bandi.tools.executor.ToolExecutor.__init__", lambda self, *a, **k: None
        )
        ex = ToolExecutor()
        ex._temp_dir = tmp_path / "tmp"
        ex._project_root = tmp_path / "proj"
        path = ex._persist_output(ToolCall(id="t", name="read", arguments={}), "full-content")
        assert path is not None
        persisted = Path(path)
        assert str(persisted).startswith(str(tmp_path / "proj")), (
            f"落盘必须进 project_root（read 允许根内可读回）：{path}"
        )
        assert persisted.read_text(encoding="utf-8") == "full-content"

    def test_falls_back_to_temp_dir_without_project_root(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        from bandi.core.message import ToolCall
        from bandi.tools.executor import ToolExecutor

        monkeypatch.setattr(
            "bandi.tools.executor.ToolExecutor.__init__", lambda self, *a, **k: None
        )
        ex = ToolExecutor()
        ex._temp_dir = tmp_path / "tmp"
        ex._project_root = None
        path = ex._persist_output(ToolCall(id="t", name="read", arguments={}), "x")
        assert path is not None and str(tmp_path) in path


class TestCleanupStale:
    def test_old_files_removed_new_kept(self, tmp_path: Path) -> None:
        from bandi.tools.executor import cleanup_stale_tool_outputs

        old = tmp_path / "tool_output_bash_old.txt"
        new = tmp_path / "tool_output_bash_new.txt"
        other = tmp_path / "unrelated.txt"
        old.write_text("x", encoding="utf-8")
        new.write_text("x", encoding="utf-8")
        other.write_text("x", encoding="utf-8")
        stale_ts = time.time() - 8 * 86400
        os.utime(old, (stale_ts, stale_ts))

        removed = cleanup_stale_tool_outputs(tmp_path)

        assert removed == 1
        assert not old.exists()
        assert new.exists() and other.exists(), "非工具输出/新文件不得误删"


class TestRequestViewFoldOutlets:
    def _tool_msg(self, content: str, turn: int) -> Message:
        return Message(
            role=MessageRole.TOOL,
            content=content,
            tool_call_id="tc-1",
            name="read",
            turn_index=turn,
        )

    def test_persisted_message_is_not_folded(self) -> None:
        from bandi.agent.request_view import build_request_view

        big_persisted = (
            "[tool:summary] 摘要..."
            + ("x" * 5000)
            + "\n[full output persisted: /p/a.txt — use read]"
        )
        messages = [self._tool_msg(big_persisted, 1), self._tool_msg("recent", 9)]
        view = build_request_view(messages, keep_recent_turns=2)
        assert view[0].content == big_persisted, "含落盘路径的压缩产物不得折叠（出路不能丢）"

    def test_plain_oversized_old_output_still_folds(self) -> None:
        from bandi.agent.request_view import build_request_view

        plain = "y" * 5000
        messages = [self._tool_msg(plain, 1), self._tool_msg("recent", 9)]
        view = build_request_view(messages, keep_recent_turns=2)
        assert "已折叠" in (view[0].content or "")

    def test_threshold_raised_to_4000(self) -> None:
        from bandi.agent.request_view import DEFAULT_FOLD_MAX_TOOL_CHARS

        assert DEFAULT_FOLD_MAX_TOOL_CHARS == 4000


class TestDocumentWindow:
    def _tool(self) -> Any:
        from bandi.tools.doc_tool import DocumentReadTool

        return DocumentReadTool()

    def test_text_offset_window_with_next(self) -> None:
        tool = self._tool()
        raw = ("0123456789" * 10).encode("utf-8")  # 100 chars
        result = tool._read_text(raw, offset=10, max_chars=20)
        assert result["content"] == ("0123456789" * 10)[10:30]
        assert result["truncated"] is True
        assert result["next_offset"] == 30
        assert result["total_chars"] == 100

    def test_text_eof_no_next(self) -> None:
        tool = self._tool()
        raw = b"short text"
        result = tool._read_text(raw, offset=5, max_chars=100)
        assert result["content"] == " text"
        assert result["truncated"] is False
        assert result["next_offset"] is None

    def test_csv_row_window(self) -> None:
        tool = self._tool()
        raw = ("h1,h2\n" + "\n".join(f"a{i},b{i}" for i in range(300))).encode("utf-8")
        result = tool._read_csv(raw, offset=100, max_rows=50)
        assert result["row_count"] == 50
        assert result["total_rows"] == 300
        assert result["rows"][0] == ["a100", "b100"]
        assert result["next_offset"] == 150
        assert result["truncated"] is True
