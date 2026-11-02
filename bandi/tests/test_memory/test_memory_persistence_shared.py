# 功能：降级注入 + 记忆持久化验证

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


class _RecordingProvider(MockProvider):
    def __init__(self) -> None:
        super().__init__()
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.seen_messages.append(messages)
        return await super().complete(messages, tools=tools, **kwargs)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.seen_messages.append(messages)
        async for ev in super().stream(messages, tools=tools, **kwargs):
            yield ev


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_degraded_capabilities_injected(home: Any) -> None:
    """降级能力必须注入 system prompt 的 {degraded_list}。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="好的"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="hello",
            session_id=sid,
            session_store=store,
        )
    assert result.reply
    system_prompt = "\n".join(
        str(m.get("content") or "") for m in provider.seen_messages[0] if m.get("role") == "system"
    )
    assert "{degraded_list}" not in system_prompt, "未降级时不得残留占位符"


@pytest.mark.asyncio
async def test_memory_persisted_across_rebuilds(home: Any) -> None:
    """记忆写入 ~/.bandi/data/long_term.db 后，重建 MemoryManager 仍可读回。"""
    from bandi.core.agent_runner import _build_memory_manager
    from bandi.memory.long_term import MemoryItem

    db_dir = Path(home) / ".bandi" / "data"
    m1 = _build_memory_manager(db_dir)
    m1.long_term.store(
        MemoryItem(
            item_id="persist-1",
            content="跨进程持久化记忆验证内容足够长以通过阈值检查",
            importance=0.9,
            metadata={},
        )
    )
    m2 = _build_memory_manager(db_dir)
    results = m2.long_term.retrieve("持久化", limit=5)
    assert any(r.id == "persist-1" for r in results), "重建 MemoryManager 后必须能读回记忆"


@pytest.mark.asyncio
async def test_memory_shared_with_knowledge_search_tool(home: Any) -> None:
    """agent_runner 的记忆与 factory 注册的 KnowledgeSearchTool 共享同一库。"""
    from bandi.core.agent_runner import _build_memory_manager
    from bandi.memory.long_term import MemoryItem

    db_dir = Path(home) / ".bandi" / "data"
    m1 = _build_memory_manager(db_dir)
    m1.long_term.store(
        MemoryItem(
            item_id="shared-1",
            content="恒星贸易是共享库验证内容",
            importance=0.9,
            metadata={},
        )
    )

    from bandi.agent.factory import build_tool_registry

    with patch("pathlib.Path.home", return_value=home):
        reg = build_tool_registry()
    tool = reg.get("knowledge_search")
    assert tool is not None
    assert tool._memory is not None
    hits = tool._memory.retrieve("恒星贸易", limit=5)
    assert any(r.id == "shared-1" for r in hits), (
        "KnowledgeSearchTool 必须能看到 agent_runner 写入的记忆"
    )
