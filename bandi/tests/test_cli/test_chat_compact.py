"""
与自动链共享 CompressionPipeline（boundary 标记由 pipeline 层统一插入）。
"""

import io
from pathlib import Path

import pytest
from rich.console import Console

from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage
from bandi.memory.session.store import SessionStore


class _SummaryStubProvider:
    """功能：摘要替身——complete 返回五节结构化摘要，count_tokens 按 4 字符/Token 估算。"""

    def __init__(self, summary_text: str) -> None:
        self._summary_text = summary_text
        self.call_count = 0

    async def complete(self, messages, tools=None, **kwargs):
        self.call_count += 1
        return LLMResponse(
            content=self._summary_text,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


_SUMMARY = (
    "## 目标\n- 验证 /compact fork 语义\n\n"
    "## 进度\n### Done\n- [x] 写入历史\n"
    "### In Progress\n- [ ] 压缩\n"
    "### Blocked\n- (none)\n\n"
    "## 关键决策\n- **fork 新会话**: append-only 存储约束\n\n"
    "## 下一步\n1. 在新会话继续\n\n"
    "## 关键上下文\n- 原会话 logs/orig.py 保留不动\n"
)


def _make_store(tmp_path: Path) -> SessionStore:
    return SessionStore(str(tmp_path / "sessions.db"))


def _seed_history(store: SessionStore, sid: str, rounds: int = 6) -> None:
    """功能：写入真实形态长对话历史（含中文路径与错误串）。"""
    store.create_session(sid, title="原始会话")
    store.append_message(sid, Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"))
    store.append_message(
        sid, Message(role=MessageRole.USER, content="请修复 logs/orig.py 的 ValueError('seed')")
    )
    for i in range(rounds):
        store.append_message(
            sid,
            Message(
                role=MessageRole.USER,
                content=f"第{i}轮排查：logs/run_{i}.py 第{i}行报 ValueError('compaction failed')，请分析根因并给出修复补丁。",
            ),
        )
        store.append_message(
            sid,
            Message(
                role=MessageRole.ASSISTANT,
                content=f"第{i}轮已定位 memory/compaction.py 分支缺失，见补丁 diff_{i}.txt",
            ),
        )


def _make_loop(store: SessionStore, session_id: str | None) -> object:
    from bandi.cli.chat import ChatLoop

    console = Console(file=io.StringIO(), width=120)
    loop = ChatLoop(
        console=console,
        session_store=store,
        llm_provider=_SummaryStubProvider(_SUMMARY),
    )
    loop._state.session_id = session_id
    return loop


@pytest.mark.asyncio
async def test_compact_replaces_current_session_with_boundary_and_summary(tmp_path) -> None:
    """/compact 后：当前会话内替换，含 [上下文摘要]/保留段，不产生新会话。"""
    store = _make_store(tmp_path)
    _seed_history(store, "s1")

    loop = _make_loop(store, "s1")
    should_exit = await loop._handle_command("/compact")
    assert should_exit is False

    assert loop._state.session_id == "s1", "/compact 必须留在当前会话"
    entries = store.get_branch("s1")
    assert entries, "当前会话必须有压缩产物"
    contents = [e.get("content") or "" for e in entries]
    assert any("[上下文摘要]" in c for c in contents), "当前会话必须含压缩摘要"
    assert store.get_session("s1") is not None
    full = store.get_full_branch("s1")
    assert any(e.get("entry_type") == "summary" for e in full), "边界摘要必须落库"
    assert any(e.get("entry_type") == "message" for e in full), "原始历史仍可审计"


@pytest.mark.asyncio
async def test_compact_reports_stats(tmp_path) -> None:
    """/compact 必须输出状态报告（前后消息数），供用户确认释放效果。"""
    import io as _io

    store = _make_store(tmp_path)
    _seed_history(store, "s2")

    from rich.console import Console as _Console

    from bandi.cli.chat import ChatLoop

    buf = _io.StringIO()
    loop = ChatLoop(
        console=_Console(file=buf, width=160),
        session_store=store,
        llm_provider=_SummaryStubProvider(_SUMMARY),
    )
    loop._state.session_id = "s2"
    await loop._handle_command("/compact")

    out = buf.getvalue()
    assert "压缩" in out, "报告必须提及压缩"
    assert "→" in out or "->" in out, "报告必须包含前后对比数字"


@pytest.mark.asyncio
async def test_compact_without_active_session_fails_closed(tmp_path) -> None:
    """无活跃会话时 /compact 必须提示错误且不创建任何会话（fail-closed）。"""
    store = _make_store(tmp_path)
    loop = _make_loop(store, None)
    before = len(store.list_sessions(limit=100))

    await loop._handle_command("/compact")

    assert loop._state.session_id is None, "不得凭空切换会话"
    assert len(store.list_sessions(limit=100)) == before, "不得创建新会话"


@pytest.mark.asyncio
async def test_compact_nonexistent_session_fails_closed(tmp_path) -> None:
    """session_id 在 store 中不存在时必须提示错误且不创建会话（fail-closed）。"""
    store = _make_store(tmp_path)
    loop = _make_loop(store, "ghost-session")
    before = len(store.list_sessions(limit=100))

    await loop._handle_command("/compact")

    assert len(store.list_sessions(limit=100)) == before, "不得创建新会话"


def test_compact_registered_in_slash_commands() -> None:
    """/compact 必须注册进命令表（防漏注册导致未知命令）。"""
    from bandi.cli.chat import ChatLoop

    assert "/compact" in ChatLoop.SLASH_COMMANDS


@pytest.mark.asyncio
async def test_compact_self_builds_provider_when_not_injected(tmp_path) -> None:
    """未注入 provider 时 /compact 必须按生产路径自建 provider（覆盖真实导入路径——
    坏模块名会让该分支直接 ModuleNotFoundError，而注入型测试覆盖不到）。"""
    from unittest.mock import patch

    from bandi.cli.chat import ChatLoop

    store = _make_store(tmp_path)
    _seed_history(store, "s1")
    loop = ChatLoop(console=Console(file=io.StringIO(), width=120), session_store=store)
    loop._state.session_id = "s1"
    stub = _SummaryStubProvider(_SUMMARY)

    with patch("bandi.agent.factory.build_provider", return_value=stub):
        await loop._handle_command("/compact")

    assert stub.call_count >= 1, "自建 provider 必须被用于生成摘要"
    assert loop._state.session_id == "s1", "/compact 必须留在当前会话"
