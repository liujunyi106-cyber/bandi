# 功能：跨独立 run_agent_turn（同 session 复用）压缩不收敛契约
# 行为族：跨 run 时历史 user 消息 turn_index 恒 0（store append 不更新）
# 连续多轮输入「完全相同」的超长文本时倒序 content 匹配最易误标历史轮——
# turn_index 归属一旦错乱，SM keep 空 + anchor 重注入永不收敛 → 假 MAX_TURNS
# 静默空回复。本契约钉住真实跨 run 生产链路：任何一轮不得静默空回复
# 切压缩窗口后必须真收敛（total_tokens 落回预算内）。
# 破坏者视角：第一/二/三轮全部用同一段超长文本（content 全等）
# 专攻「回填是否命中本轮新消息而非历史轮」。
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.config import BandiConfig
from bandi.core.message import LLMResponse, TokenUsage
from bandi.llm.mock import MockProvider
from bandi.memory.session.store import SessionStore


def _build_provider(usages: list[tuple[int, int]]) -> MockProvider:
    provider = MockProvider()
    for i, (p, c) in enumerate(usages):
        provider.add_response(
            LLMResponse(
                content=f"第{i}轮处理完成，结论已沉淀",
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=p, completion_tokens=c),
                finish_reason="stop",
            )
        )
    return provider


@pytest.mark.asyncio
async def test_cross_run_same_input_compression_no_silent_termination(tmp_path: Any) -> None:
    """契约：同 session 三轮独立 run、每轮相同超长输入、第二轮切压缩窗口——
    任何一轮不得假 MAX_TURNS 静默空回复，且压缩后必须收敛到预算内。

    工具面隔离：契约焦点是压缩收敛，patch registry 为空使请求口径 = 对话
    口径（mock 全量工具 schema ≈24K token 会物理超窗，与压缩正确性无关）。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.tools.registry import ToolRegistry

    provider = _build_provider([(8000, 100), (8000, 100), (8000, 100)])

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        cfg.agent.max_iterations = 3
        cfg.agent.context_window = 128_000
        with (
            patch("bandi.core.agent_runner.build_provider", return_value=provider),
            patch(
                "bandi.core.agent_runner._build_tool_registry_with_config",
                return_value=ToolRegistry(),
            ),
        ):
            replies: list[str] = []
            reasons: list[str] = []
            for i in range(3):
                if i == 1:
                    # 第二/三轮切换压缩窗口（有效窗口 = 窗口 - 输出预留 8192
                    # 取 24000 保证 24000-8192=15808 输入预算覆盖三轮累计）
                    cfg.agent.context_window = 24_000
                r = await run_agent_turn(
                    user_input="继续排查 seed 参数并补充说明" * 300,
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                )
                replies.append(r.reply)
                reasons.append(r.terminal_reason)
                # 注：r.total_tokens 是 usage 累计口径（含历史多轮），不代表活动
                # 上下文 live 大小；「收敛」由行为断言（不 PROMPT_TOO_LONG/不
                # MAX_TURNS）覆盖——live 未落回预算内必然触发两者之一。

    assert all(replies), f"跨 run 任何一轮都不得静默空回复（实际 replies={replies!r}）"
    assert "MAX_TURNS" not in reasons, f"不得假 MAX_TURNS 空回复（实际 reasons={reasons}）"
    assert "PROMPT_TOO_LONG" not in reasons, (
        f"压缩后不得报上下文不可收敛（重新注入 raw 后 live 应落回预算内，实际 reasons={reasons}）"
    )


@pytest.mark.asyncio
async def test_cross_run_same_input_large_window_unchanged(tmp_path: Any) -> None:
    """契约对照：相同 input 跨 run 但全程大窗口（不触发压缩）——
    回复完整、全部 VERIFIED（钉倒序 content 匹配 + turn_index 双守卫不误伤正常链路）。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.tools.registry import ToolRegistry

    provider = _build_provider([(8000, 100), (8000, 100), (8000, 100)])

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        cfg.agent.max_iterations = 3
        cfg.agent.context_window = 128_000
        with (
            patch("bandi.core.agent_runner.build_provider", return_value=provider),
            patch(
                "bandi.core.agent_runner._build_tool_registry_with_config",
                return_value=ToolRegistry(),
            ),
        ):
            replies: list[str] = []
            reasons: list[str] = []
            for i in range(3):
                r = await run_agent_turn(
                    user_input="继续排查 seed 参数并补充说明" * 300,
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                )
                replies.append(r.reply)
                reasons.append(r.terminal_reason or "")

    assert all(replies), f"大窗口对照全程必须有回复（实际 replies={replies!r}）"
    assert reasons == ["VERIFIED", "VERIFIED", "VERIFIED"], f"实际 reasons={reasons}"
