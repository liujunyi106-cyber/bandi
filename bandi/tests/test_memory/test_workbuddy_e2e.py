# 链路：create_session(project_root) → 新会话 system prompt 含记忆规则 → agent 完成
# → 落盘 → 下会话读取到项目记忆
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


def _provider(replies: list[str]) -> _RecordingProvider:
    provider = _RecordingProvider()
    for reply in replies:
        provider.add_response(LLMResponse(content=reply))
    return provider


@pytest.mark.asyncio
async def test_workbuddy_project_memory_end_to_end(home: Any, tmp_path: Any) -> None:
    """生产主链路：project_root 会话 → system prompt 含记忆规则 → 落盘 → 下会话读到。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    project_root = tmp_path / "proj"
    project_root.mkdir()
    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute", project_root=str(project_root))

    provider = _provider(["已完成任务并记录项目记忆。"])
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="完成一个演示任务",
            session_id=sid,
            session_store=store,
            project_root=str(project_root),
        )
    assert result.reply
    assert provider.seen_messages, "必须调用过 LLM"

    system_prompt = "\n".join(
        str(m.get("content") or "") for m in provider.seen_messages[0] if m.get("role") == "system"
    )
    assert "## 项目记忆规则" in system_prompt, (
        "system prompt 必须含项目记忆规则段（WorkBuddy ClawMemory 等价）"
    )
    assert "MEMORY.md" in system_prompt
    assert "自动沉淀" in system_prompt

    memory_file = project_root / ".bandi" / "memory" / "MEMORY.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("# 项目记忆\n\n## 事实\n项目核心偏好：中文文档\n", encoding="utf-8")

    # 下会话读取（复用 project_root 的会话）→ 项目记忆必须注入 system prompt
    provider2 = _provider(["知道了"])
    with patch("bandi.core.agent_runner.build_provider", return_value=provider2):
        await run_agent_turn(
            user_input="后续任务",
            session_id=sid,
            session_store=store,
            project_root=str(project_root),
        )
    second_system = "\n".join(
        str(m.get("content") or "") for m in provider2.seen_messages[0] if m.get("role") == "system"
    )
    assert "项目核心偏好：中文文档" in second_system, (
        "下会话 system prompt 必须包含项目 MEMORY.md 内容（B1 读取接线）"
    )


@pytest.mark.asyncio
async def test_workbuddy_session_metadata_project_root_wins(home: Any, tmp_path: Any) -> None:
    """会话 metadata project_root 优先于 init 兜底——新会话未显式传 project_root 也能读到项目记忆。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    project_root = tmp_path / "proj2"
    project_root.mkdir()
    mem_dir = project_root / ".bandi" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("## 事实\n会话元数据项目记忆\n", encoding="utf-8")

    store = SessionStore(str(home / "sess2.db"))
    sid = store.create_session(mode="execute", project_root=str(project_root))

    provider = _provider(["好的"])
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        await run_agent_turn(
            user_input="继续",
            session_id=sid,
            session_store=store,
        )
    system_prompt = "\n".join(
        str(m.get("content") or "") for m in provider.seen_messages[0] if m.get("role") == "system"
    )
    assert "会话元数据项目记忆" in system_prompt, (
        "run_agent_turn 未显式传 project_root 时，必须从会话 metadata 兜底读取项目记忆"
    )
