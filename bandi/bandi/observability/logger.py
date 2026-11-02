# 功能：Logger — 结构化日志系统（JSON 文件输出 + 控制台 + 按天轮转）

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Self, TextIO

from bandi.observability.secret_mask import mask_dict


class LogLevel(Enum):
    DEBUG = 0
    INFO = 1
    WARNING = 2
    ERROR = 3
    CRITICAL = 4


LEVEL_NAMES = {0: "DEBUG", 1: "INFO", 2: "WARNING", 3: "ERROR", 4: "CRITICAL"}


@dataclass
class LogEntry:
    timestamp: str
    level: str
    module: str
    message: str
    session_id: str = ""
    turn_index: int = 0
    duration_ms: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    user_id: str = ""
    tag: str = ""
    # .1：主链结构化日志所需的多 Agent / 请求维度
    agent_id: str = ""
    parent_agent_id: str = ""
    role: str = ""
    run_id: str = ""
    request_id: str = ""


# 直落顶层字段的键集合——未列入者进 `meta`。新增顶层字段时必须同步此处。
_TOP_LEVEL_LOG_FIELDS = frozenset(
    {
        "session_id",
        "turn_index",
        "duration_ms",
        "trace_id",
        "user_id",
        "tag",
        "agent_id",
        "parent_agent_id",
        "role",
        "run_id",
        "request_id",
    }
)


class Logger:
    DEFAULT_MAX_BYTES = 10 * 1024 * 1024
    # 轮转档保留份数上限（不含当前在写的 bandi.log）
    DEFAULT_BACKUP_COUNT = 5

    def __init__(
        self,
        log_dir: str | Path,
        level: LogLevel | str = LogLevel.INFO,
        retention_days: int = 180,
        console_output: bool = True,
        pii_detector: Callable[[str], str] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
    ):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(level, str):
            level = LogLevel[level.upper()]
        self._level = level
        self._retention_days = retention_days
        self._console_output = console_output
        self._max_bytes = max(1, int(max_bytes))
        self._backup_count = max(0, int(backup_count))
        self._file = self._open_current_log()
        self._lock = threading.Lock()
        self._pii_detector = pii_detector

    def _open_current_log(self) -> TextIO:
        path = self._log_dir / "bandi.log"
        return open(path, "a", encoding="utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _archive_path(self, day: str) -> Path:
        """轮转档路径：`bandi-YYYY-MM-DD.log`，同日已有则追加序号，**绝不复用已存在文件**。"""
        base = self._log_dir / f"bandi-{day}.log"
        if not base.exists():
            return base
        index = 1
        while True:
            candidate = self._log_dir / f"bandi-{day}.{index}.log"
            if not candidate.exists():
                return candidate
            index += 1

    def _rotate_if_needed(self) -> None:
        """双触发轮转：跨天 **或** 超过 max_bytes，任一成立即切档。

        两个触发共用同一套归档/清理逻辑——若只靠日期判断，单次高频会话会让
        bandi.log 无上限增长（原实现的缺口）。
        """
        current_path = self._log_dir / "bandi.log"
        if not current_path.exists():
            return
        stat = current_path.stat()
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        stamped_day = datetime.fromtimestamp(stat.st_mtime, tz=UTC).strftime("%Y-%m-%d")
        if stamped_day == today and stat.st_size < self._max_bytes:
            return

        self._file.close()
        try:
            current_path.rename(self._archive_path(stamped_day))
        except OSError:
            # Windows：其他 Logger 实例仍持有 bandi.log 句柄时 rename 被拒。
            # 轮转失败不得留关闭句柄（否则后续写入全丢）——重开当前文件继续写
            # 下一次触发时再试（日志不丢，只是归档延迟到句柄释放后的触发点）。
            self._file = self._open_current_log()
            return
        self._file = self._open_current_log()
        self._cleanup_old_logs()

    def _cleanup_old_logs(self) -> None:
        """两道清理：① 超过 retention_days 的按天淘汰；② 份数超过 backup_count 的淘汰最旧。"""
        archives = sorted(self._log_dir.glob("bandi-*.log"), key=lambda p: p.stat().st_mtime)
        cutoff = time.time() - self._retention_days * 86400
        kept: list[Path] = []
        for p in archives:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                continue
            kept.append(p)
        # 单日高频会话可能在一天内产生远超 backup_count 的档——按份数再削一次（保最新）
        for p in kept[: max(0, len(kept) - self._backup_count)]:
            p.unlink()

    def _log(self, level: LogLevel, module: str, message: str, **extra: Any) -> None:
        if level.value < self._level.value:
            return
        if self._pii_detector:
            message = self._pii_detector(message)
        with self._lock:
            self._rotate_if_needed()
            if self._file.closed:
                # close() 之后仍被长生命周期消费者写入（进程级日志桥跨 run 复用
                # 同一 Logger 引用）——自愈重开：主链日志不因某次 close 永久静默。
                self._file = self._open_current_log()
            if not extra.get("trace_id"):
                sid = extra.get("session_id")
                extra = {
                    **extra,
                    "trace_id": hashlib.sha1(sid.encode()).hexdigest()[:12]
                    if sid
                    else uuid.uuid4().hex[:12],
                }
            entry = LogEntry(
                timestamp=datetime.now(UTC).isoformat(),
                level=LEVEL_NAMES[level.value],
                module=module,
                message=message,
                meta={k: v for k, v in extra.items() if k not in _TOP_LEVEL_LOG_FIELDS},
                **{k: v for k, v in extra.items() if k in _TOP_LEVEL_LOG_FIELDS},
            )
            raw = asdict(entry)
            masked = mask_dict(raw)
            line = json.dumps(masked, ensure_ascii=False) + "\n"
            self._file.write(line)
            self._file.flush()
            if self._console_output:
                self._console_print(entry)

    def _console_print(self, entry: LogEntry) -> None:
        style_map = {
            "ERROR": "\033[91m",
            "WARNING": "\033[93m",
            "INFO": "\033[92m",
            "DEBUG": "\033[90m",
            "CRITICAL": "\033[91;1m",
        }
        reset = "\033[0m"
        color = style_map.get(entry.level, "")
        sys.stderr.write(f"{color}[{entry.level}][{entry.module}] {entry.message}{reset}\n")

    def debug(self, module: str, msg: str, **kw: Any) -> None:
        self._log(LogLevel.DEBUG, module, msg, **kw)

    def info(self, module: str, msg: str, **kw: Any) -> None:
        self._log(LogLevel.INFO, module, msg, **kw)

    def warning(self, module: str, msg: str, **kw: Any) -> None:
        self._log(LogLevel.WARNING, module, msg, **kw)

    def error(self, module: str, msg: str, **kw: Any) -> None:
        self._log(LogLevel.ERROR, module, msg, **kw)

    async def on_error(self, event: object) -> None:
        data = event.data if hasattr(event, "data") else {}
        msg = data.get("error", str(data))
        self.error("system", msg, **{k: v for k, v in data.items() if k != "error"})

    async def on_degradation(self, event: object) -> None:
        data = event.data if hasattr(event, "data") else {}
        msg = data.get("message", str(data))
        self.warning("system", msg, **{k: v for k, v in data.items() if k != "message"})

    def close(self) -> None:
        if hasattr(self, "_file") and self._file and not self._file.closed:
            self._file.close()
