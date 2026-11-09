# 功能：ContextAssembler — 组装 system_prompt + 工件消息 + history + user_message

from __future__ import annotations

from typing import TYPE_CHECKING

from bandi.company.output_contract import (
    ARTIFACT_TITLE_SKILLS,
    ARTIFACT_TITLE_TOOL_SURFACE,
    ARTIFACT_TYPE_SKILLS,
    ARTIFACT_TYPE_TOOL_SURFACE,
    RuntimeArtifact,
)
from bandi.core.message import Message, message_to_dict
from bandi.perception.context_loader import ContextBundle

if TYPE_CHECKING:
    from bandi.agent.prompt_harness.builder import PromptHarness
    from bandi.tools.registry import ToolRegistry

_DEFAULT_ENTITY_DISPLAY_LIMIT = 5


class ContextAssembler:
    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        prompt_harness: PromptHarness | None = None,
    ) -> None:
        self._harness = prompt_harness
        self._tool_registry = tool_registry

    def assemble(
        self,
        user_message: Message,
        context: ContextBundle,
        mode: str = "execute",
        allowed_tools: list[str] | None = None,
        domain_label: str = "",
        extra_context: str = "",
        artifacts: list[RuntimeArtifact] | None = None,
        previous_artifact_hashes: dict[str, str] | None = None,
        project_instructions_max_tokens: int | None = None,
    ) -> list[dict]:
        # 静态 system 消息（跨轮字节稳定 → 服务端前缀缓存可命中）与动态 system
        # 消息（项目惯例/实体/安全收尾）分离成两条消息
        system_messages = self._build_system_messages(
            mode, context, domain_label, project_instructions_max_tokens
        )
        messages: list[dict] = system_messages + [message_to_dict(m) for m in context.history]
        if artifacts:
            from bandi.company.output_contract import render_runtime_artifact_messages

            delta_msgs = render_runtime_artifact_messages(
                artifacts, previous_hashes=previous_artifact_hashes
            )
            # 工件（工具面/Skill delta）同为动态 system 通道，置于两条组装 system 之后
            messages = (
                messages[: len(system_messages)] + delta_msgs + messages[len(system_messages) :]
            )
        # 功能：易变内容（记忆召回/公司上下文）置于末尾——历史之后、当前 user
        # 之前；meta 标记供 agent 侧合并时识别，避免被「只保留 system」丢弃
        if extra_context:
            messages.append(
                {
                    "role": "user",
                    "content": extra_context,
                    "meta": {"injected_context": True},
                }
            )
        messages.append(message_to_dict(user_message))
        return messages

    def collect_runtime_artifacts(
        self,
        *,
        allowed_tools: list[str] | None = None,
        skill_catalog: str = "",
        previous_tools: list[str] | None = None,
        query: str = "",
        surface_tools: list[str] | None = None,
    ) -> list[RuntimeArtifact]:
        artifacts: list[RuntimeArtifact] = []
        tool = self._build_tool_surface_artifact(
            allowed_tools, previous_tools, query, surface_tools=surface_tools
        )
        if tool is not None:
            artifacts.append(tool)
        skill = self._build_skills_artifact(skill_catalog)
        if skill is not None:
            artifacts.append(skill)
        return artifacts

    def current_tool_names(
        self, allowed_tools: list[str] | None, surface_tools: list[str] | None = None
    ) -> list[str]:
        """本轮生效的完整工具名单（供 pipeline 记录为下轮增量基准）。"""
        if self._tool_registry is None:
            return []
        schemas = self._tool_registry.list_flat_schemas()
        if allowed_tools is not None and allowed_tools:
            names = set(allowed_tools)
            schemas = [s for s in schemas if s["name"] in names]
        # 功能：与 tools 参数同口径——相关性裁剪结果（active tools）才是模型可见面
        if surface_tools is not None:
            wanted = set(surface_tools)
            schemas = [s for s in schemas if s["name"] in wanted]
        return sorted(s["name"] for s in schemas)

    def _build_tool_surface_artifact(
        self,
        allowed_tools: list[str] | None,
        previous_tools: list[str] | None = None,
        query: str = "",
        surface_tools: list[str] | None = None,
    ) -> RuntimeArtifact | None:
        """
        新增工具——与上一轮重复的工具不再以文本形态重注入。

        闲聊/能力询问轮只留按需检索入口：同一份工具面还会经 `tools` 参数旁路发送，
        两处必须同口径裁剪，否则首轮会把全量 108 个工具（≈6100 tokens）以文本再送一遍
        ——台账中这是首轮 system 比稳态多出的全部部分。"""
        if self._tool_registry is None:
            return None

        schemas = self._tool_registry.list_flat_schemas()
        if allowed_tools is not None and allowed_tools:
            schemas = [s for s in schemas if s["name"] in allowed_tools]
        # 功能：与 tools 参数同口径——只展示相关性裁剪后的工具面
        if surface_tools is not None:
            wanted = set(surface_tools)
            schemas = [s for s in schemas if s["name"] in wanted]
        from bandi.tools.selection import is_chitchat_query

        if is_chitchat_query(query):
            # 裁剪的前提是「存在按需检索入口」。没有它时不能裁成空——本轮什么都不投递
            # 会连带断掉 Delta 增量基线：下一轮把整份工具面当新工件重发，模型看到的
            # 是从未见过的工具列表却标成增量。前提不成立时退回完整工具面。
            trimmed = [s for s in schemas if s["name"] == "tool_search"]
            if trimmed:
                schemas = trimmed
        known = set(previous_tools or [])
        delta = [s for s in schemas if s["name"] not in known]
        if not schemas:
            return None
        if not delta:
            return None
        # 功能：工具面工件只列工具名——完整描述已在 `tools` 参数（原生 schema）里
        # 发过一遍，文本工件再带描述等于双份付费（108 工具约 6.3k tokens）。
        lines = [f"- {s['name']}" for s in delta]
        return RuntimeArtifact(
            artifact_type=ARTIFACT_TYPE_TOOL_SURFACE,
            title=ARTIFACT_TITLE_TOOL_SURFACE,
            content="\n".join(lines),
            metadata={
                "allowed_tools": sorted(allowed_tools or []),
                "current_tools": sorted(s["name"] for s in schemas),
            },
        )

    @staticmethod
    def _build_skills_artifact(skill_catalog: str) -> RuntimeArtifact | None:
        # 目录常驻（模型据此调 skill_invoke）；正文由工具按需加载，不进工件
        if not skill_catalog:
            return None
        return RuntimeArtifact(
            artifact_type=ARTIFACT_TYPE_SKILLS,
            title=ARTIFACT_TITLE_SKILLS,
            content=skill_catalog,
            metadata={},
        )

    def _build_system_messages(
        self,
        mode: str,
        context: ContextBundle,
        domain_label: str = "",
        project_instructions_max_tokens: int | None = None,
    ) -> list[dict]:
        """功能：产出 system 消息序列——harness 在场时拆 静态/动态 两条（静态段连续
        在最前，跨轮字节稳定，供服务端前缀缓存命中）；兜底路径单条全量兼容。

        易变内容（记忆召回/公司上下文）不在此处产出，由 assemble 置于末尾。"""
        if self._harness is not None:
            sections = self._harness.build_sections(
                mode,
                context,
                domain_label=domain_label,
                project_instructions_max_tokens=project_instructions_max_tokens,
            )
            dynamic_parts: list[str] = []
            if sections.dynamic:
                dynamic_parts.append(sections.dynamic)
            if sections.tail:
                dynamic_parts.append(sections.tail)
            return [
                {"role": "system", "content": sections.static},
                {"role": "system", "content": "\n\n".join(dynamic_parts)},
            ]
        return [{"role": "system", "content": self._default_prompt(mode, context)}]

    @staticmethod
    def _default_prompt(mode: str, context: ContextBundle) -> str:
        parts: list[str] = [f"你是 Bandi 数字员工组织的成员助手，当前模式: {mode}。"]
        if context.project_instructions:
            parts.append(f"项目指令:\n{context.project_instructions}")
        if context.memory_context:
            parts.append(f"记忆上下文:\n{context.memory_context}")
        # 缺失——补上（context_loader 已计算 OntologySerializer.to_system_prompt）
        if context.ontology_schema:
            parts.append(f"本体模式:\n{context.ontology_schema}")
        if context.referenced_entities:
            entity_info = ", ".join(
                f"{e.name}({getattr(e, 'entity_type', '') or '未知'})"
                for e in context.referenced_entities[:_DEFAULT_ENTITY_DISPLAY_LIMIT]
            )
            parts.append(f"相关实体: {entity_info}")
        return " ".join(parts)
