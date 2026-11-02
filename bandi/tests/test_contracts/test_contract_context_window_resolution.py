# 功能：上下文窗口解析契约——context_window 必须按实际模型目录回填（1.2 补钉）
# 模型目录逐模型窗口从不回填；小窗口模型上压缩阈值与超窗硬闸同时失准。
# 放开语义（全分支）：目录窗口优先——按模型真实窗口的 85% 触发压缩；
# 配置值降级为「目录未收录」兜底（不再 min 收紧）。未收录回落 DEFAULT_CONTEXT_WINDOW，
# 见 test_contract_context_window_catalog_follow.py。
from __future__ import annotations

from bandi.agent.factory import resolve_context_window
from bandi.core.config import AgentConfig, BandiConfig, LLMConfig


class TestResolveContextWindow:
    def test_catalog_smaller_window_wins_over_configured(self) -> None:
        """目录窗口小于配置（如迁移到大配置 + 200K 模型）→ 取目录值。"""
        cfg = BandiConfig(
            llm=LLMConfig(default_model="o3"),
            agent=AgentConfig(context_window=500_000),
        )
        assert resolve_context_window(cfg) == 200_000

    def test_larger_catalog_window_wins_over_configured(self) -> None:
        """目录窗口大于配置（1M 模型 + 128K 配置）→ 取目录值（放开，不再收紧）。"""
        cfg = BandiConfig(
            llm=LLMConfig(default_model="deepseek-v4-flash"),
            agent=AgentConfig(context_window=128_000),
        )
        assert resolve_context_window(cfg) == 1_000_000

    def test_unknown_model_keeps_configured(self) -> None:
        """目录未收录模型 → 不猜，保持配置值（配置降级为兜底）。"""
        cfg = BandiConfig(
            llm=LLMConfig(default_model="totally-unknown-model-xyz"),
            agent=AgentConfig(context_window=64_000),
        )
        assert resolve_context_window(cfg) == 64_000

    def test_zero_configured_uses_catalog(self) -> None:
        """配置为 0（未声明）→ 采用目录窗口。"""
        cfg = BandiConfig(
            llm=LLMConfig(default_model="deepseek-v4-flash"),
            agent=AgentConfig(context_window=0),
        )
        assert resolve_context_window(cfg) == 1_000_000

    def test_empty_model_keeps_configured(self) -> None:
        """无默认模型 → 保持配置值（不产生假回填）。"""
        cfg = BandiConfig(
            llm=LLMConfig(default_model=""),
            agent=AgentConfig(context_window=96_000),
        )
        assert resolve_context_window(cfg) == 96_000
