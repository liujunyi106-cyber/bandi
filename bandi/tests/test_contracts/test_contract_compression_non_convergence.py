# 功能：压缩不可收敛显式失败契约（问题 B 行为族钉用例）
# 行为族：on_compressing 失败兜底 → truncate 后仍超预算 → PROMPT_TOO_LONG
# 中断合法结束（nonce/carrier），不得假 MAX_TURNS 静默空回复。
# 破坏者视角：把窗口压到连 system+当前 raw 不可裁部分都塞不下的量级
# 压缩必然无法收敛——必须显式告警而非「成功但空回复」。
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.config import BandiConfig
from bandi.core.message import LLMResponse, TokenUsage
from bandi.llm.mock import MockProvider
from bandi.memory.session.store import SessionStore


@pytest.mark.asyncio
async def test_impossible_window_compression_fails_explicitly(tmp_path: Any) -> None:
    """契约（D-）：极小窗口无法收敛 → PROMPT_TOO_LONG 非空回复（不是空 MAX_TURNS）。"""
    from bandi.core.agent_runner import run_agent_turn

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="第0轮处理完成，seed 结论：配置项缺失",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5000, completion_tokens=100),
            finish_reason="stop",
        )
    )

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        # 未收录模型：显式 context_window 作为兜底生效（放开语义下目录优先，
        # 本用例需要可控窗口，用未收录模型名让配置兜底路径生效）
        cfg.llm.default_model = "unknown-e2e-model"
        cfg.agent.max_iterations = 3
        cfg.agent.context_window = 128_000
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            r0 = await run_agent_turn(
                user_input="第0轮请继续排查 seed 参数问题并补充细节说明" * 300,
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )
            assert r0.reply, "大窗口下超长输入必须正常回复"
            assert r0.terminal_reason == "VERIFIED"
            cfg.agent.context_window = 2000  # limit=1700，system+raw 不可裁部分已超
            r1 = await run_agent_turn(
                user_input="第1轮请继续排查 seed 参数问题并补充细节说明" * 300,
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )

    assert r1.terminal_reason == "PROMPT_TOO_LONG", (
        f"不可收敛窗口必须显式 PROMPT_TOO_LONG（实际 {r1.terminal_reason}），"
        "不得假 MAX_TURNS 静默空回复"
    )
    assert r1.reply, "PROMPT_TOO_LONG 必须带非空提示文案"
    assert "压缩" in r1.reply or "窗口" in r1.reply


@pytest.mark.asyncio
async def test_reasonable_window_tolerates_same_input(tmp_path: Any) -> None:
    """契约（D-）对照：同一超长输入在正常窗口下全程正常回复（不误伤）。"""
    from bandi.core.agent_runner import run_agent_turn

    provider = MockProvider()
    for i in range(3):
        provider.add_response(
            LLMResponse(
                content=f"第{i}轮处理完成，seed 结论：配置项缺失",
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=5000, completion_tokens=100),
                finish_reason="stop",
            )
        )

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        cfg.llm.default_model = "unknown-e2e-model"
        cfg.agent.context_window = 128_000
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            replies = []
            for i in range(3):
                r = await run_agent_turn(
                    user_input=f"第{i}轮请继续排查 seed 参数问题并补充细节说明" * 300,
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                )
                replies.append(r.reply)

        assert all(replies), f"正常窗口下每轮必须有回复（实际 {replies}）"
