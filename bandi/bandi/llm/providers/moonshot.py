# 功能：Moonshot / Kimi provider——始终推理，**只**通过顶层
# `reasoning_effort` 调档（"low"/"high"/"max"，官方默认 "max"）；**不得**发送
# `thinking` 字段（上传 disabled 会报错）。⇒ 空档（UI「关闭」）下发最低档 "low"
# 而非「什么都不发」：后者等于官方默认 max（最强），与用户所选的「关闭」相反。
from __future__ import annotations

from typing import Any

from bandi.llm.providers.openai import OpenAICompatProvider

# 官方值域：low / high / max（无 medium——medium 归到 high 侧，取更保守的 high）
_KIMI_EFFORT = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}


class MoonshotProvider(OpenAICompatProvider):
    name = "moonshot"

    def __init__(self, config: Any, base_url: str = "") -> None:
        super().__init__(
            config, base_url or getattr(config, "base_url", "") or "https://api.moonshot.cn/v1"
        )

    def _thinking_payload(self) -> dict[str, Any]:
        """功能：thinking_mode → 顶层 reasoning_effort（空档 = low，**不发 thinking**）。"""
        mode = (getattr(self.config, "thinking_mode", "") or "").strip().lower()
        effort = _KIMI_EFFORT.get(mode, "low") if mode else "low"
        return {"reasoning_effort": effort}
