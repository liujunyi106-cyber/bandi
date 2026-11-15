"""
  工具副作用日志：started / succeeded / replayed 三态落库，崩溃后能列出「无终态」步骤。

破坏者断言：
- 只有 started 没有终态的步骤必须出现在 pending（否则崩溃后无法定位可疑副作用）；
- replayed（幂等命中、未真正执行）必须计入终态，不得被当作待核对步骤；
- 写入失败不得抛给调用方（日志是增强信息，不能阻断工具主链）。
"""

from typing import Any


def _log(tmp_path: Any) -> Any:
    from bandi.tools.effect_log import EffectLog

    return EffectLog(tmp_path / "effects.db")


def test_completed_step_is_not_pending(tmp_path: Any) -> None:
    log = _log(tmp_path)
    log.record(session_id="s1", turn_index=1, step_key="k1", tool_name="write", status="started")
    log.record(session_id="s1", turn_index=1, step_key="k1", tool_name="write", status="succeeded")

    assert log.pending("s1") == []
    assert log.count("s1") == 2


def test_started_without_terminal_state_is_pending(tmp_path: Any) -> None:
    log = _log(tmp_path)
    log.record(session_id="s1", turn_index=2, step_key="k2", tool_name="bash", status="started")

    pending = log.pending("s1")
    assert len(pending) == 1
    assert pending[0]["tool_name"] == "bash"
    assert pending[0]["step_key"] == "k2"


def test_replayed_counts_as_terminal(tmp_path: Any) -> None:
    log = _log(tmp_path)
    log.record(session_id="s1", turn_index=3, step_key="k3", tool_name="write", status="started")
    log.record(session_id="s1", turn_index=3, step_key="k3", tool_name="write", status="replayed")

    assert log.pending("s1") == []


def test_sessions_are_isolated(tmp_path: Any) -> None:
    log = _log(tmp_path)
    log.record(session_id="s-a", turn_index=1, step_key="ka", tool_name="bash", status="started")
    log.record(session_id="s-b", turn_index=1, step_key="kb", tool_name="bash", status="started")

    assert [row["step_key"] for row in log.pending("s-a")] == ["ka"]


def test_write_failure_is_swallowed(tmp_path: Any) -> None:
    from bandi.tools.effect_log import EffectLog

    log = EffectLog(tmp_path / "no_such_dir" / "x" / "effects.db")
    log.record(session_id="s1", step_key="k1", tool_name="write", status="started")
