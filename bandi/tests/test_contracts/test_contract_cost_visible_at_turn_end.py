"""契约：本轮 token 必须在终帧到达前即可查。

缺陷实证：成本台账由后台消费线程异步落库，`run_agent_turn` 返回那一刻记录可能
还在队列里。前端 assistant 消息在流式首字就已进 store，右栏「Token 用量」的重拉
因此早于落库，拿到的是上一轮的数；此后无新信号 ⇒ 数字停在旧值，切会话才刷新。

本契约钉行为不钉实现：轮末返回那一刻用**只读连接**查库，本轮请求必须已落盘。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from bandi.observability.cost_store import reset_shared_cost_stores

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    reset_shared_cost_stores()
    return tmp_path


def _cost_rows(db_path: Path, session_id: str) -> list[tuple[int, int]]:
    """只读连接查已落盘记录——绕过一切内存态，只认提交过的行。"""
    if not db_path.exists():
        return []
    conn = sqlite3.connect("file:" + str(db_path).replace("\\", "/") + "?mode=ro", uri=True)
    try:
        return [
            (int(a), int(b))
            for a, b in conn.execute(
                "select input_tokens, output_tokens from llm_request_log where session_id = ?",
                (session_id,),
            )
        ]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_turn_end_cost_already_committed(home: Any) -> None:
    """轮末返回时本轮请求必须已落库，且 flush 被调用过。"""
    import bandi.observability.cost_store as store_mod
    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.config import default_cost_db_path
    from bandi.core.message import LLMResponse, TokenUsage
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    class _CountingProvider(MockProvider):
        @property
        def supports_streaming(self) -> bool:
            return False

        async def complete(self, messages, tools=None, **kwargs):
            return LLMResponse(
                content="回答内容",
                usage=TokenUsage(prompt_tokens=50, completion_tokens=30),
            )

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")

    flushes: list[int] = []
    cost_db = default_cost_db_path()
    shared = store_mod.get_shared_cost_store(cost_db)
    real_flush = shared.flush

    def _spy_flush(timeout: float = 5.0) -> None:
        flushes.append(1)
        real_flush(timeout)

    shared.flush = _spy_flush  # type: ignore[method-assign]

    with patch("bandi.core.agent_runner.build_provider", return_value=_CountingProvider()):
        result = await run_agent_turn(
            user_input="你好呀，你能帮我做什么？",
            session_id=sid,
            session_store=store,
        )
    store.close()

    assert result.total_tokens == 80, f"本轮 token 应为 50+30，实际 {result.total_tokens}"
    assert flushes, "轮末必须 flush 成本台账，否则终帧到达时 cost-detail 读不到本轮 token"
    rows = _cost_rows(Path(cost_db), sid)
    assert rows, "本轮 LLM 请求必须已落盘（不刷库则终帧时查不到）"
    assert (50, 30) in rows, f"落盘记录须含本轮 usage，实际 {rows}"
    store_mod.reset_shared_cost_stores()
