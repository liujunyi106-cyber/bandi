# 功能：MemoryHierarchyManager 生产主链路端到端测试（真实 store + 断言最终产物）
from bandi.company.store import WorkItemStore
from bandi.memory.global_store import GlobalMemoryStore
from bandi.memory.markdown_memory import MarkdownMemoryStore
from bandi.memory.memory_manager import MemoryHierarchyManager
from bandi.memory.session.store import SessionStore


def _build(tmp_path) -> MemoryHierarchyManager:
    return MemoryHierarchyManager(
        global_store=GlobalMemoryStore(tmp_path / "global.db"),
        markdown_memory=MarkdownMemoryStore(tmp_path / "store", project_root=tmp_path / "proj"),
        work_item_store=WorkItemStore(str(tmp_path / "company.db")),
        session_store=SessionStore(tmp_path / "sessions.db"),
    )


def test_four_layer_write_collect_reopen(tmp_path) -> None:
    h = _build(tmp_path)
    h.save_memory("global", "产品定位：个人多 Agent 数字员工组织", allow_shared=True)
    h.save_memory("project", "选品方法论：批量上架前先做竞品分层", project_id="proj1")
    h.save_memory(
        "session", "会话：完成竞品分析报告", project_id="proj1", session_id="s1", employee_id="emp1"
    )
    h.save_memory(
        "employee",
        "员工：选品专员擅长爆品分层",
        project_id="proj1",
        employee_id="emp1",
        role_id="选品专员",
    )
    h._global.append_event("AGENT_TURN", {"summary": "完成选品分析"}, event_id="turn_1")

    ctx = h.collect_memory_context(project_id="proj1", session_id="s1", employee_id="emp1")
    assert "个人多 Agent 数字员工组织" in ctx
    assert "选品方法论" in ctx
    assert "竞品分析报告" in ctx
    assert "爆品分层" in ctx

    h2 = _build(tmp_path)
    assert h2.load_memory("global") == "产品定位：个人多 Agent 数字员工组织"
    assert (
        h2.load_memory("employee", project_id="proj1", employee_id="emp1")
        == "员工：选品专员擅长爆品分层"
    )
    assert h2._global.list_events()[0]["payload"]["summary"] == "完成选品分析"


def test_project_memory_file_lands_under_project_root(tmp_path) -> None:
    h = _build(tmp_path)
    h.save_memory("project", "落盘验证", project_id="proj1")
    assert (tmp_path / "proj" / ".bandi" / "memory" / "MEMORY.md").exists()


def test_append_then_reopen_sees_accumulated_memory(tmp_path) -> None:
    h = _build(tmp_path)
    h.save_memory("global", "第一条", allow_shared=True)
    h.append_memory("global", "第二条", allow_shared=True)
    h2 = _build(tmp_path)
    content = h2.load_memory("global")
    assert "第一条" in content
    assert "第二条" in content


def test_snapshot_upsert_is_idempotent(tmp_path) -> None:
    h = _build(tmp_path)
    h.save_memory("employee", "v1", project_id="proj1", employee_id="emp1")
    h.save_memory("employee", "v2", project_id="proj1", employee_id="emp1")
    h2 = _build(tmp_path)
    assert h2.load_memory("employee", project_id="proj1", employee_id="emp1") == "v2"


def test_session_save_updates_metadata_when_session_exists(tmp_path) -> None:
    import json

    h = _build(tmp_path)
    sid = h._session_store.create_session(mode="execute")
    h.save_memory("session", "会话记忆", project_id="proj1", session_id=sid, employee_id="emp1")
    session = h._session_store.get_session(sid)
    assert session is not None
    assert json.loads(session["metadata"])["project_id"] == "proj1"


def test_persist_memory_emits_event_via_hierarchy(tmp_path) -> None:
    import asyncio
    from types import SimpleNamespace

    from bandi.core.agent_runner import _persist_memory
    from bandi.memory.memory_provider import MemoryManager

    h = _build(tmp_path)
    manager = MemoryManager()
    # B5b 8.4：episodic/working 写入需成功才计入通道（stub 保证通道明细可断言）
    from unittest.mock import MagicMock

    manager.episodic = MagicMock()
    manager.working = MagicMock()

    class Ctx:
        tool_results = []
        final_message = "结论：已完成预算方向修复"
        current_content = None
        raw_input = "请修复预算方向"
        turn_index = 5
        terminal_reason = SimpleNamespace(name="COMPLETED")
        # B5b 8.4：_persist_memory 发 MEMORY_WRITTEN 需 bus（最小 stub）
        bus = SimpleNamespace(emit=_noop_emit)

    asyncio.run(
        _persist_memory(
            Ctx(),
            manager,
            session_id="sess-ev",
            hierarchy=h,
            project_id="proj1",
            employee_id="emp1",
        )
    )
    events = h.query_events("memory_persist")
    assert events, "记忆写入后必须产生 memory_persist 事件"
    payload = events[0]["payload"]
    assert payload.get("event_type") == "agent_turn"
    assert "预算" in payload.get("summary", "")
    assert "episodic" in (payload.get("channels") or []), "B5b：通道明细必须随事件落库"

    h2 = _build(tmp_path)
    assert h2.query_events("memory_persist"), "事件必须落盘（重开可查）"


async def _noop_emit(_event: object) -> None:
    return None
