"""功能：Chat-First 协作面板（+ Day 129 + ）——@mention 路由 + 消息处理。

@mention 路由的唯一生产源（修复后，communication.py 的旧 route_mentions 已删除）：
- `extract_mentions`：解析 @ 语法（去重保序，支持中文/大小写，排除邮箱）
- `resolve_mentions`：提取 + 已注册角色过滤 → MentionResult（target/clean/broadcast）；
  双通道——id 精确匹配优先，显示名匹配（唯一名才收录，同名歧义忽略）兜底
  （命名空间前缀角色 `team:role` 无法被 @ 正则解析的补偿通道）
- `handle_chat_message`：群聊消息处理（落盘 + 广播 route 标注 + 命中角色建 QUEUED 卡）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bandi.company.group_chat_protocol import (
    MENTION_DEPRECATED,
    MENTION_NOT_IN_PROJECT,
    MentionError,
)
from bandi.company.work_item import Phase, WorkItem, WorkItemTurnType, create_work_item

_MENTION_RE = re.compile(r"(?<![\w.+-])@([\w][\w-]*)")
_SUBJECT_MAX_CHARS = 60
_TITLE_MAX_CHARS = 80


@dataclass
class MentionResult:
    target_agents: list[str]
    clean_message: str
    is_broadcast: bool
    thread_id: str | None = None


def extract_mentions(content: str) -> list[str]:
    """提取消息中的 @角色 提及（去重保序，含中文角色名）。

    与 communication.extract_mentions 仅一份实现（旧 双源已删除）。
    邮箱 a@b.com 不会被误解析——提及必须 @ 后紧跟词字符。
    """
    seen: list[str] = []
    for m in _MENTION_RE.finditer(content or ""):
        role = m.group(1)
        if role not in seen:
            seen.append(role)
    return seen


def _registered_role_ids(org_engine: Any | None) -> set[str]:
    """收集全部已注册角色 id（保留注册原大小写）。

    大小写不敏感匹配由 `resolve_mentions` 内部负责——此处若提前小写化
    注册 id 含大写时 canonical 恒为小写，与调度端精确匹配（company_mode.claim）
    不一致 → assignee 悬空卡反方向复现。
    """
    if org_engine is None:
        return set()
    role_ids: set[str] = set()
    for role in org_engine.list_roles():
        role_ids.add(str(role.id))
    return role_ids


def _registered_name_map(org_engine: Any | None) -> dict[str, str]:
    """功能：显示名（lower）→ 注册角色 id 映射——仅唯一名收录，同名歧义不参与匹配。"""
    if org_engine is None:
        return {}
    name_to_ids: dict[str, list[str]] = {}
    for role in org_engine.list_roles():
        display = str(getattr(role, "name", "") or "").strip()
        if not display:
            continue
        name_to_ids.setdefault(display.lower(), []).append(str(role.id))
    return {name: ids[0] for name, ids in name_to_ids.items() if len(ids) == 1}


def _mention_name_map(
    org_engine: Any | None, member_role_ids: set[str] | None = None
) -> dict[str, str]:
    """功能：@ 显示名映射——全局唯一名直用；歧义名在项目成员内唯一时收敛到该成员
    （同名「产品经理」跨团队共存时按当前项目成员消歧；仍歧义不收录=宁可不路由不误路由）。"""
    if org_engine is None:
        return {}
    name_to_ids: dict[str, set[str]] = {}
    for role in org_engine.list_roles():
        display = str(getattr(role, "name", "") or "").strip()
        if not display:
            continue
        name_to_ids.setdefault(display.lower(), set()).add(str(role.id))
    list_employees = getattr(org_engine, "list_employees", None)
    if callable(list_employees):
        for employee in list_employees():
            display = str(getattr(employee, "name", "") or "").strip()
            role_id = str(getattr(employee, "role_id", "") or "").strip()
            if not display or not role_id:
                continue
            name_to_ids.setdefault(display.lower(), set()).add(role_id)
    members = {str(item).lower() for item in (member_role_ids or set())}
    resolved: dict[str, str] = {}
    for name, ids in name_to_ids.items():
        if len(ids) == 1:
            resolved[name] = next(iter(ids))
            continue
        if members:
            member_hits = [rid for rid in ids if rid.lower() in members]
            if len(member_hits) == 1:
                resolved[name] = member_hits[0]
    return resolved


def _display_name_matches(content: str, name_map: dict[str, str]) -> list[tuple[str, str]]:
    """功能：按注册显示名匹配 @提及（支持含空格/中文的显示名，如「UI 设计」），长名优先。

    @ 语法正则无法解析带空格的显示名（@UI 设计 → 仅提取 UI）——本通道
    以注册显示名整体匹配补位；同名歧义不在 name_map 内（宁可不路由不误路由）。
    """
    lowered = (content or "").lower()
    squashed = re.sub(r"\s+", "", lowered)
    matched: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for name in sorted(name_map, key=len, reverse=True):
        compact = re.sub(r"\s+", "", name)
        if not compact or f"@{compact}" not in squashed:
            continue
        if re.fullmatch(r"[\w-]+", name):
            # 可被 @ 正则直接解析的显示名走既有通道（id 精确优先，不抢同名）
            continue
        role_id = name_map[name]
        if role_id.lower() in seen_ids:
            continue
        seen_ids.add(role_id.lower())
        matched.append((name, role_id))
    return matched


def resolve_mentions(
    content: str,
    registered: list[str],
    names: dict[str, str] | None = None,
) -> MentionResult:
    """解析 @ 提及并过滤仅注册角色。

    - target_agents：命中的注册角色 id（大小写不敏感，归一保留注册 id；
      含显示名整体匹配通道——支持「UI 设计」这类带空格显示名）；
    - clean_message：去掉 @ 语法与已匹配显示名后的纯文本；
    - is_broadcast：全部未命中注册角色时为 True；`@all` / `@所有人` 已废弃，
      按普通未注册提及处理，不再触发广播。
    - names：显示名（lower）→ 角色 id 第二通道——id 未命中时兜底；
      同名歧义不在映射内（宁可不建卡也不误路由）。
    """
    name_map = {key.lower(): value for key, value in (names or {}).items()}
    display_matches = _display_name_matches(content, name_map)
    scrubbed = content or ""
    for name, _role_id in display_matches:
        pattern = "@" + r"\s*".join(re.escape(char) for char in name)
        scrubbed = re.sub(pattern, "", scrubbed, flags=re.IGNORECASE)
    mentions = extract_mentions(content)
    lower_registered: dict[str, str] = {}
    for r in registered:
        lower_registered.setdefault(r.lower(), r)
    seen: set[str] = set()
    targets: list[str] = []
    for _name, role_id in display_matches:
        if role_id.lower() in seen:
            continue
        seen.add(role_id.lower())
        targets.append(role_id)
    for mention in mentions:
        if mention.lower() in {"all", "所有人"}:
            continue
        canonical = lower_registered.get(mention.lower()) or name_map.get(mention.lower())
        if canonical is None or canonical.lower() in seen:
            continue
        seen.add(canonical.lower())
        targets.append(canonical)
    clean_parts = [part for part in (_MENTION_RE.sub("", scrubbed).split(" ")) if part.strip()]
    clean = " ".join(clean_parts)
    is_broadcast = not targets
    return MentionResult(target_agents=targets, clean_message=clean, is_broadcast=is_broadcast)


def _resolve_registered_role(target_role: str, org_engine: Any | None) -> str:
    """功能：单聊目标归一为注册角色 id（id 精确优先，显示名唯一兜底）；未注册 fail-closed。"""
    wanted = str(target_role or "").strip().lower()
    if not wanted:
        raise ValueError("target_role is empty")
    lower_map = {role.lower(): role for role in _registered_role_ids(org_engine)}
    canonical = lower_map.get(wanted)
    if canonical is None:
        name_map = {key.lower(): value for key, value in _registered_name_map(org_engine).items()}
        canonical = name_map.get(wanted)
    if canonical is None and org_engine is not None:
        for employee in org_engine.list_employees():
            if wanted in {
                str(getattr(employee, "employee_id", "")).lower(),
                str(getattr(employee, "name", "")).lower(),
            }:
                role_id = str(getattr(employee, "role_id", "") or "").strip()
                if role_id:
                    canonical = role_id
                    break
    if canonical is None:
        raise ValueError(f"target_role not registered: {target_role}")
    return canonical


def unresolved_mentions(
    content: str,
    org_engine: Any | None,
    member_role_ids: set[str] | None = None,
) -> list[str]:
    """功能：@ 提及中未注册的角色清单——提示用，不路由。

    带空格显示名（@UI 设计 → 正则仅提取 UI）按其注册显示名前缀视为已路由，
    不进入未注册提示；显示名映射与路由同源（含项目成员内消歧）。
    `@all` / `@所有人` 已废弃，按未注册提及提示。
    """
    mentions = extract_mentions(content)
    if not mentions:
        return []
    registered = {role.lower() for role in _registered_role_ids(org_engine)}
    names = {name.lower() for name in _mention_name_map(org_engine, member_role_ids)}
    unresolved: list[str] = []
    for mention in mentions:
        lowered = mention.lower()
        if lowered in {"all", "所有人"}:
            unresolved.append(mention)
            continue
        if lowered in registered or lowered in names:
            continue
        if any(name.startswith(lowered) for name in names):
            continue
        unresolved.append(mention)
    return unresolved


def _handle_user_rework(
    *,
    content: str,
    sender_role: str,
    store: Any,
    target_work_item_id: str,
    communication: Any = None,
    thread_manager: Any | None = None,
    thread_id: str | None = None,
    project_id: str = "",
) -> list[WorkItem]:
    """功能：用户对话修正「某次已派发任务」——不新建卡，受控回转原卡并
    把修正指令写进其元数据（执行侧 brief 注入），线程消息照落以保持对话连续。

    对标 OpenOPC `_prepare_company_followup_target`：修正指令落 `latest_user_directive`
    等价键（我们写 `user_rework_directive` + 复用 `rework_feedback` 注入通道）。

    仅支持已 的卡（`reopen_approved_work_item_for_rework` fail-closed 抛
    `InvalidPhaseTransitionError`）；执行中/待执行的卡由调用方按错误帧回给用户。
    """
    from bandi.company.user_rework import prepare_user_rework_target

    item = store.get(target_work_item_id)
    if item is None:
        raise ValueError(f"work item '{target_work_item_id}' not found")
    # 「线程里已有修正消息、卡却没回转」的半提交（回转入口同样 fail-closed，双保险）
    from bandi.company.phase import InvalidPhaseTransitionError, Phase

    if item.phase != Phase.APPROVED:
        raise InvalidPhaseTransitionError(item.phase, Phase.READY_FOR_REWORK)

    active_thread_id = thread_id
    if thread_manager is not None:
        if active_thread_id:
            if thread_manager.get_thread(active_thread_id) is None:
                raise ValueError(f"thread '{active_thread_id}' not found")
        else:
            active_thread_id = thread_manager.create_thread(
                participants=[sender_role, item.assignee_role or "board"]
            ).thread_id
        thread_manager.add_message(active_thread_id, sender_role, content, message_id="chat")

    if communication is not None:
        extra_metadata: dict[str, Any] = {
            "kind": "chat",
            "route": "user_rework",
            "target_work_item_id": target_work_item_id,
        }
        if active_thread_id:
            extra_metadata["thread_id"] = active_thread_id
        if project_id:
            extra_metadata["project_id"] = project_id
        communication.send_dm(
            sender_role=sender_role,
            recipient_role=item.assignee_role or "board",
            subject=f"返工要求：{item.title}"[:_SUBJECT_MAX_CHARS],
            body=content,
            extra_metadata=extra_metadata,
        )

    reopened = prepare_user_rework_target(item, content, store)
    return [reopened] if reopened is not None else []


def _find_queued_chat_card(store: Any, *, role_id: str, session_id: str) -> Any | None:
    """功能：同会话同员工的排队中聊天卡（连发合并目标；运行中/终态不复用）。"""
    from bandi.company.phase import Phase

    list_all = getattr(store, "list_all", None)
    if list_all is None:
        return None
    try:
        items = list_all()
    except Exception:  # noqa: BLE001 — 查询失败按无命中（照常建新卡）
        return None
    for item in items:
        meta = getattr(item, "metadata", None) or {}
        if str(getattr(item, "assignee_role", "") or "") != role_id:
            continue
        if getattr(item, "phase", None) != Phase.QUEUED:
            continue
        if str(meta.get("source") or "") not in ("chat_mention", "chat_dm"):
            continue
        if str(meta.get("origin_session_id") or "") != session_id:
            continue
        return item
    return None


_DEFAULT_ROUND_SERVICES: dict[tuple[str, int], Any] = {}


def _round_mention_resolver(org_engine: Any | None) -> Any:
    """功能：群聊接力提及解析——复用注册角色与显示名映射，限定本轮成员。"""

    def _resolve(text: str, members: list[str]) -> list[str]:
        registered = sorted(_registered_role_ids(org_engine))
        result = resolve_mentions(text, registered, _mention_name_map(org_engine, set(members)))
        allowed = {str(member).lower() for member in members}
        return [
            target for target in result.target_agents if not allowed or target.lower() in allowed
        ]

    return _resolve


def _default_group_round_service(store: Any, org_engine: Any | None) -> Any:
    """功能：无显式装配时的默认回合服务（同一 store/org 复用，测试与 CLI 直接调用）。"""
    db_path = str(getattr(store, "db_path", "") or "")
    if not db_path:
        raise ValueError("group round requires a WorkItemStore with db_path")
    key = (db_path, id(org_engine))
    service = _DEFAULT_ROUND_SERVICES.get(key)
    if service is None:
        from bandi.company.group_round import GroupRoundService
        from bandi.company.group_round_store import GroupRoundStore

        service = GroupRoundService(
            GroupRoundStore(db_path),
            store,
            org_engine,
            mention_resolver=_round_mention_resolver(org_engine),
        )
        _DEFAULT_ROUND_SERVICES[key] = service
    return service


def handle_chat_message(
    *,
    content: str,
    sender_role: str,
    communication: Any,
    store: Any,
    org_engine: Any | None = None,
    send_dm: bool = True,
    thread_manager: Any | None = None,
    thread_id: str | None = None,
    target_role: str | None = None,
    project_id: str = "",
    target_work_item_id: str = "",
    session_id: str = "",
    mode: str = "",
    default_role: str | None = None,
    extra_targets: list[str] | None = None,
    member_role_ids: set[str] | None = None,
    intent: str = "task",
    group_round_service: Any | None = None,
) -> list[WorkItem]:
    """处理群聊/单聊消息（落盘 + route 标注 + 建 QUEUED work_item）。

    语义（Day 129，修复；单聊；默认承接）：
    - 群聊：仅已注册角色命中的 @提及 建卡；含 @ 语法但全未命中 → broadcast 回退
      （不建卡不静默丢，WS 层另发未注册提示）。
    - 群聊 fail-closed：`@all` / `@所有人` 抛 `MENTION_DEPRECATED`；提及了
      全局注册但不在本项目的角色抛 `MENTION_NOT_IN_PROJECT`，均不落 board、
      不建线程、不建卡。
    - 群聊建卡走 `GroupRoundService`：懒创建当前发言人卡，完成后再建下一位；
      `group_round_service` 由 API 装配（广播帧），未装配时按 store/org 复用默认服务。
    - 无 @ 消息：`extra_targets`（内容级路由结果）并入目标；仍无目标时
      `default_role`（团队顶部角色）承接（route=default_route）；无团队则广播。
    - 单聊（target_role 非空）：目标归一为注册角色后直接建卡（不解析 @），
      DM 落目标 inbox（非 board），线程 participants=[user, target]；
      未注册目标 fail-closed 拒绝（不建卡不落盘）。
    - 无 org_engine 时无角色可核对 → 按广播处理（不建卡），避免静默悬空；
    - send_dm 失败（权限/IO）按 抛异常显式暴露，不吞后建卡。
    - thread_manager 存在且命中角色时：带 thread_id 则追加既有线程；未带则按
      9.12 归并（find_thread：参与者集合一致的最新线程 → 追加，找不到才新建）
      连续 @/单聊 同角色集合的消息归入同一对话链。
    """
    content = (content or "").strip()
    if not content:
        return []
    # 带目标卡的对话 = 修正已派发任务（不新建卡，受控回转原卡）
    if str(target_work_item_id or "").strip():
        return _handle_user_rework(
            content=content,
            sender_role=sender_role,
            store=store,
            target_work_item_id=str(target_work_item_id).strip(),
            communication=communication if send_dm else None,
            thread_manager=thread_manager,
            thread_id=thread_id,
            project_id=project_id,
        )
    if target_role:
        canonical = _resolve_registered_role(target_role, org_engine)
        result = MentionResult(target_agents=[canonical], clean_message=content, is_broadcast=False)
        route = "user_to_agent"
    else:
        deprecated = [m for m in extract_mentions(content) if m.lower() in {"all", "所有人"}]
        if deprecated:
            raise MentionError(MENTION_DEPRECATED, deprecated)
        registered = sorted(_registered_role_ids(org_engine))
        result = resolve_mentions(
            content, registered, _mention_name_map(org_engine, member_role_ids)
        )
        resolved_targets = list(result.target_agents)
        if member_role_ids:
            allowed = {str(role).lower() for role in member_role_ids}
            result.target_agents = [
                target for target in result.target_agents if target.lower() in allowed
            ]
            out_of_scope = [target for target in resolved_targets if target.lower() not in allowed]
            if out_of_scope:
                raise MentionError(MENTION_NOT_IN_PROJECT, out_of_scope)
            result.is_broadcast = not result.target_agents
        route = "user_to_agent" if result.target_agents else "broadcast"
        # 无 @ 消息的兜底路由：内容级目标并入；仍无目标由默认角色（团队顶部）承接
        if not extract_mentions(content):
            registered_lower = {r.lower() for r in registered}
            for extra in extra_targets or []:
                key = str(extra or "").strip()
                if (
                    key
                    and key.lower() in registered_lower
                    and key.lower() not in {t.lower() for t in result.target_agents}
                ):
                    result.target_agents.append(key)
            if result.target_agents:
                result.is_broadcast = False
                route = "user_to_agent"
            elif str(default_role or "").strip():
                try:
                    canonical = _resolve_registered_role(str(default_role), org_engine)
                except ValueError:
                    canonical = ""
                if canonical:
                    result.target_agents = [canonical]
                    result.is_broadcast = False
                    route = "default_route"
    active_thread_id: str | None = None
    # 无 thread_id 时维持「命中角色才建/并线程」旧语义（纯广播不建线程）
    if thread_manager is not None and (thread_id or result.target_agents):
        participants = (
            [sender_role, *result.target_agents] if target_role else [*result.target_agents]
        )
        if thread_id:
            # 契约（fail-closed）：thread_id 必须已存在于 ThreadManager——前端伪造/过期帧
            # 不能进入"追加/绑卡"分支（否则卡绑死线程，产出回写静默丢失）。与
            # spawn_thread_task 对 thread_id 的校验一致。无效 id 抛错，WS 层回 error 帧。
            if thread_manager.get_thread(thread_id) is None:
                raise ValueError(f"thread '{thread_id}' not found")
            active_thread_id = thread_id
        else:
            # 群聊窗口：会话 ↔ 线程一对一（删除会话即删线程；不复用其他会话的旧线程）；
            # 无会话来源（CLI/广播）与单聊仍按参与者归并（单聊每位员工一条线程）。
            is_group = not target_role
            if session_id and (is_group or target_role):
                existing = thread_manager.find_by_session(session_id)
                if existing is None:
                    existing = thread_manager.create_thread(
                        participants=participants,
                        session_id=session_id,
                        kind="dm" if target_role else "group",
                    )
                active_thread_id = existing.thread_id
            else:
                existing = thread_manager.find_thread(participants)
                if existing is not None:
                    active_thread_id = existing.thread_id
                else:
                    active_thread_id = thread_manager.create_thread(
                        participants=participants,
                        kind="dm" if target_role else "group",
                    ).thread_id
        thread_manager.add_message(
            active_thread_id,
            sender_role,
            content,
            mentions=result.target_agents,
            message_id="chat",
        )
        result.thread_id = active_thread_id
    if send_dm:
        extra_metadata: dict[str, Any] = {"kind": "chat", "route": route}
        if active_thread_id:
            extra_metadata["thread_id"] = active_thread_id
        recipient = result.target_agents[0] if target_role else "board"
        if target_role:
            extra_metadata["target_role"] = recipient
        # 先落盘再建卡；落盘失败抛异常，禁止「有任务无消息」
        communication.send_dm(
            sender_role=sender_role,
            recipient_role=recipient,
            subject=content[:_SUBJECT_MAX_CHARS],
            body=content,
            extra_metadata=extra_metadata,
        )
    if target_role:
        created: list[WorkItem] = []
        for role_id in result.target_agents:
            if session_id:
                existing = _find_queued_chat_card(store, role_id=role_id, session_id=session_id)
                if existing is not None:
                    existing.title = content[:_TITLE_MAX_CHARS]
                    existing.description = result.clean_message
                    if active_thread_id:
                        existing.metadata["thread_id"] = active_thread_id
                        existing.metadata["window_id"] = active_thread_id
                    store.save(existing)
                    created.append(existing)
                    continue
            metadata: dict[str, Any] = {
                "source": "chat_dm",
                "mentioned_by": sender_role,
            }
            if str(mode).strip():
                metadata["created_mode"] = str(mode).strip().lower()
            if intent in ("query", "confirm", "adjust"):
                metadata["chat_kind"] = intent
            if project_id:
                metadata["project_id"] = project_id
            if session_id:
                metadata["origin_session_id"] = session_id
                metadata["dm_chat"] = True
                metadata["dm_conversation_id"] = session_id
            metadata["target_role"] = role_id
            item = create_work_item(
                title=content[:_TITLE_MAX_CHARS],
                turn_type=(
                    WorkItemTurnType.INTAKE
                    if intent == "confirm"
                    else WorkItemTurnType.PLAN
                    if str(mode).strip().lower() == "plan"
                    else WorkItemTurnType.EXECUTE
                ),
                description=result.clean_message,
                assignee_role=role_id,
                metadata=metadata,
                phase=Phase.QUEUED,
            )
            if active_thread_id:
                item.metadata["thread_id"] = active_thread_id
                item.metadata["window_id"] = active_thread_id
            store.save(item)
            created.append(item)
        return created
    service = group_round_service or _default_group_round_service(store, org_engine)
    return service.submit_user_message(
        content=result.clean_message,
        sender_role=sender_role,
        targets=result.target_agents,
        session_id=session_id,
        project_id=project_id,
        member_role_ids=member_role_ids,
        thread_id=active_thread_id or "",
        mode=mode,
        intent=intent,
        default_role=default_role or "",
    )
