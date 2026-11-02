# 功能：会话恢复完整性验证 — 第二轮必须包含第一轮的 assistant 回复 + 历史顺序正确

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider
from bandi.tools.base import BaseTool, ToolCategory


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
    # 功能：技能面隔离——加载器级联经 os.path.expanduser("~")（Windows 走 USERPROFILE），
    # 仅 patch Path.home 拦不住真实全局技能；带技能的机器上常驻索引会多一条 system 消息。
    monkeypatch.setenv("BANDI_SKILLS_DIRS", str(tmp_path / "no-skills"))
    monkeypatch.setenv("BANDI_SKILLS_ALLOW", "isolation-no-such-skill")
    return tmp_path


@pytest.mark.asyncio
async def test_second_turn_history_complete(home: Any) -> None:
    """第二轮请求必须包含：第一轮 user + 第一轮 assistant 回复。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="第一轮的回答内容在这里"))
    provider.add_response(LLMResponse(content="第二轮回答"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        r1 = await run_agent_turn(
            user_input="你好",
            session_id=sid,
            session_store=store,
        )
        r2 = await run_agent_turn(
            user_input="继续",
            session_id=sid,
            session_store=store,
        )
    assert r1.reply
    assert r2.reply
    first = provider.seen_messages[0]
    assert [m["role"] for m in first][:2] == ["system", "system"], (
        "首轮必须含身份 prompt + 工具面工件两条 system"
    )
    second = provider.seen_messages[-1]
    roles = [m["role"] for m in second]
    contents = [m.get("content", "") for m in second]
    assert roles[0] == "system", "首条必须 system"
    assert "你好" in contents, "第二轮必须包含第一轮 user 消息"
    assert any("第一轮的回答内容在这里" in c for c in contents), (
        "第二轮必须包含第一轮 assistant 回复（可能带 AI 生成尾注）"
    )
    # 历史 = [静态 system, 动态 system(项目惯例/记忆/prefetch)] + user1 + assistant1 + user2
    assert roles[1] == "system", "第二条为动态上下文 system（静态/动态双 system 组装）"
    # 运行时工件（工具面增量 / prefetch）也是 system，但必须全部居于历史之前
    first_non_system = next((i for i, role in enumerate(roles) if role != "system"), len(roles))
    assert all(role == "system" for role in roles[:first_non_system]), (
        f"system 消息必须只在头部（工件不得插入历史中间）: {roles}"
    )
    assert roles.count("system") <= 4, f"system 消息数量异常: {roles}"


class _EchoTool(BaseTool):
    name = "echo"
    description = "回声工具"
    parameters = {"type": "object", "properties": {"msg": {"type": "string"}}}

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        return params.get("msg", "")


class _WriteTool(BaseTool):
    name = "write"
    category = ToolCategory.GEN
    description = "写文件工具"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
    }

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        return "written"


def _registry_with(*tools: Any) -> Any:
    from bandi.tools.registry import ToolRegistry

    registry = ToolRegistry()
    for tool in tools:
        registry.register_instance(tool)
    return registry


@pytest.mark.asyncio
async def test_second_turn_emits_delta_when_tools_change(home: Any) -> None:
    """第二轮工具表面变化时，LLM 请求工件消息必须输出 Delta 头（跨实例 hashes 恢复）。

    回归保护：PerceptionPipeline 每轮重建，若 hashes 未持久化到 session metadata
    previous_hashes 恒为 None → 第二轮恒输出全量头，Delta 头永不出现。
    """
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="第一轮回答"))
    provider.add_response(LLMResponse(content="第二轮回答"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        with patch(
            "bandi.core.agent_runner.build_tool_registry",
            return_value=_registry_with(_EchoTool()),
        ):
            r1 = await run_agent_turn(user_input="你好", session_id=sid, session_store=store)
        with patch(
            "bandi.core.agent_runner.build_tool_registry",
            return_value=_registry_with(_EchoTool(), _WriteTool()),
        ):
            r2 = await run_agent_turn(user_input="继续", session_id=sid, session_store=store)
    assert r1.reply
    assert r2.reply
    second = provider.seen_messages[-1]
    contents = [m.get("content", "") for m in second]
    assert any("## Runtime Artifact Delta:" in c for c in contents), (
        "第二轮工具集变化时 LLM 请求必须输出 Delta 头（跨实例 hashes 恢复失败则恒全量头）"
    )
    assert any("write" in c for c in contents), "Delta 工件内容必须包含新增工具 write"
