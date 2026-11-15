# 功能：契约——副作用日志的短连接必须关闭，不得随写入次数累积句柄。
from __future__ import annotations

import sqlite3
from pathlib import Path

from bandi.tools.effect_log import EffectLog


def _log(tmp_path: Path) -> EffectLog:
    return EffectLog(path=tmp_path / "effects.db")


class TestConnectionNotLeaked:
    """`with sqlite3.Connection` 只提交事务不关闭连接，必须显式 close。"""

    def test_write_closes_connection(self, tmp_path: Path, monkeypatch) -> None:
        opened: list[sqlite3.Connection] = []
        real_connect = sqlite3.connect

        def spy(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        monkeypatch.setattr(sqlite3, "connect", spy)

        log = _log(tmp_path)
        log.record(
            session_id="s1",
            turn_index=1,
            step_key="k1",
            tool_name="bash",
            args_hash="h1",
            read_only=False,
            status="started",
        )
        log.pending("s1")

        assert opened, "必须真的建立过连接"
        for idx, conn in enumerate(opened):
            try:
                conn.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                continue  # 已关闭 = 期望结果
            raise AssertionError(f"第 {idx + 1} 个连接未关闭——句柄会随写入次数累积")

    def test_repeated_writes_do_not_accumulate(self, tmp_path: Path, monkeypatch) -> None:
        """连续写入后不应留下未关闭的连接。

        sqlite3.Connection.close 是内建方法、无法 monkeypatch，因此改为在日志模块
        的 connect 出口收集句柄，事后逐个探测是否已关闭。
        """
        from bandi.utils import db as db_mod

        opened: list[sqlite3.Connection] = []
        real_connect = db_mod.connect

        def spy(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        monkeypatch.setattr(db_mod, "connect", spy)

        log = _log(tmp_path)
        for i in range(5):
            log.record(
                session_id="s1",
                turn_index=i,
                step_key=f"k{i}",
                tool_name="bash",
                args_hash=f"h{i}",
                read_only=False,
                status="started",
            )
        log.count("s1")

        assert opened, "必须真的建立过连接"
        still_open = 0
        for conn in opened:
            try:
                conn.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                continue
            still_open += 1
        assert still_open == 0, f"仍有 {still_open} 个连接未关闭"

    def test_data_still_persisted(self, tmp_path: Path) -> None:
        """关闭连接不得影响落盘——写入后仍能读回。"""
        log = _log(tmp_path)
        log.record(
            session_id="s1",
            turn_index=2,
            step_key="k9",
            tool_name="bash",
            args_hash="h9",
            read_only=False,
            status="started",
        )
        assert log.count("s1") == 1, "写入必须落盘"
        rows = log.pending("s1")
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "bash"
