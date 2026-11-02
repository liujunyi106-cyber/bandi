# 功能：旁路 LLM 调用统一计费入口——预算闸门 + 成本台账 + 观测事件
# 主链路（states._do_llm_call）经 ExecutionContext 事件总线记账；Verifier/Judge/
# 幻觉检查/文档工具/公司组件等旁路无 ctx，经本入口接入同一套 CostTracker 预算
# 与台账，杜绝游离于统一计费之外的 LLM 调用。

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import Any

from bandi.core.events import Event, EventType
from bandi.core.message import LLMResponse
from bandi.llm.base import BaseProvider

# 旁路质量检查（验证器 / 裁判 / 幻觉校验 / 门禁）的**总时间预算**：
# 一次检查的最坏耗时 = 上游超时 × 重试轮数
# 把整个回合拖到 3 分钟以上，而主回复早已流完）。这些检查都是 best-effort
# （调用方失败即降级），故用总预算封顶：超时按失败降级，不拖回合。
QUALITY_CHECK_BUDGET_SEC = 20.0

# 公司执行链裁判（verifier / gate_harness）独立预算：LLM 单次含内部重试
# 常超 20s；超时按基础设施失败降级放行（不误杀已产出回复），60s 为最坏等待上限。
COMPANY_JUDGE_BUDGET_SEC = 60.0


class BudgetExhaustedError(Exception):
    """旁路调用被预算闸门拒绝（fail-closed）。"""


def resolve_budget_tracker(budget_limit: float = -1.0) -> Any:
    # 进程内已有共享单例（agent_runner 以 config 预算建立）直接复用——保证
    # 旁路与主链路共享同一预算口径；无单例（独立 CLI 组件）时按传入预算新建。
    # 函数内延迟 import：_shared_cost_tracker 是模块级可变全局，顶部 from-import
    # 会快照成 None 导致预算闸门失效（单例重建为 -1 永不拦截）。
    from bandi.llm.cost_tracker import _shared_cost_tracker, get_shared_cost_tracker
    from bandi.llm.model_catalog import ModelCatalog

    if _shared_cost_tracker is not None:
        return _shared_cost_tracker
    return get_shared_cost_tracker(ModelCatalog(), budget_limit)


def _real_provider_name(provider: Any) -> str:
    # 与主链路同一提取规则：config.provider / api_protocol / model_name——
    # 存真实 provider 名，成本台账 provider 维度不失真；旁路 provider 多为
    # 鸭子类型（仅 complete），缺属性时降级 "unknown"（与 CostTracker 默认一致）
    cfg = getattr(provider, "config", None)
    return (
        getattr(cfg, "provider", "")
        or getattr(provider, "api_protocol", "")
        or getattr(provider, "model_name", "")
        or "unknown"
    )


async def accounted_complete(
    provider: BaseProvider,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict] | None = None,
    label: str = "",
    session_id: str = "default",
    budget_limit: float = -1.0,
    budget_sec: float | None = None,
    bus: Any = None,
    tracer: Any = None,
    **kwargs: Any,
) -> LLMResponse:
    # 功能：旁路 LLM 统一调用——预算闸门（超限抛 BudgetExhaustedError）
    # 调用（kwargs 原样透传）、计费（直写共享台账唯一通道，bus 事件仅观测）
    #
    # budget_sec：可选的**调用总预算**（含 provider 内部重试）——超时抛 TimeoutError，
    # 由调用方按 best-effort 降级。质量检查类旁路应传 QUALITY_CHECK_BUDGET_SEC。
    tracker = resolve_budget_tracker(budget_limit)
    if not tracker.check_budget():
        raise BudgetExhaustedError(f"LLM budget exhausted before '{label or 'unlabeled'}' call")
    provider_name = _real_provider_name(provider)
    model = getattr(provider, "model_name", "") or "unknown"
    if bus is not None:
        await bus.emit(
            Event(
                type=EventType.LLM_REQUEST,
                data={"provider": provider_name, "model": model, "label": label},
            )
        )
    span = (
        tracer.span(
            "llm_call",
            attributes={"provider": provider_name, "model": model, "label": label},
        )
        if tracer is not None
        else nullcontext()
    )

    async def _call() -> LLMResponse:
        if tools is not None:
            return await provider.complete(messages, tools=tools, **kwargs)
        # (messages)，显式 tools=None 会 TypeError（Fake/Mock 场景）
        return await provider.complete(messages, **kwargs)

    with span:
        if budget_sec and budget_sec > 0:
            resp = await asyncio.wait_for(_call(), timeout=budget_sec)
        else:
            resp = await _call()
    usage = getattr(resp, "usage", None)
    if usage is not None:
        # 计费唯一通道 = 直写共享台账（无装配依赖，旁路必定入账）；
        tracker.record(
            session_id=session_id,
            provider=provider_name,
            model=model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
            cache_creation_tokens=int(getattr(usage, "cache_creation_tokens", 0) or 0),
            input_tokens_total=int(getattr(usage, "input_tokens_total", 0) or 0),
            # cache_tokens 恒 0、命中率不可观测（阻塞点）。经 breakdown
            # 透传进 CostEntry（无 schema 变更，落 breakdown JSON 列）。
            breakdown={
                "cache_tokens": int(getattr(usage, "cache_read_tokens", 0) or 0),
                "cache_creation_tokens": int(getattr(usage, "cache_creation_tokens", 0) or 0),
                "input_tokens_total": int(getattr(usage, "input_tokens_total", 0) or 0),
            },
        )
        if bus is not None:
            await bus.emit(
                Event(
                    type=EventType.LLM_RESPONSE,
                    data={
                        "provider": provider_name,
                        "model": model,
                        "session_id": session_id,
                        # settled：本次调用已由直写台账落定——事件仅供观测
                        # CostTracker 订阅端见 settled 不得再计（单一计费源）
                        "settled": True,
                        "usage": {
                            "prompt_tokens": usage.prompt_tokens,
                            "completion_tokens": usage.completion_tokens,
                        },
                    },
                )
            )
    return resp
