"""
契约族：context_window 放开语义（按模型真实窗口，85% 水位压缩）。

- 原 `AgentConfig.context_window` 默认 128000 与「用户显式配置 128000」不可区分
  `resolve_context_window` 又只取 `min(配置, 目录)`（只收紧不放松）——于是默认模型
  `deepseek-v4-flash`（目录窗口 1,000,000）实际在 ~91.8K 就触发压缩，而非按 1M 窗口
  的 85%（~833K）。现象可跑通但违背语义：大窗口模型的压缩水位被一个静态默认值锁死。
- 放开语义（全分支）：目录窗口优先——任何模型按自己的真实窗口计算 85% 压缩水位；
  配置值（>0）降级为「目录未收录」时的兜底，不再收紧。
  目录未收录该模型且未配置时回落 `DEFAULT_CONTEXT_WINDOW` 安全值（不猜、也不退化）。

契约钉的行为族：
- `core/config.py::AgentConfig.context_window` 默认哨兵
- `agent/factory.py::resolve_context_window`（装配唯一入口）
- `agent/base.py::effective_context_window`（窗口消费点，哨兵不得退化为 1）
- `agent/states.py` 不可收敛判定（哨兵下不得恒真）
"""

from __future__ import annotations

from bandi.agent.factory import resolve_context_window
from bandi.core.config import DEFAULT_CONTEXT_WINDOW, AgentConfig, BandiConfig, LLMConfig


def test_agent_config_default_is_unset_sentinel() -> None:
    """默认必须是哨兵 0（未显式配置），而非某个静态窗口值。"""
    assert AgentConfig().context_window == 0
    assert AgentConfig().context_window == 0, "哨兵语义：0 = 跟随模型目录"


def test_default_config_follows_catalog_window_for_1m_model() -> None:
    """未显式配置 + 目录收录 → 采用目录窗口（1M 模型不再是 128K）。"""
    cfg = BandiConfig(llm=LLMConfig(default_model="deepseek-v4-flash"))
    assert resolve_context_window(cfg) == 1_000_000


def test_default_config_follows_catalog_window_for_small_model() -> None:
    """未显式配置 + 目录收录小窗口模型 → 仍按目录（不得放大）。"""
    cfg = BandiConfig(llm=LLMConfig(default_model="o3"))
    assert resolve_context_window(cfg) == 200_000


def test_unconfigured_and_unknown_model_falls_back_to_safe_default() -> None:
    """未显式配置 + 目录未收录 → 回落安全默认值（不猜目录，也不退化为 0）。"""
    cfg = BandiConfig(llm=LLMConfig(default_model="totally-unknown-model-xyz"))
    assert resolve_context_window(cfg) == DEFAULT_CONTEXT_WINDOW
    assert resolve_context_window(cfg) > 0, "回落后不得为 0（0 会让 effective 退化）"


def test_explicit_small_config_is_raised_by_larger_catalog() -> None:
    """用户显式配小值 + 目录 1M → 取目录 1M（放开：85% 水位按真实窗口算）。"""
    cfg = BandiConfig(
        llm=LLMConfig(default_model="deepseek-v4-flash"),
        agent=AgentConfig(context_window=200_000),
    )
    assert resolve_context_window(cfg) == 1_000_000


def test_explicit_config_yields_to_catalog_window() -> None:
    """用户显式配 128K + 目录 1M → 取目录 1M（放开语义，配置降级为兜底）。"""
    cfg = BandiConfig(
        llm=LLMConfig(default_model="deepseek-v4-flash"),
        agent=AgentConfig(context_window=128_000),
    )
    assert resolve_context_window(cfg) == 1_000_000


def test_explicit_config_still_backs_unknown_catalog_model() -> None:
    """目录未收录 + 显式配置 → 仍以配置兜底（兜底语义保留）。"""
    cfg = BandiConfig(
        llm=LLMConfig(default_model="totally-unknown-model-xyz"),
        agent=AgentConfig(context_window=64_000),
    )
    assert resolve_context_window(cfg) == 64_000


def test_effective_window_never_degrades_to_one_on_sentinel() -> None:
    """哨兵窗口直接进 ExecutionContext（未经 factory 装配）时不得退化为 1 token——

    退化会让全链 PROMPT_TOO_LONG，是本次改默认值的最大回归风险面。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.event_bus import EventBus

    ctx = ExecutionContext(messages=[], provider=None, config=AgentConfig(), bus=EventBus())
    assert ctx.effective_context_window == DEFAULT_CONTEXT_WINDOW - 20_000
    assert ctx.effective_context_window > 1_000


def test_million_window_model_drives_compression_watermark() -> None:
    """目录收录 1M 窗口的模型（mimo-v2.6-flash）→ 85% 水位按目录真实窗口计算。

    未收录时解析为兜底 128K；收录后应取目录真实窗口——这是「放开」语义对
    第三方 hub 模型的直接收益面。"""
    cfg = BandiConfig(llm=LLMConfig(default_model="mimo-v2.6-flash"))
    assert resolve_context_window(cfg) == 1_048_576
    assert int((1_048_576 - 20_000) * 0.85) == 874_289
