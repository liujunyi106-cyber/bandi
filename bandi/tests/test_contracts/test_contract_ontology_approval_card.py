"""契约：本体式写入审批分流 + 草稿结构化展示（2026-10-14 用户拍板批）

覆盖：
- 结构化写入工具（ontology_define/ontology_assert/ontology_retract/memory_assert）
  审批走权限申请卡片：卡片 prompt 中文、detail 说明白「调用什么工具、提取什么内容」
- 权限卡三态（仅本次允许 / 本会话始终允许 / 拒绝）对上述工具不变
- memory_assert 纳入 ask 名单（pipeline 与 config 双单源一致）
- 批准后写本体：零草稿（memory pending 队列零新增）+ 零长期记忆
- 批准后 memory_assert 直写长期记忆：不进草稿
- 草稿结构化：摘要 kind 前缀中文化（[事实] 而非 [fact]）；暂存记录携带
  draft 元数据（草稿类型 + 写入目标），供前端多标签分行展示

破坏者断言：缺失参数说明不得渲染成空 detail；未入 ask 名单的工具不产生卡片说明。
"""

import json
from typing import Any

import pytest

# ── 权限卡工具意图说明 ─────────────────────────────────────────────────────


def test_describe_ontology_assert_intent_lists_facts() -> None:
    """契约：ontology_assert 卡片说明须含工具用途与提取的事实内容。"""
    from bandi.tools.approval import describe_tool_intent

    detail = describe_tool_intent(
        "ontology_assert",
        {
            "entities": [{"id": "寰宇控股", "classes": ["公司"]}],
            "facts": [{"subject": "寰宇控股", "predicate": "invests_in", "object": "星桥资本"}],
        },
    )
    assert "ontology_assert" in detail, f"说明须点名工具：{detail}"
    assert "本体" in detail, f"说明须写明写入目标是本体库：{detail}"
    assert "寰宇控股" in detail and "星桥资本" in detail, f"说明须列出提取内容：{detail}"
    assert "invests_in" in detail, f"说明须列出关系谓词：{detail}"


def test_describe_ontology_define_intent_lists_vocabulary() -> None:
    """契约：ontology_define 卡片说明须列出要声明的类与关系。"""
    from bandi.tools.approval import describe_tool_intent

    detail = describe_tool_intent(
        "ontology_define",
        {
            "classes": [{"id": "公司"}, {"id": "基金"}],
            "relations": [{"id": "invests_in"}],
        },
    )
    assert "ontology_define" in detail
    assert "公司" in detail and "基金" in detail and "invests_in" in detail


def test_describe_ontology_retract_intent_lists_targets() -> None:
    """契约：ontology_retract 卡片说明须点名撤回目标（破坏性操作可审计）。"""
    from bandi.tools.approval import describe_tool_intent

    detail = describe_tool_intent(
        "ontology_retract",
        {"entities": ["星桥资本"], "facts": [{"subject": "a", "predicate": "p", "object": "b"}]},
    )
    assert "ontology_retract" in detail
    assert "撤回" in detail or "移除" in detail, f"说明须写明破坏性动作：{detail}"
    assert "星桥资本" in detail


def test_describe_memory_assert_intent_renders_triple() -> None:
    """契约：memory_assert 卡片说明须把三元组渲染成「主语·关系·宾语」。"""
    from bandi.tools.approval import describe_tool_intent

    detail = describe_tool_intent(
        "memory_assert",
        {"subject": "老板", "predicate": "prefers", "object": "深色主题"},
    )
    assert "memory_assert" in detail
    assert "长期记忆" in detail, f"说明须写明写入目标是长期记忆：{detail}"
    assert "老板" in detail and "深色主题" in detail


def test_describe_tool_intent_covers_shell_and_write() -> None:
    """契约：bash/write 审批必须携带命令/路径人读说明；普通只读工具仍返回空。"""
    from bandi.tools.approval import describe_tool_intent

    assert "ls" in describe_tool_intent("bash", {"command": "ls"})
    assert "a.txt" in describe_tool_intent("write", {"path": "a.txt", "content": "x"})
    assert describe_tool_intent("ontology_query", {"mode": "stats"}) == ""


def test_build_approval_question_carries_detail_and_choices() -> None:
    """契约：卡片数据源携带中文 prompt + 内容说明 + 三态选项。"""
    from bandi.tools.approval import build_approval_question, describe_tool_intent

    question = build_approval_question(
        tool_call_id="tc-1",
        tool_name="ontology_assert",
        reason="requires user approval",
        detail=describe_tool_intent(
            "ontology_assert",
            {"facts": [{"subject": "A", "predicate": "rel", "object": "B"}]},
        ),
    )
    assert question["ui_variant"] == "permission"
    assert question["permission_kind"] == "tool_run"
    assert len(question["choices"]) == 3, "三态：仅本次 / 本会话 / 拒绝"
    assert "本体" in question["prompt"], f"prompt 须中文并写明写本体：{question['prompt']}"
    assert "A" in question["detail"]


# ── memory_assert 纳入 ask 名单 ────────────────────────────────────────────


def test_memory_assert_in_ask_tools_single_source() -> None:
    """契约：memory_assert 同时出现在 pipeline 常量与 config 默认值（双单源一致）。"""
    from bandi.core.config import PermissionsConfig
    from bandi.tools.permissions_pipeline import _ASK_TOOLS

    assert "memory_assert" in _ASK_TOOLS
    assert "memory_assert" in set(PermissionsConfig().ask_tools)
    assert set(PermissionsConfig().ask_tools) == _ASK_TOOLS


def test_plan_mode_still_gates_ask_listed_memory_assert() -> None:
    """契约（破坏者）：plan 模式不得静默放行 ask 名单成员——

    memory_assert 在 plan 白名单（计划要记依赖关系，既有拍板），但它同时在
    ask 名单：plan 下必须仍返回 ask_user=True（弹权限卡），不得借 plan 跳过
    询问层直写长期记忆。"""
    from bandi.core.message import ToolCall
    from bandi.tools.permissions_pipeline import PermissionsPipeline, plan_mode_tool_violations
    from bandi.tools.registry import ToolRegistry

    pipeline = PermissionsPipeline()
    tc = ToolCall(
        id="t-plan",
        name="memory_assert",
        arguments={"subject": "A", "predicate": "depends_on", "object": "B"},
    )
    verdict = pipeline._check_ask_list(tc, None, "plan")
    assert verdict.ask_user is True, "plan 下 ask 名单成员必须仍触发询问"
    assert pipeline.needs_approval(tc, None, "plan") is True

    # 守卫健康：plan 白名单含 memory_assert 不再是违规（会弹卡非静默旁路）
    assert plan_mode_tool_violations(ToolRegistry()) == []

    # 对照：非 ask 名单的只读成员在 plan 下零行为变化
    tc_read = ToolCall(id="t-plan-2", name="read", arguments={"path": "x"})
    assert pipeline.needs_approval(tc_read, None, "plan") is False


# ── 批准链路隔离：本体零草稿零长期记忆 ─────────────────────────────────────


class _HeadlessPipelineStub:
    """headless 权限管道（无同步 UI）+ ask 名单含本体写入工具。"""

    def __init__(self) -> None:
        self.ask_tools = {"ontology_define", "ontology_assert", "memory_assert"}
        self.cached: list[tuple[str, dict]] = []

    @property
    def user_interaction_available(self) -> bool:
        return False

    def needs_approval(self, tc: Any, tool: Any, mode: str) -> bool:
        if mode == "plan":
            return False
        return tc.name in self.ask_tools

    def check_mode_available(self, tc: Any, tool: Any, mode: str) -> Any:
        from bandi.tools.permissions_pipeline import PermissionVerdict

        return PermissionVerdict(allow=True)

    def cache_approved(self, tool_name: str, arguments: dict) -> None:
        self.cached.append((tool_name, dict(arguments)))


def _headless_pipeline(tmp_path: Any) -> Any:
    """真实权限管道（headless：无同步 UI），ask 名单走 config 默认值。"""
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    return PermissionsPipeline(project_root=str(tmp_path))


@pytest.mark.asyncio
async def test_ontology_assert_approved_writes_only_ontology(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """契约（端到端 + 破坏者）：本体写入经审批放行后——

    - 本体库新增实体/事实；
    - 长期记忆计数不变（写本体不写长期记忆）；
    - memory pending 队列零新增（写本体不产生草稿）。
    """
    from bandi.core.message import ToolCall
    from bandi.memory.long_term import LongTermMemory
    from bandi.memory.staging import MemoryPendingStore
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.tools import OntologyToolContext
    from bandi.ontology.triple_store import OntologyStore
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.ontology_tool_adapter import build_ontology_tools
    from bandi.tools.registry import ToolRegistry

    long_term = LongTermMemory(str(tmp_path / "long_term.db"))
    pending = MemoryPendingStore(tmp_path)
    ontology = OntologyStore(open_ontology(tmp_path / "ontology.db"))
    # 预置关系词表——ontology_assert 拒绝未声明谓词（TBox 白名单语义）
    ontology.define_relation({"id": "invests_in"}, "2026-10-14T00:00:00+00:00")
    tools = build_ontology_tools(OntologyToolContext(store=ontology))
    tool_registry = ToolRegistry()
    for tool in tools:
        tool_registry.register_instance(tool)

    ex = ToolExecutor(
        tool_registry,
        temp_dir=str(tmp_path / "tmp"),
        permissions_pipeline=_headless_pipeline(tmp_path),
        mode="execute",
        agent_id="sess-onto",
    )

    # 审批授权单例隔离——进程单例读 ~/.bandi/approvals.json（真机持久授权会
    # 命中 wildcard grant 直接放行，破坏「未批准先暂停」前提）；禁用
    # reset_approval_store（会清真实用户授权数据）。
    from bandi.tools import approval as approval_mod

    isolated_store = approval_mod.ApprovalStore(str(tmp_path / "approvals.json"))
    monkeypatch.setattr(approval_mod, "get_approval_store", lambda: isolated_store)

    before_long_term = long_term.count()
    before_pending = pending.pending_count()

    call = ToolCall(
        id="tc-onto-1",
        name="ontology_assert",
        arguments={
            "entities": [
                {"id": "huanyu_holdings", "label": "寰宇控股", "classes": []},
                {"id": "xingqiao_capital", "label": "星桥资本", "classes": []},
            ],
            "facts": [
                {
                    "subject": "huanyu_holdings",
                    "predicate": "invests_in",
                    "object": "xingqiao_capital",
                }
            ],
        },
    )
    first = (await ex.execute([call]))[0]
    assert first.error.startswith("APPROVAL_REQUIRED"), "未批准时必须先暂停出权限卡"
    question = json.loads(first.error.removeprefix("APPROVAL_REQUIRED:"))
    assert "本体库" in question["prompt"], f"卡片 prompt 须写明写本体：{question['prompt']}"
    assert "huanyu_holdings" in question["detail"], "卡片 detail 须列出提取内容"

    from bandi.tools import approval as approval_mod

    isolated_store.grant("sess-onto", "ontology_assert", scope="session")
    result = (await ex.execute([call]))[0]

    assert result.success, f"批准后的本体写入应直接执行成功：{result.error}"
    graph = ontology.graph()
    assert "huanyu_holdings|invests_in|xingqiao_capital" in graph.facts, "事实应已写入本体库"
    assert long_term.count() == before_long_term, "写本体不得写长期记忆"
    assert pending.pending_count() == before_pending, "写本体不得产生草稿"


# ── 草稿结构化展示 ─────────────────────────────────────────────────────────


def test_structured_preview_kind_label_is_chinese() -> None:
    """契约：提名草稿摘要 kind 前缀中文（[事实]），不得再出英文 kind（[fact]）。"""
    from bandi.memory.structured import structured_preview

    text = structured_preview("老板偏好深色主题", {"kind": "fact", "key": "theme"})
    assert text.startswith("[事实]"), f"前缀应中文：{text}"
    assert "[fact]" not in text
    # 未知 kind 原样保留（不硬造标签）
    text2 = structured_preview("内容", {"kind": "custom_kind", "key": "k"})
    assert text2.startswith("[custom_kind]")


def test_stage_consolidation_carries_draft_meta() -> None:
    """契约：系统提名草稿携带 draft 元数据（类型=系统提名，目标=长期记忆）。"""
    from bandi.memory.episodic import Episode
    from bandi.memory.long_term import MemoryItem
    from bandi.memory.staging import MemoryPendingStore, reset_pending_store

    store = MemoryPendingStore(tmp_pending_dir := _tmp_dir("consol-meta"))
    reset_pending_store(store)
    try:
        from bandi.memory.consolidation import _stage_consolidation

        episode = Episode(
            episode_id="ep:s1:1",
            summary="用户偏好深色主题",
            session_id="s1",
            user_input="我喜欢深色主题",
            importance=0.9,
        )
        item = MemoryItem(item_id="consolidated:ep:s1:1", content="老板偏好深色主题")
        _stage_consolidation(item, episode)
        rows = store.list_pending()
        assert len(rows) == 1
        draft = rows[0].payload.get("draft") or {}
        assert draft.get("kind") == "系统提名", f"草稿类型须标注：{draft}"
        assert draft.get("target") == "长期记忆", f"写入目标须标注：{draft}"
    finally:
        reset_pending_store(None)
        _cleanup(tmp_pending_dir)


def test_stage_long_term_write_carries_draft_meta() -> None:
    """契约：来源门/冲突暂存携带 draft 元数据——冲突暂存标「冲突裁决」，
    来源门暂存标「后台直写暂存」；写入目标均=长期记忆。"""
    from bandi.memory.long_term import MemoryItem
    from bandi.memory.staging import (
        MemoryPendingStore,
        reset_pending_store,
        stage_long_term_write,
    )

    store = MemoryPendingStore(tmp_pending_dir := _tmp_dir("lt-meta"))
    reset_pending_store(store)
    try:
        stage_long_term_write(
            MemoryItem(item_id="turn:s1:2", content="部署端口 8080"),
            project_id=None,
            employee_id=None,
            action="store_long_term",
            summary="部署端口 8080",
            origin="background",
            conflict=True,
        )
        stage_long_term_write(
            MemoryItem(item_id="turn:s1:3", content="部署端口 9090"),
            project_id=None,
            employee_id=None,
            action="store_long_term",
            summary="部署端口 9090",
            origin="background",
        )
        rows = store.list_pending()
        assert len(rows) == 2
        drafts = {(r.payload.get("draft") or {}).get("kind") for r in rows}
        assert drafts == {"冲突裁决", "后台直写暂存"}, f"草稿类型须按冲突区分：{drafts}"
        for row in rows:
            assert (row.payload.get("draft") or {}).get("target") == "长期记忆"
        assert any(r.payload.get("conflict") is True for r in rows)
    finally:
        reset_pending_store(None)
        _cleanup(tmp_pending_dir)


def test_memory_pending_api_row_exposes_draft_meta() -> None:
    """契约：/api/memory/pending 行携带 draft 结构（前端多标签数据源）。"""
    import asyncio

    from bandi.api.app import list_memory_pending as api_list_memory_pending
    from bandi.memory.staging import (
        MemoryPendingStore,
        reset_pending_store,
        stage_write,
    )

    store = MemoryPendingStore(tmp_pending_dir := _tmp_dir("api-meta"))
    reset_pending_store(store)
    try:
        stage_write(
            action="record_episode",
            summary="经历摘要",
            payload={
                "target": "episodic",
                "item": {"episode_id": "ep:x"},
                "draft": {"kind": "后台直写暂存", "target": "情景记忆"},
            },
            origin="background",
        )
        rows = asyncio.run(api_list_memory_pending())["pending"]
        assert rows, "暂存记录应在 API 可见"
        draft = rows[0].get("draft") or {}
        assert draft.get("kind") == "后台直写暂存"
        assert draft.get("target") == "情景记忆"
    finally:
        reset_pending_store(None)
        _cleanup(tmp_pending_dir)


# ── 测试辅助 ───────────────────────────────────────────────────────────────


def _tmp_dir(name: str) -> Any:
    import tempfile
    from pathlib import Path

    base = Path(tempfile.mkdtemp(prefix=f"bandi-card-{name}-"))
    return base / "pending-root"


def _cleanup(path: Any) -> None:
    import shutil

    try:
        shutil.rmtree(path.parent, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
