# 功能：依赖装配容器（Dependencies 集中构建运行时组件）
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bandi.core.config import BandiConfig
from bandi.core.event_bus import EventBus

if TYPE_CHECKING:
    from bandi.agent.recovery_manager import RecoveryManager
    from bandi.llm.cost_tracker import CostTracker
    from bandi.memory.session.store import SessionStore
    from bandi.observability.logger import Logger
    from bandi.observability.trace_explainer import TraceExplainer
    from bandi.tools.registry import ToolRegistry


@dataclass
class Dependencies:  # 功能：全局依赖容器，Agent 启动时一次性注入
    bus: EventBus
    config: BandiConfig
    cost_tracker: CostTracker | None = None
    session_store: SessionStore | None = None
    trace_explainer: TraceExplainer | None = None
    logger: Logger | None = None
    tool_registry: ToolRegistry | None = None
    recovery_manager: RecoveryManager | None = None
