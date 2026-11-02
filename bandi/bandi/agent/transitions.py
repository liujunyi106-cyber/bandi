# 功能：Agent 状态迁移表（build_default_machine 默认迁移覆盖）
#
# needs_verification 分支不再横穿工具轮（executing→observing→thinking 循环
# 过度拦截）。只有最终回复进入 VER 岔路（VERIFIED/模型可继续走界或终止）。
# 该裁决为正式设计决策，如未来调整工具轮中途验证，需重新检查信号源
from bandi.agent.base import ParseOutcome, StateMachine, TerminalReason, Transition

_EXECUTABLE_OUTCOMES = (ParseOutcome.PARSED, ParseOutcome.PARTIAL)


def build_default_machine() -> (
    StateMachine
):  # 功能：构建默认状态机（OBSERVING→REFLECTING/COMPRESSING 岔路已预置）
    sm = StateMachine()
    sm.register(
        "init",
        [
            Transition(
                target="thinking",
                condition=lambda ctx: bool(ctx.messages) and ctx.terminal_reason is None,
                label="input_present",
            ),
            Transition(target="terminated", label="no_input"),
        ],
    )
    sm.register(
        "thinking",
        [
            Transition(
                target="compressing",
                condition=lambda ctx: ctx.needs_compression(),
                label="needs_compression",
            ),
            Transition(
                target="thinking",
                condition=lambda ctx: ctx.needs_clarification(),
                label="clarification_round",
            ),
            Transition(
                target="parsing",
                condition=lambda ctx: ctx.current_response is not None,
                label="response_ready",
            ),
            Transition(
                target="terminated",
                condition=lambda ctx: ctx.terminal_reason is not None,
                label="terminal_reason_set",
            ),
        ],
    )
    sm.register(
        "parsing",
        [
            # 且未达上限时置 _hallucination_rewrite_pending 并注入校对指令，此处
            # 回 thinking 重新生成（重写内容再次过 judge）；置最前保优先于
            # verification_required（verifier 存在时不得绕道 reflecting）。
            Transition(
                target="thinking",
                condition=lambda ctx: bool(ctx.meta.get("_hallucination_rewrite_pending")),
                label="hallucination_rewrite",
            ),
            # 命中 _EXECUTABLE_OUTCOMES（替换原 bool(current_tool_calls) 硬编码）
            Transition(
                target="executing",
                condition=lambda ctx: ctx.parse_outcome in _EXECUTABLE_OUTCOMES,
                label="executable_outcome",
            ),
            # 原顺序下 needs_verification 先命中会绕道 reflecting（on_reflecting
            # 早退兜住但多绕一拍 STATE_CHANGED）。COMPLETED 除外：on_parsing 纯
            # 文本分支提前设 COMPLETED，在此短路则 VER 永不触发（验证闸门被
            # 「提前完成」绕过），须留给 reflecting 分支介入
            Transition(
                target="terminated",
                condition=lambda ctx: (
                    ctx.should_terminate() and ctx.terminal_reason != TerminalReason.COMPLETED
                ),
                label="stop_without_verification",
            ),
            # 原挂点在 observing（工具轮中间），单只读工具轮被
            # verification_required=False 短路成 VERIFIED，模型永远看不到
            # 时允许 VER 介入判定（pass→VERIFIED / rework→重试 / escalate）
            Transition(
                target="reflecting",
                condition=lambda ctx: (
                    ctx.needs_verification()
                    and ctx.terminal_reason in (None, TerminalReason.COMPLETED)
                ),
                label="verification_required",
            ),
            Transition(
                target="terminated",
                condition=lambda ctx: ctx.terminal_reason is not None,
                label="terminal_reason_set",
            ),
            Transition(target="terminated", label="default"),
        ],
    )
    sm.register(
        "executing",
        [
            Transition(target="observing", label="tools_dispatched"),
        ],
    )
    sm.register(
        "observing",
        [
            # 必须回 thinking 续调 LLM 生成下一步/最终回答；TOOL_ERROR/
            # AWAITING_APPROVAL 已设 terminal_reason 时落 terminated。
            # 原「observing→reflecting」分支使带 verifier 的生产装配在工具轮
            # 中间被 VER 短路（见 parsing 处注记）
            Transition(
                target="thinking",
                condition=lambda ctx: ctx.terminal_reason is None,
                label="continue_after_tools",
            ),
            Transition(target="terminated", label="stop_after_tools"),
        ],
    )
    sm.register(
        "reflecting",
        [
            Transition(
                target="thinking",
                condition=lambda ctx: ctx.should_retry(),
                label="verdict_rework",
            ),
            Transition(
                target="terminated",
                condition=lambda ctx: ctx.verdict_pass(),
                label="verdict_pass",
            ),
            Transition(
                target="terminated",
                condition=lambda ctx: ctx.should_escalate(),
                label="verdict_escalate",
            ),
            Transition(target="terminated", label="default"),
        ],
    )
    sm.register(
        "compressing",
        [
            Transition(
                target="thinking",
                condition=lambda ctx: not ctx.needs_compression() or ctx.compress_failed(),
                label="compression_done",
            ),
            # 再评估续压——否则巨大单条输入首轮单次压缩部分收敛即被默认分支
            # terminated → 兜底假 MAX_TURNS 空回复。turns 计数天然限制续压轮数；
            # 无成效或 reason 已设（PROMPT_TOO_LONG 不可收敛）→ 落默认直终防空转。
            Transition(
                target="thinking",
                condition=lambda ctx: ctx.terminal_reason is None and ctx.compress.last_shrunk,
                label="compression_partial_retry",
            ),
            Transition(target="terminated", label="default"),
        ],
    )
    sm.register("terminated", [])
    return sm
