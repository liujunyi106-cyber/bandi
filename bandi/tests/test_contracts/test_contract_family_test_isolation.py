"""
写方：tests/conftest.py autouse fixture（Path.home → tmp）。
读方：agent_runner / subagent 的 CheckpointStore() 默认路径（~/.bandi/checkpoints.db）。

行为族：
  A 默认路径隔离：默认构造的 CheckpointStore 落隔离 home（tmp），不碰真实 ~/.bandi
  B 真实库护栏（防复发）：run_agent_turn 跑一回合后真实 checkpoints.db 的
    mtime 与行数不变——若 conftest 的 Path.home patch 被移除即红（守卫自身即破坏者）
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

# 功能：契约——测试隔离（扩展）：Path.home() 派生组件不写真实用户库


# 注意：module 级求值（收集期，conftest patch 生效前）——恒指向真实家目录
REAL_DB = Path.home() / ".bandi" / "checkpoints.db"


def _row_count(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    finally:
        conn.close()


class TestDefaultPathIsolated:
    def test_checkpoint_store_default_lands_in_isolated_home(self, tmp_path: Path) -> None:
        """默认构造 CheckpointStore → 库文件创建在隔离 home（tmp）。"""
        from bandi.agent.checkpoint import CheckpointStore

        CheckpointStore()
        assert (tmp_path / ".bandi" / "checkpoints.db").exists(), (
            "默认路径必须随隔离的 Path.home 派生（而非常量 ~）"
        )


class _RecordingMock:
    """功能：MockProvider 子类（complete/stream 双通道，防 通道盲区）。"""

    def __new__(cls):
        from bandi.llm.mock import MockProvider

        class _Mock(MockProvider):
            async def complete(self, messages, tools=None, **kwargs):  # type: ignore[override]
                return await super().complete(messages, tools, **kwargs)

            async def stream(self, messages, tools=None, **kwargs):  # type: ignore[override]
                async for event in super().stream(messages, tools, **kwargs):
                    yield event

        return _Mock()


class TestRealDbUntouched:
    @pytest.mark.asyncio
    async def test_run_agent_turn_does_not_write_real_checkpoint_db(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """（防复发守卫）：跑一回合后真实 checkpoints.db mtime + 行数不变。"""
        from bandi.core import agent_runner as runner_mod
        from bandi.memory.session.store import SessionStore

        before_mtime = REAL_DB.stat().st_mtime if REAL_DB.exists() else None
        before_rows = _row_count(REAL_DB) if REAL_DB.exists() else None

        monkeypatch.setattr(runner_mod, "build_provider", lambda *_a, **_k: _RecordingMock())
        store = SessionStore(str(tmp_path / "s.db"))
        await runner_mod.run_agent_turn(
            user_input="隔离验证", session_id="iso-guard-1", session_store=store
        )

        if before_mtime is None:
            assert not REAL_DB.exists(), "测试创建了真实 checkpoints.db（隔离失效）"
            return
        assert REAL_DB.stat().st_mtime == before_mtime, "测试写穿真实 checkpoints.db（mtime 变化）"
        assert _row_count(REAL_DB) == before_rows, "测试写穿真实 checkpoints.db（行数变化）"


# ==================== seen_state_isolation ====================
# 功能：契约——seen 状态持久化路径必须惰性求值（同族：测试不得写穿真实 home）


@pytest.mark.bandi_real_seen_path
def test_seen_persist_follows_home_patch(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """契约（同族修复）：seen 状态持久化路径必须惰性求值——Path.home patch
    后写入必须落在隔离目录（此前模块级常量在 import 时固化真实 home
    测试触发 persist 会写穿用户 ~/.bandi/seen_memory.json）。

    opt-out：本测试必须走真实 `_seen_store_path()` 才能验证派生语义
    故标记退出 conftest 的路径 patch（自身以 Path.home patch 隔离）。"""
    import bandi.context.sources.memory_source as ms

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with ms._SEEN_LOCK:
        ms.SEEN_BY_SESSION.clear()
        ms.SEEN_BY_SESSION["s-contract"] = {"long_term:x": 1}
    try:
        ms.persist_seen_state()
        target = tmp_path / ".bandi" / "seen_memory.json"
        assert target.exists(), "持久化必须落在 patch 后的 home 隔离目录"
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data.get("s-contract") == {"long_term:x": 1}
    finally:
        with ms._SEEN_LOCK:
            ms.SEEN_BY_SESSION.pop("s-contract", None)


def test_seen_state_containers_reset_between_tests() -> None:
    """契约（跨测试泄漏防护）：conftest 必须重置模块级容器——前一测试残留的
    已注入标记不得污染后一测试（HIERARCHY_INJECTED_BY_SESSION / SEEN_BY_SESSION
    为进程级容器，跨测试同 session_id 会误剔除/误降权）。"""
    import bandi.context.sources.memory_source as ms

    with ms._HIERARCHY_LOCK:
        snapshot_hierarchy = dict(ms.HIERARCHY_INJECTED_BY_SESSION)
    with ms._SEEN_LOCK:
        snapshot_seen = dict(ms.SEEN_BY_SESSION)
    assert snapshot_hierarchy == {}, f"测试起点 hierarchy 容器必须为空（实际 {snapshot_hierarchy}）"
    assert snapshot_seen == {}, f"测试起点 seen 容器必须为空（实际 {snapshot_seen}）"
