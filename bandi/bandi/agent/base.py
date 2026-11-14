# 功能：Agent 运行时状态与上下文基础类型（AgentState/CoTStep/ExecutionContext/终态判定）
from __future__ import annotations

import dataclasses
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, TypedDict

from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import LLMResponse, Message, ToolCall, ToolResult
from bandi.llm.base import BaseProvider

if TYPE_CHECKING:
    from bandi.agent.deps import QueryDeps
    from bandi.agent.recovery_manager import RecoveryManager
    from bandi.agent.verifier import Verifier
    from bandi.safety.content_filter import ContentFilter
    from bandi.safety.output_sanitizer import OutputSanitizer
    from bandi.safety.pii_detector import PIIDetector
    from bandi.safety.prompt_guard import PromptGuard
    from bandi.tools.executor import ToolExecutor

logger = logging.getLogger(__name__)


class AgentState(Enum):  # 功能：Agent 状态枚举（值=小写名称，供 core/ 层用字符串引用）
    INIT = "init"
    THINKING = "thinking"
    PARSING = "parsing"
    EXECUTING = "executing"
    OBSERVING = "observing"
    REFLECTING = "reflecting"
    COMPRESSING = "compressing"
    TERMINATED = "terminated"


STATE_NAMES: dict[str, AgentState] = {s.value: s for s in AgentState}


class CoTStepType(Enum):  # 功能：CoT 推理轨迹步骤类型（思考/工具调用/工具结果/观察/答案/错误）
    THOUGHT = auto()
    TOOL_CALL = auto()
    TOOL_RESULT = auto()
    OBSERVATION = auto()
    ANSWER = auto()
    ERROR = auto()


@dataclass
class CoTStep:  # 功能：CoT 推理轨迹单步 —— step/n 枚举 + thought/observation（n 在 ANSWER 时回填总步数）
    step: int
    type: CoTStepType
    thought: str = ""
    observation: str = ""
    tool_name: str = ""
    n: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


class RecoveryAction(Enum):
    RETRY = auto()
    TERMINATE = auto()
    CONTINUE_DEGRADED = auto()
    ESCALATE = auto()


class MetaDict(TypedDict, total=False):
    mode: str
    mode_manager: Any
    bandi_config: Any
    perception: Any
    session_id: str
    stream_handler: Any
    max_iterations: int
    content_filter: Any
    pii_detector: Any
    output_sanitizer: Any
    prompt_guard: Any


class TerminalReason(Enum):  # 功能：Agent 终止原因（15 种）
    COMPLETED = auto()
    MAX_TURNS = auto()
    MODEL_ERROR = auto()
    PROMPT_TOO_LONG = auto()
    TOOL_ERROR = auto()
    VERIFIED = auto()
    REWORK = auto()
    ESCALATED = auto()
    USER_INTERRUPT = auto()
    SAFETY_BLOCKED = auto()
    LOOP_DETECTED = auto()
    TOKEN_BUDGET_EXHAUSTED = auto()
    TIMEOUT = auto()
    AWAITING_APPROVAL = auto()
    NO_REPLY = auto()


class ParseOutcome(Enum):  # 功能：LLM 响应解析产物显式状态（on_parsing 写方 / transitions 读方）
    NONE = auto()  # 无 tool_calls，纯文本回答
    PARSED = auto()  # tool_calls 完整解析成功 → executing
    PARTIAL = auto()  # tool_calls 存在但 finish_reason=="length" 截断（参数可能不完整）


@dataclass
class TurnState:  # 功能：每轮 Agent Loop 的瞬态数据
    turn_index: int = 0
    # 用户轮内的模型步序号（1 起）：每次进入 THINKING 自增，同一用户轮内可多次
    step_index: int = 0
    current_response: LLMResponse | None = None
    current_content: str | None = None
    current_tool_calls: list[ToolCall] | None = None
    tool_results: list[ToolResult] | None = None
    raw_input: str = ""
    provider_name: str = ""
    parse_outcome: ParseOutcome | None = None  # None=未解析；NONE=已解析且无工具调用


@dataclass
class ToolExecutionState:  # 功能：工具执行相关状态
    active_tools: list[dict] | None = None
    tool_executor: ToolExecutor | None = None
    tool_sequence_hashes: list[str] = field(default_factory=list)
    loop_detection_window: int | None = None
    recovery_manager: RecoveryManager | None = None


@dataclass
class SafetyContext:  # 功能：安全护栏模块（由 AgentLoop._wire_safety 注入）
    prompt_guard: PromptGuard | None = None
    content_filter: ContentFilter | None = None
    pii_detector: PIIDetector | None = None
    output_sanitizer: OutputSanitizer | None = None
    verifier: Verifier | None = None


@dataclass
class CompressionState:  # 功能：压缩/截断中间状态
    compress_ok: bool = False
    compress_failed_count: int = 0
    continuity_injected: bool = False
    # 跨轮复用的 CompressionPipeline（含 AutoCompactScheduler 的 token 历史
    # 使 fast_growth 检测真正生效——每轮新建则 _token_history 恒空）
    pipeline: Any | None = None
    turns_since_maintenance: int = 0
    last_active_ts: float | None = None
    # consecutive_fallbacks 达阈值发 loss_risk 告警；attempts/successes 为运行统计
    compression_attempts: int = 0
    compression_successes: int = 0
    consecutive_fallbacks: int = 0
    # 本次压缩 handler 是否产生真实缩减（transient，不持久化）——转移层据此决定
    last_shrunk: bool = False


@dataclass
class VerifierState:  # 功能：VER 验证闸门中间状态
    rework_count: int = 0
    rework_feedback: str = ""


@dataclass
class Transition:  # 功能：状态转换规则（目标状态名+可选条件谓词）
    target: str
    condition: Callable[[ExecutionContext], bool] | None = None
    label: str = ""


@dataclass(frozen=True)
class QueryState:  # 功能：ExecutionContext 的不可变快照，用于 checkpoint 持久化与恢复
    current_state: str = "init"
    trace_id: str = ""
    terminal_reason: str | None = None
    final_message: str | None = None
    total_tokens: int = 0
    consumed_tokens: int = 0
    turn_index: int = 0
    provider_name: str = ""
    clarification_injected: bool = False
    tool_sequence_hashes: tuple[str, ...] = ()
    loop_detection_window: int = 5
    compress_ok: bool = False
    compress_failed_count: int = 0
    # 重建后归零，「满 10 轮 / 空闲 30 分钟」两条维护触发在跨轮场景永不生效。
    turns_since_maintenance: int = 0
    last_active_ts: float | None = None
    compress_token_history: tuple[int, ...] = ()
    compress_low_returns: int = 0
    consecutive_fallbacks: int = 0
    rework_count: int = 0
    rework_feedback: str = ""
    pending_tool_calls: list[dict] = field(default_factory=list)
    # 表示已派发、结果未知，恢复时不重放）
    dispatched_tool_call_ids: tuple[str, ...] | None = None
    readonly_loop_sig: tuple[str, ...] | None = None
    readonly_loop_streak: int = 0

    def with_state(self, **kwargs: object) -> QueryState:
        for k in kwargs:
            if k not in self.__dataclass_fields__:
                msg = f"unknown QueryState field: {k}"
                raise ValueError(msg)
        return dataclasses.replace(self, **kwargs)


@dataclass
class ExecutionContext:  # 功能：Agent 执行上下文，贯穿整个循环的共享状态容器
    messages: list[Message]
    provider: BaseProvider
    config: AgentConfig
    bus: EventBus
    trace_id: str = ""
    session_id: str = ""
    current_state: str = "init"
    terminal_reason: TerminalReason | None = None
    final_message: str | None = None
    total_tokens: int = 0
    # 会话累计 LLM 消费（预算检查用）——total_tokens 是上下文大小信号
    # （压缩会重置），二者语义分离，压缩不得绕过消费预算
    consumed_tokens: int = 0
    meta: MetaDict = field(default_factory=dict)  # type: ignore[assignment]
    turn: TurnState = field(default_factory=TurnState)
    tool: ToolExecutionState = field(default_factory=ToolExecutionState)
    safety: SafetyContext = field(default_factory=SafetyContext)
    compress: CompressionState = field(default_factory=CompressionState)
    verifier_state: VerifierState = field(default_factory=VerifierState)
    clarification_injected: bool = False
    clarification_count: int = 0
    hallucination_checker: Any | None = None
    badcase_tracker: Any | None = None
    cot_trace: list[CoTStep] = field(default_factory=list)
    injected_input: str | None = None
    _injected_consumed: bool = False
    injected_text: str | None = None
    _injection_lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)
    query_deps: QueryDeps | None = None

    @property
    def raw_input(self) -> str:
        return self.turn.raw_input

    @raw_input.setter
    def raw_input(self, value: str) -> None:
        self.turn.raw_input = value

    @property
    def provider_name(self) -> str:
        return self.turn.provider_name

    @provider_name.setter
    def provider_name(self, value: str) -> None:
        self.turn.provider_name = value

    @property
    def turn_index(self) -> int:
        return self.turn.turn_index

    @turn_index.setter
    def turn_index(self, value: int) -> None:
        self.turn.turn_index = value

    @property
    def step_index(self) -> int:
        return self.turn.step_index

    @step_index.setter
    def step_index(self, value: int) -> None:
        self.turn.step_index = value

    @property
    def current_response(self) -> LLMResponse | None:
        return self.turn.current_response

    @current_response.setter
    def current_response(self, value: LLMResponse | None) -> None:
        self.turn.current_response = value

    @property
    def current_content(self) -> str | None:
        return self.turn.current_content

    @current_content.setter
    def current_content(self, value: str | None) -> None:
        self.turn.current_content = value

    @property
    def current_tool_calls(self) -> list[ToolCall] | None:
        return self.turn.current_tool_calls

    @current_tool_calls.setter
    def current_tool_calls(self, value: list[ToolCall] | None) -> None:
        self.turn.current_tool_calls = value

    @property
    def tool_results(self) -> list[ToolResult] | None:
        return self.turn.tool_results

    @tool_results.setter
    def tool_results(self, value: list[ToolResult] | None) -> None:
        self.turn.tool_results = value

    @property
    def parse_outcome(self) -> ParseOutcome | None:
        return self.turn.parse_outcome

    @parse_outcome.setter
    def parse_outcome(self, value: ParseOutcome | None) -> None:
        self.turn.parse_outcome = value

    @property
    def active_tools(self) -> list[dict] | None:
        return self.tool.active_tools

    @active_tools.setter
    def active_tools(self, value: list[dict] | None) -> None:
        self.tool.active_tools = value

    @property
    def tool_executor(self) -> ToolExecutor | None:
        return self.tool.tool_executor

    @tool_executor.setter
    def tool_executor(self, value: ToolExecutor | None) -> None:
        self.tool.tool_executor = value

    @property
    def tool_sequence_hashes(self) -> list[str]:
        return self.tool.tool_sequence_hashes

    @tool_sequence_hashes.setter
    def tool_sequence_hashes(self, value: list[str]) -> None:
        self.tool.tool_sequence_hashes = value

    # 循环检测窗口类默认（ctx 未显式设置且 config 未声明同名字段时）
    _DEFAULT_LOOP_DETECTION_WINDOW = 5
    # 维护压缩的最低上下文占比：低于该比例不做免费 prune/micro，
    # 避免长任务刚起步就因 10 轮计数反复压缩工作集。
    _MAINTENANCE_MIN_RATIO = 0.30

    @property
    def loop_detection_window(self) -> int:
        # 功能：循环检测窗口——ctx 显式设置（含 checkpoint 恢复值）最高优先；
        # 未设置时回落 config 同名字段（AgentConfig 字段化后自动接线）
        # 最后回落类默认。消费方（states.on_executing）只读本属性。
        override = self.tool.loop_detection_window
        if override is not None and int(override) > 0:
            return int(override)
        configured = int(getattr(self.config, "loop_detection_window", 0) or 0)
        return configured if configured > 0 else self._DEFAULT_LOOP_DETECTION_WINDOW

    @loop_detection_window.setter
    def loop_detection_window(self, value: int) -> None:
        self.tool.loop_detection_window = value

    @property
    def recovery_manager(self) -> RecoveryManager | None:
        return self.tool.recovery_manager

    @recovery_manager.setter
    def recovery_manager(self, value: RecoveryManager | None) -> None:
        self.tool.recovery_manager = value

    @property
    def prompt_guard(self) -> PromptGuard | None:
        return self.safety.prompt_guard

    @prompt_guard.setter
    def prompt_guard(self, value: PromptGuard | None) -> None:
        self.safety.prompt_guard = value

    @property
    def content_filter(self) -> ContentFilter | None:
        return self.safety.content_filter

    @content_filter.setter
    def content_filter(self, value: ContentFilter | None) -> None:
        self.safety.content_filter = value

    @property
    def pii_detector(self) -> PIIDetector | None:
        return self.safety.pii_detector

    @pii_detector.setter
    def pii_detector(self, value: PIIDetector | None) -> None:
        self.safety.pii_detector = value

    @property
    def output_sanitizer(self) -> OutputSanitizer | None:
        return self.safety.output_sanitizer

    @output_sanitizer.setter
    def output_sanitizer(self, value: OutputSanitizer | None) -> None:
        self.safety.output_sanitizer = value

    @property
    def verifier(self) -> Verifier | None:
        return self.safety.verifier

    @verifier.setter
    def verifier(self, value: Verifier | None) -> None:
        self.safety.verifier = value

    @property
    def _compress_ok(self) -> bool:
        return self.compress.compress_ok

    @_compress_ok.setter
    def _compress_ok(self, value: bool) -> None:
        self.compress.compress_ok = value

    @property
    def _compress_failed_count(self) -> int:
        return self.compress.compress_failed_count

    @_compress_failed_count.setter
    def _compress_failed_count(self, value: int) -> None:
        self.compress.compress_failed_count = value

    def _message_tokens(self, counter: Any, m: Any) -> int:
        # 功能：RF-6——单消息真实请求 token（content + tool_calls.arguments；
        # arguments 随请求序列化发送（message.py），原口径漏计致大参数低估）
        total = counter(m.content or "")
        for tc in getattr(m, "tool_calls", None) or []:
            total += counter(json.dumps(tc.arguments, default=str))
        return total

    def live_token_count(self) -> int:
        """功能：当前 messages 真实 token 总数（provider 口径，含 SYSTEM 固定区）——
        LLM 请求前真实性检查用。压缩触发与不可收敛判定不用本口径（SYSTEM 的
        工具 schema/静态提示不可被压缩缩减，计入预算会令「压缩后仍超阈值 →
        无限压缩循环」，冒烟 PROMPT_TOO_LONG/MAX_TURNS）。provider 无
        count_tokens 能力（统计型 stub）时回退 usage 累计字段保持原语义。"""
        counter = getattr(self.provider, "count_tokens", None)
        if counter is None:
            return self.total_tokens
        return sum(self._message_tokens(counter, m) for m in self.messages)

    def tool_schema_token_count(self) -> int:
        """功能：当前激活工具面的 schema token 数（真实请求经 tools 参数旁路发送）。

        工具 JSON Schema 不进入 messages，live_token_count() 覆盖不到；窗口硬闸与
        成本拆解须单独计入，否则工具面开销在超窗判定中缺席。provider 无
        count_tokens 能力时按 0 计（无法度量则不虚增）。
        """
        counter = getattr(self.provider, "count_tokens", None)
        if counter is None or not self.active_tools:
            return 0
        try:
            return int(counter(json.dumps(self.active_tools, ensure_ascii=False, default=str)))
        except (TypeError, ValueError):
            return 0

    def request_token_count(self) -> int:
        """功能：真实请求 token 总量（messages + 工具 schema）——LLM 请求前硬闸口径。"""
        return self.live_token_count() + self.tool_schema_token_count()

    def compressible_token_count(self) -> int:
        """功能：可压缩对话口径 token 数（排除 SYSTEM 固定区）——压缩触发
        （needs_compression）与不可收敛判定统一用。SYSTEM（身份/框架/工具
        schema）不受压缩/截断影响，必须从「可被压缩动作缩减的预算」中剔除
        否则窗口相对工具面较小时压缩永不收敛。provider 无 count_tokens 时
        回退 total_tokens（usage 累计，无 count_tokens 无法拆分口径，保持
        既有 stub 语义）。"""
        from bandi.core.message import MessageRole

        counter = getattr(self.provider, "count_tokens", None)
        if counter is None:
            return self.total_tokens
        return sum(
            self._message_tokens(counter, m) for m in self.messages if m.role != MessageRole.SYSTEM
        )

    # 至少留 20K 输出空间——预留随配置增大，但不小于此值）
    _OUTPUT_RESERVE_FLOOR = 20_000
    # 请求口径前置闸系数：SYSTEM 膨胀型会话（大工具面 + Skill）在 compressible
    # 口径之外也须触发压缩，防直接以 400 / reactive compact 兜底
    _REQUEST_GUARD_RATIO = 0.92

    @property
    def effective_context_window(self) -> int:
        """功能：有效上下文窗口 = context_window - 输出预留（下限 20K，上限窗口一半）。
        RF-13：预留按窗口 50% 再钳——原 max(1, window-reserve) 在
        reserve>=window（用户配小窗）时退化为 1 致全链 PROMPT_TOO_LONG。
        context_window=0 是「未显式配置」哨兵——直接构造 ExecutionContext
        （绕过 factory 装配）时必须回落 DEFAULT_CONTEXT_WINDOW，否则窗口算成 1
        会让全链 PROMPT_TOO_LONG。预留下限 20K max(output, buffer)
        ——1M 模型按模型真实窗口压缩（~833K 才触发），不为省输出空间过早压缩。"""
        from bandi.core.config import DEFAULT_CONTEXT_WINDOW

        window = int(getattr(self.config, "context_window", 0) or 0)
        if window <= 0:
            window = DEFAULT_CONTEXT_WINDOW
        reserve = max(
            max(int(getattr(self.config, "reserved_output_tokens", 0) or 0), 0),
            self._OUTPUT_RESERVE_FLOOR,
        )
        reserve = min(reserve, max(1, window // 2))
        return max(1, window - reserve)

    def needs_compression(self) -> bool:
        window = self.effective_context_window
        threshold = self.config.compression_threshold
        if self.compressible_token_count() >= window * threshold:
            return True
        # 2.9 请求口径前置闸——messages + 工具 schema 达有效窗口 92% 且对话区
        # 本身已有可观规模（≥ 半阈值）时提前压缩，防直接以 400 兜底。
        # 纯 SYSTEM 膨胀（大工具面但对话小）不触发：SYSTEM 不可压，触发只会
        if (
            self.request_token_count() >= window * self._REQUEST_GUARD_RATIO
            and self.compressible_token_count() >= window * threshold * 0.5
        ):
            return True
        # 维护模式——免费档 prune/micro，不调 LLM）
        import time

        from bandi.memory.compression_pipeline import is_maintenance_due

        return is_maintenance_due(
            self.compress.turns_since_maintenance, self.compress.last_active_ts, time.time()
        )

    def should_terminate(self) -> bool:
        return self.terminal_reason is not None

    def needs_clarification(self) -> bool:
        return self.clarification_injected

    def needs_verification(self) -> bool:
        return self.verifier is not None

    def should_retry(self) -> bool:
        # （pass→VERIFIED / escalate / fail）时必须优先终止而非重回 thinking
        if self.terminal_reason is not None:
            return False
        return self.verifier_state.rework_count > 0

    def verdict_pass(self) -> bool:
        return self.terminal_reason == TerminalReason.VERIFIED

    def should_escalate(self) -> bool:
        return self.terminal_reason == TerminalReason.ESCALATED

    def compress_failed(self) -> bool:
        return self._compress_failed_count >= 2

    def to_query_state(self) -> QueryState:
        return QueryState(
            current_state=self.current_state,
            trace_id=self.trace_id,
            terminal_reason=self.terminal_reason.name if self.terminal_reason else None,
            final_message=self.final_message,
            total_tokens=self.total_tokens,
            consumed_tokens=self.consumed_tokens,
            turn_index=self.turn_index,
            provider_name=self.provider_name,
            clarification_injected=self.clarification_injected,
            tool_sequence_hashes=tuple(self.tool_sequence_hashes),
            loop_detection_window=self.loop_detection_window,
            compress_ok=self.compress.compress_ok,
            compress_failed_count=self.compress.compress_failed_count,
            turns_since_maintenance=self.compress.turns_since_maintenance,
            last_active_ts=self.compress.last_active_ts,
            compress_token_history=(
                tuple(self.compress.pipeline.token_history())
                if self.compress.pipeline is not None
                else ()
            ),
            compress_low_returns=(
                self.compress.pipeline.low_return_count if self.compress.pipeline is not None else 0
            ),
            consecutive_fallbacks=self.compress.consecutive_fallbacks,
            rework_count=self.verifier_state.rework_count,
            rework_feedback=self.verifier_state.rework_feedback,
            pending_tool_calls=[
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in (self.current_tool_calls or [])
            ],
            # 恢复辅助 meta 显式纳入（不整份序列化 meta，仅白名单键）
            dispatched_tool_call_ids=(
                tuple(str(i) for i in sorted(self.meta["dispatched_tool_call_ids"]))
                if self.meta.get("dispatched_tool_call_ids") is not None
                else None
            ),
            readonly_loop_sig=(
                tuple(str(p) for p in self.meta["_readonly_loop_sig"])
                if self.meta.get("_readonly_loop_sig")
                else None
            ),
            readonly_loop_streak=int(self.meta.get("_readonly_loop_streak", 0) or 0),
        )

    def apply_query_state(self, qs: QueryState) -> None:
        self.current_state = qs.current_state
        self.trace_id = qs.trace_id
        self.final_message = qs.final_message
        self.total_tokens = qs.total_tokens
        self.consumed_tokens = qs.consumed_tokens
        self.clarification_injected = qs.clarification_injected
        self.turn_index = qs.turn_index
        self.provider_name = qs.provider_name
        self.tool_sequence_hashes = list(qs.tool_sequence_hashes)
        self.loop_detection_window = qs.loop_detection_window
        self.compress.compress_ok = qs.compress_ok
        self.compress.compress_failed_count = qs.compress_failed_count
        self.compress.turns_since_maintenance = qs.turns_since_maintenance
        self.compress.last_active_ts = qs.last_active_ts
        self.compress.consecutive_fallbacks = qs.consecutive_fallbacks
        self.verifier_state.rework_count = qs.rework_count
        self.verifier_state.rework_feedback = qs.rework_feedback
        if qs.terminal_reason:
            try:
                self.terminal_reason = TerminalReason[qs.terminal_reason]
            except KeyError:
                logger.warning("unknown terminal_reason in restored state: %s", qs.terminal_reason)
        else:
            self.terminal_reason = None
        if qs.pending_tool_calls:
            from bandi.core.message import ToolCall

            self.current_tool_calls = [
                ToolCall(id=p["id"], name=p["name"], arguments=p.get("arguments", {}))
                for p in qs.pending_tool_calls
            ]
        # 恢复派发记录与只读循环计数（None=无记录，视为未启动可重放）
        if qs.dispatched_tool_call_ids is not None:
            self.meta["dispatched_tool_call_ids"] = {
                str(i) for i in qs.dispatched_tool_call_ids if i
            }
        else:
            self.meta.pop("dispatched_tool_call_ids", None)
        if qs.readonly_loop_sig:
            self.meta["_readonly_loop_sig"] = tuple(str(p) for p in qs.readonly_loop_sig)
            self.meta["_readonly_loop_streak"] = int(qs.readonly_loop_streak or 0)
        else:
            self.meta.pop("_readonly_loop_sig", None)
            self.meta.pop("_readonly_loop_streak", None)

    def inject_input(
        self, text: str
    ) -> None:  # 功能：A3 实时转向——暂存用户中途注入的指令（空/纯空白 no-op）
        # 空串/纯空白不得写入任何字段（防清空标志/触发伪消费）
        text = text.strip()
        if not text:
            return
        with self._injection_lock:
            if self.injected_input is not None and not self._injected_consumed:
                self.injected_input = self.injected_input + "\n" + text
                self.injected_text = (self.injected_text or "") + "\n" + text
                return
            self.injected_input = text
            self.injected_text = text
            self._injected_consumed = False
            # 本方法是**外部任务**入口（API /steer 等），而主循环在
            # `loop.py:728/735` 直接读 `ctx.current_state` 取 handler——工具执行期
            # 插话会把状态改成 thinking，而 thinking 的迁移表**无 default 兜底**
            # （next() 未匹配即 terminated）→ 本轮提前终止、工具结果白跑。
            # 状态改由 loop 自己的消费点（`_consume_injected_input`）在有序位置设置。

    def take_injected_input(
        self,
    ) -> str | None:  # 功能：E6 原子取走待消费注入文本（锁内读取并清空；无则 None）
        with self._injection_lock:
            text = self.injected_input
            if text is None or self._injected_consumed:
                return None
            self.injected_input = None
            self._injected_consumed = True
            return text

    def clear_injection(self) -> None:  # 功能：消费注入后清除标志
        with self._injection_lock:
            self.injected_input = None
            self._injected_consumed = False
            self.injected_text = None


class StateMachine:  # 功能：状态机（字符串状态名+转换表，按条件判断下一状态）
    def __init__(self):
        self._transitions: dict[str, list[Transition]] = {}
        self.last_reason: str = ""

    def register(
        self, state: str, transitions: list[Transition]
    ) -> None:  # 功能：注册一个状态的转换规则列表
        self._transitions[state] = transitions

    def next(
        self, ctx: ExecutionContext
    ) -> str:  # 功能：根据当前上下文判断下一状态，未匹配条件则终止
        current = ctx.current_state
        if current == "terminated":
            self.last_reason = "terminal_sticky"
            return current
        candidates = self._transitions.get(current, [])
        for t in candidates:
            if t.condition is None or t.condition(ctx):
                self.last_reason = t.label or "unlabeled_transition"
                return t.target
        self.last_reason = "no_condition_matched"
        return "terminated"
