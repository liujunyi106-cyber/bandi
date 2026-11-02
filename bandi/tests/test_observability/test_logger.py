# 功能：Logger 单元测试 — 结构化日志 / 级别过滤 / 轮转

import os
import tempfile

from bandi.observability.logger import LogEntry, LogLevel
from bandi.observability.logger import Logger as _MemoLogger

Logger = _MemoLogger  # type: ignore[has-type]


class TestLogger:
    def test_log_level_enum(self) -> None:
        assert LogLevel.DEBUG.value == 0
        assert LogLevel.INFO.value == 1
        assert LogLevel.WARNING.value == 2
        assert LogLevel.ERROR.value == 3
        assert LogLevel.CRITICAL.value == 4

    def test_log_debug_below_info_level(self) -> None:
        tmp = tempfile.mkdtemp()
        logger = Logger(tmp, level=LogLevel.INFO, console_output=False)
        logger.debug("test", "should not appear")
        logger.info("test", "should appear")
        logger.close()
        with open(os.path.join(tmp, "bandi.log"), encoding="utf-8") as f:
            content = f.read()
        assert "should not appear" not in content
        assert "should appear" in content

    def test_log_writes_json(self) -> None:
        import json

        tmp = tempfile.mkdtemp()
        logger = Logger(tmp, level=LogLevel.DEBUG, console_output=False)
        logger.info("agent.loop", "状态变更", session_id="s-001", turn_index=1)
        logger.close()
        with open(os.path.join(tmp, "bandi.log"), encoding="utf-8") as f:
            line = f.readline()
        entry = json.loads(line)
        assert entry["level"] == "INFO"
        assert entry["module"] == "agent.loop"
        assert entry["session_id"] == "s-001"

    def test_log_with_pii_detector_masks_message(self) -> None:
        tmp = tempfile.mkdtemp()
        logger = Logger(
            tmp,
            level=LogLevel.DEBUG,
            console_output=False,
            pii_detector=lambda m: m.replace("sk-secret999", "****"),
        )
        logger.info("agent", "connecting with sk-secret999 now")
        logger.close()
        with open(os.path.join(tmp, "bandi.log"), encoding="utf-8") as f:
            content = f.read()
        assert "sk-secret999" not in content, "PII 明文泄漏进日志"
        assert "****" in content

    def test_log_rotation(self) -> None:
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        logger = Logger(tmp, level=LogLevel.INFO, console_output=False)
        logger.info("test", "rotation test")
        logger.close()

    def test_multiple_entries(self) -> None:
        tmp = tempfile.mkdtemp()
        logger = Logger(tmp, level=LogLevel.INFO, console_output=False)
        logger.info("mod.a", "msg1")
        logger.warning("mod.b", "msg2")
        logger.error("mod.c", "msg3")
        logger.close()
        with open(os.path.join(tmp, "bandi.log"), encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) >= 3

    def test_log_entry_dataclass(self) -> None:
        from datetime import UTC, datetime

        entry = LogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            level="INFO",
            module="test",
            message="hello",
        )
        assert entry.level == "INFO"
        assert entry.message == "hello"
