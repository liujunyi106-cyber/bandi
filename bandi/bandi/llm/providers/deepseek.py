# 功能：DeepSeek API 提供方（复用 OpenAI 兼容协议基类）
from __future__ import annotations

from typing import Any

from bandi.core.config import LLMConfig
from bandi.llm.providers.openai import OpenAICompatProvider


class DeepSeekProvider(OpenAICompatProvider):
    # 功能：thinking_mode 值域（收窄为官方真值域：
    # 列进来会让滑杆出现与 high 同档的重复档）
    _EFFORT_MODES = ("low", "high", "max")
    _DISABLED_MODES = ("disabled", "off", "none")

    def __init__(self, config: LLMConfig):
        # 显式 config.base_url（私有网关/中转）优先于官方默认域名
        super().__init__(config, getattr(config, "base_url", "") or "https://api.deepseek.com/v1")

    def _thinking_payload(self) -> dict[str, Any]:
        # 功能：thinking_mode → DeepSeek 官方参数（参考源 LeAgent：
        # thinking.type 开关 + 独立 reasoning_effort）；空=不发送（用服务端默认）
        mode = (self.config.thinking_mode or "").strip().lower()
        if mode in self._DISABLED_MODES:
            return {"thinking": {"type": "disabled"}}
        if mode in self._EFFORT_MODES:
            return {"thinking": {"type": "enabled"}, "reasoning_effort": mode}
        return {}
