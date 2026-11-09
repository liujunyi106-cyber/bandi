"""契约：长期记忆按**性质**落点，不按「对话发生在哪个入口」落点。

老板口径（2026-11-06）：会话（助理/员工私聊/群聊）都按会话隔离，那只该管情节与
草稿；长期记忆要被**所有入口**检索到 —— 在助理那里说「以后叫我老板」，员工私聊
也必须叫得出来。

缺陷实证：`agent_runner` 的 dm/group 分支把 long_term 一律写进 `dm:<hash>`，
于是「在员工私聊里教的身份/偏好」出不了该会话；而读侧
（`memory_manager.collect_memory_context`、`memory_provider.search_dm_all`）
本来就会查全局层，形成读写不对称。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bandi.agent.base import TerminalReason
from bandi.core.agent_runner import _long_term_scope_for, _persist_memory
from bandi.memory.dm_scope import dm_scope_id
from bandi.memory.episodic import EpisodicStore
from bandi.memory.extraction import extract_memory_fields
from bandi.memory.long_term import LongTermMemory
from bandi.memory.memory_provider import MemoryManager
from bandi.memory.memory_types import MemoryType


class TestScopeByKind:
    def test_identity_is_preference_type(self) -> None:
        """「以后叫我老板」必须判成偏好（否则分流拿不到全局层）。"""
        assert extract_memory_fields("以后叫我老板").memory_type == MemoryType.PREFERENCE
        assert extract_memory_fields("记住：称呼我总监").memory_type == MemoryType.PREFERENCE

    @pytest.mark.parametrize(
        "text",
        ["以后叫我老板", "记住：称呼我总监", "我偏好报告用中文", "以后都用 CSV 导出"],
    )
    def test_preference_goes_global(self, text: str) -> None:
        """偏好/身份/称呼 → 全局层，与会话作用域无关。"""
        fields = extract_memory_fields(text)
        project, employee = _long_term_scope_for(
            dm_scope_id("p1", "conv-a"), None, fields.memory_type, project_id="p1"
        )
        assert (project, employee) == (None, None), f"{text} 落到了 {project}/{employee}"

    def test_business_fact_stays_in_session_scope(self) -> None:
        """业务事实留在原作用域：不因为一条查询结果就跨会话污染。"""
        scope = dm_scope_id("p1", "conv-a")
        fields = extract_memory_fields("这次导出的表名是 staff_dedup")
        project, employee = _long_term_scope_for(scope, None, fields.memory_type, project_id="p1")
        assert (project, employee) == (scope, None)

    def test_process_conclusion_goes_project(self) -> None:
        """过程结论是项目共同约束 → 项目层（团队里其他人也该看到）。"""
        fields = extract_memory_fields("结论：交付文档统一放 交付文档收集/ 目录")
        project, employee = _long_term_scope_for(
            dm_scope_id("p1", "conv-a"), None, fields.memory_type, project_id="p1"
        )
        assert (project, employee) == ("p1", None)

    def test_conclusion_without_project_keeps_scope(self) -> None:
        """没有项目上下文时不得凭空造一个作用域。"""
        scope = dm_scope_id("", "conv-a")
        project, employee = _long_term_scope_for(
            scope, None, MemoryType.PROCESS_CONCLUSION, project_id=None
        )
        assert (project, employee) == (scope, None)


def _manager(tmp_path: Path) -> tuple[MemoryManager, LongTermMemory]:
    long_term = LongTermMemory(str(tmp_path / "long.db"))
    episodic = EpisodicStore(str(tmp_path / "epi.db"))
    return MemoryManager(long_term=long_term, episodic=episodic), long_term


def _dm_ctx(conversation_id: str, raw_input: str) -> SimpleNamespace:
    return SimpleNamespace(
        meta={"dm_conversation_id": conversation_id},
        raw_input=raw_input,
        turn_index=1,
        terminal_reason=TerminalReason.COMPLETED,
        final_message="已确认，记住了这条。",
        current_content="",
        messages=[],
        tool_results=[],
        bus=AsyncMock(),
    )


def test_dm_identity_lands_in_global_scope(tmp_path: Path) -> None:
    """端到端：员工私聊里教的称呼必须落全局层——另一条会话、助理会话都能读到。"""
    manager, long_term = _manager(tmp_path)
    asyncio.run(
        _persist_memory(
            _dm_ctx("dm-a", "记住：以后叫我老板"),
            manager,
            "s1",
            project_id="p1",
            employee_id="emp-1",
        )
    )

    global_items = long_term.list_items(project_id=None, employee_id=None)
    assert any("老板" in item.content for item in global_items), (
        f"称呼未落全局层，全局条目={[i.content[:30] for i in global_items]}"
    )
    assert not long_term.list_items(project_id=dm_scope_id("p1", "dm-a"), employee_id=None)


def test_dm_task_fact_does_not_leak_to_global(tmp_path: Path) -> None:
    """破坏者视角：会话内的任务事实不得因为分流而跑到全局层。"""
    manager, long_term = _manager(tmp_path)
    from bandi.memory.staging import MemoryPendingStore, reset_pending_store

    reset_pending_store(MemoryPendingStore(str(tmp_path / "memory")))
    asyncio.run(
        _persist_memory(
            _dm_ctx("dm-b", "临时规范：导出表统一命名 staff_dedup_v2"),
            manager,
            "s2",
            project_id="p1",
            employee_id="emp-1",
        )
    )

    assert long_term.list_items(project_id=None, employee_id=None) == [], "会话事实不得落到全局层"
    # 非显式记住的会话事实先进草稿池，落点仍是本会话作用域（批准后按该维度重放）
    from bandi.memory.staging import GATE_DRAFT, get_pending_store

    drafts = get_pending_store().list_pending(GATE_DRAFT)
    scoped = [
        record
        for record in drafts
        if "staff_dedup" in json.dumps(record.payload, ensure_ascii=False)
    ]
    assert scoped, "会话事实应进草稿池而不是直接落库"
    try:
        assert scoped[0].payload.get("project_id") == dm_scope_id("p1", "dm-b"), (
            "草稿的落点维度必须留在本会话"
        )
    finally:
        reset_pending_store()


class _RecordingHierarchy:
    """记录 markdown 写入调用（不落盘），用于断言注入源的内容形态。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def append_memory(self, scope: str, content: str, **kwargs) -> None:
        self.calls.append((scope, content))


def test_markdown_carries_sediment_not_transcript(tmp_path: Path) -> None:
    """dm/group 的 markdown 只装沉淀条目——不得再写「用户: X → 回答: Y」原文快照。

    原文快照会在注入时被原样读回，等于把上一轮对话当记忆复读，且绕过噪声闸门与
    价值门。"""
    manager, _long_term = _manager(tmp_path)
    hierarchy = _RecordingHierarchy()

    asyncio.run(
        _persist_memory(
            _dm_ctx("dm-md1", "记住：以后叫我老板"),
            manager,
            "s-md1",
            hierarchy,
            project_id="p1",
            employee_id="emp-1",
        )
    )

    assert hierarchy.calls, "有沉淀时必须写 markdown（注入源靠它）"
    _scope, content = hierarchy.calls[0]
    assert "→ 回答:" not in content, f"markdown 又写成了对话原文：{content!r}"
    assert "老板" in content, f"markdown 内容应是沉淀断言：{content!r}"


def test_markdown_not_written_for_chitchat(tmp_path: Path) -> None:
    """破坏者视角：寒暄/无沉淀的回合不得写 markdown（否则注入源又被噪音填满）。"""
    manager, _long_term = _manager(tmp_path)
    hierarchy = _RecordingHierarchy()

    chitchat = _dm_ctx("dm-md2", "你好")
    chitchat.final_message = "老板好！有什么可以帮你的？"
    asyncio.run(
        _persist_memory(chitchat, manager, "s-md2", hierarchy, project_id="p1", employee_id="emp-1")
    )

    assert hierarchy.calls == [], f"寒暄轮不该写 markdown：{hierarchy.calls}"
