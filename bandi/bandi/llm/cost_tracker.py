# 功能：Token 成本追踪（按会话累计费用/用量）
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bandi.llm.model_catalog import ModelCatalog

if TYPE_CHECKING:
    from bandi.observability.cost_store import CostStore

_PER_MILLION = 1_000_000

# DeepSeek Flash 线保守下限计；须 ≥ 真实价，宁高勿低）。⚠️ 它**不是展示价**：未定价模型不再把该值当真
# 实费用写给用户（展示为「未定价」），但仍必须参与**预算累加**——否则换个模型名
# 绕过目录即可让预算失效（预算语义 = 「不低于此价」）。
_BUDGET_FLOOR_INPUT_PRICE = 1.584
_BUDGET_FLOOR_OUTPUT_PRICE = 4.752

_MAX_CONSUMED_EVENT_IDS = 100_000

# 用 deque(maxlen) 环形有界——丢最旧不影响 `_entries[-1]` 的 tool 归因打标语义
# （该处只关心"最后一条"）。超限丢弃的明细已由 cost_store 落库，历史查询走库。
_MAX_COST_ENTRIES = 10_000


@dataclass
class CostEntry:  # 功能：单次 LLM 调用成本记录
    session_id: str
    timestamp: float
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float | None = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    # 无法回答「哪个 AI 员工最贵 / 哪条通道花了多少」。
    agent_role: str = ""
    # 具体 agent 实例（role@会话尾），同角色并发会话可区分
    agent_id: str = ""
    turn_id: int = 0
    attempt_id: int = 0
    prompt_hash: str = ""
    system_tokens: int = 0
    # system 消息按二级标题分段的 token 占用——定位固定开销落在哪一段
    system_sections: dict[str, int] = field(default_factory=dict)
    history_tokens: int = 0
    memory_tokens: int = 0
    tool_schema_tokens: int = 0
    input_tokens_estimated: int = 0
    input_tokens_reported: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    # 命中率分母口径统一，见 core/message.py::TokenUsage.input_tokens_total
    cache_creation_tokens: int = 0
    input_tokens_total: int = 0
    reason: str = ""
    # `live:<provider>`（该 provider 在 models.dev 的实时价，含网关转售加价）/
    # `unpriced`（无价，金额为 None 且预算按下限估计）
    pricing_source: str = ""


@dataclass
class SessionCost:  # 功能：单 Session 聚合成本（总 token/总费用/分模型明细）
    session_id: str
    total_tokens: int = 0
    total_cost_cny: float = 0.0
    token_breakdown: dict[str, int] = field(default_factory=lambda: {"prompt": 0, "completion": 0})
    per_model: dict[str, dict[str, float]] = field(default_factory=dict)
    unpriced_calls: int = 0


# 进程级共享 tracker——预算语义为「当日累计」（跨 run 生效）。
# run_agent_turn/run_agent 注入时复用共享实例。
_shared_cost_tracker: CostTracker | None = None


def get_shared_cost_tracker(
    catalog: ModelCatalog, budget_limit: float, store: CostStore | None = None
) -> CostTracker:
    """获取进程级共享 CostTracker（当日累计语义）。

    * `budget_limit` 变化 → 重建（原语义）；
    * `store` 非 None → **就地挂载**到既有实例，**不重建**。

    为什么 store 变化不重建：调用方常在 run 之前先取一次 tracker 再断言其累计值；
    若此时因 store 变化而重建，先前捕获的引用会变成游离对象，记账看起来「凭空消失」
    （`test_contract_run_agent_records_cost` 正是这种用法——曾被此条改动打破）。
    """
    global _shared_cost_tracker
    if _shared_cost_tracker is None:
        _shared_cost_tracker = CostTracker(catalog=catalog, budget_limit=budget_limit, store=store)
        return _shared_cost_tracker
    # 语义失效、预算形同虚设：调用点两处传不同 budget 时反复清零）
    if _shared_cost_tracker._budget_limit != budget_limit:
        _shared_cost_tracker._budget_limit = budget_limit
    if store is not None and _shared_cost_tracker._store is not store:
        _shared_cost_tracker._store = store
    return _shared_cost_tracker


def reset_shared_cost_tracker() -> None:
    """
    当日累计会让快跑与慢跑产生不同基线。仅测试 fixture 调用，生产禁用。"""
    global _shared_cost_tracker
    _shared_cost_tracker = None


def compute_cost_cny(
    pricing: Any,
    *,
    input_tokens_total: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    completion_tokens: int = 0,
) -> float:
    """功能：**唯一**分档计价公式（`CostTracker.record` 与台账重算 `reprice` 共用）。

    为什么要抽出来：历史台账重算若自己再写一遍公式，两处迟早漂移（改一处忘一处）
    而「重算出来的钱」与「新记账的钱」必须是同一口径。

    口径（先归一再分档）：各协议 `prompt_tokens` 含不含缓存部分并不一致
    （OpenAI/DeepSeek 含，Anthropic 不含），故统一用归一后的**总输入**推未缓存部分：
        `未缓存 = max(0, 总输入 − 缓存读 − 缓存写)`
    缓存单价缺失（0）⇒ 回落输入单价 = **无折扣**（绝不臆造折扣）。
    """
    total_input = max(0, int(input_tokens_total))
    cache_read = max(0, int(cache_read_tokens))
    cache_write = max(0, int(cache_creation_tokens))
    uncached = max(0, total_input - cache_read - cache_write)
    read_price = pricing.cache_read_per_1m or pricing.input_per_1m
    write_price = pricing.cache_write_per_1m or pricing.input_per_1m
    return (
        uncached * pricing.input_per_1m
        + cache_read * read_price
        + cache_write * write_price
        + max(0, int(completion_tokens)) * pricing.output_per_1m
    ) / _PER_MILLION


class CostTracker:  # 功能：成本追踪器（记录调用/聚合统计/预算检查）
    def __init__(
        self,
        catalog: ModelCatalog,
        budget_limit: float = -1.0,
        store: CostStore | None = None,
        max_entries: int = _MAX_COST_ENTRIES,
    ):
        self._catalog = catalog
        # 并发记账存在丢更新与跨日重置判定竞态。可重入锁便于内部方法互相调用。
        self._entries: deque[CostEntry] = deque(maxlen=max(1, int(max_entries)))
        self._daily_total: float = 0.0
        self._budget_limit = budget_limit
        self._lock = threading.RLock()
        # .2：可选台账落库。默认 None —— 内存语义不变，由装配点显式开启
        # 避免测试/库用法意外写到真实 ~/.bandi/data。
        self._store = store
        self._pending_tool_calls: dict[
            str, str
        ] = {}  # tool_call_id -> session_id (for D12 per-tool attribution)
        self._processed_event_ids: set[str] = set()

    def _already_consumed(self, event: object) -> bool:
        # 功能：幂等检查——同 event_id 事件已消费则跳过（replay/重复 emit 防护）
        eid = getattr(event, "event_id", "") or ""
        if not eid:
            return False
        with self._lock:
            if eid in self._processed_event_ids:
                return True
            # 上限裁剪：防长期运行无限增长（清空后历史重放不再拦截，近期防护为主）
            if len(self._processed_event_ids) >= _MAX_CONSUMED_EVENT_IDS:
                self._processed_event_ids.clear()
            self._processed_event_ids.add(eid)
            return False

    @staticmethod
    def _resolve_pricing(provider: str, model: str, spec: Any) -> tuple[Any, str]:
        """功能：定价解析——**厂商 CNY 表是权威，网关实时价按需接管**。

        为什么需要它：同一模型经不同 provider 转售价格可能不同（deepseek 官方
        $0.15/$0.6，OpenRouter 转售 $0.3/$1.2）。此前一律用目录价 ⇒ **网关加价部分
        完全不记账**（成本偏低）。价格不硬编码：取自 models.dev 的 per-provider
        `cost`（随 TTL 自动刷新）。

        顺序（返回 `(pricing | None, source)`）：
        1. `(provider, model)` 有实时价 **且 provider 不是该模型的厂商**
           （= 网关转售）⇒ 用**实时价**（含加价）；`source = "live:<provider>"`。
        2. 目录命中 ⇒ 用**目录价**（厂商 CNY 权威：DeepSeek 峰谷价等本地口径不可被
           USD 换算覆盖）；`source = "catalog"`。
        3. 都没有 ⇒ `(None, "unpriced")`（调用方按预算下限估计并记 unpriced）。

        ⚠️ `openai_compat`（未知 OpenAI 兼容网关的兜底名）**不做别名映射**——猜错
        provider 会错价，宁可回落目录价。
        """
        from bandi.llm.model_catalog import ModelPricing

        live: dict[str, Any] | None = None
        try:
            from bandi.llm.models_dev import lookup_provider_price

            live = lookup_provider_price(provider, model)
        except Exception:  # noqa: BLE001 — 元数据不可用不得影响记账主链
            live = None

        vendor = (getattr(spec, "provider", "") or "").strip().lower()
        is_gateway = bool(vendor) and (provider or "").strip().lower() != vendor
        if live is not None and (spec is None or is_gateway):
            return (
                ModelPricing(
                    live["input_per_1m"],
                    live["output_per_1m"],
                    str(live.get("currency") or "CNY"),
                    cache_read_per_1m=float(live.get("cache_read_per_1m") or 0.0),
                    cache_write_per_1m=float(live.get("cache_write_per_1m") or 0.0),
                ),
                f"live:{live.get('provider') or provider}",
            )
        if spec is not None:
            return spec.pricing, "catalog"
        return None, "unpriced"

    def record(  # 功能：记录一次 LLM 调用成本（**含缓存分档计价**），跨日自动重置 daily_total
        self,
        session_id: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        breakdown: dict[str, Any] | None = None,
        *,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        input_tokens_total: int = 0,
        agent_id: str = "",
    ) -> CostEntry:
        """分档计价 + 未定价模型不编造金额。

        计价口径（**先归一再分档，避免重复计费**）：各协议的 `prompt_tokens` 含不含
        缓存部分并不一致（OpenAI/DeepSeek 含，Anthropic 不含），故一律用归一后的
        `input_tokens_total` 推出未缓存部分：
            未缓存 = max(0, input_tokens_total − cache_read − cache_creation)
            费用 = 未缓存×input价 + 命中×cache_read价 + 写入×cache_write价 + 输出×output价
        缓存单价缺失（0）⇒ 回退 input 单价（= 无折扣，绝不臆造折扣）。

        catalog 外模型：`cost_cny = None`（前端显示「未定价」），但**预算仍按下限估计
        扣减**（防换名绕过）。
        """
        now = time.time()

        spec = self._catalog.get(model)
        pricing, pricing_source = self._resolve_pricing(provider, model, spec)
        cost: float | None = None
        charge = 0.0  # 预算扣减额：恒有值（未定价模型用下限估计）
        if pricing is not None:
            cost = compute_cost_cny(
                pricing,
                input_tokens_total=input_tokens_total or prompt_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                completion_tokens=completion_tokens,
            )
            charge = cost
            breakdown = {**(breakdown or {}), "pricing_source": pricing_source}
        else:
            # 未定价（catalog 外且无实时价）：展示 None；预算按「不低于此价」估计
            charge = (
                prompt_tokens * _BUDGET_FLOOR_INPUT_PRICE
                + completion_tokens * _BUDGET_FLOOR_OUTPUT_PRICE
            ) / _PER_MILLION
            breakdown = {
                **(breakdown or {}),
                "unpriced": True,
                "budget_estimate_cny": round(charge, 6),
                "pricing_source": "unpriced",
            }

        entry = CostEntry(
            session_id=session_id,
            timestamp=now,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_cny=cost,
            agent_id=agent_id,
        )
        for k, v in (breakdown or {}).items():
            if hasattr(entry, k):
                setattr(entry, k, v)
        with self._lock:
            # 跨日重置判定与追加必须同临界区：否则并发下"判定→追加"之间可被另一
            if self._entries:
                last_day = time.localtime(self._entries[-1].timestamp).tm_yday
                if time.localtime(now).tm_yday != last_day:
                    self._daily_total = 0.0
            self._entries.append(entry)
            self._daily_total += charge
        self._persist(entry)  # best-effort 落库放锁外，避免 IO 阻塞记账主链
        return entry

    def _persist(self, entry: CostEntry) -> None:
        """.2：把本条成本落进台账（best-effort，非阻塞）。

        观测设施故障绝不影响计费主链——失败只计数，不抛。
        """
        if self._store is None:
            return
        try:
            from bandi.observability.cost_store import CostRecord, build_event_id

            self._store.enqueue(
                CostRecord(
                    session_id=entry.session_id,
                    provider_name=entry.provider,
                    model=entry.model,
                    agent_role=entry.agent_role,
                    agent_id=entry.agent_id,
                    call_kind="llm",
                    input_tokens=entry.prompt_tokens,
                    output_tokens=entry.completion_tokens,
                    cache_tokens=entry.cache_tokens,
                    # 台账列 NOT NULL ⇒ 未定价写 0.0，真实性由 breakdown.unpriced 承载
                    total_cost_cny=float(entry.cost_cny or 0.0),
                    turn_id=entry.turn_id,
                    attempt_id=entry.attempt_id,
                    trace_id=entry.trace_id,
                    event_id=build_event_id(
                        entry.session_id,
                        entry.timestamp,
                        entry.model,
                        entry.prompt_tokens,
                        entry.completion_tokens,
                    ),
                    breakdown={
                        "system_tokens": entry.system_tokens,
                        "system_sections": entry.system_sections,
                        "history_tokens": entry.history_tokens,
                        "memory_tokens": entry.memory_tokens,
                        "tool_schema_tokens": entry.tool_schema_tokens,
                        "input_tokens_estimated": entry.input_tokens_estimated,
                        "input_tokens_reported": entry.input_tokens_reported,
                        "prompt_hash": entry.prompt_hash,
                        "reason": entry.reason,
                        "pricing_source": entry.pricing_source,
                        "cache_creation_tokens": entry.cache_creation_tokens,
                        "input_tokens_total": entry.input_tokens_total,
                        "unpriced": entry.cost_cny is None,
                    },
                )
            )
        except Exception:  # noqa: BLE001 — 落库故障不得影响 LLM 主链
            pass

    @property
    def daily_total(self) -> float:
        with self._lock:
            return self._daily_total

    @property
    def entries(self) -> list[CostEntry]:
        # 副本返回：外部清空/改动不得污染内部明细（快照语义）
        with self._lock:
            return list(self._entries)

    def check_budget(self) -> bool:  # 功能：检查当日累计花费是否超过预算上限
        with self._lock:
            # 跨日自愈——check_budget 在 LLM 调用前读昨日累计时先重置
            # 避免昨天超预算今天永远拦截无法自愈
            if self._entries:
                last_day = time.localtime(self._entries[-1].timestamp).tm_yday
                current_day = time.localtime(time.time()).tm_yday
                if current_day != last_day:
                    self._daily_total = 0.0
            if self._budget_limit < 0:
                return True
            return self._daily_total < self._budget_limit

    def get_session_cost(
        self, session_id: str
    ) -> SessionCost:  # 功能：按 session_id 聚合查询总 token 和总费用
        with self._lock:
            sc = SessionCost(session_id=session_id)
            for e in self._entries:
                if e.session_id != session_id:
                    continue
                sc.total_tokens += e.prompt_tokens + e.completion_tokens
                if e.cost_cny is None:
                    sc.unpriced_calls += 1
                sc.total_cost_cny += e.cost_cny or 0.0
                sc.token_breakdown["prompt"] += e.prompt_tokens
                sc.token_breakdown["completion"] += e.completion_tokens
                if e.model not in sc.per_model:
                    sc.per_model[e.model] = {"tokens": 0, "cost": 0.0}
                sc.per_model[e.model]["tokens"] += e.prompt_tokens + e.completion_tokens
                sc.per_model[e.model]["cost"] += e.cost_cny or 0.0
            return sc

    def aggregate_by_role(self) -> dict[str, dict[str, float]]:
        """功能：按角色聚合 token/费用（多 Agent 协同成本归属）。

        此前只有 session 维度，无法回答「哪个 AI 员工最贵」；席位主链 session_id
        虽编码了 role，但缺一等公民的聚合入口。无角色记入 "(unassigned)"。
        """
        with self._lock:
            out: dict[str, dict[str, float]] = {}
            for e in self._entries:
                role = e.agent_role or "(unassigned)"
                bucket = out.setdefault(role, {"tokens": 0, "cost_cny": 0.0, "calls": 0})
                bucket["tokens"] += e.prompt_tokens + e.completion_tokens
                bucket["cost_cny"] += e.cost_cny or 0.0
                if e.cost_cny is None:
                    bucket["unpriced_calls"] = bucket.get("unpriced_calls", 0) + 1
                bucket["calls"] += 1
            return out

    def aggregate_by_agent(self) -> dict[str, dict[str, float]]:
        """功能：按 agent 实例聚合 token/费用（同角色并发会话可区分）。"""
        with self._lock:
            out: dict[str, dict[str, float]] = {}
            for e in self._entries:
                agent = e.agent_id or "(unassigned)"
                bucket = out.setdefault(agent, {"tokens": 0, "cost_cny": 0.0, "calls": 0})
                bucket["tokens"] += e.prompt_tokens + e.completion_tokens
                bucket["cost_cny"] += e.cost_cny or 0.0
                if e.cost_cny is None:
                    bucket["unpriced_calls"] = bucket.get("unpriced_calls", 0) + 1
                bucket["calls"] += 1
            return out

    def reset_daily(self) -> None:  # 功能：清空所有记录和当日累计
        with self._lock:
            self._daily_total = 0.0
            self._entries.clear()

    async def on_llm_response(
        self, event: object
    ) -> None:  # 功能：EventBus LLM_RESPONSE 事件处理器——从 event data 提取模型名和用量
        if self._already_consumed(event):
            return
        data = event.data if hasattr(event, "data") else {}
        if data.get("settled"):
            # 旁路 accounted_complete 已直写台账——事件仅供观测，不得再计一笔
            return
        provider = data.get("provider", "unknown")
        model = data.get("model", "unknown")
        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        session_id = data.get("session_id", "default")
        self.record(
            session_id=session_id,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=int(data.get("cache_tokens", 0) or 0),
            cache_creation_tokens=int(data.get("cache_creation_tokens", 0) or 0),
            input_tokens_total=int(data.get("input_tokens_total", 0) or 0),
            agent_id=str(data.get("agent_id", "") or ""),
            breakdown={
                "trace_id": str(data.get("trace_id", "")),
                "agent_role": str(data.get("agent_role", "") or ""),
                "turn_id": int(data.get("turn", 0) or 0),
                "attempt_id": int(data.get("attempt_id", 0) or 0),
                "prompt_hash": str(data.get("prompt_hash", "")),
                "system_tokens": int(data.get("system_tokens", 0) or 0),
                "system_sections": {
                    str(k): int(v) for k, v in (data.get("system_sections") or {}).items()
                },
                "history_tokens": int(data.get("history_tokens", 0) or 0),
                "memory_tokens": int(data.get("memory_tokens", 0) or 0),
                "tool_schema_tokens": int(data.get("tool_schema_tokens", 0) or 0),
                "input_tokens_estimated": int(data.get("input_tokens_estimated", 0) or 0),
                "input_tokens_reported": int(data.get("input_tokens_reported", 0) or 0),
                "output_tokens": int(data.get("output_tokens", 0) or 0),
                "cache_tokens": int(data.get("cache_tokens", 0) or 0),
                "cache_creation_tokens": int(data.get("cache_creation_tokens", 0) or 0),
                "input_tokens_total": int(data.get("input_tokens_total", 0) or 0),
                "reason": str(data.get("reason", "")),
            },
        )
        # 多 session 并发交错时不互抢/不清对方（旧全局取末+clear 会把
        # 🟢-5：entries 空判防并发 reset（record 与归因块之间被 reset_daily 清空）
        with self._lock:
            if self._pending_tool_calls and self._entries:
                matched = [tc for tc, sid in self._pending_tool_calls.items() if sid == session_id]
                if matched:
                    self._entries[-1].meta["attributed_tool_call_id"] = matched[-1]
                    for tc in matched:
                        self._pending_tool_calls.pop(tc, None)

    async def on_tool_done(self, event: object) -> None:
        if self._already_consumed(event):
            return
        data = event.data if hasattr(event, "data") else {}
        if not data:
            return
        tool_call_id = data.get("tool_call_id", "")
        session_id = data.get("session_id", "default")
        if tool_call_id:
            with self._lock:
                self._pending_tool_calls[tool_call_id] = session_id
        entry = CostEntry(
            session_id=session_id,
            timestamp=time.time(),
            provider="tool",
            model="tool",
            meta={
                "kind": "tool",
                "tool_name": data.get("tool_name", "unknown"),
                "tool_call_id": tool_call_id,
                "duration_ms": data.get("duration_ms", 0),
                "success": bool(data.get("success", True)),
            },
        )
        with self._lock:
            self._entries.append(entry)

    def tool_stats(self, limit: int = 50) -> dict[str, dict[str, float | int]]:
        """聚合工具执行统计（次数/平均耗时/失败数），供观测与审计消费。"""
        with self._lock:
            stats: dict[str, dict[str, float | int]] = {}
            for e in self._entries:
                if e.meta.get("kind") != "tool" and "tool_name" not in e.meta:
                    continue
                name = e.meta["tool_name"]
                agg = stats.setdefault(name, {"calls": 0, "failures": 0, "total_ms": 0.0})
                agg["calls"] = int(agg["calls"]) + 1
                if not e.meta.get("success", True):
                    agg["failures"] = int(agg["failures"]) + 1
                agg["total_ms"] = float(agg["total_ms"]) + float(e.meta.get("duration_ms", 0))
            return {k: v for k, v in list(stats.items())[:limit]}

    def tool_token_stats(
        self, limit: int = 50
    ) -> dict[str, dict[str, int | float]]:  # 功能：D12 per-tool token 归因统计
        """按工具聚合 token 用量（prompt/completion）和调用统计。"""
        with self._lock:
            return self._tool_token_stats_locked(limit)

    def _tool_token_stats_locked(self, limit: int) -> dict[str, dict[str, int | float]]:
        """功能：`tool_token_stats` 的锁内实现（调用方必须已持 `_lock`）。"""
        tool_tokens: dict[str, tuple[int, int]] = {}
        for e in self._entries:
            attr_id = e.meta.get("attributed_tool_call_id")
            if not attr_id:
                continue
            pt = e.prompt_tokens
            ct = e.completion_tokens
            tool_tokens[attr_id] = (pt, ct)

        stats: dict[str, dict[str, int | float]] = {}
        for e in self._entries:
            if e.meta.get("kind") != "tool" and "tool_name" not in e.meta:
                continue
            name = e.meta.get("tool_name", "unknown")
            call_id = e.meta.get("tool_call_id", "")
            toks = tool_tokens.get(call_id, (0, 0))
            agg = stats.setdefault(
                name,
                {
                    "calls": 0,
                    "failures": 0,
                    "total_ms": 0.0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                },
            )
            agg["calls"] = int(agg["calls"]) + 1
            if not e.meta.get("success", True):
                agg["failures"] = int(agg["failures"]) + 1
            agg["total_ms"] = float(agg["total_ms"]) + float(e.meta.get("duration_ms", 0))
            agg["prompt_tokens"] = int(agg["prompt_tokens"]) + toks[0]
            agg["completion_tokens"] = int(agg["completion_tokens"]) + toks[1]
        return {k: v for k, v in list(stats.items())[:limit]}
