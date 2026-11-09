# 功能：事件类型与事件结构（EventType/Event）
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any
from uuid import uuid4


class EventType(Enum):  # 功能：系统事件类型枚举（状态变更/转轮/LLM 请求响应/终止）
    STATE_CHANGED = auto()
    TURN_STARTED = auto()
    TERMINATED = auto()
    LLM_REQUEST = auto()
    LLM_RESPONSE = auto()
    LLM_ERROR = auto()
    TOOL_INVOKED = auto()
    TOOL_COMPLETED = auto()
    TOOL_ERROR = auto()
    TOOL_PROGRESS = auto()  # LA-19：流式工具中间进度（工具时间线 UI 数据源）
    SKILL_INJECTED = auto()  # 技能注入（前端过程块「Skill 使用组」数据源）
    ERROR_OCCURRED = auto()
    DEGRADATION = auto()
    SAFETY_GUARD_TRIGGERED = auto()
    PII_DETECTED = auto()
    LLM_STREAM_EVENT = auto()
    AGENT_REQUEST = auto()
    VERDICT_ISSUED = auto()
    REWORK_REQUESTED = auto()
    PERCEPTION_ANALYZED = auto()
    TASK_LEDGER_UPDATED = auto()
    PREFETCH_HITS = auto()
    USER_INPUT_INJECTED = auto()  # A3：用户中途注入新指令，触发新一轮 THINKING
    OUTPUT_CONTRACT_VIOLATION = auto()  # P1-7：席位输出契约校验失败（阻断 SeatResult）
    # B5b 8.3/8.4：压缩与记忆写入事件（trace 可复盘上下文为何被砍/记忆写了哪些通道）
    COMPRESSION_STARTED = auto()
    COMPRESSION_COMPLETED = auto()
    MEMORY_WRITTEN = auto()
    # 无事件流、deny 无痕——事后无法回答「谁在何时批了哪次调用」。
    APPROVAL_REQUESTED = auto()
    APPROVAL_GRANTED = auto()
    APPROVAL_DENIED = auto()


@dataclass
class Event:  # 功能：事件数据载体（type + data 结构，EventBus 的核心传递单元）
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid4().hex)
    sequence: int = 0
    # 事件溯源字段（emit 时自动从上下文填充，发射点零侵入）
    trace_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    attempt_id: str = ""
    parent_event_id: str = ""
    command_id: str = ""
    schema_version: int = 1
    source: str = ""
