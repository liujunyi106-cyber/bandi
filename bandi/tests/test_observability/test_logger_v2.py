from __future__ import annotations

import json
import tempfile
from pathlib import Path

from bandi.observability.logger import LogEntry, LogLevel
from bandi.observability.logger import Logger as _MemoLogger

Logger = _MemoLogger  # type: ignore[has-type]


class TestLoggerV2:
    def test_log_entry_has_user_id_field(self) -> None:
        entry = LogEntry(
            timestamp="T00:00:00",
            level="INFO",
            module="test",
            message="hello",
            user_id="u-001",
        )
        assert entry.user_id == "u-001"

    def test_log_with_user_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        logger = Logger(tmp, level=LogLevel.DEBUG, console_output=False)  # type: ignore[name-defined]
        logger.info("test", "user msg", user_id="u-001")
        logger.close()
        with open(tmp / "bandi.log", encoding="utf-8") as f:
            entry = json.loads(f.readline())
        assert entry["user_id"] == "u-001"

    def test_log_auto_trace_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        logger = Logger(tmp, level=LogLevel.DEBUG, console_output=False)
        logger.info("test", "auto trace")
        logger.close()
        with open(tmp / "bandi.log", encoding="utf-8") as f:
            entry = json.loads(f.readline())
        assert len(entry["trace_id"]) > 0

    def test_trace_id_stable_per_session(self) -> None:
        """#6：trace_id 从 session_id 稳定派生——同 session 多次 log 的 trace_id 相同。"""
        tmp = Path(tempfile.mkdtemp())
        logger = Logger(tmp, level=LogLevel.DEBUG, console_output=False)
        logger.info("test", "first", session_id="s-001")
        logger.info("test", "second", session_id="s-001")
        logger.info("test", "third", session_id="s-001")
        logger.close()
        with open(tmp / "bandi.log", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        tids = {entry["trace_id"] for entry in lines}
        assert len(tids) == 1
        assert len(lines[0]["trace_id"]) == 12

    def test_trace_id_distinct_per_session(self) -> None:
        """#6：不同 session 的 trace_id 不同。"""
        tmp = Path(tempfile.mkdtemp())
        logger = Logger(tmp, level=LogLevel.DEBUG, console_output=False)
        logger.info("test", "a", session_id="s-001")
        logger.info("test", "b", session_id="s-002")
        logger.close()
        with open(tmp / "bandi.log", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert lines[0]["trace_id"] != lines[1]["trace_id"]

    def test_log_with_tag(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        logger = Logger(tmp, level=LogLevel.DEBUG, console_output=False)
        logger.info("test", "tagged", tag="security")
        logger.close()
        with open(tmp / "bandi.log", encoding="utf-8") as f:
            entry = json.loads(f.readline())
        assert entry.get("tag") == "security"

    def test_log_pii_auto_mask(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        logger = Logger(tmp, level=LogLevel.DEBUG, console_output=False)
        logger.info("test", "user login", api_key="sk-secret-12345")
        logger.close()
        data = (tmp / "bandi.log").read_bytes()
        assert b"sk-secret-12345" not in data
