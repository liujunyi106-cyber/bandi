# 功能：席位执行器——协议定义 + 执行代理

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from bandi.agent.base import TerminalReason
from bandi.agent.factory import run_agent
from bandi.company.company_runtime import CompanyRuntime
from bandi.company.company_runtime_contract import build_company_work_item_contract
from bandi.company.group_chat_protocol import is_pass_output
from bandi.company.output_contract import render_output_contract_context, validate_work_item_output
from bandi.company.prompt_contract import (
    has_prompt_contract,
    normalize_prompt_contract,
    prompt_contract_from_work_item,
    render_assignment_context_from_contract,
)
from bandi.company.work_item import WorkItem, WorkItemTurnType
from bandi.company.work_item_transition import DEPENDENCY_WORK_ITEM_IDS_KEY
from bandi.context.sources.company_source import ROLE_IDENTITY_CONTEXT_KEY
from bandi.core.account import DEFAULT_LOCAL_USER_ID
from bandi.knowledge.scopes import KNOWLEDGE_BASES_PERMISSION_KEY, intersect_knowledge_bases
from bandi.knowledge.scopes import reset_runtime_knowledge_bases as _reset_knowledge_scope
from bandi.knowledge.scopes import reset_runtime_knowledge_employee as _reset_knowledge_employee
from bandi.knowledge.scopes import set_runtime_knowledge_bases as _set_knowledge_scope
from bandi.knowledge.scopes import set_runtime_knowledge_employee as _set_knowledge_employee

logger = logging.getLogger(__name__)

# 功能：清洗模型把 DeepSeek DSML 工具标记写进正文时的泄漏
_TOOL_MARKUP_BLOCK = re.compile(
    r"<[^<>]{0,12}DSML[^<>]*calls>.*?</[^<>]{0,12}DSML[^<>]*calls>",
    re.S | re.I,
)
_TOOL_MARKUP_TAG = re.compile(r"</?[^<>]{0,12}DSML[^<>]*>", re.S | re.I)

# 功能：GFM 表格分隔行——用于识别「缺表头表格」
_TABLE_SEPARATOR = re.compile(r"^\|[\s:|\-]+\|$")


def group_conversation_id_for(work_item: WorkItem) -> str:
    """功能：解析群聊卡所属群会话 id，同一群跨回合共享群记忆。"""
    metadata = getattr(work_item, "metadata", None) or {}
    if not (metadata.get("group_chat") or metadata.get("round_id")):
        return ""
    session_id = str(metadata.get("origin_session_id") or "").strip()
    if session_id:
        return session_id
    return str(metadata.get("round_id") or "").strip()


def dm_conversation_id_for(work_item: WorkItem) -> str:
    """功能：解析员工单聊卡所属会话 id（每条会话独立记忆域）。"""
    metadata = getattr(work_item, "metadata", None) or {}
    if metadata.get("group_chat") or metadata.get("round_id"):
        return ""
    if not (
        metadata.get("dm_chat")
        or metadata.get("target_role")
        or str(metadata.get("source") or "") == "chat_dm"
    ):
        return ""
    return str(
        metadata.get("dm_conversation_id")
        or metadata.get("origin_session_id")
        or metadata.get("session_id")
        or ""
    ).strip()


def _normalize_tables(text: str) -> str:
    """功能：为缺少表头的表格补一行空表头。"""
    if "|" not in text:
        return text
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        if _TABLE_SEPARATOR.match(line.strip()):
            previous = out[-1].strip() if out else ""
            if not (previous.startswith("|") and previous.endswith("|")):
                columns = max(1, line.count("|") - 1)
                out.append("|" + "  |" * columns)
        out.append(line)
    return "\n".join(out)


def _strip_tool_markup(text: str) -> str:
    """功能：清洗正文中的 DSML 工具标记并修复缺表头表格。"""
    if not text:
        return text
    cleaned = text
    if "DSML" in cleaned:
        cleaned = _TOOL_MARKUP_BLOCK.sub("", cleaned)
        cleaned = _TOOL_MARKUP_TAG.sub("", cleaned)
    return _normalize_tables(cleaned).strip()


# 槽位门等待上限：用户不答复时按未作答继续，等待只发生一次
SLOT_GATE_TIMEOUT_SECONDS = 1800.0

_SLOT_LABELS: dict[str, str] = {
    "market": "目标市场",
    "category": "品类",
    "candidates": "候选方向",
    "business": "生意模式",
}


def _format_slot_answers(answers: dict[str, Any]) -> str:
    """功能：把槽位答复格式化为任务描述顶部的口径行。

    已知槽位用中文标签，未登记的槽位用原始键名——用户答了什么就回注什么，
    不能因为键不在标签表里把答复丢掉。
    """
    parts: list[str] = []
    covered: set[str] = set()
    for key, label in _SLOT_LABELS.items():
        value = answers.get(key)
        if isinstance(value, (list, tuple)):
            value = "；".join(str(item) for item in value if str(item).strip())
        text = str(value or "").strip()
        if text:
            parts.append(f"{label}：{text}")
            covered.add(key)
    for key, value in answers.items():
        if key in covered:
            continue
        if isinstance(value, (list, tuple)):
            value = "；".join(str(item) for item in value if str(item).strip())
        text = str(value or "").strip()
        if text:
            parts.append(f"{key}：{text}")
    return "；".join(parts) if parts else "（用户未作答，按默认口径推进）"


def resolve_requester_user_id(work_item: Any) -> str:
    """功能：解析工作项的发起用户标识（并发审计 ）。

    公司链此前不透传 `user_id` → 执行面身份回落 `_agent_id`，导致
    ①「用户→岗位→权限档」解析不触发；② 审计/日志无法按用户归因。
    取值：卡上声明的 `requester_user_id`；缺省回落本地默认用户（本地单机形态）。
    """
    meta = getattr(work_item, "metadata", None) or {}
    declared = str(meta.get("requester_user_id") or "").strip()
    return declared or DEFAULT_LOCAL_USER_ID


# 合成/失败分诊释放时由 work_item_transition 写入的扇入来源（与 refresh_dependents 同键）
_SYNTHESIS_SOURCE_KEY = "synthesis_source_work_item_ids"


def approval_metadata(capture: dict[str, Any] | None) -> dict[str, Any]:
    """功能：把执行链上报的审批锚点转成 SeatResult metadata（缺值不写键）。"""
    data = capture or {}
    call_id = str(data.get("tool_call_id") or "").strip()
    tool_name = str(data.get("tool_name") or "").strip()
    metadata: dict[str, Any] = {}
    if call_id:
        metadata["approval_tool_call_id"] = call_id
    if tool_name:
        metadata["approval_tool_name"] = tool_name
    return metadata


def _consume_approval_resume(work_item: Any, store: Any | None = None) -> str:
    """功能：消费卡的审批恢复指令——一次性读出即清除（尽力落库）
    防止失败重跑重复注入；返回前置注入文本（无恢复指令时为空串）。

    指令文本优先用决策端点写入的 `note`（re-issue/拒绝说明，模型可执行）；
    缺失时回退固定句式。
    """
    metadata = getattr(work_item, "metadata", None)
    if not isinstance(metadata, dict):
        return ""
    payload = metadata.get("approval_resume")
    if not isinstance(payload, dict):
        return ""
    metadata.pop("approval_resume", None)
    if store is not None:
        try:
            store.save(work_item)
        except Exception:
            logger.debug("approval_resume consume persist failed", exc_info=True)
    note = str(payload.get("note") or "").strip()
    if not note:
        decision = str(payload.get("decision") or "").strip() or "已处理"
        tool = str(payload.get("tool_name") or "").strip()
        note = f"审批决策：{decision}（工具 `{tool}`）——请按审批结果继续。"
    logger.info("approval resume injected for work item %s", getattr(work_item, "id", "?"))
    return f"[审批决策恢复] {note}"


SOP_PROGRESS_KEY = "sop_progress"


def _resolve_project_root_for(project_id: str) -> str | None:
    """功能：-11③——项目根解析（教训详情档案 entries 落点）；无项目/无根
    /项目库异常均返回 None（不阻断执行，MemoryManager 落 db fallback）。"""
    pid = str(project_id or "").strip()
    if not pid:
        return None
    try:
        from bandi.company.projects import ProjectStore

        record = ProjectStore().get_project(pid)
    except Exception:  # noqa: BLE001 — 项目库异常按无根处理
        return None
    return str(getattr(record, "root_dir", "") or "").strip() or None


@dataclass
class SeatResult:
    success: bool
    output: str
    metadata: dict[str, Any] | None = None


@runtime_checkable
class SeatExecutor(Protocol):
    async def prepare_seat(self, runtime: CompanyRuntime, work_item: WorkItem) -> None: ...
    async def run_turn(self, runtime: CompanyRuntime, work_item: WorkItem) -> SeatResult: ...
    async def checkpoint(self, runtime: CompanyRuntime) -> dict[str, Any]: ...
    async def interrupt(self, runtime: CompanyRuntime) -> None: ...
    async def shutdown(self) -> None: ...


class EngineSeatExecutor:
    def __init__(
        self,
        communication: Any | None = None,
        org_engine: Any | None = None,
        event_bus: Any | None = None,
        store: Any | None = None,
        thread_manager: Any | None = None,
        frames_dir: str | None = None,
        progress_callback: Any | None = None,
        interaction_provider: Any | None = None,
        slot_answer_provider: Any | None = None,
        artifact_store: Any | None = None,
        group_context_provider: Any | None = None,
    ) -> None:
        self._active: dict[str, CompanyRuntime] = {}
        # 传 bundle.communication；CLI/API 装配点同目录传 CommunicationManager
        # 全部 3 个生产装配点均已接线（wiring-check -AssemblyPoint 复验）
        self._communication = communication
        # 做 role-scoped 工具过滤（None = 不过滤，保持现状）
        self._org_engine = org_engine
        # 装配点可注入 EventBus 使 OUTPUT_CONTRACT_VIOLATION 事件可观测
        self._event_bus = event_bus
        # 装配点把公司上下文传入 run_turn 供 run_agent→run_agent_turn 对 spawn 工具
        self._store = store
        self._thread_manager = thread_manager
        self._abort_provider: Any = None
        self._frames_dir = frames_dir
        # run_turn 构造 EmployeeProgressHandler 把过程帧转 thread_progress 事件
        self._progress_callback = progress_callback
        # 员工交互通道工厂（多问卡/权限卡）——provider(session_id) → UserInteraction；
        # 未装配时工具层 fail-closed 自报（不静默等待）
        self._interaction_provider = interaction_provider
        # 槽位门等待器：async (interaction_id, timeout) -> answers dict | None
        self._slot_answer_provider = slot_answer_provider
        # 员工产物登记存储（写类工具成功后登记到员工执行会话——右栏「产物」数据源）
        self._artifact_store = artifact_store
        # 群聊回合上下文提供者（按卡 round_id 渲染顺序/未读消息/PASS 规则）
        self._group_context_provider = group_context_provider
        # run_turn 按角色 home team 做团队分享摘要

    def bind_abort_provider(self, provider: Any) -> None:
        """功能：绑定工作项级 AbortController 查询器——run_turn 透传给 run_agent
        实现员工执行链可中断（dispatcher 装配点调用，对齐 bind_registry 先例）。"""
        self._abort_provider = provider

    def _artifact_summary(self, runtime: CompanyRuntime) -> str:
        """功能：空正文兜底——用已产出文件生成交付摘要（模型跑完没写正文时）。"""
        store = self._artifact_store
        if store is None or not hasattr(store, "list_artifacts"):
            return ""
        try:
            artifacts = store.list_artifacts(runtime.member_session_id())
        except Exception:  # noqa: BLE001 — 读产物失败按无产物处理
            return ""
        paths: list[str] = []
        for item in artifacts[-10:]:
            path = str(item.get("path") or "").strip()
            if path and path not in paths:
                paths.append(path)
        if not paths:
            return ""
        listing = "\n".join(f"- `{path}`" for path in paths)
        return f"本轮已产出以下交付文件（正文未附说明）：\n{listing}"

    def _project_sop_progress(
        self, runtime: CompanyRuntime, work_item: WorkItem
    ) -> dict[str, Any] | None:
        """功能（桥接）：把员工执行会话的 SOP 帧进度投影到组织层元数据——
        组织看进度、skill 层私域不被读写/推进（只读值快照，防污染）；无帧不写键；
        投影失败 best-effort 不阻断执行链。返回投影（供 run_turn 携带进
        SeatResult.metadata，经 company_mode 落库——只写本对象会被调度方丢弃）。"""
        try:
            from bandi.skills.frame_store import DEFAULT_FRAMES_DIR, load_frames
            from bandi.skills.progress import build_sop_progress, frame_scope_for_session

            scope = str(
                work_item.runtime_metadata.get("frame_scope") or ""
            ) or frame_scope_for_session(runtime.member_session_id())
            stack = load_frames(scope, self._frames_dir or DEFAULT_FRAMES_DIR)
            progress = build_sop_progress(stack)
        except Exception as exc:  # noqa: BLE001 — 投影不阻断执行链
            logger.warning("sop progress projection failed: %s", exc)
            return None
        if progress is not None:
            work_item.runtime_metadata[SOP_PROGRESS_KEY] = progress
        return progress

    async def _emit_violation(self, work_item_id: str, violation: dict[str, Any]) -> None:
        """违规事件发射——bus 存在才 emit（观测，不阻断）。"""
        if self._event_bus is None:
            return
        try:
            from bandi.core.events import Event, EventType

            await self._event_bus.emit(
                Event(
                    type=EventType.OUTPUT_CONTRACT_VIOLATION,
                    data={
                        "work_item_id": work_item_id,
                        "missing_keys": violation.get("missing_keys", []),
                        "format_errors": violation.get("format_errors", []),
                    },
                )
            )
        except Exception:
            logger.debug("output contract violation event emit failed", exc_info=True)

    def _role_tool_tags(self, role_id: str) -> list[str] | None:
        """功能：解析员工工具面——配置优先，未配置与助理一致（None＝不过滤）。

        语义边界：助理与员工共用 `agent/factory.py::build_agent_context`，
        唯一差别是传入的 `role_tool_tags`——助理传 None。因此「员工未配置」
        必须返回 None，而不是另造默认面或落空集；token 由 tool_groups 组开关、
        select_relevant_schemas（>35 才裁）、mcp_max_exposed_tools 三道闸控制。
        读取角色配置失败时上抛：装配不出来就该由派发层延后/失败，不能给空工具面。
        """
        if self._org_engine is None or not role_id:
            return None
        get_role = getattr(self._org_engine, "get_role", None)
        if get_role is None:
            return None
        try:
            role = get_role(role_id)
        except Exception as e:
            logger.error("role tool_tags query failed (role_id=%s): %s", role_id, e, exc_info=True)
            raise
        tags = getattr(role, "tool_tags", None) if role is not None else None
        if tags:
            # （工具名/中文/照抄占位符）⇒ 直接下发会让过滤求交为空、员工拿到 0 个工具。
            # 归一化后：有合法值则只下发合法值；全非法则视为未配置（与助理一致）。
            from bandi.tools.registry import normalize_role_tool_tags

            valid, dropped = normalize_role_tool_tags(tags)
            if dropped:
                logger.warning(
                    "role %s tool_tags 含非法值已忽略（非 ToolCategory 值域）：%s；采用=%s",
                    role_id,
                    dropped,
                    valid,
                )
            if valid:
                return valid
        # 未配置/全非法 = 与助理同一口径（None 不过滤）；岗位模板只在创建时写初值，
        # 运行时不按角色名收窄（内置角色标签表已不参与员工工具面）。
        return None

    def _role_manager_capable(self, role_id: str) -> bool:
        """功能：角色是否有派发面（有下级或管辖团队）——决定是否投放管理者工具。"""
        if self._org_engine is None or not role_id:
            return False
        get_role = getattr(self._org_engine, "get_role", None)
        if get_role is None:
            return False
        try:
            role = get_role(role_id)
        except Exception:  # noqa: BLE001 — 查询失败按无派发面（工具面收紧不放大权限）
            return False
        if role is None:
            return False
        can_spawn = [
            str(x).strip() for x in (getattr(role, "can_spawn", None) or []) if str(x).strip()
        ]
        return bool(can_spawn)

    def _role_fs_scopes(self, role_id: str) -> list[str] | None:
        """.6：角色级落盘目录白名单（fs_scopes）——空/未配置 = 不过滤；
        查询异常回落工具面 fail-closed（tool_tags 空名单已全禁）不下发 scope。"""
        if self._org_engine is None or not role_id:
            return None
        get_role = getattr(self._org_engine, "get_role", None)
        if get_role is None:
            return None
        try:
            role = get_role(role_id)
        except Exception as e:  # noqa: BLE001 — 查询失败不额外放宽（工具面已 fail-closed）
            logger.error("role fs_scopes query failed (role_id=%s): %s", role_id, e)
            return None
        scopes = getattr(role, "fs_scopes", None) if role is not None else None
        return list(scopes) if scopes else None

    def _render_group_context(self, work_item: WorkItem) -> str:
        """功能：渲染群聊回合上下文（发言顺序 + 本轮未读 + PASS 规则）。"""
        if self._group_context_provider is None:
            return ""
        try:
            return str(self._group_context_provider(work_item) or "")
        except Exception:
            logger.debug("group round context render failed", exc_info=True)
            return ""

    def _render_dependency_results(self, work_item: WorkItem) -> str:
        """.5：扇入卡执行前注入子任务产出（`turn_summary` 原为死产出）——
        聚合 Agent 必须看到子卡实际交付物方可合成。"""
        if self._store is None:
            return ""
        metadata = dict(work_item.metadata or {})
        dep_ids = [
            str(v)
            for v in (
                metadata.get(_SYNTHESIS_SOURCE_KEY)
                or metadata.get(DEPENDENCY_WORK_ITEM_IDS_KEY)
                or []
            )
            if str(v).strip()
        ]
        if not dep_ids:
            return ""
        sections: list[str] = []
        used = 0
        summary = str(metadata.get("turn_summary") or "").strip()
        if summary:
            block = f"## 分诊摘要\n\n{summary[:2000]}"
            sections.append(block)
            used += len(block)
        for dep_id in dep_ids[:10]:
            try:
                child = self._store.get(dep_id)
            except Exception:  # noqa: BLE001 — 子卡读取失败不阻断聚合执行
                continue
            if child is None:
                continue
            output = str((child.runtime_metadata or {}).get("_last_output") or "").strip()
            if not output:
                continue
            child_meta = getattr(child, "metadata", None) or {}
            if bool(child_meta.get("group_chat")) and is_pass_output(output):
                continue
            phase = getattr(getattr(child, "phase", None), "value", "") or ""
            if len(output) > 6000:
                from bandi.tools.output_store import persist_tool_content

                path = persist_tool_content("handoff", output, prefix="handoff")
                if path:
                    block = (
                        f"### {child.title}（{phase}）\n"
                        f"完整结果见：{path}\n"
                        f"{output[:1500]}\n...\n{output[-500:]}"
                    )
                else:
                    block = f"### {child.title}（{phase}）\n{output[:1200]}"
            else:
                block = f"### {child.title}（{phase}）\n{output[:1200]}"
            if used + len(block) > 6000:
                break
            sections.append(block)
            used += len(block)
        if not sections:
            return ""
        return "## 子任务结果\n\n" + "\n\n".join(sections)

    def _render_rework_feedback(self, work_item: WorkItem) -> str:
        """功能：返工轮的修正要求段（用户对话修正 / 评审驳回共用通道）。

        门控看**内容**而非 phase：调度器
        会先把卡翻到 RUNNING 再装配 prompt，用 phase==READY_FOR_REWORK 判定会静默吞掉
        反馈；approve 时该字段被清空，故非空本身即「这是返工轮」的信号。看不到修正要求
        就重跑，模型要么重复同样的错、要么盲改。
        """
        metadata = dict(work_item.metadata or {})
        directive = str(metadata.get("user_rework_directive") or "").strip()
        feedback = str(metadata.get("rework_feedback") or "").strip()
        reason = directive or feedback
        if not reason:
            return ""
        source = "用户在对话中提出的修正" if directive else "评审驳回理由"
        lines = [
            "## 返工要求（必须按此修正后重交）",
            "",
            f"### {source}",
            reason,
        ]
        previous = str(
            metadata.get("rework_previous_output")
            or (work_item.runtime_metadata or {}).get("_last_output")
            or ""
        ).strip()
        if previous:
            lines += [
                "",
                "### 上一版产出（据此修改，不要从零重写）",
                previous[:1500],
            ]
        return "\n".join(lines)

    def _resolve_provider_config(
        self, employee_id: str | None
    ) -> tuple[dict[str, Any] | None, str]:
        """换大脑：解析员工 llm_config 为 provider_config（键名对齐 LLMConfig）。

        返回 (provider_config, preflight_error)。查询失败/无配置/非法类型 → (None, "")
        （跟随全局模型，零影响现状——llm_config 非权限语义，不沿用 tool_tags 的
        fail-closed 空名单先例）；模型无法解析 provider → 预检错误 fail-closed。
        """
        if self._org_engine is None or not employee_id:
            return None, ""
        get_employee = getattr(self._org_engine, "get_employee", None)
        if get_employee is None:
            return None, ""
        try:
            employee = get_employee(employee_id)
        except Exception:  # noqa: BLE001 — 查询失败跟随全局（见上，非权限语义）
            logger.error("employee llm_config query failed (employee_id=%s)", employee_id)
            return None, ""
        llm_config = getattr(employee, "llm_config", None) if employee is not None else None
        if not isinstance(llm_config, dict) or not llm_config:
            return None, ""
        overrides = {k: v for k, v in llm_config.items() if v is not None}
        if not overrides:
            return None, ""
        model = overrides.get("default_model")
        if model:
            from bandi.core.config import BandiConfig
            from bandi.llm.registry import validate_model_provider

            # 模型名 → 已配置档案解析：员工只写模型名时，从同模型档案补齐
            # base_url/api_key/provider（llm_config 显式给出的键优先；无匹配保持现状）
            if not overrides.get("base_url") or not overrides.get("api_key"):
                try:
                    from bandi.llm.profiles import store_singleton

                    store = store_singleton()
                    match = next(
                        (
                            profile
                            for profile in store.list_profiles()
                            if str(getattr(profile, "model", "") or "") == str(model)
                        ),
                        None,
                    )
                except Exception:  # noqa: BLE001 — 档案读取失败保持现状（不阻断）
                    match = None
                if match is not None:
                    if not overrides.get("base_url") and getattr(match, "base_url", ""):
                        overrides["base_url"] = str(match.base_url)
                    if not overrides.get("api_key") and getattr(match, "api_key", ""):
                        overrides["api_key"] = str(match.api_key)
                    if not overrides.get("provider") and getattr(match, "provider", ""):
                        overrides["provider"] = str(match.provider)
            config = BandiConfig.load()
            base_url = overrides.get("base_url")
            if base_url:
                # 员工自带网关：预检必须看见与 run_agent 同款的覆盖（provider_config
                # 逐键覆盖 config.llm），否则「私有模型 + 私有 base_url」被误判 fail-closed
                config = config.model_copy(
                    update={"llm": config.llm.model_copy(update={"base_url": str(base_url)})}
                )
            error = validate_model_provider(str(model), config)
            if error:
                return None, error
        return overrides, ""

    def _render_role_identity(self, runtime: CompanyRuntime) -> str:
        """断链接线：渲染员工执行身份段（职责/汇报关系/员工名/persona）——
        经 runtime.metadata 交给 CompanyContextSource 注入 system prompt。
        org_engine 缺失/查询失败返回空串（身份段是加固项，不阻断执行主链路）。"""
        if self._org_engine is None or not runtime.role_id:
            return ""
        render = getattr(self._org_engine, "get_role_prompt_context", None)
        if render is None:
            return ""
        try:
            return str(render(runtime.role_id, employee_id=runtime.employee_id) or "").strip()
        except Exception as exc:  # noqa: BLE001 — 身份段渲染失败不阻断执行主链路
            logger.warning("role identity render failed (role_id=%s): %s", runtime.role_id, exc)
            return ""

    def _render_knowledge_hint(self, allowed_bases: list[str] | None, employee_id: str) -> str:
        """功能：把可见资料库名写进员工身份段——提升 knowledge_search 触发率。"""
        if allowed_bases is not None and not allowed_bases:
            return "你当前没有可检索的资料库；资料类问题请说明无法访问。"
        try:
            from bandi.knowledge.store import get_shared_store

            bases = get_shared_store().list_bases()
        except Exception:  # noqa: BLE001 — 提示是加固项，读取失败不阻断执行
            return ""
        allowed = set(allowed_bases) if allowed_bases is not None else None
        names = [
            str(base.name or base.id)
            for base in bases
            if (allowed is None or base.id in allowed)
            and (not base.employee_id or base.employee_id == employee_id)
        ][:8]
        if not names:
            return ""
        return (
            "你可检索的资料库："
            + "、".join(names)
            + "。资料类问题优先调用 knowledge_search（catalog → search → read）。"
        )

    def _role_knowledge_bases(self, role_id: str, employee_id: str = "") -> list[str] | None:
        """角色可见域 = 角色权限 ∩ 员工绑定——单侧 None 表示该侧不设限，
        两者都无 = None（不过滤）；角色权限键存在但空 = fail-closed 全禁。"""
        if self._org_engine is None or not role_id:
            return None
        get_role = getattr(self._org_engine, "get_role", None)
        if get_role is None:
            return None
        try:
            role = get_role(role_id)
        except Exception as e:  # noqa: BLE001 — 权限面查询异常 fail-closed（空名单=全禁）
            logger.error(
                "role knowledge_bases query failed (fail-closed, role_id=%s): %s", role_id, e
            )
            return []
        if role is None:
            return None
        permissions = getattr(role, "permissions", None) or {}
        raw = permissions.get(KNOWLEDGE_BASES_PERMISSION_KEY)
        role_bases = (
            [str(base).strip() for base in raw if str(base).strip()]
            if isinstance(raw, list)
            else None
        )
        return intersect_knowledge_bases(role_bases, self._employee_knowledge_bases(employee_id))

    def _employee_knowledge_bases(self, employee_id: str) -> list[str] | None:
        # 功能：员工档案里的 knowledge_base 绑定（active）——无绑定返回 None（不设限）。
        who = str(employee_id or "").strip()
        if not who:
            return None
        try:
            from bandi.company.agent_profile import AgentProfileManager

            profile = AgentProfileManager().get_profile(who)
        except Exception:  # noqa: BLE001 — 档案不可读按未配置处理
            return None
        if profile is None:
            return None
        bases = [
            str(getattr(item, "resource_id", "") or "").strip()
            for item in (getattr(profile, "resources", []) or [])
            if str(getattr(item, "resource_type", "")) == "knowledge_base"
            and str(getattr(item, "status", "active")) == "active"
        ]
        bases = [base for base in bases if base]
        return bases or None

    async def prepare_seat(self, runtime: CompanyRuntime, _work_item: WorkItem) -> None:
        self._active[runtime.member_session_id()] = runtime

    async def _consume_slot_gate(self, work_item: WorkItem) -> None:
        """功能：槽位门一次性消费——本轮尝试取答复后无论取到、超时还是异常都摘掉门，
        同一张卡的同一个门只阻塞一次（超时后返工重试不再二次等满等待上限）。

        取到答复：口径写回任务描述顶部并置 slot_answered；未取到：置
        slot_answer_timeout，门同样摘除，本轮按默认口径继续执行。
        """
        metadata = dict(work_item.metadata or {})
        slot_gate_id = str(metadata.get("slot_gate") or "").strip()
        if not slot_gate_id or metadata.get("slot_answered"):
            return
        answers: dict[str, Any] = {}
        if self._slot_answer_provider is not None:
            try:
                answers = (
                    await self._slot_answer_provider(slot_gate_id, SLOT_GATE_TIMEOUT_SECONDS) or {}
                )
            except Exception:  # noqa: BLE001 — 等待失败按未答复继续（不吞执行）
                answers = {}
        metadata.pop("slot_gate", None)
        if answers:
            metadata["slot_answered"] = True
            metadata.pop("slot_answer_timeout", None)
            work_item.description = (
                f"【已确认口径】{_format_slot_answers(answers)}\n\n{work_item.description}"
            )
        else:
            metadata["slot_answer_timeout"] = True
        work_item.metadata = metadata
        try:
            if self._store is not None:
                self._store.save(work_item)
        except Exception:
            logger.debug("slot gate persist failed", exc_info=True)

    async def run_turn(self, runtime: CompanyRuntime, work_item: WorkItem) -> SeatResult:
        # 槽位门：系统预建多问卡未答复前不执行；答复后把口径写回任务描述
        await self._consume_slot_gate(work_item)
        user_input = work_item.title
        if work_item.description:
            user_input = f"{work_item.title}\n\n任务描述：{work_item.description}"
        group_context = self._render_group_context(work_item)
        if group_context:
            user_input = f"{group_context}\n\n{user_input}"
        # 5.5：依赖卡注入——把上游卡的输出汇总为下游可见上下文
        dependency_results = self._render_dependency_results(work_item)
        if dependency_results:
            user_input = f"{dependency_results}\n\n{user_input}"
        contract_keys = ("ownership_contract", "context_hint")
        extras = {k: v for k, v in work_item.metadata.items() if k in contract_keys and v}
        if group_context:
            extras.pop("context_hint", None)
        if extras:
            user_input += "\n\n任务元数据：" + json.dumps(extras, ensure_ascii=False)
        # 返工轮：修正要求（用户对话指令 / 评审驳回理由）+ 上一版产出，拼在契约之后
        # 任务正文之前——模型先看边界契约，再看必须改什么，最后看本轮任务
        rework_block = self._render_rework_feedback(work_item)
        if rework_block:
            user_input = f"{rework_block}\n\n{user_input}"
        # 聚合轮（子卡全部完成后的复活轮）按 aggregate 契约：汇总上游产出，
        # 不再重复出计划
        metadata_now = dict(work_item.metadata or {})
        raw_prompt_contract = metadata_now.get("prompt_contract")
        legacy_contract_text = (
            str(raw_prompt_contract).strip()
            if isinstance(raw_prompt_contract, str) and str(raw_prompt_contract).strip()
            else ""
        )
        if not legacy_contract_text and not has_prompt_contract(raw_prompt_contract):
            metadata_now["prompt_contract"] = prompt_contract_from_work_item(
                work_item,
                task_description=str(work_item.description or ""),
            )
            work_item.metadata = metadata_now
            if self._store is not None:
                try:
                    self._store.save(work_item)
                except Exception:
                    logger.debug("prompt contract persist failed", exc_info=True)
        is_aggregate_turn = str(metadata_now.get("frontier") or "").strip().lower() == "resumed"
        contract_turn_type = (
            "aggregate"
            if is_aggregate_turn
            else (
                work_item.turn_type.value
                if hasattr(work_item.turn_type, "value")
                else str(work_item.turn_type)
            )
        )
        contract_text = build_company_work_item_contract(
            {
                "turn_type": contract_turn_type,
                "title": work_item.title,
                "metadata": metadata_now,
            },
            audience="native",
        )
        if contract_text:
            # 合同后显式分界——下游从「【本卡任务如下】」取真实任务段
            user_input = f"{contract_text}\n\n【本卡任务如下】\n\n{user_input}"
        assignment_ctx = render_assignment_context_from_contract(
            normalize_prompt_contract(metadata_now.get("prompt_contract")),
            include_dispatch_fields=True,
        )
        if assignment_ctx:
            user_input = f"{assignment_ctx}\n\n{user_input}"
        if legacy_contract_text:
            user_input = f"## Work Item Contract\n{legacy_contract_text}\n\n{user_input}"
        # 功能：外网受限时禁止反复 bash 网络诊断（DNS/curl/ping），证据缺失
        # 标注「待取证」后仍须完成本卡交付并写出最终正文。
        user_input = (
            "【环境约束】本机外网可能受限：不要反复用 bash 诊断网络（DNS/curl/ping/"
            "mcporter 探测）；证据缺失标注「待取证」后仍须完成本卡交付，并写出最终正文。\n\n"
            f"{user_input}"
        )
        output_ctx = render_output_contract_context(work_item.metadata)
        if output_ctx:
            user_input = f"{output_ctx}\n\n{user_input}"
        # 先看到「已批准/已拒绝」再决定重发/放弃调用，配合 _consume_approval_grant 放行
        approval_resume = _consume_approval_resume(work_item, self._store)
        if approval_resume:
            user_input = f"{approval_resume}\n\n{user_input}"
        # 必须显式失败，不得静默回退全局模型（否则换大脑悄悄失效）。
        provider_config, preflight_error = self._resolve_provider_config(runtime.employee_id)
        if preflight_error:
            logger.warning(
                "provider preflight failed for employee %s: %s",
                runtime.employee_id,
                preflight_error,
            )
            return SeatResult(
                success=False,
                output=f"provider preflight failed: {preflight_error}",
                metadata={"provider_preflight_error": preflight_error},
            )
        abort_controller = (
            self._abort_provider(str(work_item.id)) if self._abort_provider is not None else None
        )
        # 归属取 runtime 维度 + 工作项 thread_id（前端按项目/员工/线程过滤）
        stream_handler: Any | None = None
        if self._progress_callback is not None:
            from bandi.company.seat_progress import EmployeeProgressHandler

            stream_handler = EmployeeProgressHandler(
                self._progress_callback,
                project_id=runtime.project_id or "",
                thread_id=str((work_item.metadata or {}).get("thread_id") or ""),
                role_id=runtime.role_id or "",
                artifact_store=self._artifact_store,
                work_item_id=str(work_item.id),
            )
        # 员工交互通道（多问卡）：问题落到**发起会话**（origin_session_id）——用户在
        # 群聊/单聊现场看到决策卡；无 origin 回落员工会话。用户答复经 confirm 端点
        # 解析 → 工具阻塞调用返回 → 执行继续（不中断）。构建失败不阻断执行（工具层自报）
        user_interaction: Any = None
        if (work_item.metadata or {}).get("background_run"):
            from bandi.core.user_interaction import AutoApproveInteraction

            user_interaction = AutoApproveInteraction()
        elif self._interaction_provider is not None:
            interaction_session = str((work_item.metadata or {}).get("origin_session_id") or "")
            if not interaction_session:
                interaction_session = runtime.member_session_id()
            try:
                user_interaction = self._interaction_provider(interaction_session)
            except Exception:
                logger.warning("interaction provider build failed", exc_info=True)
                user_interaction = None
            if user_interaction is not None and hasattr(user_interaction, "_executor_session_id"):
                user_interaction._executor_session_id = runtime.member_session_id()
        allowed_bases = self._role_knowledge_bases(runtime.role_id, runtime.employee_id or "")
        identity_context = self._render_role_identity(runtime)
        knowledge_hint = self._render_knowledge_hint(allowed_bases, runtime.employee_id or "")
        if knowledge_hint:
            identity_context = (
                f"{identity_context}\n\n{knowledge_hint}" if identity_context else knowledge_hint
            )
        if identity_context:
            runtime.set_metadata(ROLE_IDENTITY_CONTEXT_KEY, identity_context)
        dm_conversation_id = dm_conversation_id_for(work_item)
        # finally 复位防泄漏到同任务后续运行
        knowledge_scope_token = _set_knowledge_scope(allowed_bases)
        employee_scope_token = _set_knowledge_employee(runtime.employee_id or None)
        # 可见性先行：进入 run_agent 前先发 started（关闭“等首 token 才见员工”的空窗）
        if stream_handler is not None:
            await stream_handler.on_started(runtime.member_session_id(), str(work_item.id))
        try:
            approval_capture: dict[str, Any] = {}
            output, terminal_reason, total_tokens, turn_index = await run_agent(
                user_input=user_input,
                # 规划轮以 plan 模式驱动员工（群聊统一商讨/单聊 plan）；执行轮与
                # 汇总轮保持 execute（汇总要写终稿文件，plan 模式会触发切模式弹卡）
                mode=(
                    "plan"
                    if work_item.turn_type == WorkItemTurnType.PLAN and not is_aggregate_turn
                    else "execute"
                ),
                session_id=runtime.member_session_id(),
                work_item=work_item,
                abort_controller=abort_controller,
                attachments=list(work_item.metadata.get("image_attachments") or []) or None,
                # 公司链路记忆按员工×项目维度隔离——runtime 已持有
                # project_id/employee_id，透传后记忆不跨员工/项目共享
                project_id=runtime.project_id or None,
                employee_id=runtime.employee_id or None,
                group_conversation_id=group_conversation_id_for(work_item),
                dm_conversation_id=dm_conversation_id,
                # 无根时 MemoryManager 落 db fallback，不阻断执行
                project_root=_resolve_project_root_for(runtime.project_id),
                communication=self._communication,
                agent_role=runtime.role_id,
                company_runtime=runtime,
                role_tool_tags=self._role_tool_tags(runtime.role_id),
                role_fs_scopes=self._role_fs_scopes(runtime.role_id),
                # 功能：无派发面的员工卡不投放管理者专属工具（board 巡检/改卡/删卡）
                manager_capable=self._role_manager_capable(runtime.role_id),
                # spawn_thread_task 装配 deps——run_agent_turn 建 registry 后 bind_deps
                company_store=self._store,
                company_thread_manager=self._thread_manager,
                company_org_engine=self._org_engine,
                provider_config=provider_config,
                user_id=resolve_requester_user_id(work_item),
                stream_handler=stream_handler,
                user_interaction=user_interaction,
                # 员工工作区内写操作自动放行（fs/shell 守卫 + deny 规则兜底）
                relax_workspace_asks=True,
                # 任务级 MCP 服务器白名单（来自调度任务 payload → work_item metadata）
                mcp_allowed_servers=[
                    str(name)
                    for name in (work_item.metadata or {}).get("mcp_allowed_servers") or []
                    if str(name).strip()
                ]
                or None,
                approval_capture=approval_capture,
            )
        except Exception as exc:
            if stream_handler is not None:
                await stream_handler.on_error(runtime.member_session_id(), str(exc))
            raise
        finally:
            _reset_knowledge_scope(knowledge_scope_token)
            _reset_knowledge_employee(employee_scope_token)
        if stream_handler is not None:
            # 执行结束转发 done 帧（前端清过程槽依据；run_agent_turn 不代调 on_done）
            await stream_handler.on_done(
                runtime.member_session_id(), terminal_reason or "", total_tokens, turn_index
            )
        # 有旧输出时校验旧值——时滞 bug）。违规时 result.metadata 携带原始
        # output 供调度方保留真实产物。
        output = _strip_tool_markup(output)
        work_item.runtime_metadata["_last_output"] = output
        # 只写组织层元数据，不读写/不推进 skill 层帧（防污染）
        sop_progress = self._project_sop_progress(runtime, work_item)
        violation: dict[str, Any] | None = None
        try:
            report = validate_work_item_output(work_item)
        except Exception as exc:  # noqa: BLE001 — 校验器异常按违规处理（fail-closed）
            report = None
            violation = {
                "missing_keys": [],
                "format_errors": [f"输出契约校验器异常: {exc}"],
                "warnings": [],
            }
            logger.warning(
                "output contract validator crashed for work item %s: %s", work_item.id, exc
            )
            await self._emit_violation(work_item.id, violation)
        if report is not None and not report.valid:
            # 不得降级成成功；warnings（如无标题/长度提示）仅记录不阻断。
            violation = {
                "missing_keys": report.missing_keys,
                "format_errors": report.format_errors,
                "warnings": report.warnings,
            }
            logger.warning(
                "output contract violation for work item %s: %s",
                work_item.id,
                violation,
            )
            await self._emit_violation(work_item.id, violation)
        process_events = stream_handler.snapshot() if stream_handler is not None else []
        return SeatResult(
            success=(
                violation is None
                and (
                    terminal_reason
                    in (
                        TerminalReason.COMPLETED.name,
                        TerminalReason.VERIFIED.name,
                        None,
                    )
                    # 轮次用尽但已写出正文：按完成处理（不把有产出误判为失败）
                    or (
                        terminal_reason == TerminalReason.MAX_TURNS.name
                        and bool((output or "").strip())
                    )
                )
            ),
            output=(
                f"输出契约校验失败: {violation}"
                if violation is not None
                else output
                or self._artifact_summary(runtime)
                or f"（本轮未产出正文，任务：{work_item.title[:60]}）"
            ),
            metadata={
                "terminal_reason": terminal_reason,
                "total_tokens": total_tokens,
                "turn_index": turn_index,
                **({"process_events": process_events} if process_events else {}),
                **({"sop_progress": sop_progress} if sop_progress else {}),
                **({"output_contract_violation": violation} if violation is not None else {}),
                **({"_original_output": output} if violation is not None else {}),
                **(
                    {"output_contract_warnings": report.warnings}
                    if report is not None and report.warnings and violation is None
                    else {}
                ),
                **approval_metadata(approval_capture),
            },
        )

    async def checkpoint(self, runtime: CompanyRuntime) -> dict[str, Any]:
        return {"session": runtime.member_session_id(), "queue_size": runtime.queue_size()}

    async def interrupt(self, runtime: CompanyRuntime) -> None:
        self._active.pop(runtime.member_session_id(), None)

    async def shutdown(self) -> None:
        self._active.clear()
