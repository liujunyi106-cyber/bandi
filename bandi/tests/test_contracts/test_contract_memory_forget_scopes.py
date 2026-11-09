# 功能：memory_forget 全 scope 契约——episodic/working/global 显式遗忘（4.3 收口）
# 经历层（episodic）/工作区（working）/全局层（global）只能等容量或 TTL 被动淘汰
# 敏感/错误记忆无法主动清除。本契约钉「删除最终产物 + 歧义拒删 + 生产注入链」。
from __future__ import annotations

import asyncio
from typing import Any

from bandi.core.message import ToolCall
from bandi.memory.episodic import Episode, EpisodicStore
from bandi.memory.global_store import GlobalMemoryStore
from bandi.memory.working import ScratchpadStore
from bandi.tools.base import ToolPermissionContext
from bandi.tools.executor import ToolExecutor
from bandi.tools.memory_tools import MemoryForgetTool
from bandi.tools.registry import ToolRegistry


class TestEpisodicForget:
    def test_forget_deletes_episode_row(self, tmp_path: Any) -> None:
        """按 id 遗忘经历：主表行删除且检索不可达（非 TTL 淘汰路径）。"""
        store = EpisodicStore(str(tmp_path / "episodic.db"))
        episode = store.record(Episode(summary="部署事故：连接池耗尽导致超时", session_id="s1"))
        tool = MemoryForgetTool(db_dir=tmp_path, episodic=store)
        result = asyncio.run(
            tool.execute({"scope": "episodic", "id": episode.episode_id}, ToolPermissionContext())
        )
        assert result.get("forgotten") is True, result
        assert store.get(episode.episode_id) is None
        assert store.search("连接池耗尽") == []

    def test_ambiguous_query_rejects_batch_delete(self, tmp_path: Any) -> None:
        """破坏者：query 命中多条经历 → 拒绝删除，两条都必须还在。"""
        store = EpisodicStore(str(tmp_path / "episodic.db"))
        store.record(Episode(summary="缓存策略讨论记录", session_id="s1"))
        store.record(Episode(summary="缓存失效复盘记录", session_id="s1"))
        tool = MemoryForgetTool(db_dir=tmp_path, episodic=store)
        result = asyncio.run(
            tool.execute({"scope": "episodic", "query": "缓存"}, ToolPermissionContext())
        )
        assert result.get("forgotten") is False
        assert store.count() == 2


class TestWorkingForget:
    def test_forget_deletes_kv_key(self, tmp_path: Any) -> None:
        """按 key 遗忘工作区 KV 快照（含 FTS 索引同步，检索不可达）。"""
        store = ScratchpadStore(str(tmp_path / "working.db"))
        store.set("draft", "临时草稿：不要提交的敏感内容", session_id="s1")
        tool = MemoryForgetTool(db_dir=tmp_path, working=store)
        context = ToolPermissionContext(extra={"session_id": "s1"})
        result = asyncio.run(tool.execute({"scope": "working", "id": "draft"}, context))
        assert result.get("forgotten") is True, result
        assert store.get("draft", session_id="s1") is None
        assert store.search("敏感内容", session_id="s1") == []

    def test_executor_injects_session_for_working_scope(self, tmp_path: Any) -> None:
        """生产注入链（DoD-12）：真实 executor 注入 session_id → working 遗忘可达。"""
        store = ScratchpadStore(str(tmp_path / "working.db"))
        store.set("task-snapshot", "用户要清理的中间产物", session_id="sess-9")
        registry = ToolRegistry()
        registry.register_instance(MemoryForgetTool(db_dir=tmp_path, working=store))
        executor = ToolExecutor(registry)
        executor.bind_session_meta({"session_id": "sess-9"})
        results = asyncio.run(
            executor.execute(
                [
                    ToolCall(
                        id="c1",
                        name="memory_forget",
                        arguments={"scope": "working", "id": "task-snapshot"},
                    )
                ]
            )
        )
        assert results and results[0].success, results
        assert store.get("task-snapshot", session_id="sess-9") is None


class TestGlobalForget:
    def test_forget_deletes_global_key(self, tmp_path: Any) -> None:
        """按 key 遗忘全局层记忆（跨项目通用事实的显式清除）。"""
        store = GlobalMemoryStore(tmp_path / "global.db")
        store.put_global_memory("用户偏好", "偏好简体中文回复")
        tool = MemoryForgetTool(db_dir=tmp_path, global_store=store)
        result = asyncio.run(
            tool.execute({"scope": "global", "id": "用户偏好"}, ToolPermissionContext())
        )
        assert result.get("forgotten") is True, result
        assert store.get("用户偏好") is None

    def test_unknown_scope_rejected(self, tmp_path: Any) -> None:
        """未知 scope 显式报错（不猜、不误删）。"""
        tool = MemoryForgetTool(db_dir=tmp_path)
        result = asyncio.run(tool.execute({"scope": "bogus", "id": "x"}, ToolPermissionContext()))
        assert "error" in result
