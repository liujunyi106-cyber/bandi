# 功能：自定义运行时——隔离装配 org runtime + Secretary 默认编排链

"""Custom/org 运行时入口。

自定义模式以 company 语义运行，但使用用户选择的组织配置。runner 拥有自定义
隔离边界：

- 主运行时的 org/dispatcher 对象不被修改；
- 自定义运行时使用自己的 OrgEngine / Recruiter / CommunicationManager /
  CompanyDispatcher 实例；
- store 与父运行时共享，使 UI session/task 状态保持在发起聊天的同一项目上下文。

message_bus 等价物，隔离运行时 = company 模块的独立装配
（meth:`CustomRuntimeRunner.build_org_runtime`）；``process_message`` 委托注入的
执行回调；无回调时走默认 Secretary 编排链（opc-collab 无回退链，Bandi 以
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bandi.company.communication import CommunicationManager
from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.company_runtime_profiles import (
    CompanyProfile,
    RoleProfile,
    get_builtin_profiles,
)
from bandi.company.external_seat_executor import ExternalSeatExecutor
from bandi.company.org_engine import OrgEngine
from bandi.company.recruiter import CompanyRecruiter, Recruiter
from bandi.company.reorg_manager import ReorgManager
from bandi.company.seat_executor import EngineSeatExecutor
from bandi.company.secretary import SecretaryService
from bandi.company.store import WorkItemStore
from bandi.company.talent_market import TalentMarket
from bandi.company.work_item import create_work_item, normalize_turn_type

_SECRETARY_RELATIVE_DIR = (".bandi", "company", "secretary")

_CREATED_WORK_ITEM_RE = re.compile(r"created work item `([^`]+)`")

# 自定义运行时默认 id 常量（RuntimeHost.project_id / resolved_org_id 回退值）
DEFAULT_PROJECT_ID = "default"
DEFAULT_ORG_ID = "custom"


def _extract_created_work_item_ids(applied_actions: list[str]) -> list[str]:
    """从 Secretary applied_actions 文本中提取 work item id（反引号间首个字段）。"""
    ids: list[str] = []
    for text in applied_actions:
        match = _CREATED_WORK_ITEM_RE.search(text)
        if match:
            ids.append(match.group(1).strip())
    return ids


@dataclass
class RuntimeHost:
    """父运行时上下文（Bandi 无 OPCEngine，用轻量 host 提供共享资源）。"""

    store: WorkItemStore
    project_id: str = DEFAULT_PROJECT_ID
    workspace: str = ""
    session_id: str = ""


@dataclass
class OrgRuntimeBundle:
    """隔离的自定义组织运行时装配结果。"""

    org_id: str
    org_engine: OrgEngine
    recruiter: Recruiter
    communication: CommunicationManager
    dispatcher: CompanyDispatcher
    reorg_manager: ReorgManager | None = None
    profile: CompanyProfile | None = None
    roles: list[RoleProfile] = field(default_factory=list)
    recruiter_planner: CompanyRecruiter | None = None


class CustomRuntimeRunner:
    """用自定义 org 配置跑 company 语义会话，隔离边界不污染主运行时。"""

    def __init__(self, parent: RuntimeHost) -> None:
        self.parent = parent
        # 同一 OrgEngine/Recruiter/Comms/Dispatcher 实例，角色审批/消息状态跨
        # 消息连续，避免每条消息重新装配（状态丢失 + 初始化成本）。
        self._bundle_cache: dict[tuple[str, str, str, str], OrgRuntimeBundle] = {}

    @staticmethod
    def _cache_key(
        org_id: str,
        project_id: str,
        session_id: str,
        profile: CompanyProfile | None,
        roles: list[RoleProfile],
    ) -> tuple[str, str, str, str, str]:
        role_ids = tuple(sorted(r.id for r in roles))
        return (org_id, project_id, session_id, profile.value if profile else "", role_ids)

    def _resolve_ids(
        self,
        project_id: str | None,
        session_id: str | None,
        workspace: str | None,
    ) -> tuple[str, str, str]:
        pid = (project_id or "").strip() or self.parent.project_id or "default"
        # 兜底 "default"：comms._safe_path_segment 拒绝空段，空宿主（如默认
        # RuntimeHost session_id=""）透传空串会延迟到 send 才崩——回落默认隔离目录
        sid = (session_id or "").strip() or self.parent.session_id or "default"
        ws = (workspace or "").strip() or self.parent.workspace
        return pid, sid, ws

    def invalidate(self, org_id: str = "") -> None:
        """失效并释放缓存中的运行时（reorg/员工变更/进程退出时调用）。"""
        keys = [k for k in self._bundle_cache if not org_id or k[0] == org_id]
        for key in keys:
            bundle = self._bundle_cache.pop(key, None)
            if bundle is not None:
                try:
                    # shutdown 注销 dispatcher wake 回调——不注销则回调列表单调
                    # 增长（长驻进程泄漏 + 唤醒已失效实例）
                    bundle.dispatcher.shutdown()
                except Exception:  # noqa: BLE001 — 释放失败不阻断失效
                    pass
                try:
                    bundle.dispatcher.release_all()
                except Exception:  # noqa: BLE001 — 释放失败不阻断失效
                    pass

    def shutdown(self) -> None:
        """释放全部缓存运行时（进程退出）。"""
        self.invalidate()

    def build_org_runtime(
        self,
        *,
        roles: list[RoleProfile],
        profile: CompanyProfile | None = None,
        org_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        workspace: str | None = None,
        talent_market: TalentMarket | None = None,
    ) -> OrgRuntimeBundle:
        """从 org 配置装配隔离运行时（OrgEngine/Recruiter/Comms/Dispatcher/RecruiterPlanner）。

        同 key 二次调用返回缓存的既有实例（内存状态连续）；无缓存才新建。
        """
        resolved_org_id = (org_id or "").strip() or DEFAULT_ORG_ID
        pid, sid, ws = self._resolve_ids(project_id, session_id, workspace)
        key = self._cache_key(resolved_org_id, pid, sid, profile, roles)
        cached = self._bundle_cache.get(key)
        if cached is not None:
            # 场景进程 A 崩溃遗留 stale 卡，进程 B 复用缓存时无人恢复会滞留。
            try:
                cached.dispatcher.recover_stale_claims()
            except Exception:  # noqa: BLE001 — 恢复失败不阻断复用
                pass
            return cached

        org = OrgEngine()
        for role in roles:
            org.register_role(
                role_id=role.id,
                name=role.name,
                responsibility=role.responsibility,
                reports_to=role.reports_to,
                can_spawn=list(role.can_spawn),
            )

        recruiter = Recruiter()
        for r in org.list_roles():
            recruiter.register_seat(r.id)

        resolved_talent_market = talent_market if talent_market is not None else TalentMarket()
        resolved_talent_market.bind_org_engine(org)
        planner = CompanyRecruiter(
            llm=None,
            org_engine=org,
            talent_market=resolved_talent_market,
        )

        pid, sid, ws = self._resolve_ids(project_id, session_id, workspace)
        communication = CommunicationManager(
            workspace=ws, project_id=pid, session_id=sid, org_engine=org
        )
        # 线程容器装配——自定义运行时独立 workspace/project/session 隔离线程空间
        from bandi.company.discussion_thread import ThreadManager

        thread_manager = ThreadManager(workspace=ws, project_id=pid, session_id=sid)
        thread_manager.hydrate()

        reorg_manager = ReorgManager(
            store=self.parent.store,
            org_engine=org,
            communication=communication,
        )
        communication.task_adjustment_suggester = reorg_manager.suggest_task_adjustment

        runtime_config = get_builtin_profiles().get(profile or CompanyProfile.CUSTOM)
        # 装配 seat_executor——否则 secretary 链创建的工作项调度时
        # run_work_item 恒返回 "No seat executor configured"，自定义运行时
        # 无法真正执行任何工作项。event_bus 使输出契约违规事件可观测。
        from bandi.core.event_bus import EventBus

        dispatcher = CompanyDispatcher(
            store=self.parent.store,
            recruiter=recruiter,
            org_engine=org,
            communication=communication,
            reorg_manager=reorg_manager,
            seat_executor=EngineSeatExecutor(
                communication=communication,
                org_engine=org,
                event_bus=EventBus(),
                store=self.parent.store,
                thread_manager=thread_manager,
            ),
            # 装配：外部引擎一次性执行路径（真外聘 ）——
            # 选中 assigned_external_agent 的卡由此执行器消费
            external_seat_executor=ExternalSeatExecutor(),
            config=DispatcherConfig(
                max_concurrent=runtime_config.max_concurrent_work_items,
                enable_auto_process=runtime_config.allow_auto_process,
            ),
            thread_manager=thread_manager,
            runtime_owner=f"custom:{resolved_org_id}",
        )
        # 崩溃恢复：与 CLI/API 启动链路一致，首次 tick 前把 store 中
        # 崩溃遗留的 in-flight 卡转 READY（共享 store 语义下必须恢复
        # 否则自定义 runtime 装配的调度器永远不认领遗留卡）
        dispatcher.recover_stale_claims()

        bundle = OrgRuntimeBundle(
            org_id=resolved_org_id,
            org_engine=org,
            recruiter=recruiter,
            communication=communication,
            dispatcher=dispatcher,
            reorg_manager=reorg_manager,
            profile=profile,
            roles=list(roles),
            recruiter_planner=planner,
        )
        self._bundle_cache[key] = bundle
        return bundle

    async def process_message(
        self,
        content: str,
        *,
        roles: list[RoleProfile],
        profile: CompanyProfile | None = None,
        org_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        workspace: str | None = None,
        llm: Any | None = None,
        executor: Callable[[OrgRuntimeBundle, str], Awaitable[str]] | None = None,
    ) -> str:
        """装配/复用隔离运行时并委托执行回调。运行时经缓存长生命周期复用
        不再每消息 release_all——共享 dispatcher 座位跨消息保留；显式失效走
        invalidate()/shutdown()。

        无 executor 时走默认链：Secretary 编排（创建 work item → 调度执行）
        返回 Secretary 回复；未创建 work item 时返回回复本身。
        """
        bundle = self.build_org_runtime(
            roles=roles,
            profile=profile,
            org_id=org_id,
            project_id=project_id,
            session_id=session_id,
            workspace=workspace,
        )
        pid, sid, ws = self._resolve_ids(project_id, session_id, workspace)
        if executor is not None:
            return await executor(bundle, content)
        return await self._secretary_chain(
            bundle, content, llm=llm, workspace=ws, project_id=pid, session_id=sid
        )

    async def _secretary_chain(
        self,
        bundle: OrgRuntimeBundle,
        content: str,
        *,
        llm: Any | None,
        workspace: str,
        project_id: str,
        session_id: str,
    ) -> str:
        """默认编排链：Secretary 解析意图，创建 work item 后交给调度器执行。"""
        base_dir = Path(workspace or ".").joinpath(*_SECRETARY_RELATIVE_DIR)
        # 草案经 confirm_org_build 显式确认后落地到该组织（None = fail-closed skip）；
        # 角色席位同步注册 + 员工能力档案与 CLI/API 口径一致建档
        from bandi.company.agent_profile import AgentProfileManager

        secretary = SecretaryService(
            base_dir=str(base_dir),
            org_engine=bundle.org_engine,
            recruiter=bundle.recruiter,
            profile_manager=AgentProfileManager(),
        )

        def create_from_payload(payload: dict[str, Any]) -> str:
            item = create_work_item(
                title=payload["title"],
                turn_type=normalize_turn_type(payload.get("turn_type") or "execute"),
                description=payload.get("description") or "",
                assignee_role=payload.get("assignee_role") or None,
                metadata={"runtime_owner": bundle.dispatcher.runtime_owner},
            )
            bundle.dispatcher.store.save(item)
            return item.id

        reply = await secretary.handle_message(
            content,
            project_id=project_id,
            session_id=session_id,
            llm=llm,
            on_create_work_item=create_from_payload,
        )
        summary = reply["response"]
        created = _extract_created_work_item_ids(reply["applied_actions"])
        if created:
            await bundle.dispatcher.tick()
            summary += f"\n[created work item(s): {', '.join(created)}]"
        return summary
