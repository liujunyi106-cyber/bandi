# 功能：契约 — 冒烟抓出的三缺陷
# ① 同轮 assistant 重复落库（loop checkpoint 屏障 + run_agent_turn 轮末双写）
# ② 无项目会话的工作区根从不创建 ⇒ fs 工具族撞「不存在的根」
# ③ 破坏者：去重读失败不得误跳过真实回复（宁可多写，不丢历史）
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


class TestAssistantSinglePersist:
    """① 轮末落库必须与 checkpoint 屏障的去重口径一致。"""

    def _store(self, entries: list[dict[str, Any]]) -> Any:
        return SimpleNamespace(
            # 真实 store 的 get_branch 支持 full 关键字（P0-1），替身同签名
            get_branch=lambda _sid, **_kw: list(entries),
            append_message=lambda _sid, _msg: None,
        )

    def test_detects_duplicate_by_turn_and_content(self) -> None:
        from bandi.core.agent_runner import _assistant_already_persisted

        store = self._store(
            [
                {
                    "entry_type": "message",
                    "role": "assistant",
                    "turn_index": 1,
                    "content": "你好",
                }
            ]
        )
        assert _assistant_already_persisted(store, "s1", 1, "你好") is True
        # 同轮不同内容 = 不同回复，必须落库
        assert _assistant_already_persisted(store, "s1", 1, "别的回复") is False
        # 不同轮同内容 = 合法重复（模型可能复述），必须落库
        assert _assistant_already_persisted(store, "s1", 2, "你好") is False

    def test_ignores_non_message_entries(self) -> None:
        """process 类 entry（entry_type=process）不算已落库。"""
        from bandi.core.agent_runner import _assistant_already_persisted

        store = self._store(
            [{"entry_type": "process", "role": "assistant", "turn_index": 1, "content": "你好"}]
        )
        assert _assistant_already_persisted(store, "s1", 1, "你好") is False

    def test_read_failure_falls_back_to_not_persisted(self) -> None:
        """破坏者：store 读失败 ⇒ 视为未落过（宁可多写一条，不丢历史）。"""
        from bandi.core.agent_runner import _assistant_already_persisted

        def _boom(_sid: str) -> list[dict[str, Any]]:
            raise RuntimeError("db gone")

        store = SimpleNamespace(get_branch=_boom)
        assert _assistant_already_persisted(store, "s1", 1, "x") is False

    @pytest.mark.asyncio
    async def test_real_turn_persists_assistant_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """端到端：真实 run_agent_turn 一轮 ⇒ store 里 assistant 消息**恰好 1 条**。

        复现条件：checkpoint_interval=1 ⇒ 屏障每轮触发补写，轮末再写 ⇒ 2 条。
        """

        from bandi.agent import deps as deps_mod
        from bandi.core import agent_runner as runner_mod
        from bandi.core.agent_runner import run_agent_turn
        from bandi.core.config import BandiConfig
        from bandi.llm.base import BaseProvider
        from bandi.memory.session.store import SessionStore

        class _P(BaseProvider):
            def __init__(self) -> None:
                from bandi.core.config import LLMConfig

                super().__init__(LLMConfig())

            async def complete(self, messages: list[dict], tools: Any = None, **kw: Any) -> Any:
                from bandi.core.message import LLMResponse

                return LLMResponse(content="唯一回复")

            def stream(self, messages: list[dict], tools: Any = None, **kw: Any) -> Any:
                raise NotImplementedError

        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))

        provider = _P()
        monkeypatch.setattr(runner_mod, "build_provider", lambda _cfg: provider)

        async def _call(ctx: Any, messages: list[dict]) -> Any:
            return await provider.complete(messages)

        base = deps_mod.make_default_deps()
        monkeypatch.setattr(
            deps_mod,
            "make_default_deps",
            lambda: deps_mod.QueryDeps(
                call_model=_call,
                autocompact=base.autocompact,
                microcompact=base.microcompact,
                new_id=base.new_id,
            ),
        )

        cfg = BandiConfig()
        cfg.verification.enabled = False
        store = SessionStore(str(tmp_path / "s.db"))
        sid = store.create_session(title="t")
        result = await run_agent_turn(
            user_input="你好",
            bandi_config=cfg,
            mode="execute",
            session_id=sid,
            session_store=store,
            checkpoint_interval=1,
        )
        assert result.terminal_reason
        rows = [
            e
            for e in store.get_branch(sid)
            if e.get("entry_type") == "message" and e.get("role") == "assistant"
        ]
        assert len(rows) == 1, f"同轮 assistant 应恰好落库 1 条，实际 {len(rows)}: {rows}"
        assert rows[0]["content"] == "唯一回复"
        store.close()


class TestWorkspaceRootAlwaysExists:
    """② API/桌面装配路径必须创建工作区根（此前只有 CLI `bandi init` 建）。"""

    def test_default_root_created_on_first_resolve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.delenv("BANDI_WORKSPACE_ROOT", raising=False)

        import bandi.tools.fs_tool as fs

        importlib.reload(fs)
        root = fs.workspace_root()
        assert root == home / ".bandi" / "workspace"
        assert root.exists(), f"工作区根必须自动创建（实际 {root}）"

    def test_env_override_wins_and_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        custom = tmp_path / "custom_ws"
        monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(custom))
        import bandi.tools.fs_tool as fs

        importlib.reload(fs)
        root = fs.workspace_root()
        assert root == custom
        assert root.exists(), "env 覆盖的工作区根也必须创建"

    def test_unwritable_root_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """破坏者：只读/不可创建环境不得抛——调用方按「路径不存在」报错即可。"""
        import importlib

        blocked = tmp_path / "blocked"
        blocked.mkdir()
        blocked.chmod(0o500)
        monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(blocked / "ws"))
        import bandi.tools.fs_tool as fs

        try:
            importlib.reload(fs)
            root = fs.workspace_root()
            assert root.name == "ws", "解析结果仍应返回目标路径"
        finally:
            blocked.chmod(0o700)
