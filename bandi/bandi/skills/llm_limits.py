# 功能：DL 蒸馏任务的 LLM 输出预算约束（派生克隆配置，不修改源对象）。
from __future__ import annotations

from bandi.core.config import LLMConfig

SKILL_MAX_OUTPUT_TOKENS = 8192


def skill_model_config(config: LLMConfig) -> LLMConfig:
    """功能：为蒸馏任务派生 LLM 配置（max_output_tokens 抬升至至少
    SKILL_MAX_OUTPUT_TOKENS）。传入应用级配置，返回互不影响的副本。"""
    base = config.max_output_tokens
    target_tokens = max(SKILL_MAX_OUTPUT_TOKENS, base)
    if target_tokens == base:
        return config.model_copy(deep=False)
    return config.model_copy(update={"max_output_tokens": target_tokens})
