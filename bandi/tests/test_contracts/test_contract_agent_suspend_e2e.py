# 功能：Day132c 端到端契约——AgentLoop 内真挂起完整闭环（老板验收标准）
# 场景：LLM 生成 request_user_input 工具调用 → 真实 UserInputTool 经注入的
# 答复 → 工具返回 answers 进上下文 → LLM 第二轮收尾 → COMPLETED。
# 契约钉最终产物：interaction 帧可见、loop 挂起可证、答复后同 loop 续跑完成
# 工具答案进入消息上下文。
from __future__ import annotations

import asyncio
import json

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.loop import AgentLoop
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.events import Event
from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall, ToolResult
from bandi.core.user_interaction import HTTPUserInteraction, InteractionStore
from bandi.tools.user_input import UserInputTool


class _Bus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class _Provider:
    def __init__(self) -> None:
        self._responses: list[LLMResponse] = []
        self.supports_streaming = False
        self.call_count = 0

    def add_response(self, response: LLMResponse) -> None:
        self._responses.append(response)

    @property
    def model_name(self) -> str:
        return "mock"

    @property
    def protocol(self) -> str:
        return "openai"

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None, **kw
    ) -> LLMResponse:
        self.call_count += 1
        if not self._responses:
            return LLMResponse(content="ok")
        return self._responses.pop(0)

    async def stream(self, messages: list[dict], tools: list[dict] | None = None, **kw):
        yield


class _AskExecutor:
    """薄 executor：真实执行 UserInputTool（经注入的 HTTP UI），其余直接完成。"""

    def __init__(self, ui: HTTPUserInteraction) -> None:
        self.tool = UserInputTool(ui=ui)

    async def execute(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        results: list[ToolResult] = []
        for tc in tool_calls:
            if tc.name == "request_user_input":
                out = await self.tool.execute(tc.arguments, None)  # ← 挂起点（真挂起）
                results.append(
                    ToolResult(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        success=True,
                        output=json.dumps(out, ensure_ascii=False),
                    )
                )
            else:
                results.append(
                    ToolResult(tool_call_id=tc.id, tool_name=tc.name, success=True, output="ok")
                )
        return results


@pytest.mark.asyncio
async def test_agent_asks_frontend_sees_resume_same_loop() -> None:
    """端到端：agent 对话内主动提问 → interaction 帧推送 → 用户答复 → 同一 loop 续跑完成。"""
    store = InteractionStore()
    frames: list = []

    async def _push(item) -> None:
        frames.append(item)

    ui = HTTPUserInteraction(store, session_id="e2e-s1", on_interaction=_push)

    provider = _Provider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="request_user_input",
                    arguments={
                        "reason": "需要仓库地址",
                        "questions": [{"id": "repo", "question": "请提供仓库地址？"}],
                    },
                )
            ],
        )
    )
    provider.add_response(LLMResponse(content="已收到仓库地址，继续执行。"))

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="帮我查一下仓库地址")],
        provider=provider,
        config=AgentConfig(),
        bus=_Bus(),
    )
    ctx.tool_executor = _AskExecutor(ui)

    loop = AgentLoop()
    run_task = asyncio.create_task(loop.run(ctx))
    await asyncio.sleep(0.2)

    assert frames, "agent 提问必须推送 interaction 帧（前端可见）"
    assert frames[0].kind == "input"
    assert "请提供仓库地址" in str(frames[0].payload)
    assert run_task.done() is False, "用户答复前 AgentLoop 必须真挂起（非结束 turn）"

    store.resolve(frames[0].interaction_id, "https://github.com/example/repo")
    reason = await asyncio.wait_for(run_task, timeout=5)
    assert reason == TerminalReason.COMPLETED

    # 最终产物：工具答案进入消息上下文，LLM 第二轮基于答复收尾
    assistant_msgs = [m for m in ctx.messages if m.role == MessageRole.ASSISTANT]
    tool_msgs = [m for m in ctx.messages if m.role == MessageRole.TOOL]
    assert any(
        "https://github.com/example/repo" in str(getattr(m, "content", "")) for m in tool_msgs
    ), "工具答复必须写入消息上下文（LLM 第二轮可读）"
    assert any("已收到仓库地址" in str(getattr(m, "content", "")) for m in assistant_msgs), (
        "LLM 第二轮必须基于答复收尾（同 turn 续跑语义）"
    )
    assert provider.call_count == 2, "共两次 LLM 调用（工具前 + 答复后）"
