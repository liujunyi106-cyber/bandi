# 功能：双模式管理器——Plan/Execute 模式切换 + 工具过滤 + 提示词选择

from __future__ import annotations

from enum import Enum


class AgentMode(Enum):
    EXECUTE = "execute"
    PLAN = "plan"


_VALID_MODES = ("execute", "plan")

_MODE_LABELS: dict[str, str] = {
    "execute": "Execute（全功能执行）",
    "plan": "Plan（只读规划）",
}


class ModeManager:
    def __init__(self, initial_mode: str = "execute"):
        self._mode = initial_mode if initial_mode in _VALID_MODES else "execute"

    @property
    def mode(self) -> str:
        return self._mode

    def switch(self, new_mode: str) -> str:
        if new_mode in _VALID_MODES:
            old = self._mode
            self._mode = new_mode
            return old
        return self._mode

    def filter_tools(self, tool_names: list[str]) -> list[str]:
        if self._mode == "plan":
            # 引用 registry 单一白名单常量，避免双处定义漂移
            # （PLAN_MODE_ALLOWED 含全域交互通道——plan 模式也必须能向用户提问
            # 否则提问工具在 schema 侧被裁掉，LLM 根本看不见）
            from bandi.tools.registry import ToolRegistry

            return [t for t in tool_names if t in ToolRegistry.PLAN_MODE_ALLOWED]
        return tool_names

    def mode_label(self) -> str:
        return _MODE_LABELS.get(self._mode, self._mode)
