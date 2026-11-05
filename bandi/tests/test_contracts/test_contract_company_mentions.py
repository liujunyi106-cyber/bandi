"""
契约族：群聊 @ 路由语义（Day 129）——已注册角色匹配 + 中文/大小写归一 + clean_message + 广播回退。

背景（技术债 / 架构 §38.6 / ADR-24 决策一）：已实现 @角色 → QUEUED work_item
但 `_MENTION_RE` 大小写敏感且不匹配中文角色名（`@Worker` 提取 "Worker" ≠ 注册 "worker"
→ 创建 assignee 悬空卡）。本契约钉 Day 129 强化后的行为族：

- `api/company_chat.py::extract_mentions`（正则解析唯一源）
- `api/company_chat.py::resolve_mentions`（提纯 → 已注册过滤 → MentionResult）
- `api/company_chat.py::handle_chat_message`（生产入口：用户消息落线程 → 命中角色建卡）
- `communication.py::send_dm` 回调 payload（WS 广播标注 route 类型）

断言「最终产物」（生产主链路：真实 store + 真实 OrgEngine）：
- 中文角色名 `@供应链分析师` 命中注册角色 → 建卡 assignee=注册角色；
- `@Worker` 大小写不敏感 → 归一化到注册小写 id（杜绝悬空卡）；
- `@未注册角色` → 不建卡（broadcast 回退，消息仍落盘广播）；
- `@all` / `@所有人` 已废弃 → 不建卡、不落盘；无 @ 的消息走默认首位/未命中回退；
- clean_message 去掉 @ 语法纯文本，落盘 work_item 用 clean 文本；
- 广播回调 payload 带 route 标注（user_to_agent / broadcast），供 面板消费。
"""

from __future__ import annotations

import pytest

from bandi.api.company_chat import handle_chat_message
from bandi.company.communication import CommunicationManager, list_inbox
from bandi.company.group_chat_protocol import MENTION_DEPRECATED, MentionError
from bandi.company.group_round import SlotStatus
from bandi.company.org_engine import OrgEngine
from bandi.company.store import WorkItemStore
from bandi.company.work_item import Phase

_MENTION_SUBJECT = "群聊路由契约"


def _mk_comm(
    tmp_path,
    org: OrgEngine | None,
    on_message_sent=None,
) -> CommunicationManager:
    return CommunicationManager(
        workspace=str(tmp_path / "ws"),
        project_id="p1",
        session_id="s1",
        org_engine=org,
        on_message_sent=on_message_sent,
    )


def _mk_store(tmp_path) -> WorkItemStore:
    return WorkItemStore(str(tmp_path / "wi.db"))


def _mk_org_with(*role_ids: str) -> OrgEngine:
    org = OrgEngine()
    for role_id in role_ids:
        org.register_role(role_id, role_id)
    return org


def test_contract_mention_chinese_role_registered(tmp_path) -> None:
    """中文角色名 @供应链分析师 命中注册角色 → 建卡 assignee=注册角色（生产主链路）。"""
    org = _mk_org_with("供应链分析师")
    comm = _mk_comm(tmp_path, org)
    store = _mk_store(tmp_path)

    created = handle_chat_message(
        content="请评估 @供应链分析师 库存数据",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert len(created) == 1, f"中文 @提及 必须建卡，实际 {len(created)}"
    assert created[0].assignee_role == "供应链分析师", (
        f"assignee 必须命中注册角色，实际 {created[0].assignee_role}"
    )
    assert created[0].phase == Phase.QUEUED
    assert created[0].description == "请评估 库存数据", "落盘描述必须用 clean_message（去 @ 语法）"


def test_contract_mention_case_insensitive_to_registered(tmp_path) -> None:
    """@Worker 大小写不敏感 → 归一化到注册小写 id（杜绝 assignee 悬空卡）。"""
    org = _mk_org_with("worker")
    comm = _mk_comm(tmp_path, org)
    store = _mk_store(tmp_path)

    created = handle_chat_message(
        content="麻烦 @Worker 处理一下",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert len(created) == 1, f"大小写偏移的 @提及 必须命中注册 id，实际 {len(created)}"
    assert created[0].assignee_role == "worker", (
        f"assignee 必须归一化到注册 id，实际 {created[0].assignee_role}"
    )


def test_contract_mention_unregistered_skips_work_item(tmp_path) -> None:
    """@未注册角色 → 不建卡（broadcast 回退），消息仍落盘 board 广播。"""
    org = _mk_org_with("worker")
    comm = _mk_comm(tmp_path, org)
    store = _mk_store(tmp_path)

    created = handle_chat_message(
        content="请 @ghost-role 处理一下",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert created == [], f"未注册角色的 @提及 不得建卡（悬空卡），实际 {created}"
    msgs = list_inbox(str(tmp_path / "ws"), "p1", "s1", "board")
    assert msgs, "未命中注册角色时消息必须仍落盘（广播回退不静默丢弃）"


def test_contract_deprecated_at_all_no_assignee(tmp_path) -> None:
    """@all 已废弃：抛结构化拒绝，不建卡、不落盘广播。"""
    org = _mk_org_with("worker")
    comm = _mk_comm(tmp_path, org)
    store = _mk_store(tmp_path)

    with pytest.raises(MentionError) as exc:
        handle_chat_message(
            content="【全体注意】@all 明天复盘",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
        )
    assert exc.value.code == MENTION_DEPRECATED
    assert store.list_all() == [], "@all 已废弃，不得创建 assignee 卡"
    msgs = list_inbox(str(tmp_path / "ws"), "p1", "s1", "board")
    assert not msgs, "@all 已废弃，不得落盘广播"


def test_contract_broadcast_mention_unmatched_is_broadcast(tmp_path) -> None:
    """含 @ 语法但全部未命中注册角色 → is_broadcast 回退（不建卡不静默丢）。"""
    org = _mk_org_with("worker")
    seen: list[dict] = []
    comm = _mk_comm(tmp_path, org, on_message_sent=seen.append)
    store = _mk_store(tmp_path)

    created = handle_chat_message(
        content="谁有空 @nobody-here 帮我看看",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert created == []
    assert seen, "广播回调必须触发（on_message_sent 接线点）"
    payload = seen[0]
    assert payload.get("metadata", {}).get("route") == "broadcast", (
        f"全部未命中 → 广播 route 标注，实际 {payload.get('metadata', {}).get('route')}"
    )


def test_contract_mention_route_marker_user_to_agent(tmp_path) -> None:
    """@已注册角色 → 广播 payload 标注 route=user_to_agent（供 面板区分）。"""
    org = _mk_org_with("worker")
    seen: list[dict] = []
    comm = _mk_comm(tmp_path, org, on_message_sent=seen.append)
    store = _mk_store(tmp_path)

    handle_chat_message(
        content="请 @worker 处理",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert seen
    payload = seen[0]
    assert payload.get("metadata", {}).get("route") == "user_to_agent", (
        f"命中注册角色 → user_to_agent 标注，实际 {payload.get('metadata', {}).get('route')}"
    )
    assert payload.get("metadata", {}).get("kind") == "chat", (
        "落盘 metadata 必须带 kind=chat（B1 不变式保持）"
    )


def test_contract_extract_mentions_chinese_and_boundary() -> None:
    """extract_mentions 解析族：中文角色名 + 边界不吞 @ 后句读 + 去重保序。"""
    from bandi.api.company_chat import extract_mentions

    assert extract_mentions("请 @供应链分析师 看看") == ["供应链分析师"]
    assert extract_mentions("hi @worker，详细见 @boss。谢谢") == ["worker", "boss"]
    assert extract_mentions("请 @worker @worker 处理") == ["worker"]
    assert extract_mentions("无提及") == []
    assert extract_mentions("邮箱 a@b.com 不是提及") == []


def test_contract_resolve_mentions_registered_filter() -> None:
    """resolve_mentions：仅保留注册角色内的 target + clean_message + is_broadcast。"""
    from bandi.api.company_chat import resolve_mentions

    registered = ["worker", "供应链分析师"]

    result = resolve_mentions("请 @Worker 和 @供应链分析师 一起办", registered)
    assert result.target_agents == ["worker", "供应链分析师"], (
        f"target 必须归一化到注册 id 且保序，实际 {result.target_agents}"
    )
    assert result.clean_message == "请 和 一起办", (
        f"clean_message 必须去掉 @ 语法且归一空格，实际 {result.clean_message!r}"
    )
    assert result.is_broadcast is False

    result2 = resolve_mentions("@nobody 处理", registered)
    assert result2.target_agents == []
    assert result2.is_broadcast is True, "全部未命中 → broadcast 回退"

    result3 = resolve_mentions("@all 全体注意", registered)
    assert result3.target_agents == []
    assert result3.is_broadcast is True, "@all → broadcast"

    result4 = resolve_mentions("普通消息无提及", registered)
    assert result4.target_agents == []
    assert result4.is_broadcast is True, "无 @ 命中 → broadcast 回退（对齐架构 §38.6）"


def test_contract_mention_keeps_registered_role_case(tmp_path) -> None:
    """注册角色 id 含大写 → assignee 保留注册原 id（防悬空卡反方向）。

    调度端 `claim(role_id)` 精确匹配（company_mode.py:413 无 lower 归一）
    若 assignee 被小写化则找不到座席 → 悬空卡依旧。的承诺是「归一保留注册 id」。
    """
    from bandi.api.company_chat import _registered_role_ids

    org = _mk_org_with("Worker")
    comm = _mk_comm(tmp_path, org)
    store = _mk_store(tmp_path)

    registered = sorted(_registered_role_ids(org))
    assert "Worker" in registered, "registered 必须保留注册原 id（含大写），实际 {registered}"

    created = handle_chat_message(
        content="请 @Worker 处理",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert len(created) == 1
    assert created[0].assignee_role == "Worker", (
        f"assignee 必须等于注册原 id 'Worker'（不能小写成 worker），实际 {created[0].assignee_role!r}"
    )

    created_lower = handle_chat_message(
        content="也请 @worker 看看",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        session_id="sess-case-2",
    )
    assert created_lower and created_lower[0].assignee_role == "Worker", (
        f"@worker（小写输入）同样须归一到注册原 id 'Worker'，实际 {created_lower[0].assignee_role!r}"
    )


def test_contract_extract_mentions_chinese_email_not_mentioned() -> None:
    """中文前缀邮箱不误提：@ 前是 字符的 xxx@b.com 形状不得提取。"""
    from bandi.api.company_chat import extract_mentions

    assert extract_mentions("请发信到 张三@b.com") == [], (
        "中文前缀邮箱 张三@b.com 不得被当作 @提及 提取"
    )
    assert extract_mentions("联系 李四@example.com 或 @worker") == ["worker"], (
        "中文前缀邮箱之外的真实 @提及 仍须提取"
    )


def test_contract_deprecated_at_all_wins_over_role_named_all(tmp_path) -> None:
    """@all 已废弃：即使注册了 id='all' 的角色，也抛结构化拒绝、不建卡、不落盘。"""
    org = _mk_org_with("all")
    comm = _mk_comm(tmp_path, org)
    store = _mk_store(tmp_path)

    with pytest.raises(MentionError) as exc:
        handle_chat_message(
            content="【重要】@all 明天复盘",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
        )
    assert exc.value.code == MENTION_DEPRECATED
    assert store.list_all() == [], "@all 已废弃，不得建卡给同名注册角色 'all'"
    msgs = list_inbox(str(tmp_path / "ws"), "p1", "s1", "board")
    assert not msgs, "@all 已废弃，不得落盘"


def test_contract_mention_at_prefix_role_not_broadcast(tmp_path) -> None:
    """`all` 前缀角色不被 @all 提权误伤（复杀二批）：@allhands 必须命中注册 id 建卡。

    破坏者反例：裸子串 `"@all" in content` 会把 @allhands/@allrounder 等
    all 前缀角色吞成广播 → 用户 @ 已注册角色却不建卡（任务无主）。
    """
    org = _mk_org_with("allhands")
    comm = _mk_comm(tmp_path, org)
    store = _mk_store(tmp_path)

    created = handle_chat_message(
        content="@allhands 汇总本日数据",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert len(created) == 1, f"@allhands（all 前缀角色）必须建卡，实际 {len(created)}"
    assert created[0].assignee_role == "allhands", (
        f"assignee 必须等于注册角色 allhands，实际 {created[0].assignee_role!r}"
    )


def test_contract_deprecated_at_all_chinese(tmp_path) -> None:
    """@所有人 已废弃：抛结构化拒绝，不建卡、不落盘、不广播。"""
    org = _mk_org_with("worker")
    seen: list[dict] = []
    comm = _mk_comm(tmp_path, org, on_message_sent=seen.append)
    store = _mk_store(tmp_path)

    with pytest.raises(MentionError) as exc:
        handle_chat_message(
            content="@所有人 下午三点上会",
            sender_role="user",
            communication=comm,
            store=store,
            org_engine=org,
        )
    assert exc.value.code == MENTION_DEPRECATED
    assert store.list_all() == [], "@所有人 已废弃，不得建卡"
    assert not seen, "@所有人 已废弃，不得触发广播回调"
    msgs = list_inbox(str(tmp_path / "ws"), "p1", "s1", "board")
    assert not msgs, "@所有人 已废弃，不得落盘"


def test_contract_mention_display_name_unique_hits(tmp_path) -> None:
    """@选品经理（唯一显示名）→ 命中注册角色 → 建卡 assignee=注册 id。"""
    org = OrgEngine()
    org.register_role("selection-team_selection_lead", "选品经理")
    comm = _mk_comm(tmp_path, org)
    store = _mk_store(tmp_path)

    created = handle_chat_message(
        content="请 @选品经理 评估这批货",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert len(created) == 1, f"@显示名必须命中注册角色，实际 {len(created)}"
    assert created[0].assignee_role == "selection-team_selection_lead", (
        f"assignee 必须等于注册 id，实际 {created[0].assignee_role!r}"
    )


def test_contract_mention_ambiguous_name_ignored(tmp_path) -> None:
    """两个角色同显示名 '选品经理' → @选品经理 忽略（宁可不建卡也不误路由），无其余命中 → 广播。

    注：若输入与某注册角色 id 同形（如 @CEO 对内置 id='ceo'），id 通道优先命中——
    歧义仅发生在 name-vs-name 冲突且不与任何 id 匹配时。
    """
    org = OrgEngine()
    org.register_role("t1:lead", "选品经理")
    org.register_role("t2:lead", "选品经理")
    comm = _mk_comm(tmp_path, org)
    store = _mk_store(tmp_path)

    created = handle_chat_message(
        content="@选品经理 下午开会",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert created == [], f"同名歧义不得误路由建卡，实际 {created}"


def test_contract_mention_id_wins_and_name_case_insensitive(tmp_path) -> None:
    """@boss 命中 id='boss'（即使另有角色显示名为 'boss'）；@dispatcher 命中显示名 'Dispatcher'。"""
    org = OrgEngine()
    org.register_role("boss", "领导")
    org.register_role("t:boss", "boss")  # 显示名 'boss' 与 id 'boss' 同形
    org.register_role("dispatcher", "Dispatcher")
    comm = _mk_comm(tmp_path, org)
    store = _mk_store(tmp_path)

    created = handle_chat_message(
        content="@boss 看一下",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
    )
    assert len(created) == 1
    assert created[0].assignee_role == "boss", (
        f"id 通道优先：assignee 应为 'boss'，实际 {created[0].assignee_role!r}"
    )

    created2 = handle_chat_message(
        content="@dispatcher 派个活",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        session_id="sess-id-2",
    )
    assert len(created2) == 1
    assert created2[0].assignee_role == "dispatcher", (
        f"显示名大小写不敏感：@dispatcher 应命中 'Dispatcher'，实际 {created2[0].assignee_role!r}"
    )


def test_contract_ambiguous_display_name_resolves_within_project_members(tmp_path) -> None:
    """契约：同名显示名跨团队共存时 @ 按当前项目成员消歧——成员内唯一 → 命中建卡
    且不再误报未注册；仍歧义（候选都在成员集合内）→ 不建卡不误路由。"""
    from bandi.api.company_chat import unresolved_mentions

    org = OrgEngine()
    org.register_role("teamA_lead", "产品经理")
    org.register_role("teamB_lead", "产品经理")
    comm = _mk_comm(tmp_path, org)
    store = _mk_store(tmp_path)

    created = handle_chat_message(
        content="请开始 @产品经理 排期",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        member_role_ids={"teamA_lead"},
    )
    assert len(created) == 1, f"项目成员内唯一必须命中建卡，实际 {created}"
    assert created[0].assignee_role == "teamA_lead"
    assert unresolved_mentions("请开始 @产品经理 排期", org, {"teamA_lead"}) == []

    created2 = handle_chat_message(
        content="再试 @产品经理 一次",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        member_role_ids={"teamA_lead", "teamB_lead"},
    )
    assert created2 == [], "歧义未消解时不得误路由"
    assert unresolved_mentions("再试 @产品经理 一次", org, {"teamA_lead", "teamB_lead"}) == [
        "产品经理"
    ]


def test_contract_group_burst_joins_current_round_without_duplicate_card(tmp_path) -> None:
    """契约：同会话连发消息进入当前回合；当前发言人未结束时不重复建卡，完成后
    再为排队位建下一张卡。"""
    from bandi.api.company_chat import _default_group_round_service
    from bandi.company.seat_executor import SeatResult

    org = _mk_org_with("worker-1")
    comm = _mk_comm(tmp_path, org)
    store = _mk_store(tmp_path)

    first = handle_chat_message(
        content="@worker-1 你好",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        session_id="sess-burst",
    )
    assert len(first) == 1, f"首次消息应建卡，实际 {first}"
    second = handle_chat_message(
        content="@worker-1 再来",
        sender_role="user",
        communication=comm,
        store=store,
        org_engine=org,
        session_id="sess-burst",
    )
    assert second == [], "当前发言人未结束时不重复建卡"
    service = _default_group_round_service(store, org)
    round_ = service.get_active("sess-burst")
    assert round_ is not None
    assert [slot.role_id for slot in round_.slots] == ["worker-1", "worker-1"]
    service.on_slot_finished(first[0], SeatResult(success=True, output="第一轮完成"))
    round_ = service.get_active("sess-burst")
    assert round_ is not None
    assert [slot.status for slot in round_.slots] == [
        SlotStatus.POSTED,
        SlotStatus.SPEAKING,
    ]
    next_card = store.get(round_.slots[1].work_item_id)
    assert next_card is not None
    assert next_card.id != first[0].id, "排队消息在第一轮完成后单独建卡"
