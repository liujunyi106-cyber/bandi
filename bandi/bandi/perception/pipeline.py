# 功能：PerceptionPipeline — 感知管线编排（解析→领域路由→意图路由→加载→组装）

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bandi.context.planner import ContextPlanner
from bandi.context.source import ContextSourceRegistry
from bandi.core.message import Message
from bandi.eval.fairness_evaluator import FairnessEvaluator, FairnessReport
from bandi.memory.session.store import SessionStore
from bandi.perception.context_assembler import ContextAssembler
from bandi.perception.context_loader import ContextBundle, ContextLoader
from bandi.perception.domain_router import DomainRoute, DomainRouter
from bandi.perception.input_parser import InputParser
from bandi.perception.intent_router import IntentRouter
from bandi.tools.registry import ToolRegistry, with_always_available
from bandi.utils.token_counter import TokenCounter


def _detect_project_root() -> Path:
    cwd = Path.cwd()
    for marker in ("AGENTS.md", "CLAUDE.md", ".git"):
        if (cwd / marker).exists():
            return cwd
    return cwd


# 4.6：动态 system 区预算单源——规则+层级记忆（A 通道）与注册表上下文（B 通道）
# 合并受总上限约束，替代三处各 4096 互不知情
_DYNAMIC_SECTION_BUDGET_TOKENS = 8192
_DYNAMIC_TOTAL_BUDGET_TOKENS = 12288
_DYNAMIC_REGISTRY_MIN_TOKENS = 1024


def _registry_context_budget(project_instructions: str, memory_context: str) -> int:
    """功能：注册表上下文预算——按「规则+层级记忆」已占用量压缩，合并 ≤ 总上限。"""
    from bandi.utils.token_counter import TokenCounter

    used = min(
        _DYNAMIC_SECTION_BUDGET_TOKENS,
        TokenCounter.estimate(project_instructions or "")
        + TokenCounter.estimate(memory_context or ""),
    )
    return max(
        _DYNAMIC_REGISTRY_MIN_TOKENS,
        min(_DYNAMIC_SECTION_BUDGET_TOKENS, _DYNAMIC_TOTAL_BUDGET_TOKENS - used),
    )


if TYPE_CHECKING:
    from bandi.agent.prompt_harness.builder import PromptHarness


@dataclass
class Dependencies:
    session_store: SessionStore | None = None
    entity_lookup: Any | None = None
    tool_registry: ToolRegistry | None = None
    prompt_harness: PromptHarness | None = None
    skill_injector: Any | None = None
    fairness_evaluator: FairnessEvaluator | None = None
    hierarchy: Any | None = None
    project_id: str | None = None
    employee_id: str | None = None
    team_id: str | None = None
    group_conversation_id: str = ""
    dm_conversation_id: str = ""
    # 功能：三元组本体存储——有则词表段由它渲染（≤60 词、含推理语义），
    # 缺省时回退旧序列化器（本体是增强，不是主链依赖）
    triple_ontology: Any | None = None


@dataclass
class PerceptionResult:
    messages: list[Message]
    mode: str
    intent_hint: str = ""
    domain_route: DomainRoute | None = None
    entities_referenced: list[str] = field(default_factory=list)
    estimated_input_tokens: int = 0
    planned_context_items: list[Any] = field(default_factory=list)
    fairness_report: FairnessReport | None = None


class PerceptionPipeline:
    def __init__(
        self,
        deps: Dependencies | None = None,
        context_registry: ContextSourceRegistry | None = None,
        context_planner: ContextPlanner | None = None,
    ) -> None:
        if deps is None:
            # 唯一为 agent_runner.py 的 PerceptionDeps；此分支仅测试/缺省场景）
            logging.getLogger(__name__).debug("PerceptionPipeline: empty Dependencies (no deps)")
            deps = Dependencies()
        self.parser = InputParser()
        self.domain_router = DomainRouter()
        self.router = IntentRouter()
        self.loader = (
            ContextLoader(
                deps.session_store,
                deps.entity_lookup,
                project_root=str(_detect_project_root()),
                hierarchy=deps.hierarchy,
                project_id=deps.project_id,
                employee_id=deps.employee_id,
                team_id=deps.team_id,
                group_conversation_id=deps.group_conversation_id,
                dm_conversation_id=deps.dm_conversation_id,
                ontology_store=deps.triple_ontology,
            )
            if deps.session_store
            else None
        )
        self._session_store = deps.session_store
        self.assembler = ContextAssembler(
            tool_registry=deps.tool_registry, prompt_harness=deps.prompt_harness
        )
        self._artifact_hashes: dict[str, dict[str, str]] = {}
        self._tool_surface_tools: dict[str, list[str]] = {}
        self._context_registry = context_registry
        self._context_planner = context_planner or ContextPlanner()
        self._skill_injector = deps.skill_injector
        self._fairness_evaluator = deps.fairness_evaluator

    def _load_previous_hashes(self, session_id: str | None) -> dict[str, str] | None:
        if not session_id:
            return None
        if session_id in self._artifact_hashes:
            return self._artifact_hashes[session_id]
        if self._session_store is None or not hasattr(self._session_store, "get_session"):
            return None
        session = self._session_store.get_session(session_id)
        if session is None:
            return None
        metadata = json.loads(session.get("metadata") or "{}")
        return metadata.get("artifact_hashes")

    def _persist_runtime_state(
        self,
        session_id: str,
        hashes: dict[str, str] | None = None,
        tools: list[str] | None = None,
    ) -> None:
        """加固：artifact_hashes 与 tool_surface_tools 合并单次写入——
        消除两次 update_session 的非原子窗口。"""
        if self._session_store is None or not hasattr(self._session_store, "update_session"):
            return
        session = self._session_store.get_session(session_id)
        if session is None:
            return
        metadata = json.loads(session.get("metadata") or "{}")
        if hashes is not None:
            metadata["artifact_hashes"] = hashes
            self._artifact_hashes[session_id] = hashes
        if tools is not None:
            metadata["tool_surface_tools"] = tools
            self._tool_surface_tools[session_id] = tools
        self._session_store.update_session(
            session_id, metadata=json.dumps(metadata, ensure_ascii=False)
        )

    def _load_previous_tools(self, session_id: str | None) -> list[str] | None:
        """
        session metadata 恢复——与 artifact_hashes 同路。"""
        if not session_id:
            return None
        cached = self._tool_surface_tools.get(session_id)
        if cached is not None:
            return cached
        if self._session_store is None or not hasattr(self._session_store, "get_session"):
            return None
        session = self._session_store.get_session(session_id)
        if session is None:
            return None
        metadata = json.loads(session.get("metadata") or "{}")
        names = metadata.get("tool_surface_tools")
        return list(names) if isinstance(names, list) else None

    async def process(
        self,
        raw_input: str,
        session_id: str | None = None,
        session_mode: str = "execute",
        surface_tools: list[str] | None = None,
    ) -> PerceptionResult:
        try:
            message = self.parser.parse(raw_input)
        except ValueError:
            return PerceptionResult(messages=[], mode="execute")

        domain_route = self.domain_router.route(raw_input)

        # 唯一权威，system prompt 模式段按 session_mode 渲染，工具面不受
        # 关键词推断影响
        intent_hint = self.router.route(message)
        mode = session_mode if session_mode in ("execute", "plan") else "execute"

        fairness_report: FairnessReport | None = None
        if self._fairness_evaluator is not None:
            fairness_report = self._fairness_evaluator.evaluate(raw_input)

        skill_catalog = (
            self._skill_injector.build_catalog() if self._skill_injector is not None else ""
        )

        # 领域硬边界仅高置信（action=auto ≥0.85）生效；confirm/clarify 档不裁剪
        # 防单关键词误命中（如「写」落内容领域）裁掉 bash 等真实所需工具
        domain_allowed = (
            domain_route.domain.allowed_tools
            if domain_route.domain is not None and domain_route.action == "auto"
            else None
        )
        effective_allowed = domain_allowed

        # 「业务能力」，若不放行 request_user_input，Agent 无法向用户提问
        #
        # `_build_tool_surface_artifact` / `current_tool_names` 均按「非空才筛」判定。
        # 把 `[]` 并入成 `["request_user_input"]` 会让「不限制」退化为「只允许 1 个工具」：
        # 首轮工具面工件恒空 → artifact_hashes 恒 {} → 第二轮永远拿不到 previous_hashes
        # → 「Runtime Artifact Delta」永不出现（test_session_restore_complete 两用例实锤）。
        # 注意与 factory.filter_tools_by_role_tags 路径的空名单语义相反（那里 [] = 显式全禁
        # 并交互通道是正确行为），故修正落在本调用点而非 with_always_available 内部。
        if effective_allowed:
            effective_allowed = with_always_available(effective_allowed)

        if self.loader and session_id:
            context = await self.loader.load(
                message, session_id, max_tokens=_DYNAMIC_SECTION_BUDGET_TOKENS
            )
        else:
            context = ContextBundle()

        planned_items: list[Any] = []
        extra_context = ""
        if self._context_registry and session_id:
            all_items = await self._context_registry.collect_all(
                message, session_id, intent_hint=intent_hint
            )
            # collect_all 的 entity 项过滤——实体走 loader 权威路径
            # （referenced_entities），双注入重复进 system prompt
            all_items = [it for it in all_items if it.source_type != "entity"]
            planned_items = (
                self._context_planner.plan(
                    all_items,
                    max_tokens=_registry_context_budget(
                        context.project_instructions, context.memory_context
                    ),
                )
                if all_items
                else []
            )
            if planned_items:
                memory_parts = [
                    item.content for item in planned_items if item.source_type == "memory"
                ]
                company_parts = [
                    item.content for item in planned_items if item.source_type == "company"
                ]
                # 闲聊/能力询问轮不注入召回记忆——这类输入的召回结果只有自我介绍类
                # 噪音，占 memory_tokens 的绝大部分却对作答无贡献。
                from bandi.tools.selection import is_chitchat_query

                if is_chitchat_query(message.content):
                    memory_parts = []
                sections: list[str] = []
                if memory_parts:
                    sections.append("## 上下文参考\n\n" + "\n".join(memory_parts))
                if company_parts:
                    sections.append("## 公司上下文\n\n" + "\n".join(company_parts))
                extra_context = "\n\n".join(sections)

        previous_tools = self._load_previous_tools(session_id)
        artifacts = self.assembler.collect_runtime_artifacts(
            allowed_tools=effective_allowed,
            skill_catalog=skill_catalog,
            previous_tools=previous_tools,
            query=message.content,
            surface_tools=surface_tools,
        )
        current_tools = (
            self.assembler.current_tool_names(effective_allowed, surface_tools)
            if session_id
            else None
        )
        if not current_tools:
            current_tools = None
        previous_hashes = self._load_previous_hashes(session_id)
        dict_messages = self.assembler.assemble(
            message,
            context,
            mode,
            allowed_tools=effective_allowed,
            domain_label=domain_route.domain.label if domain_route.domain else "",
            extra_context=extra_context,
            artifacts=artifacts or None,
            previous_artifact_hashes=previous_hashes,
            project_instructions_max_tokens=_DYNAMIC_SECTION_BUDGET_TOKENS,
        )
        if session_id:
            from bandi.company.output_contract import build_runtime_artifact_manifest

            _, new_hashes = build_runtime_artifact_manifest(artifacts)
            self._persist_runtime_state(session_id, hashes=new_hashes, tools=current_tools)
        from bandi.core.message import message_from_dict

        msg_objects = [message_from_dict(m) for m in dict_messages]

        entity_ids = [e.id for e in context.referenced_entities]
        estimated = TokenCounter.estimate(" ".join(m.get("content", "") for m in dict_messages))
        return PerceptionResult(
            messages=msg_objects,
            mode=mode,
            intent_hint=intent_hint,
            domain_route=domain_route,
            entities_referenced=entity_ids,
            estimated_input_tokens=estimated,
            planned_context_items=planned_items,
            fairness_report=fairness_report,
        )
