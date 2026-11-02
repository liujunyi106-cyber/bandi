# 功能：CompanyContextSource — Company Mode 上下文注入（执行身份段 + 其他成员角色/状态/聚焦工作项）
# 功能：EmployeeSopContextSource — 员工绑定 SOP 卡注入（执行流程约束）
from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from bandi.context.item import ContextItem
from bandi.context.source import RuntimeContextSource
from bandi.core.message import Message
from bandi.utils.token_counter import TokenCounter

# 本 source 读取转发进 system prompt——单一来源常量，双方同 imports）
ROLE_IDENTITY_CONTEXT_KEY = "role_identity_context"

TEAM_MEMORY_CONTEXT_KEY = "team_memory_context"

# 注入序 Org→Team→Project→Role：团队记忆（Team 层）priority 高于身份段（Role 层）
_TEAM_MEMORY_PRIORITY = 10
# 身份段 priority 高于成员状态项（4）——上下文预算裁剪时优先保留
_IDENTITY_PRIORITY = 9

_STATUS_LABELS = {"idle": "空闲", "running": "执行中", "blocked": "阻塞"}


def _field(session: Any, key: str) -> str:
    val = getattr(session, key, None)
    if val is None and isinstance(session, dict):
        val = session.get(key)
    return str(val or "").strip()


class CompanyContextSource(RuntimeContextSource):
    """从公司运行时收集其他成员会话状态，作为感知层上下文注入。"""

    def __init__(self, company_runtime: Any = None, max_members: int = 8) -> None:
        self._runtime = company_runtime
        self._max_members = max_members

    def _collect_team_memory(self) -> ContextItem | None:
        """功能：读取 runtime.metadata 中的团队记忆摘要（Phase3）——键缺失/
        空串/duck-typing stub 均静默跳过。"""
        get_meta = getattr(self._runtime, "metadata", None)
        if get_meta is None:
            return None
        try:
            text = str(get_meta(TEAM_MEMORY_CONTEXT_KEY) or "").strip()
        except Exception:  # noqa: BLE001 — 团队记忆读取失败不阻断感知链路
            return None
        if not text:
            return None
        role_id = str(getattr(self._runtime, "role_id", "") or "").strip()
        return ContextItem(
            id=f"company:team_memory:{role_id or 'unknown'}",
            source_type="company",
            content=text,
            priority=_TEAM_MEMORY_PRIORITY,
            token_estimate=TokenCounter.estimate(text),
        )

    def _collect_identity(self) -> ContextItem | None:
        """功能：读取 runtime.metadata 中的执行身份段——duck-typing 安全
        （旧 stub runtime 无 metadata 方法时静默跳过）。"""
        get_meta = getattr(self._runtime, "metadata", None)
        if get_meta is None:
            return None
        try:
            text = str(get_meta(ROLE_IDENTITY_CONTEXT_KEY) or "").strip()
        except Exception:  # noqa: BLE001 — 身份段读取失败不阻断感知链路
            return None
        if not text:
            return None
        role_id = str(getattr(self._runtime, "role_id", "") or "").strip()
        return ContextItem(
            id=f"company:identity:{role_id or 'unknown'}",
            source_type="company",
            content=text,
            priority=_IDENTITY_PRIORITY,
            token_estimate=TokenCounter.estimate(text),
        )

    async def collect(
        self,
        message: Message,
        session_id: str,
        budget: int = 4096,
        intent_hint: str | None = None,
    ) -> list[ContextItem]:
        if self._runtime is None:
            return []
        items: list[ContextItem] = []
        team_memory = self._collect_team_memory()
        if team_memory is not None:
            items.append(team_memory)
        identity = self._collect_identity()
        if identity is not None:
            items.append(identity)
        try:
            sessions = self._runtime.sorted_member_sessions() or []
        except Exception:  # noqa: BLE001 — 公司运行时异常不阻断感知链路（fail-safe）；
            # 已收集的身份段保留（执行身份不依赖群聊状态面）
            return items
        for s in sessions[: self._max_members]:
            role_id = _field(s, "role_id")
            if not role_id:
                continue
            status = _STATUS_LABELS.get(_field(s, "status"), "未知")
            focused = _field(s, "focused_work_item_id")
            employee_id = _field(s, "employee_id")
            parts = [f"{role_id}（{status}）"]
            if focused:
                parts.append(f"聚焦 {focused}")
            if employee_id:
                parts.append(f"员工 {employee_id}")
            content = " · ".join(parts)
            items.append(
                ContextItem(
                    id=f"company:{role_id}:{employee_id or session_id}",
                    source_type="company",
                    content=content,
                    priority=4,
                    token_estimate=TokenCounter.estimate(content),
                )
            )
        return items


_SOP_PRIORITY = 8
_SOP_MAX_CHARS = 8000
_SOP_BODY_PRIORITY = 9
# 任务会话前缀：公司派单/角色运行时会加载完整 SOP 正文
_TASK_SESSION_PREFIXES = ("role-session::", "role-runtime::")
_SOP_EXPLICIT_MARKERS = ("sop", "按流程", "按步骤", "操作规程")


class EmployeeSopContextSource(RuntimeContextSource):
    """功能：读取员工档案绑定的 SOP 技能卡，渲染为执行流程段注入上下文。"""

    def __init__(
        self,
        employee_id: str,
        *,
        profile_manager: Any = None,
        skills_dir: str | Path | None = None,
    ) -> None:
        self._employee_id = str(employee_id or "").strip()
        self._profile_manager = profile_manager
        self._skills_dir = Path(skills_dir or Path.home() / ".bandi" / "skills").expanduser()

    def _render_card(self, card: Any) -> str:
        lines = [f"【绑定 SOP】{card.name}", str(card.description or "").strip()]
        if card.goal:
            lines.append("目标：" + "；".join(str(item) for item in card.goal))
        if card.required_info:
            lines.append("执行前必须收集：" + "；".join(str(item) for item in card.required_info))
        if card.response_rules:
            lines.append("回复规则：" + "；".join(str(item) for item in card.response_rules))
        nodes = {node.node_id: node for node in card.nodes}
        edges: dict[str, list[Any]] = {node_id: [] for node_id in nodes}
        for edge in card.edges:
            edges.setdefault(edge.source_node_id, []).append(edge)
        ordered: list[Any] = []
        seen: set[str] = set()
        queue = deque([card.start_node_id])
        while queue:
            node_id = queue.popleft()
            node = nodes.get(node_id)
            if node is None or node_id in seen:
                continue
            seen.add(node_id)
            ordered.append(node)
            for edge in sorted(edges.get(node_id, []), key=lambda item: item.priority):
                queue.append(edge.next_node_id)
        for index, node in enumerate(ordered, 1):
            text = str(node.instruction or node.name or "").strip()
            suffix = ""
            if node.expected_user_info:
                suffix = (
                    "（需收集：" + "；".join(str(item) for item in node.expected_user_info) + "）"
                )
            if node.condition:
                suffix += f"（条件：{node.condition}）"
            lines.append(f"{index}. {node.name}：{text}{suffix}")
        content = "\n".join(item for item in lines if item)
        return content[:_SOP_MAX_CHARS]

    @staticmethod
    def _needs_full_body(text: str, card_names: list[str], session_id: str) -> bool:
        """功能：本轮是否需要 SOP 正文（任务会话/显式要求/点名流程）。"""
        if str(session_id or "").startswith(_TASK_SESSION_PREFIXES):
            return True
        lowered = (text or "").lower()
        if not lowered:
            return False
        if any(marker in lowered for marker in _SOP_EXPLICIT_MARKERS):
            return True
        return any(name and name.lower() in lowered for name in card_names)

    @staticmethod
    def _render_catalog(cards: list[Any]) -> str:
        """功能：渲染 SOP 目录（名称+用途）；完整流程按需加载。"""
        lines = ["【绑定 SOP】（需要完整流程时说明「按<名称>流程」）"]
        for card in cards:
            description = str(card.description or "").strip()
            lines.append(f"- {card.name}：{description}" if description else f"- {card.name}")
        return "\n".join(lines)

    async def collect(
        self,
        message: Message,
        session_id: str,
        budget: int = 4096,
        intent_hint: str | None = None,
    ) -> list[ContextItem]:
        if not self._employee_id:
            return []
        manager = self._profile_manager
        if manager is None:
            from bandi.company.agent_profile import AgentProfileManager

            manager = AgentProfileManager(skills_dir=str(self._skills_dir))
        try:
            profile = manager.get_profile(self._employee_id)
        except Exception:  # noqa: BLE001 — 档案不可读按无 SOP 处理
            return []
        if profile is None:
            return []
        from bandi.skills.serializer import load_card

        cards: list[Any] = []
        for item in getattr(profile, "resources", []) or []:
            if str(getattr(item, "resource_type", "")) != "skill":
                continue
            if str(getattr(item, "status", "active")) != "active":
                continue
            skill_id = str(getattr(item, "resource_id", "") or "").strip()
            if not skill_id:
                continue
            try:
                cards.append(load_card(self._skills_dir / f"{skill_id}.card.json"))
            except Exception:  # noqa: BLE001 — 单卡损坏不阻断其他 SOP
                continue
        if not cards:
            return []
        catalog = self._render_catalog(cards)[:_SOP_MAX_CHARS]
        items = [
            ContextItem(
                id=f"employee_sop:{self._employee_id}:catalog",
                source_type="employee_sop",
                content=catalog,
                priority=_SOP_PRIORITY,
                token_estimate=TokenCounter.estimate(catalog),
            )
        ]
        if not self._needs_full_body(
            str(message.content or ""), [card.name for card in cards], session_id
        ):
            return items
        body = "\n\n".join(self._render_card(card) for card in cards)[:_SOP_MAX_CHARS]
        items.append(
            ContextItem(
                id=f"employee_sop:{self._employee_id}:body",
                source_type="employee_sop",
                content=body,
                priority=_SOP_BODY_PRIORITY,
                token_estimate=TokenCounter.estimate(body),
            )
        )
        return items
