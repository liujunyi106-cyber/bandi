# 功能：契约——输出预算动态钳制
from __future__ import annotations

from bandi.core.config import LLMConfig
from bandi.llm.providers._http_provider import _estimate_prompt_tokens, _resolve_max_tokens


def test_dynamic_clamp_shrinks_budget_near_window() -> None:
    """契约（增强）：prompt 逼近目录窗口时输出预算被动态钳制
    （budget = 窗口 - prompt - 256）——静态目录钳制无法感知本次输入规模
    超长 prompt + 大输出预算叠加会超窗 400。"""
    cfg = LLMConfig(default_model="deepseek-v4-flash")
    # 目录窗口 1_000_000；1_000_000 - 990_000 - 256 = 9_744
    assert _resolve_max_tokens(cfg, 32_000, prompt_tokens=990_000) == 9_744


def test_dynamic_clamp_skipped_for_short_prompt() -> None:
    """防过紧：prompt 远小于窗口时输出预算保持原值（不被无谓缩水）。"""
    cfg = LLMConfig(default_model="deepseek-v4-flash")
    assert _resolve_max_tokens(cfg, 32_000, prompt_tokens=1_000) == 32_000


def test_dynamic_clamp_skipped_for_unknown_model() -> None:
    """目录未收录模型不猜窗口——按原值透传（与 `_clamp_tokens` 同口径）。"""
    cfg = LLMConfig(default_model="not-in-catalog-model")
    assert _resolve_max_tokens(cfg, 32_000, prompt_tokens=999_999) == 32_000


def test_dynamic_clamp_applies_to_config_default_budget() -> None:
    """动态钳制同样覆盖 config 默认预算路径（非仅显式 override）。"""
    cfg = LLMConfig(default_model="deepseek-v4-flash", max_output_tokens=8192)
    expected = 1_000_000 - 999_000 - 256
    assert _resolve_max_tokens(cfg, None, prompt_tokens=999_000) == expected


def test_estimate_prompt_tokens_counts_content_and_tool_args() -> None:
    """估算器：content 与 tool_calls 参数均计入（动态钳制的输入面）。"""
    msgs = [
        {"role": "user", "content": "中文内容" * 10},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "name": "x", "arguments": "a" * 40}],
        },
    ]
    assert _estimate_prompt_tokens(msgs) > 0
