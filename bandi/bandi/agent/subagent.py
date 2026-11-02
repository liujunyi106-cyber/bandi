# 功能：子 Agent 会话管理（配置/结果/会话生命周期）
from __future__ import annotations

import asyncio
import contextvars
import copy
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from bandi.company.handoff import AgentHandoff
from bandi.observability.tracer import generate_trace_id

logger = logging.getLogger(__name__)


class _ChildLedgerRegistry:
    """功能：子代理专用注册表视图——`todo_read`/`todo_write`
    换绑到**子台账**，其余工具复用父实例（同一实例跨会话无状态，安全）。

    存在意义：子代理继承父注册表时，两个 todo 工具持有的是**父会话台账对象**——子代理
    写 todo 会脏写父台账，下一次父轮 persist 把子任务项写进父会话任务条（用户可见污染
    + `max_items` 挤掉父任务项）。子台账不发运行时事件、不落盘，只服务子代理自身
    todo_read/todo_write 往返一致。
    """

    def __init__(self, base: Any, ledger: Any) -> None:
        from bandi.tools.todo import TodoReadTool, TodoWriteTool

        self._base = base
        self._overrides = {
            "todo_read": TodoReadTool(ledger=ledger),
            "todo_write": TodoWriteTool(ledger=ledger),
        }
        self.ledger = ledger

    def get(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return self._base.get(name)

    def list_tools(self) -> list[Any]:
        replaced = set(self._overrides)
        return [
            self._overrides[tool.name] if tool.name in replaced else tool
            for tool in self._base.list_tools()
        ]

    def list_schemas(
        self, mode: str | None = None, allowed_tools: list[str] | None = None
    ) -> list[Any]:
        return self._base.list_schemas(mode=mode, allowed_tools=allowed_tools)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def _subagent_schema_allowlist(
    candidates: list[str] | None,
    tool_registry: Any,
    role_tool_tags: list[str] | None,
) -> list[str] | None:
    """功能：子代理 schema 白名单计算（与主链 factory 同源口径）。

    - `candidates is None`（未显式限定）且角色受限 → 以全量为候选按 tags 过滤；
    - 过滤后/显式名单**必须并入全域交互通道**（with_always_available）——
       修复：受限角色父代理 spawn 的子代理此前 schema 面
      丢失 request_user_input（schema 可见面与主链不一致）；
    - `None` = 不过滤（继承全量，由 list_schemas 语义承担）；空名单 = 显式全禁
      但交互通道仍并（与主链 factory 对空名单的处理一致）。
    """
    schema_allowlist = candidates
    if role_tool_tags is not None:
        from bandi.agent.factory import filter_tools_by_role_tags

        base = (
            schema_allowlist
            if schema_allowlist is not None
            else [t.name for t in tool_registry.list_tools()]
        )
        schema_allowlist = filter_tools_by_role_tags(base, tool_registry, role_tool_tags)
    if schema_allowlist is None:
        return None
    from bandi.tools.registry import with_always_available

    return with_always_available(schema_allowlist)


def build_child_ledger_bundle(registry: Any) -> tuple[Any, Any]:
    """功能：构造子代理独立台账 + 换绑注册表视图（生产与契约同源入口）。

    子台账配置 = 不发运行时事件（不上父链帧）/ 不落会话 / 不落工作项（只服务子代理自身）。
    """
    from bandi.tools.task_ledger import TaskLedger, TaskLedgerConfig

    ledger = TaskLedger(
        config=TaskLedgerConfig(
            emit_runtime_events=False, persist_to_session=False, persist_to_work_item=False
        )
    )
    if registry is None:
        return ledger, None
    return ledger, _ChildLedgerRegistry(registry, ledger)


_SUBAGENT_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "bandi_subagent_depth", default=0
)

COMPLETED_SNAPSHOTS_CAP = 200

# 仅在配置对象缺该字段（旧形态）时生效；显式 0 仍表示"主动关闸门"（交执行器告警）。
DEFAULT_MAX_CONCURRENCY = 8


def subagent_batch_concurrency(tools_cfg: Any) -> Any:
    """功能：子代理批内并发上限取值（单一取值点，与主链 config 同源）。

    装配点此前完全漏传该参数 → 子代理批内无限并发，的闸门被绕过。
    取单一函数便于契约直接验证取值（含配置缺字段的兜底路径）。
    """
    if tools_cfg is None:
        return DEFAULT_MAX_CONCURRENCY
    value = getattr(tools_cfg, "max_concurrency", None)
    return DEFAULT_MAX_CONCURRENCY if value is None else value


# SubAgentTool 每次 execute 新建 manager，实例级快照无消费者——查询/取消面
_ACTIVE_TASKS: dict[str, SubagentManager] = {}
_ACTIVE_TASKS_LOCK = threading.Lock()
_COMPLETED_SNAPSHOTS: OrderedDict[str, SubagentResult] = OrderedDict()
_COMPLETED_SNAPSHOTS_LOCK = threading.Lock()


def _now_iso() -> str:
    # 功能：完成时间戳（naive 本地 ISO，对齐全局时间口径）
    return datetime.now().isoformat(timespec="seconds")


def build_child_agent_identity(parent_agent_id: str, session_id: str) -> tuple[str, str]:
    """功能：子代理日志身份——返回 (子 agent_id, 父 agent_id)。

    子 id 形如 `<parent>::sub@<会话尾8位>`；无父时 `subagent@<会话尾8位>`
    保证子代理链路日志的 agent 维度非空、同角色多会话可区分（续）。
    空会话省略 `@` 分隔符——不得产出悬空 `subagent@`。
    """
    suffix = str(session_id or "")[-8:]
    parent = str(parent_agent_id or "")
    at_suffix = f"@{suffix}" if suffix else ""
    if parent:
        return f"{parent}::sub{at_suffix}", parent
    return f"subagent{at_suffix}", ""


def _clone_hook_runner(runner: Any, agent_id: str) -> Any:
    """功能：为子代理克隆 hooks runner 并绑定子身份。

    HookRunner.set_agent_id 只影响自身，浅拷贝即可让每个子代理持有独立血缘——
    子代理内 hooks 派生的孙代理 parent_agent_id 由此等于子代理 id；克隆体经
    executor extra 继续下传，孙代理工具面不丢 PreToolUse/PostToolUse。克隆失败
    回落同一实例（仅血缘回落旧行为，不阻断子代理）。
    """
    try:
        cloned = copy.copy(runner)
    except Exception:  # noqa: BLE001 — 克隆失败不阻断子代理
        return runner
    setter = getattr(cloned, "set_agent_id", None)
    if callable(setter):
        setter(agent_id)
    return cloned


def list_active_tasks() -> list[str]:
    # 功能：活跃子代理 task_id 列表（进程内）
    with _ACTIVE_TASKS_LOCK:
        return list(_ACTIVE_TASKS)


def cancel_active_task(task_id: str) -> bool:
    # 功能：按 task_id 定向取消活跃子代理——命中与否如实返回（API 404 依据）
    with _ACTIVE_TASKS_LOCK:
        manager = _ACTIVE_TASKS.get(task_id)
    if manager is None:
        return False
    return bool(manager.cancel(task_id))


def record_completed_task(result: SubagentResult) -> None:
    # 功能：写完成快照（FIFO 200 淘汰最旧，重复 task_id 幂等移位）
    if not result.task_id:
        return
    with _COMPLETED_SNAPSHOTS_LOCK:
        _COMPLETED_SNAPSHOTS[result.task_id] = result
        _COMPLETED_SNAPSHOTS.move_to_end(result.task_id)
        while len(_COMPLETED_SNAPSHOTS) > COMPLETED_SNAPSHOTS_CAP:
            _COMPLETED_SNAPSHOTS.popitem(last=False)


# 预留：按 task_id 查子任务完成快照；启用时机 子任务注册表单查
def get_completed_task(task_id: str) -> SubagentResult | None:
    # 功能：按 task_id 查完成快照
    with _COMPLETED_SNAPSHOTS_LOCK:
        return _COMPLETED_SNAPSHOTS.get(task_id)


def list_completed_tasks(limit: int = 50) -> list[SubagentResult]:
    # 功能：完成快照列表（最近完成优先，limit 截断）
    with _COMPLETED_SNAPSHOTS_LOCK:
        items = list(_COMPLETED_SNAPSHOTS.values())
    items.reverse()
    return items[: max(0, limit)]


def clear_completed_tasks() -> None:
    # 功能：清空完成快照（测试隔离/运维用）
    with _COMPLETED_SNAPSHOTS_LOCK:
        _COMPLETED_SNAPSHOTS.clear()


class _SubagentEventBus:
    """功能：子代理事件标记总线——emit 时在 data 注入 subagent 标签
    父链 `_HandlerBindings` 据此把子代理过程归属转发为嵌套过程帧。"""

    def __init__(self, inner: Any, label: dict[str, Any]) -> None:
        self._inner = inner
        self._label = label

    async def emit(self, event: Any) -> Any:
        data = getattr(event, "data", None)
        if isinstance(data, dict) and "subagent" not in data:
            data["subagent"] = dict(self._label)
        return await self._inner.emit(event)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ToolCallLimiter:
    """子 Agent 工具调用次数上限——包装 ToolExecutor，超限设 MAX_TURNS 终态。"""

    def __init__(self, inner: Any, max_tool_calls: int, ctx: Any | None = None) -> None:
        self._inner = inner
        self._max = max_tool_calls
        self._count = 0
        # 生产调用形态为位置参数 execute(calls)——ctx 必须构造期绑定
        # （kwargs 形态仅测试用，不可作为唯一通道）
        self._ctx = ctx

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        if self._count >= self._max:
            from bandi.agent.base import TerminalReason

            ctx = kwargs.get("ctx") or self._ctx
            if ctx is not None:
                ctx.terminal_reason = TerminalReason.MAX_TURNS
                ctx.final_message = f"子代理工具调用次数达到上限（{self._max}），任务未完成。"
            from bandi.core.message import ToolResult

            call = args[0][0] if args and args[0] else None
            return [
                ToolResult(
                    tool_call_id=getattr(call, "id", "limit"),
                    tool_name=getattr(call, "name", "limit"),
                    success=False,
                    output="",
                    error=f"subagent tool call limit ({self._max}) reached",
                )
            ]
        self._count += 1
        return await self._inner.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@dataclass
class SubagentConfig:
    tools_allowlist: list[str] | None = None
    max_turns: int = 10
    timeout_sec: int = 300
    max_tool_calls: int = 0
    max_depth: int = 3
    # 技能级覆盖（空串 = 跟随全局配置）：模型名与思考档位
    model: str = ""
    effort: str = ""


def subagent_overridden_config(config_obj: Any, overrides: SubagentConfig) -> Any:
    # 功能：按技能覆盖生成配置副本（模型名 + 思考档位）；无覆盖返回原对象
    if not (overrides.model or overrides.effort):
        return config_obj
    tuned = config_obj.model_copy(deep=True)
    if overrides.model:
        tuned.llm.default_model = overrides.model
    if overrides.effort:
        tuned.llm.thinking_mode = overrides.effort
    return tuned


def subagent_provider_for_overrides(parent: Any, overrides: SubagentConfig, config_obj: Any) -> Any:
    # 功能：按覆盖后的配置解析 provider；解析失败回退父 provider（不阻断子代理）
    if not (overrides.model or overrides.effort):
        return parent
    model = overrides.model or str(getattr(config_obj.llm, "default_model", "") or "")
    if not model:
        return parent
    try:
        from bandi.agent.factory import get_provider_registry

        return get_provider_registry(config_obj).get_provider(model, config_obj)
    except Exception as exc:  # noqa: BLE001 — 覆盖失败降级父 provider
        logger.warning("subagent skill override provider resolution failed (%s): %s", model, exc)
        return parent


@dataclass
class SubagentResult:
    success: bool = False
    summary: str = ""
    message: str = ""
    artifacts: list[str] | None = None
    total_tokens: int = 0
    session_id: str = ""
    task_id: str = ""
    completed_at: str = ""
    # .12：结构化交接面（结论面）——子代理产出不再只有纯文本
    handoff: AgentHandoff | None = None


@dataclass
class SubagentSession:
    session_id: str = ""
    task_id: str = ""
    config: SubagentConfig | None = None
    status: str = "running"
    task: Any | None = None
    depth: int = 0
    parent_trace_id: str = ""
    # 父流程（gather 聚合）不被单 worker 取消炸断；父中断仍向上传播
    cancelled: bool = False


class SubagentManager:
    def __init__(
        self,
        parent_context: Any | None = None,
        provider: Any | None = None,
        permissions_pipeline: Any | None = None,
        project_root: str | None = None,
        idempotency_store: Any | None = None,
        event_bus: Any | None = None,
        role_tool_tags: list[str] | None = None,
        role_fs_scopes: list[str] | None = None,
        hook_runner: Any | None = None,
        user_id: str = "",
        policy_engine: Any | None = None,
        parent_agent_id: str = "",
    ) -> None:
        self._parent = parent_context
        # 日志/血缘据此写 parent_agent_id；空值时回落父 context.meta
        self._parent_agent_id = str(parent_agent_id or "")
        # 父总线（context 透传）——子 Agent 事件/成本挂父链的唯一入口；
        # 缺省保留 parent_context.bus 旧形态兼容，均无则 _run_subagent 自建
        self._event_bus = event_bus
        self._active: dict[str, SubagentSession] = {}
        self._task_index: dict[str, str] = {}
        self._provider = provider
        if permissions_pipeline is None:
            from bandi.tools.permissions_pipeline import PermissionsPipeline

            # 与子代理继承的项目根不一致）
            permissions_pipeline = PermissionsPipeline(project_root=project_root)
        self._permissions_pipeline = permissions_pipeline
        # 子 Agent 装配对齐（父→子）：project_root 沙箱继承（子代理相对路径
        # 落父项目根而非全局 workspace）+ 幂等存储共享（跨调度重放保序）
        self._project_root = project_root
        self._idempotency_store = idempotency_store
        # .1：角色门控/沙箱继承——None=不过滤；空名单=显式全禁（fail-closed
        # 与 ToolExecutor 同语义），否则受限角色可借子代理绕过角色边界
        self._role_tool_tags = list(role_tool_tags) if role_tool_tags is not None else None
        self._role_fs_scopes = list(role_fs_scopes) if role_fs_scopes else None
        self._hook_runner = hook_runner
        # （审批/策略/缓存身份不因 spawn 断链）
        self._user_id = user_id
        # （如 member.execute=DENY）可经 subagent_spawn 在子代理内绕过
        self._policy_engine = policy_engine
        from bandi.agent.checkpoint import CheckpointStore

        self._loop_checkpoint_store = CheckpointStore()

    def close(self) -> None:
        """关闭共享 CheckpointStore 连接——每次 spawn 新建 SubagentManager
        时，调用方（SubAgentTool）用后调用，防 sqlite 连接泄漏（每次 subagent
        调用泄漏一个 ~/.bandi/checkpoints.db 连接）。"""
        closer = getattr(self._loop_checkpoint_store, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:  # noqa: BLE001 — 关闭失败不阻断
                pass

    async def spawn(
        self,
        task: str,
        config: SubagentConfig | None = None,
        tool_registry: Any | None = None,
        parent_trace_id: str | None = None,
        depth: int = 0,
        parent_tool_call_id: str = "",
    ) -> SubagentResult:
        cfg = config or SubagentConfig()
        if depth >= cfg.max_depth:
            return SubagentResult(
                success=False, summary=f"Subagent recursion depth limit ({cfg.max_depth}) exceeded"
            )
        session_id = uuid4().hex
        task_id = f"subagent-{uuid4().hex[:12]}"
        session = SubagentSession(
            session_id=session_id,
            task_id=task_id,
            config=cfg,
            depth=depth,
            parent_trace_id=parent_trace_id or "",
        )
        task_obj = asyncio.create_task(
            self._run_subagent(
                task,
                cfg,
                session,
                tool_registry,
                parent_trace_id or "",
                depth,
                **({"parent_tool_call_id": parent_tool_call_id} if parent_tool_call_id else {}),
            )
        )
        session.task = task_obj
        self._active[session_id] = session
        self._task_index[task_id] = session_id
        with _ACTIVE_TASKS_LOCK:
            _ACTIVE_TASKS[task_id] = self
        result: SubagentResult | None = None
        try:
            result = await asyncio.wait_for(task_obj, timeout=cfg.timeout_sec)
            return result
        except TimeoutError:
            task_obj.cancel()
            try:
                await task_obj
            except asyncio.CancelledError:
                if session.cancelled:
                    result = SubagentResult(
                        success=False,
                        summary="Subagent cancelled",
                        session_id=session_id,
                        task_id=task_id,
                    )
                    return result
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    raise
            except Exception:  # noqa: BLE001
                pass
            result = SubagentResult(
                success=False,
                summary="Subagent timed out",
                session_id=session_id,
                task_id=task_id,
            )
            return result
        except asyncio.CancelledError:
            # 单 worker 取消不炸父流程；父中断（标记未置位）仍向上传播
            if session.cancelled:
                result = SubagentResult(
                    success=False,
                    summary="Subagent cancelled",
                    session_id=session_id,
                    task_id=task_id,
                )
                return result
            raise
        finally:
            # 外部取消（CancelledError）也必须清理会话 + 取消子任务
            # 不只在超时分支清理
            self._active.pop(session_id, None)
            self._task_index.pop(task_id, None)
            with _ACTIVE_TASKS_LOCK:
                _ACTIVE_TASKS.pop(task_id, None)
            if not task_obj.done():
                task_obj.cancel()
            if result is None:
                import sys as _sys

                exc = _sys.exc_info()[1]
                crashed = exc is not None and not isinstance(exc, asyncio.CancelledError)
                result = SubagentResult(
                    success=False,
                    summary="Subagent crashed" if crashed else "Subagent cancelled",
                    session_id=session_id,
                    task_id=task_id,
                )
            result.completed_at = _now_iso()
            record_completed_task(result)

    async def spawn_many(
        self,
        tasks: list[str],
        config: SubagentConfig | None = None,
        tool_registry: Any | None = None,
        parent_trace_id: str | None = None,
        depth: int = 0,
        max_concurrent: int = 0,
        parent_tool_call_id: str = "",
    ) -> list[SubagentResult]:
        """功能：多 worker 并行派生——按 max_concurrent 限流排队
        逐 worker 独立会话（spawn），结果聚合回传（顺序与 tasks 一致）。
        单任务时直通 spawn（行为与既有调用完全一致，向后兼容）。"""
        if not tasks:
            return []
        cfg = config or SubagentConfig()
        spawn_extra = {"parent_tool_call_id": parent_tool_call_id} if parent_tool_call_id else {}
        if len(tasks) == 1:
            return [
                await self.spawn(
                    tasks[0],
                    cfg,
                    tool_registry=tool_registry,
                    parent_trace_id=parent_trace_id,
                    depth=depth,
                    **spawn_extra,
                )
            ]
        semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent > 0 else None

        async def run_one(task: str) -> SubagentResult:
            # （传播会取消兄弟 worker 且 SubAgentTool 整体抛错），CancelledError 除外
            try:
                if semaphore is not None:
                    async with semaphore:
                        return await self.spawn(
                            task,
                            cfg,
                            tool_registry=tool_registry,
                            parent_trace_id=parent_trace_id,
                            depth=depth,
                            **spawn_extra,
                        )
                return await self.spawn(
                    task,
                    cfg,
                    tool_registry=tool_registry,
                    parent_trace_id=parent_trace_id,
                    depth=depth,
                    **spawn_extra,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 单 worker 崩溃不阻断兄弟
                return SubagentResult(
                    success=False,
                    summary=f"Subagent worker crashed: {task[:80]}",
                    message=str(exc),
                )

        return await asyncio.gather(*(run_one(t) for t in tasks))

    async def _run_subagent(
        self,
        task: str,
        config: SubagentConfig,
        session: SubagentSession,
        tool_registry: Any | None = None,
        parent_trace_id: str = "",
        depth: int = 0,
        parent_tool_call_id: str = "",
    ) -> SubagentResult:
        from bandi.agent.base import ExecutionContext, TerminalReason
        from bandi.agent.loop import AgentLoop
        from bandi.agent.prompt_harness.builder import prompt_harness_from_config
        from bandi.core.config import AgentConfig
        from bandi.core.event_bus import EventBus
        from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage
        from bandi.llm.mock import MockProvider
        from bandi.perception.context_loader import ContextBundle

        # 也无人订阅（成本台账缺失、trace 断裂）。父总线已带 persist 与订阅者
        # 事件经 session_id/trace_id 区分归属。显式 event_bus 优先，其次旧形态
        # parent_context.bus，两者皆无才自建。
        parent_bus = (
            self._event_bus if self._event_bus is not None else getattr(self._parent, "bus", None)
        )
        bus = parent_bus if parent_bus is not None else EventBus()
        # 子代理过程带归属标签（父链 _HandlerBindings 据此转发为嵌套过程帧）
        subagent_label = {
            "id": session.task_id,
            "task": task[:120],
            "parent_tool_call_id": parent_tool_call_id,
            "parent_session_id": str(getattr(self._parent, "session_id", "") or ""),
        }
        bus = _SubagentEventBus(bus, subagent_label)
        provider = self._provider
        if provider is None:
            provider = MockProvider()
            provider.add_response(
                LLMResponse(
                    content=f"Subagent response for: {task[:100]}",
                    tool_calls=[],
                    usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
                    finish_reason="stop",
                )
            )

        # reserved=0、静态 128K，与主链口径不一
        from bandi.agent.factory import resolve_context_window
        from bandi.core.config import DEFAULT_CONTEXT_WINDOW, BandiConfig

        _cfg = None
        _agent_base: dict[str, Any] = {}
        try:
            _cfg = BandiConfig.load()
            _cfg = subagent_overridden_config(_cfg, config)
            _window = resolve_context_window(_cfg)
            _reserved = min(max(int(getattr(_cfg.llm, "max_output_tokens", 0) or 0), 0), 20_000)
            # token_budget_limit/stream_to_ui 等），仅覆盖子代理特有字段
            _agent_base = _cfg.agent.model_dump()
        except Exception:  # noqa: BLE001 — 配置不可用回落模型默认（不阻断子代理）
            # 回落安全默认窗口而非 AgentConfig().context_window——后者现为
            # 「未显式配置」哨兵 0，直接用作窗口会退化成 1 token 致子代理全链超窗。
            _window = DEFAULT_CONTEXT_WINDOW
            _reserved = 0
        if _cfg is not None and (config.model or config.effort):
            provider = subagent_provider_for_overrides(provider, config, _cfg)
        _agent_base.update(
            {
                "max_iterations": config.max_turns,
                "mode": "execute",
                "context_window": _window,
                "reserved_output_tokens": _reserved,
            }
        )
        agent_cfg = AgentConfig(**_agent_base)
        ctx = ExecutionContext(messages=[], provider=provider, config=agent_cfg, bus=bus)
        from bandi.utils.token_counter import set_active_model_for_provider

        set_active_model_for_provider(provider)
        ctx.trace_id = parent_trace_id or generate_trace_id()
        ctx.session_id = session.session_id
        ctx.meta["session_id"] = session.session_id
        # 生产者，子代理链路日志 agent 维度恒空（父链缺省只写子身份，降级不空）
        _parent_meta = getattr(self._parent, "meta", None) or {}
        _child_agent_id, _parent_agent_id = build_child_agent_identity(
            str(self._parent_agent_id or _parent_meta.get("agent_id") or ""),
            str(session.session_id or ""),
        )
        ctx.meta["agent_id"] = _child_agent_id
        if _parent_agent_id:
            ctx.meta["parent_agent_id"] = _parent_agent_id
        # -子代理独立台账（不发事件、不 surface 到父任务条）+ 注册表换绑视图
        child_ledger, child_tool_registry = build_child_ledger_bundle(tool_registry)
        ctx.meta["task_ledger"] = child_ledger
        # （孙代理继承链不断）
        child_hook_runner = (
            _clone_hook_runner(self._hook_runner, _child_agent_id)
            if self._hook_runner is not None
            else None
        )
        from bandi.core.event_bus import reset_event_context, set_event_context

        _ev_ctx_token = set_event_context(
            session_id=ctx.session_id or "", trace_id=ctx.trace_id, source="subagent"
        )
        ctx.raw_input = task
        # 原子化：只挂 schema 不挂 executor 会让真实 LLM 的工具轮必然 TOOL_ERROR）
        if tool_registry is not None:
            from bandi.agent._assembly import ensure_subagent_idempotency_store
            from bandi.tools.cache import ToolResultCache
            from bandi.tools.executor import ToolExecutor

            # .1：角色门控继承——schema 与执行面双重收敛（与主链 factory 同源）
            schema_allowlist = _subagent_schema_allowlist(
                config.tools_allowlist, tool_registry, self._role_tool_tags
            )
            ctx.active_tools = tool_registry.list_schemas(allowed_tools=schema_allowlist)
            _tools_cfg = getattr(_cfg, "tools", None) if _cfg is not None else None
            # 否则用户配置的 PreToolUse deny/updatedInput 被子代理调用绕过
            sub_pipeline = None
            if child_hook_runner is not None and getattr(child_hook_runner, "has_rules", False):
                from bandi.tools.middleware import HookAfterMiddleware, HookBeforeMiddleware
                from bandi.tools.pipeline import ToolPipeline

                sub_pipeline = ToolPipeline()
                sub_pipeline.add(HookAfterMiddleware(child_hook_runner))
                sub_pipeline.add(HookBeforeMiddleware(child_hook_runner), index=0)
            ctx.tool_executor = ToolExecutor(
                child_tool_registry,
                permissions_pipeline=self._permissions_pipeline,
                event_bus=bus,
                mode="execute",
                project_root=self._project_root,
                idempotency_store=ensure_subagent_idempotency_store(self._idempotency_store),
                result_cache=ToolResultCache(),
                role_tool_tags=self._role_tool_tags,
                role_fs_scopes=self._role_fs_scopes,
                tool_pipeline=sub_pipeline,
                # F3a：hooks runner 透传子执行器——否则子代理内派生的孙代理工具面
                # 拿不到 runner（PreToolUse deny 二代绕过）
                hook_runner=child_hook_runner,
                user_id=getattr(self, "_user_id", ""),
                policy_engine=getattr(self, "_policy_engine", None),
                # 不再是 "default"；缺省会让孙代理的 parent_agent_id 恒为 default
                agent_id=_child_agent_id,
                execution_timeout=getattr(_tools_cfg, "execution_timeout", 120),
                output_preview_chars=getattr(_tools_cfg, "output_preview_chars", 200),
                consecutive_failure_threshold=getattr(
                    _tools_cfg, "consecutive_failure_threshold", 3
                ),
                chars_per_token=getattr(_tools_cfg, "chars_per_token", 4),
                max_output_tokens=getattr(_tools_cfg, "max_output_tokens", 8192),
                temp_dir=getattr(_tools_cfg, "temp_dir", "~/.bandi/tmp"),
                per_tool_max_output_tokens=getattr(_tools_cfg, "per_tool_max_output_tokens", None),
                # 漏传 max_concurrency → 得 None → 子代理批内无限并发，闸门被绕过
                # （父 8 × fan-out 10 × 批内 N 的乘积峰值未收敛）
                max_concurrency=subagent_batch_concurrency(_tools_cfg),
            )
            if config.max_tool_calls > 0:
                ctx.tool_executor = _ToolCallLimiter(
                    ctx.tool_executor, config.max_tool_calls, ctx=ctx
                )

        # 组装 system prompt（与主 Agent 同源：身份/框架/模式/安全/输出/体验）
        harness = prompt_harness_from_config(_cfg if _cfg is not None else BandiConfig())
        system_prompt = harness.build(
            mode="execute",
            context=ContextBundle(),
        )
        ctx.messages.append(Message(role=MessageRole.SYSTEM, content=system_prompt))
        ctx.messages.append(Message(role=MessageRole.USER, content=task))

        loop = AgentLoop(
            max_iterations=config.max_turns,
            checkpoint_store=self._loop_checkpoint_store,
        )
        # SubAgentTool 读到当前深度 +1，嵌套深度真实递增。
        depth_token = _SUBAGENT_DEPTH.set(depth)
        try:
            terminal_reason = await loop.run(ctx, session_timeout_sec=config.timeout_sec)
        finally:
            _SUBAGENT_DEPTH.reset(depth_token)
            reset_event_context(_ev_ctx_token)

        succeeded = terminal_reason in (
            TerminalReason.COMPLETED,
            TerminalReason.VERIFIED,
            TerminalReason.MAX_TURNS,
        )
        if not succeeded and self._loop_checkpoint_store is not None:
            # 被当作有效中间态续跑
            try:
                self._loop_checkpoint_store.delete_by_session(ctx.session_id or "")
            except Exception:  # noqa: BLE001 — 清理失败不阻断结果返回
                pass

        final_text = (ctx.final_message or ctx.current_content or "").strip()
        return SubagentResult(
            success=succeeded,
            summary=f"Subagent completed: {task[:50]}",
            message=final_text,
            total_tokens=ctx.total_tokens,
            session_id=session.session_id,
            task_id=session.task_id,
            # .12：结论面交接（证据/未解决项由调用方按需补充；
            # 无产出时不伪造空 handoff）
            handoff=AgentHandoff(conclusion=final_text[:1000]) if final_text else None,
        )

    def get_active(self) -> dict[str, SubagentSession]:
        return dict(self._active)

    def cancel(self, handle: str) -> bool:
        """按 task_id 或 session_id 取消活跃子 Agent（双句柄兼容）。
        定向取消置 session.cancelled 标记——spawn 将其降级为取消结果
        父流程不中断；cancel_all（父中断清理）语义保持。
        修复：返回是否真实命中（未命中 False——API 不得虚报成功）。"""
        session_id = self._task_index.get(handle, handle)
        session = self._active.get(session_id)
        if session is None:
            return False
        session.cancelled = True
        task_obj = session.task
        if task_obj is not None:
            self._request_cancel(task_obj)
        self._active.pop(session_id, None)
        self._task_index.pop(session.task_id, None)
        return True

    @staticmethod
    def _request_cancel(task_obj: asyncio.Task[Any]) -> None:
        # 功能：跨 event loop 安全取消——同 loop 直接 cancel，异 loop 调度回目标 loop
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        loop = task_obj.get_loop()
        if running is loop:
            task_obj.cancel()
        else:
            try:
                loop.call_soon_threadsafe(task_obj.cancel)
            except RuntimeError:
                # 不得让 RuntimeError 冒到 API 端点成为 500
                pass

    def cancel_all(self) -> None:
        """orphan 清理——父会话中断/进程退出时取消全部活跃子 Agent。"""
        for session_id in list(self._active):
            self.cancel(session_id)
