# 功能：spawn_thread_task——Agent 互@ 建子任务（真实执行）+ 线程消息绑定
# fail-closed：target_role 未注册拒绝；依赖（store/thread_manager/org_engine）缺失拒绝；
# 不轮询、不阻塞（与 ask_peer_and_wait 区别）。
# 由现有 refresh_dependents 汇聚（synthesis 扇入）。
# .4：可选 handoff（结构化交接契约）随线程消息与子卡 metadata 落盘。

from __future__ import annotations

from typing import Any

from bandi.company.handoff import AgentHandoff
from bandi.company.phase import Phase
from bandi.company.prompt_contract import prompt_contract_from_delegate_item
from bandi.company.work_item import WorkItem, WorkItemTurnType, create_work_item
from bandi.company.work_item_transition import (
    DELEGATED_CHILDREN_PENDING_KEY,
    DEPENDENCY_WORK_ITEM_IDS_KEY,
    FRONTIER_KEY,
    WAITING_FOR_CHILDREN_FRONTIER,
)
from bandi.tools.base import (
    BaseTool,
    RetryPolicy,
    ToolCategory,
    ToolPermissionContext,
)

_TITLE_MAX_CHARS = 60


def _err(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


def _registered_role_ids(org_engine: Any | None) -> set[str]:
    """收集全部已注册角色 id（保留注册原大小写）；无 org_engine → 空集（fail-closed）。"""
    if org_engine is None:
        return set()
    role_ids: set[str] = set()
    for role in org_engine.list_roles():
        role_ids.add(str(role.id).lower())
    return role_ids


def _parse_handoff(raw: Any) -> AgentHandoff | None:
    if isinstance(raw, AgentHandoff):
        return raw
    return AgentHandoff.from_dict(raw)


def _text_list(value: Any) -> list[str]:
    """功能：把交付物/验收标准参数归一为字符串列表。"""
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


class SpawnThreadTaskTool(BaseTool):
    name: str = "spawn_thread_task"
    description: str = (
        "Agent-to-agent task spawning: an agent @mentions peer role(s) to execute "
        "subtask(s) (child work items) and replies into a shared discussion thread. "
        "Pass `tasks` to fan out multiple parallel cards in one call — the parent card "
        "registers a dependency barrier (or a join card is created when no parent) so "
        "results are aggregated by the existing synthesis turn. Does not block or poll."
    )
    category: ToolCategory = ToolCategory.WORKFLOW
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "target_role": {"type": "string", "description": "目标角色 id（已注册才可派发）"},
            "task_prompt": {
                "type": "string",
                "description": "子任务描述（单卡模式；与 tasks 二选一）",
            },
            "upstream_intent_summary": {
                "type": "string",
                "description": "上游目标/用户最新指令摘要；缺省继承父卡 prompt_contract",
            },
            "deliverables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "子卡交付物清单（结构化验收，不塞进 task_prompt）",
            },
            "acceptance_criteria": {
                "type": "array",
                "items": {"type": "string"},
                "description": "子卡验收标准",
            },
            "scope_key": {
                "type": "string",
                "description": "同角色多卡的稳定作用域键，用于依赖/去重",
            },
            "depends_on": {
                "type": "array",
                "items": {"type": "string"},
                "description": "依赖的兄弟工作项 id/scope_key（单卡模式可选）",
            },
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "target_role": {"type": "string", "description": "目标角色 id"},
                        "task_prompt": {"type": "string", "description": "子任务描述"},
                        "upstream_intent_summary": {"type": "string"},
                        "deliverables": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "acceptance_criteria": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "scope_key": {"type": "string"},
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["target_role", "task_prompt"],
                },
                "description": "多卡扇出模式：一次派发 N 张子卡（父卡屏障/join 卡汇聚）",
            },
            "sequence": {
                "type": "boolean",
                "description": (
                    "串行模式（默认 true）：tasks 按给定顺序依次执行——后一张卡等"
                    "前一张完成才放行（WAITING_DEPENDENCIES + chain_step）；"
                    "明确要求并行时传 false"
                ),
            },
            "thread_id": {"type": "string", "description": "可选既有线程 id；缺省自动创建新线程"},
            "parent_task_id": {
                "type": "string",
                "description": "父 work_item id（调用 Agent 的当前执行卡）",
            },
            "aggregate_role": {
                "type": "string",
                "description": "无父卡时 join 卡归属角色（默认调用者角色）",
            },
            "handoff": {
                "type": "object",
                "description": "可选结构化交接（conclusion/evidence/unresolved/artifacts/confidence）",
            },
        },
        "required": [],
    }
    # 声明非并发安全交由执行器串行化
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    def __init__(
        self,
        store: Any | None = None,
        thread_manager: Any | None = None,
        org_engine: Any | None = None,
    ) -> None:
        self.store = store
        self.thread_manager = thread_manager
        self.org_engine = org_engine
        self.current_work_item: Any | None = None

    def bind_deps(
        self,
        store: Any | None = None,
        thread_manager: Any | None = None,
        org_engine: Any | None = None,
        work_item: Any | None = None,
    ) -> None:
        """后绑定依赖——沿 subagent.bind_registry/UserInputTool.bind_ui 先例。

        build_tool_registry 以裸构造注册全部工具（无公司上下文），公司链路装配点
        （EngineSeatExecutor→run_agent→run_agent_turn）在 registry 建成后注入
        store/thread_manager/org_engine；`work_item` = 当前执行卡，缺省 parent_task_id
        时作为父卡兜底（评审桥/依赖屏障据此挂接）。None 注入保持 fail-closed。
        """
        if store is not None:
            self.store = store
        if thread_manager is not None:
            self.thread_manager = thread_manager
        if org_engine is not None:
            self.org_engine = org_engine
        if work_item is not None:
            self.current_work_item = work_item

    def _resolve_parent_task_id(self, params: dict[str, Any]) -> str | None:
        explicit = str(params.get("parent_task_id") or "").strip()
        if explicit:
            return explicit
        current_id = str(getattr(self.current_work_item, "id", "") or "").strip()
        return current_id or None

    def _parent_upstream_intent(self) -> str:
        """功能：从父卡 prompt_contract 继承上游目标，避免子卡从零解释任务。"""
        metadata = dict(getattr(self.current_work_item, "metadata", None) or {})
        contract = dict(metadata.get("prompt_contract") or {})
        assignment = dict(contract.get("assignment_context") or {})
        return str(
            assignment.get("upstream_intent_summary")
            or metadata.get("upstream_intent_summary")
            or ""
        ).strip()

    def _upstream_roles(self, parent_task_id: str | None) -> set[str]:
        """功能：当前卡的上游角色集合（等待本活结果的派活者链）。"""
        roles: set[str] = set()
        if not parent_task_id:
            return roles
        seen: set[str] = set()
        item = self.store.get(parent_task_id) if self.store is not None else None
        parent_id = str(getattr(item, "parent_id", "") or "") if item is not None else ""
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            parent = self.store.get(parent_id) if self.store is not None else None
            if parent is None:
                break
            role_id = str(getattr(parent, "assignee_role", "") or "").strip()
            if role_id:
                roles.add(role_id)
            parent_id = str(getattr(parent, "parent_id", "") or "")
        return roles

    def _register_single_barrier(self, parent_task_id: str | None, child_id: str) -> None:
        """功能：单张子卡也登记父卡等待门（子结果未回传前父卡不交付）。"""
        if not parent_task_id or self.store is None:
            return
        parent = self.store.get(parent_task_id)
        if parent is None:
            return
        metadata = dict(parent.metadata or {})
        existing = [str(item) for item in (metadata.get(DEPENDENCY_WORK_ITEM_IDS_KEY) or [])]
        if child_id not in existing:
            existing.append(child_id)
        metadata[DEPENDENCY_WORK_ITEM_IDS_KEY] = existing
        metadata[DELEGATED_CHILDREN_PENDING_KEY] = True
        metadata[FRONTIER_KEY] = WAITING_FOR_CHILDREN_FRONTIER
        parent.metadata = metadata
        self.store.save(parent)

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> dict[str, Any]:
        if self.store is None or self.thread_manager is None or self.org_engine is None:
            return _err("spawn_thread_task requires store/thread_manager/org_engine deps")
        raw_tasks = params.get("tasks")
        if raw_tasks is not None:
            if str(params.get("task_prompt") or "").strip():
                return _err("provide either task_prompt (single) or tasks (fan-out), not both")
            return await self._execute_fan_out(params, context)
        return await self._execute_single(params, context)

    # -- 单卡模式（原语义不变 + handoff 可选）-------------------------------

    async def _execute_single(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None,
    ) -> dict[str, Any]:
        target_role = str(params.get("target_role") or "").strip()
        task_prompt = str(params.get("task_prompt") or "").strip()
        parent_task_id = self._resolve_parent_task_id(params)
        if not target_role or not task_prompt:
            return _err("spawn_thread_task requires target_role and task_prompt")
        if target_role.lower() not in _registered_role_ids(self.org_engine):
            return _err(f"target_role '{target_role}' is not a registered role")
        if target_role in self._upstream_roles(parent_task_id):
            return _err(
                f"target_role '{target_role}' is waiting for this work; "
                "finish this turn and the result will return automatically"
            )
        sender_role = (context.role if context is not None else "") or "unknown"
        handoff = _parse_handoff(params.get("handoff"))
        thread_id, err = self._resolve_thread(
            str(params.get("thread_id") or "").strip() or None,
            sender_role,
            [target_role],
            handoff=handoff,
            content=task_prompt,
        )
        if err:
            return _err(err)
        item: WorkItem = self._create_child(
            target_role=target_role,
            task_prompt=task_prompt,
            thread_id=thread_id,
            parent_task_id=parent_task_id,
            handoff=handoff,
            upstream_intent_summary=str(params.get("upstream_intent_summary") or "").strip()
            or self._parent_upstream_intent(),
            deliverables=_text_list(params.get("deliverables")),
            acceptance_criteria=_text_list(params.get("acceptance_criteria")),
            scope_key=str(params.get("scope_key") or "").strip(),
            depends_on=_text_list(params.get("depends_on")),
        )
        self._register_single_barrier(parent_task_id, item.id)
        return {
            "success": True,
            "thread_id": thread_id,
            "work_item_id": item.id,
            "target_role": target_role,
        }

    # -- 多卡扇出模式（9.5）--------------------------------------------------

    async def _execute_fan_out(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None,
    ) -> dict[str, Any]:
        raw_tasks = params.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return _err("tasks must be a non-empty list")
        registered = _registered_role_ids(self.org_engine)
        specs: list[dict[str, Any]] = []
        for index, entry in enumerate(raw_tasks):
            if not isinstance(entry, dict):
                return _err(f"tasks[{index}] must be an object")
            target_role = str(entry.get("target_role") or "").strip()
            task_prompt = str(entry.get("task_prompt") or "").strip()
            if not target_role or not task_prompt:
                return _err(f"tasks[{index}] requires target_role and task_prompt")
            if target_role.lower() not in registered:
                return _err(f"target_role '{target_role}' is not a registered role")
            specs.append(
                {
                    "target_role": target_role,
                    "task_prompt": task_prompt,
                    "upstream_intent_summary": str(
                        entry.get("upstream_intent_summary") or ""
                    ).strip(),
                    "deliverables": _text_list(entry.get("deliverables")),
                    "acceptance_criteria": _text_list(entry.get("acceptance_criteria")),
                    "scope_key": str(entry.get("scope_key") or "").strip(),
                    "depends_on": _text_list(entry.get("depends_on")),
                }
            )
        parent_task_id = self._resolve_parent_task_id(params)
        if parent_task_id and self.store.get(parent_task_id) is None:
            return _err(f"parent work item '{parent_task_id}' not found")
        upstream = self._upstream_roles(parent_task_id)
        for spec in specs:
            if spec["target_role"] in upstream:
                return _err(
                    f"target_role '{spec['target_role']}' is waiting for this work; "
                    "finish this turn and the result will return automatically"
                )
        sender_role = (context.role if context is not None else "") or "unknown"
        aggregate_role = str(params.get("aggregate_role") or "").strip()
        if aggregate_role and aggregate_role.lower() not in registered:
            return _err(f"aggregate_role '{aggregate_role}' is not a registered role")
        handoff = _parse_handoff(params.get("handoff"))
        parent_upstream = str(params.get("upstream_intent_summary") or "").strip()
        if not parent_upstream:
            parent_upstream = self._parent_upstream_intent()

        # 线程：一次扇出只开一个对话链（多卡共享），逐卡追帖
        thread_id, err = self._resolve_thread(
            str(params.get("thread_id") or "").strip() or None,
            sender_role,
            [spec["target_role"] for spec in specs],
            handoff=handoff,
            content=specs[0]["task_prompt"],
            extra_posts=[(spec["target_role"], spec["task_prompt"]) for spec in specs[1:]],
        )
        if err:
            return _err(err)

        child_ids: list[str] = []
        # 默认串行（公司链按执行顺序逐环放行）；明确要求并行时传 sequence=false
        sequence = bool(params.get("sequence", True))
        for spec in specs:
            child = self._create_child(
                target_role=spec["target_role"],
                task_prompt=spec["task_prompt"],
                thread_id=thread_id,
                parent_task_id=parent_task_id,
                handoff=handoff,
                upstream_intent_summary=spec["upstream_intent_summary"] or parent_upstream,
                deliverables=spec["deliverables"],
                acceptance_criteria=spec["acceptance_criteria"],
                scope_key=spec["scope_key"],
                depends_on=(
                    spec["depends_on"] or ([child_ids[-1]] if sequence and child_ids else None)
                ),
            )
            child_ids.append(child.id)

        barrier_id, err = self._register_fan_in_barrier(
            child_ids, parent_task_id, sender_role, aggregate_role
        )
        if err:
            return _err(err)
        return {
            "success": True,
            "thread_id": thread_id,
            "work_item_ids": child_ids,
            "aggregate_work_item_id": barrier_id,
            "target_roles": [spec["target_role"] for spec in specs],
        }

    # -- internals -----------------------------------------------------------

    def _resolve_thread(
        self,
        thread_id: str | None,
        sender_role: str,
        targets: list[str],
        *,
        handoff: AgentHandoff | None,
        content: str,
        extra_posts: list[tuple[str, str]] | None = None,
    ) -> tuple[str, str]:
        if thread_id:
            if self.thread_manager.get_thread(thread_id) is None:
                return "", f"thread '{thread_id}' not found"
            self.thread_manager.add_message(
                thread_id, sender=sender_role, content=content, mentions=targets, handoff=handoff
            )
            self._post_extra_posts(thread_id, sender_role, extra_posts, handoff)
            return thread_id, ""
        thread = self.thread_manager.create_thread(
            participants=list(dict.fromkeys([sender_role, *targets]))
        )
        self.thread_manager.add_message(
            thread.thread_id,
            sender=sender_role,
            content=content,
            mentions=targets,
            handoff=handoff,
        )
        self._post_extra_posts(thread.thread_id, sender_role, extra_posts, handoff)
        return thread.thread_id, ""

    def _post_extra_posts(
        self,
        thread_id: str,
        sender_role: str,
        extra_posts: list[tuple[str, str]] | None,
        handoff: AgentHandoff | None,
    ) -> None:
        # 多卡：其余子任务逐条追帖（同线程对话链完整可见，新建/复用线程一致）
        for target_role, task_prompt in extra_posts or []:
            self.thread_manager.add_message(
                thread_id,
                sender=sender_role,
                content=task_prompt,
                mentions=[target_role],
                handoff=handoff,
            )

    def _create_child(
        self,
        *,
        target_role: str,
        task_prompt: str,
        thread_id: str,
        parent_task_id: str | None,
        handoff: AgentHandoff | None,
        upstream_intent_summary: str = "",
        deliverables: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        scope_key: str = "",
        depends_on: list[str] | None = None,
    ) -> WorkItem:
        metadata: dict[str, Any] = {"thread_id": thread_id, "source": "agent_to_agent"}
        if handoff is not None:
            metadata["handoff"] = handoff.to_dict()
        # 会话模式继承：plan 模式下派发的执行卡挂起，等用户切执行模式再放行
        if parent_task_id:
            parent = self.store.get(parent_task_id)
            parent_metadata = dict(parent.metadata or {}) if parent is not None else {}
            parent_created_mode = str(parent_metadata.get("created_mode", "") or "").strip().lower()
            if parent_created_mode:
                metadata["created_mode"] = parent_created_mode
                if parent_created_mode == "plan":
                    metadata["mode_gate"] = "plan"
            # 会话/项目归属继承：下游卡必须能被原会话与右栏计划识别
            parent_origin = str(parent_metadata.get("origin_session_id", "") or "").strip()
            if parent_origin:
                metadata["origin_session_id"] = parent_origin
            parent_window = str(parent_metadata.get("window_id", "") or "").strip()
            if parent_window:
                metadata["window_id"] = parent_window
            parent_project = str(parent_metadata.get("project_id", "") or "").strip()
            if parent_project:
                metadata["project_id"] = parent_project
            parent_owner = str(parent_metadata.get("runtime_owner", "") or "").strip()
            if parent_owner:
                metadata["runtime_owner"] = parent_owner
        phase = Phase.QUEUED
        if depends_on:
            metadata[DEPENDENCY_WORK_ITEM_IDS_KEY] = list(depends_on)
            # 顺序链标记：依赖满足后以「执行」轮放行，而非父卡聚合轮
            metadata["chain_step"] = True
            phase = Phase.WAITING_DEPENDENCIES
        if scope_key:
            metadata["scope_key"] = scope_key
        if upstream_intent_summary:
            metadata["upstream_intent_summary"] = upstream_intent_summary
        contract = prompt_contract_from_delegate_item(
            {
                "deliverables": list(deliverables or []),
                "acceptance_criteria": list(acceptance_criteria or []),
            },
            task_brief=task_prompt,
            upstream_intent_summary=upstream_intent_summary,
            scope_key=scope_key,
            dependency_specs=list(depends_on or []),
        )
        metadata["prompt_contract"] = contract
        if str((contract.get("source") or {}).get("kind") or "") == "prompt_contract_blocker":
            metadata["prompt_contract_blocker"] = True
        item: WorkItem = create_work_item(
            title=task_prompt[:_TITLE_MAX_CHARS],
            turn_type=WorkItemTurnType.FOLLOWUP,
            description=task_prompt,
            assignee_role=target_role,
            parent_id=parent_task_id,
            phase=phase,
            metadata=metadata,
        )
        self.store.save(item)
        return item

    def _register_fan_in_barrier(
        self,
        child_ids: list[str],
        parent_task_id: str | None,
        sender_role: str,
        aggregate_role: str,
    ) -> tuple[str, str]:
        """功能：扇出屏障登记——有父卡则写父卡依赖（刷新后由 synthesis 汇聚）；
        无父卡则建 aggregate join 卡（WAITING_FOR_CHILDREN 起步，refresh 直接释放）。"""
        if parent_task_id:
            parent = self.store.get(parent_task_id)
            if parent is None:
                return "", f"parent work item '{parent_task_id}' not found"
            metadata = dict(parent.metadata or {})
            existing = [str(v) for v in (metadata.get(DEPENDENCY_WORK_ITEM_IDS_KEY) or [])]
            metadata[DEPENDENCY_WORK_ITEM_IDS_KEY] = list(dict.fromkeys([*existing, *child_ids]))
            metadata[DELEGATED_CHILDREN_PENDING_KEY] = True
            metadata[FRONTIER_KEY] = WAITING_FOR_CHILDREN_FRONTIER
            parent.metadata = metadata
            self.store.save(parent)
            return parent.id, ""
        join = create_work_item(
            title=f"聚合 {len(child_ids)} 个子任务",
            turn_type=WorkItemTurnType.EXECUTE,
            description="等待全部子任务完成后汇总结果并交付（join 卡由依赖刷新释放）。",
            assignee_role=aggregate_role or sender_role,
            phase=Phase.WAITING_FOR_CHILDREN,
            metadata={
                DEPENDENCY_WORK_ITEM_IDS_KEY: list(child_ids),
                DELEGATED_CHILDREN_PENDING_KEY: True,
                FRONTIER_KEY: WAITING_FOR_CHILDREN_FRONTIER,
                "source": "agent_to_agent_join",
            },
        )
        self.store.save(join)
        return join.id, ""
