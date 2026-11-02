# 功能：Zhipu / GLM provider——GLM-5.2 及以上用 `thinking.type` +
# 顶层 `reasoning_effort`
# 强制思考**：传 `thinking.type="disabled"` 请求失败 ⇒ 这些模型的空档（UI「关闭」）
# 下发最低档 "low"，而非 disabled。
from __future__ import annotations

from typing import Any

from bandi.llm.providers.openai import OpenAICompatProvider

# GLM-5.3 系（强制思考）：仅 max / high / low
_GLM_ALWAYS_ON_MARKERS = ("glm-5.3",)
# 官方值域 = max / high / low（语义：low 与 medium 在服务端均按 high 执行——
_GLM_EFFORT = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}
# 可关思考的 GLM（5.2 及以下）：none/minimal 表示放弃思考（官方）
_GLM_OFF_EFFORT = {"none": "none", "minimal": "minimal"}


class ZhipuProvider(OpenAICompatProvider):
    name = "zhipu"

    def __init__(self, config: Any, base_url: str = "") -> None:
        super().__init__(
            config,
            base_url or getattr(config, "base_url", "") or "https://open.bigmodel.cn/api/paas/v4",
        )

    def _always_reasoning(self) -> bool:
        model = str(getattr(self.config, "default_model", "") or "").lower()
        return any(marker in model for marker in _GLM_ALWAYS_ON_MARKERS)

    def _thinking_payload(self) -> dict[str, Any]:
        """功能：thinking_mode → {thinking.type, reasoning_effort}（强制思考族不下发 disabled）。"""
        mode = (getattr(self.config, "thinking_mode", "") or "").strip().lower()
        if not mode:
            if self._always_reasoning():
                # 5.3 系：不可关闭 ⇒ 最低档（不发 = 官方默认 max，与「关闭」相反）
                return {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
            # 5.2 及以下：显式关闭（不依赖服务端默认是否思考）
            return {"thinking": {"type": "disabled"}}
        if mode in _GLM_OFF_EFFORT and not self._always_reasoning():
            return {"thinking": {"type": _GLM_OFF_EFFORT[mode]}, "reasoning_effort": "none"}
        effort = _GLM_EFFORT.get(mode, "high")
        return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
