# 功能：OpenCode Go 网关 provider（OpenAI 兼容协议）——
# ① 网关强制 `x-opencode-session` 头（缺失返回 400 MissingSessionID），并建议客户端以
#    自有 UA 标识、以稳定会话 ID 供路由与提示缓存优化；本实现取进程级稳定标识
#    （provider 层拿不到对话会话号，取舍见交付文档），档案另有 extra_headers 可显式覆盖；
# ② 思考挡位仅通过顶层 `reasoning_effort` 下发（官方值域 low/high/max，其余不发送防 400）。
from __future__ import annotations

import uuid
from typing import Any

from bandi.core.config import LLMConfig
from bandi.llm.providers.openai import OpenAICompatProvider

_DEFAULT_BASE_URL = "https://opencode.ai/inference/go/openai/v1"
_SESSION_ID = f"bandi-{uuid.uuid4().hex[:16]}"
_USER_AGENT = "bandi-app/0.1"


class OpenCodeGoProvider(OpenAICompatProvider):
    # 功能：thinking_mode 值域（网关官方值域：low/high/max）
    _EFFORT_MODES = ("low", "high", "max")

    def __init__(self, config: LLMConfig):
        # 显式 config.base_url（档案/配置）优先于网关默认端点
        super().__init__(config, getattr(config, "base_url", "") or _DEFAULT_BASE_URL)

    def _thinking_payload(self) -> dict[str, Any]:
        # 功能：thinking_mode → 顶层 reasoning_effort；值域外（空/关闭/未知档）不发送
        mode = (self.config.thinking_mode or "").strip().lower()
        if mode in self._EFFORT_MODES:
            return {"reasoning_effort": mode}
        return {}

    def _extra_headers(self) -> dict[str, str]:
        # 功能：附加头 = 档案显式配置 > 自动会话标识/UA（setdefault 保证网关必填头存在）
        headers = dict(super()._extra_headers())
        headers.setdefault("x-opencode-session", _SESSION_ID)
        headers.setdefault("User-Agent", _USER_AGENT)
        return headers
