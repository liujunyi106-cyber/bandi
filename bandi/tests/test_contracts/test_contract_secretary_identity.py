# 功能：契约——无项目会话「秘书」身份（无项目归属的会话由秘书接管）。
# 钉行为：无项目 → 系统提示词含秘书身份；有项目 → 成员身份且不含秘书；
# 静态缓存键区分两变体（互不串用）；ContextLoader 的 project_scoped 忠实反映 project_id。
from __future__ import annotations

import asyncio
from pathlib import Path

from bandi.agent.prompt_harness.builder import PromptHarness
from bandi.core.message import Message
from bandi.perception.context_loader import ContextBundle, ContextLoader

SECRETARY_MARK = "你是 Bandi 助理"
MEMBER_MARK = "你是 Bandi 数字员工组织的成员助手"


class TestSecretaryIdentity:
    def test_no_project_uses_secretary_identity(self) -> None:
        harness = PromptHarness(preferred_language="zh-CN")
        static = harness.build_sections("execute", ContextBundle(project_scoped=False)).static
        assert static.startswith(SECRETARY_MARK), "无项目会话的身份段必须是秘书（排最前）"
        assert not static.startswith(MEMBER_MARK)

    def test_project_uses_member_identity(self) -> None:
        harness = PromptHarness(preferred_language="zh-CN")
        static = harness.build_sections("execute", ContextBundle(project_scoped=True)).static
        assert static.startswith(MEMBER_MARK)
        assert not static.startswith(SECRETARY_MARK)

    def test_static_cache_does_not_cross_contaminate(self) -> None:
        """破坏者：两变体交替构建，缓存键必须区分（成员缓存命中不得被秘书污染，反之亦然）。"""
        harness = PromptHarness(preferred_language="zh-CN")
        member = harness.build_sections("execute", ContextBundle(project_scoped=True)).static
        secretary = harness.build_sections("execute", ContextBundle(project_scoped=False)).static
        assert member.startswith(MEMBER_MARK)
        assert secretary.startswith(SECRETARY_MARK)
        member_again = harness.build_sections("execute", ContextBundle(project_scoped=True)).static
        assert member_again == member, "成员变体缓存命中不得被秘书变体污染"

    def test_context_loader_sets_project_scoped(self, tmp_path: Path) -> None:
        """链路：ContextLoader 有/无 project_id → bundle.project_scoped 忠实反映。"""
        from bandi.memory.session.store import SessionStore

        db_dir = tmp_path / "db"
        db_dir.mkdir()
        store = SessionStore(str(db_dir / "s.db"))
        message = Message(role="user", content="你好")
        scoped = asyncio.run(ContextLoader(store, project_id="p1").load(message, session_id=""))
        unscoped = asyncio.run(ContextLoader(store).load(message, session_id=""))
        assert scoped.project_scoped is True
        assert unscoped.project_scoped is False
