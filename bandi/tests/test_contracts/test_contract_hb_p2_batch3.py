# 功能：契约——批 3（8 项，收官）
# 钉行为：
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# ── consolidation 报告 ──────────────────────────────────────────────


class TestConsolidationReportStaged:
    @pytest.fixture(autouse=True)
    def _pending_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BANDI_MEMORY_PENDING_DIR", str(tmp_path / "pending"))

    def test_staged_not_counted_as_promoted(self) -> None:
        from bandi.memory.consolidation import consolidate_episodes
        from bandi.memory.episodic import Episode, EpisodicStore
        from bandi.memory.long_term import LongTermMemory

        episodic = EpisodicStore(":memory:")
        long_term = LongTermMemory()
        ep = episodic.record(Episode(summary="高价值归并经历", importance=1.0, recall_count=99))
        report = consolidate_episodes(episodic, long_term)
        assert hasattr(report, "staged"), "报告必须提供 staged 维度（否则失真不可见）"
        assert ep.episode_id in report.staged, "background 来源恒 staged（gate 判定）"
        assert ep.episode_id not in report.promoted, "staged 不得记入 promoted（报告失真）"


# ── 降级记账 model 字段 ─────────────────────────────────────────────


class TestResponseModelName:
    def test_provider_name_takes_precedence(self) -> None:
        from bandi.agent.states import _response_model_name

        ctx = SimpleNamespace(
            provider_name="actual-model-9", provider=SimpleNamespace(model_name="stub")
        )
        assert _response_model_name(ctx) == "actual-model-9", (
            "降级后必须按实际调用模型记账（原恒取首选 ctx.provider.model_name）"
        )
        ctx2 = SimpleNamespace(provider_name="", provider=SimpleNamespace(model_name="stub"))
        assert _response_model_name(ctx2) == "stub"


# ── web_fetch 流式上限 ──────────────────────────────────────────────


class _FakeStream:
    def __init__(self, chunks: list[bytes], counter: list[int]) -> None:
        self._chunks = chunks
        self._counter = counter
        self.status_code = 200
        self.headers = {"content-type": "text/html"}
        self.url = "https://example.com/x"

    def raise_for_status(self) -> None:
        return None

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *a: object) -> bool:
        return False

    async def aiter_bytes(self):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            self._counter[0] += 1
            yield chunk


class _FakeClient:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.iterated = [0]

    def stream(self, method: str, url: str, **kwargs: object) -> _FakeStream:
        return _FakeStream(self._chunks, self.iterated)

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *a: object) -> bool:
        return False


class TestWebFetchStreamCap:
    def test_stops_consuming_after_cap(self) -> None:
        from bandi.tools.web_fetch import WebFetchTool

        chunks = [b"a" * 30, b"b" * 30, b"c" * 30]
        client = _FakeClient(chunks)
        tool = WebFetchTool(max_bytes=50, allow_private_networks=True)
        with patch("bandi.tools.web_fetch._build_guarded_client", lambda *a, **k: client):
            out = asyncio.run(tool.execute({"url": "https://example.com/x"}))
        assert client.iterated[0] < len(chunks), "超限必须停止消费（流式读取，不得全量入内存）"
        assert "[truncated]" in out


# ── hook 超时杀进程树 ───────────────────────────────────────────────


class TestHookTimeoutKillsTree:
    def test_timeout_uses_kill_process_tree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bandi.tools.hooks as hooks_mod

        calls: list[object] = []
        real = hooks_mod.kill_process_tree

        def spy(proc: object) -> None:
            calls.append(proc)
            real(proc)

        monkeypatch.setattr(hooks_mod, "kill_process_tree", spy)
        runner = object.__new__(hooks_mod.HookRunner)
        rule = hooks_mod.HookRule(
            event="PreToolUse",
            action_type="command",
            command="ping -n 30 127.0.0.1",
            timeout=0.5,
        )
        outcome = asyncio.run(runner._run_command(rule, {"k": "v"}))
        assert calls, "超时必须走 kill_process_tree（不只杀直接子进程）"
        assert any("timed out" in e for e in outcome.hook_errors)


# ── write 原子 + fsync ──────────────────────────────────────────────


class TestWriteAtomicFsync:
    def test_fsync_and_atomic_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
        import bandi.tools.file_ops as fo
        from bandi.tools.file_ops import FileWriteTool

        fsync_calls: list[int] = []
        monkeypatch.setattr(fo.os, "fsync", lambda fd: fsync_calls.append(fd))
        tool = FileWriteTool()
        target = tmp_path / "w.txt"
        asyncio.run(tool.execute({"path": "w.txt", "content": "hello"}))
        assert fsync_calls, "写入必须 fsync（崩溃不丢数据）"
        assert target.read_text(encoding="utf-8") == "hello"

        def boom(*a: object) -> None:
            raise OSError("replace boom")

        monkeypatch.setattr(fo.os, "replace", boom)
        with pytest.raises(OSError):
            asyncio.run(tool.execute({"path": "w.txt", "content": "new"}))
        monkeypatch.undo()
        assert target.read_text(encoding="utf-8") == "hello", "replace 失败不得半写（原子）"


# ── InteractionStore 清理 ───────────────────────────────────────────


class TestInteractionPurgesTerminal:
    def test_resolved_expired_beyond_retention_removed(self) -> None:
        from bandi.core.user_interaction import INTERACTION_KIND_CONFIRM, InteractionStore

        store = InteractionStore(ttl_seconds=0.2)
        store.register(session_id="s3", kind=INTERACTION_KIND_CONFIRM)
        iid = next(iter(store._items))
        assert store.resolve(iid, True) is True
        item = store._items[iid]
        item.resolved_at = time.time() - 10.0
        item.created_at = item.created_at - 10.0
        store._expire_stale()
        assert iid not in store._items, "终态条目超保留期必须清理（防无界驻留）"


# ── UIStateStore 连接关闭 ───────────────────────────────────────────


class _TrackedConn:
    def __init__(self, real: object) -> None:
        self._real = real
        self.closed = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)

    def __enter__(self) -> object:
        return self._real.__enter__()  # type: ignore[attr-defined]

    def __exit__(self, *a: object) -> object:
        return self._real.__exit__(*a)  # type: ignore[attr-defined]

    def close(self) -> None:
        self.closed = True
        self._real.close()  # type: ignore[attr-defined]


class TestUiStateClosesConnections:
    def test_every_operation_closes_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import bandi.cli.ui_state as ui_mod

        tracked: list[_TrackedConn] = []
        real_connect = ui_mod.connect

        def factory(path: object) -> _TrackedConn:
            conn = _TrackedConn(real_connect(path))
            tracked.append(conn)
            return conn

        monkeypatch.setattr(ui_mod, "connect", factory)
        store = ui_mod.UIStateStore(str(tmp_path / "ui.db"))
        store._set("k", "v")
        assert store._get("k") == "v"
        assert len(tracked) >= 3  # init + set + get
        assert all(c.closed for c in tracked), "每次操作的连接必须显式关闭（防泄漏）"


# ── MetricsBridge 计时表清理 ────────────────────────────────────────


class TestMetricsBridgePopsToolStart:
    def test_tool_start_removed_on_completed_and_error(self) -> None:
        from bandi.observability.bridge import MetricsBridge

        bridge = MetricsBridge()
        asyncio.run(
            bridge.on_tool_invoked(SimpleNamespace(data={"tool_name": "t1", "tool_call_id": "c1"}))
        )
        assert bridge._tool_start
        asyncio.run(
            bridge.on_tool_completed(
                SimpleNamespace(data={"tool_name": "t1", "tool_call_id": "c1"})
            )
        )
        assert not bridge._tool_start, "完成后必须 pop（防无界累积）"

        asyncio.run(
            bridge.on_tool_invoked(SimpleNamespace(data={"tool_name": "t1", "tool_call_id": "c2"}))
        )
        asyncio.run(
            bridge.on_tool_error(
                SimpleNamespace(data={"tool_name": "t1", "tool_call_id": "c2", "error": "x"})
            )
        )
        assert not bridge._tool_start, "失败路径同样必须 pop"


# ── share token TTL 清理 ────────────────────────────────────────────


class TestShareTokenTtl:
    def test_expired_share_token_purged(self) -> None:
        import sys

        app_mod = sys.modules["bandi.api.app"]
        with app_mod._share_lock:
            app_mod._share_tokens["tok-old"] = "s-old"
            app_mod._share_token_times["tok-old"] = 0.0  # 远古创建
            app_mod._share_tokens["tok-new"] = "s-new"
            app_mod._share_token_times["tok-new"] = time.time()
        try:
            app_mod._purge_share_tokens()
            with app_mod._share_lock:
                assert "tok-old" not in app_mod._share_tokens, (
                    "超龄 share token 必须清理（防无界驻留）"
                )
                assert "tok-new" in app_mod._share_tokens, "未超龄 token 不得误删"
        finally:
            with app_mod._share_lock:
                app_mod._share_tokens.pop("tok-old", None)
                app_mod._share_tokens.pop("tok-new", None)
                app_mod._share_token_times.pop("tok-old", None)
                app_mod._share_token_times.pop("tok-new", None)
