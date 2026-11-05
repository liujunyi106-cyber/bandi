# 功能：工作流契约族——DAG 生命周期/条件分支/取消暂停恢复/feature gate fail-closed/装配 E2E
import asyncio
import pathlib
import tempfile
from typing import Any

from bandi.agent.factory import build_tool_registry
from bandi.llm.mock import MockProvider
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.registry import ToolRegistry
from bandi.tools.workflow.chat_workflow import ChatWorkflowTool
from bandi.tools.workflow.service import WorkflowService
from bandi.tools.workflow.store import (
    ExecutionStatus,
    Flow,
    FlowStatus,
    FlowType,
    WorkflowExecution,
    WorkflowStore,
)
from bandi.tools.workflow.workflow_crud import (
    WorkflowCancelTool,
    WorkflowListTool,
    WorkflowPauseTool,
    WorkflowResumeTool,
    WorkflowRunTool,
    WorkflowStatusTool,
)
from bandi.tools.workflow.workflow_save import WorkflowSaveTool

ALL_WORKFLOW_TOOLS = (
    WorkflowListTool,
    WorkflowRunTool,
    WorkflowStatusTool,
    WorkflowCancelTool,
    WorkflowPauseTool,
    WorkflowResumeTool,
    WorkflowSaveTool,
)


def tnode(nid, template, output_var=None):
    node = {"id": nid, "type": "TransformNode", "name": nid, "config": {"template": template}}
    if output_var:
        node["config"]["output_var"] = output_var
    return node


def edge(eid, source, target, handle=None):
    e = {"id": eid, "source": source, "target": target}
    if handle:
        e["sourceHandle"] = handle
    return e


def build_conditional_doc() -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            {
                "id": "cond",
                "type": "ConditionNode",
                "config": {"condition": {"left": "${inputs.score}", "operator": "ge", "right": 60}},
            },
            tnode("pass", "通过"),
            tnode("fail", "待改进"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [
            edge("e1", "start", "cond"),
            edge("e2", "cond", "pass", "true"),
            edge("e3", "cond", "fail", "false"),
            edge("e4", "pass", "end"),
            edge("e5", "fail", "end"),
        ],
        "start_node": "start",
        "end_node": "end",
    }


def make_service(
    db_path: str = ":memory:", provider=None, store=None, tool_runner=None
) -> WorkflowService:
    return WorkflowService(
        store or WorkflowStore(db_path),
        provider=provider or MockProvider(),
        tool_runner=tool_runner,
    )


def save_flow(store: WorkflowStore, flow_id: str, doc: dict) -> None:
    store.save_flow(
        Flow(
            flow_id=flow_id,
            name="工作流",
            status=FlowStatus.PUBLISHED,
            flow_type=FlowType.WORKFLOW,
            data=doc,
        )
    )


def test_run_lifecycle_persists_final_artifacts() -> None:
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            tnode("n2", "${inputs.text}-已处理"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "n2"), edge("e2", "n2", "end")],
        "start_node": "start",
        "end_node": "end",
    }
    store = WorkflowStore(":memory:")
    save_flow(store, "flw_1", doc)
    svc = make_service(store=store)

    record = asyncio.run(svc.start("flw_1", user_id="u_1", inputs={"text": "合同"}))
    assert record["status"] == "completed"
    assert record["outputs"]["n2"] == "合同-已处理"
    assert record["execution_id"]

    persisted = asyncio.run(svc.get_execution(record["execution_id"]))
    assert persisted["flow_id"] == "flw_1"
    assert persisted["node_count"] == 3
    assert [h["node_id"] for h in persisted["execution_history"]] == ["start", "n2", "end"]

    flow = store.get_flow("flw_1")
    assert flow is not None
    assert flow.run_count == 1
    assert flow.last_run_at is not None


def test_run_chinese_multi_node_branch_end_to_end() -> None:
    store = WorkflowStore(":memory:")
    save_flow(store, "flw_1", build_conditional_doc())
    svc = make_service(store=store)

    passed = asyncio.run(svc.start("flw_1", inputs={"score": 88}))
    assert passed["status"] == "completed"
    assert passed["outputs"]["pass"] == "通过"
    assert passed["outputs"].get("fail") is None

    failed = asyncio.run(svc.start("flw_1", inputs={"score": 59}))
    assert failed["status"] == "completed"
    assert failed["outputs"]["fail"] == "待改进"
    assert failed["outputs"].get("pass") is None


def test_run_missing_flow_returns_error_no_execution() -> None:
    svc = make_service()
    record = asyncio.run(svc.start("nope", inputs={}))
    assert record.get("ok") is False
    assert "not found" in (record.get("error") or "").lower()
    assert asyncio.run(svc.list_executions(flow_id="nope")) == []


def test_list_flows_filter_and_limit_via_service() -> None:
    store = WorkflowStore(":memory:")
    save_flow(store, "flw_1", build_conditional_doc())
    save_flow(store, "flw_2", build_conditional_doc())
    store.save_flow(Flow(flow_id="flw_3", name="草稿", status=FlowStatus.DRAFT))
    svc = make_service(store=store)

    flows = svc.list_flows(status=FlowStatus.PUBLISHED)
    assert [f["id"] for f in flows] == ["flw_1", "flw_2"]
    assert len(svc.list_flows(limit=1)) == 1


def test_status_tool_returns_readable_record() -> None:
    store = WorkflowStore(":memory:")
    save_flow(store, "flw_1", build_conditional_doc())
    svc = make_service(store=store)
    tool = WorkflowRunTool(service=svc)
    record = asyncio.run(tool.execute({"flow_id": "flw_1", "inputs": {"score": 90}}))
    assert record["status"] == "completed"

    status_tool = WorkflowStatusTool(service=svc)
    detail = asyncio.run(status_tool.execute({"execution_id": record["execution_id"]}))
    assert detail["status"] == "completed"
    assert detail["inputs"] == {"score": 90}
    assert detail["outputs"]["pass"] == "通过"


def test_cancel_semantics_terminal_and_running() -> None:
    store = WorkflowStore(":memory:")
    save_flow(store, "flw_1", build_conditional_doc())
    svc = make_service(store=store)
    record = asyncio.run(svc.start("flw_1", inputs={"score": 90}))
    assert record["status"] == "completed"
    assert asyncio.run(svc.cancel_execution(record["execution_id"])) is False

    running = WorkflowExecution(
        execution_id="ex_running", flow_id="flw_1", status=ExecutionStatus.RUNNING
    )
    store.create_execution(running)
    assert asyncio.run(svc.cancel_execution("ex_running")) is True
    assert asyncio.run(svc.get_execution("ex_running"))["status"] == "cancelled"


def test_pause_then_resume_from_checkpoint() -> None:
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            tnode("n2", "${inputs.x}-2"),
            tnode("n3", "${outputs.n2}-${suffix}"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "n2"), edge("e2", "n2", "n3"), edge("e3", "n3", "end")],
        "start_node": "start",
        "end_node": "end",
    }
    store = WorkflowStore(":memory:")
    save_flow(store, "flw_1", doc)
    svc = make_service(store=store)
    running = WorkflowExecution(
        execution_id="ex_paused",
        flow_id="flw_1",
        status=ExecutionStatus.RUNNING,
        inputs={"x": 1},
    )
    store.create_execution(running)
    assert asyncio.run(svc.pause_execution("ex_paused")) is True
    assert asyncio.run(svc.get_execution("ex_paused"))["status"] == "paused"

    checkpoint = {
        "resume_node": "n2",
        "outputs": {"start": {}},
        "variables": {"suffix": 7},
        "history": [{"node_id": "start", "status": "completed", "output": {}, "next_node": "n2"}],
    }
    store.update_execution("ex_paused", state_json=checkpoint)
    resumed = asyncio.run(svc.resume_execution("ex_paused", "flw_1"))
    assert resumed["status"] == "completed"
    assert resumed["outputs"]["n3"] == "1-2-7"
    assert [h["node_id"] for h in resumed["execution_history"]] == ["start", "n2", "n3", "end"]


def test_feature_gate_fail_closed_when_workflow_disabled() -> None:
    def build_with(flags):
        reg = ToolRegistry(feature_flags=flags)
        svc = make_service()
        for tool_cls in ALL_WORKFLOW_TOOLS:
            reg.register_instance(tool_cls(service=svc))
        reg.register_instance(ChatWorkflowTool(registry=reg))
        return reg

    closed = build_with({"workflow": False})
    assert closed.get("workflow_list") is None
    assert closed.get("workflow_run") is None
    assert closed.get("workflow_save") is None
    assert closed.get("chat_workflow") is None
    assert not [
        t
        for t in closed.list_tools()
        if t.name.startswith("workflow_") or t.name == "chat_workflow"
    ]

    opened = build_with({})
    assert opened.get("workflow_list") is not None
    assert opened.get("workflow_save") is not None
    assert opened.get("chat_workflow") is not None
    assert any(t.name.startswith("workflow_") for t in opened.list_tools())


def test_factory_wiring_production_path_end_to_end() -> None:
    tmp_dir = tempfile.mkdtemp(prefix="bandi_workflow_")
    db_path = str(pathlib.Path(tmp_dir) / "workflow.db")
    provider = MockProvider()
    registry = build_tool_registry(
        feature_flags={"workflow": True},
        workflow_db_path=db_path,
        subagent_provider=provider,
    )

    assert registry.get("workflow_list") is not None
    assert registry.get("workflow_run") is not None
    assert registry.check_feature_group_coverage() == []

    store = WorkflowStore(db_path)
    save_flow(store, "flw_e2e", build_conditional_doc())

    run_tool = registry.get("workflow_run")
    result = asyncio.run(run_tool.execute({"flow_id": "flw_e2e", "inputs": {"score": 88}}))
    assert result["status"] == "completed"
    assert result["outputs"]["pass"] == "通过"

    status_tool = registry.get("workflow_status")
    detail = asyncio.run(status_tool.execute({"execution_id": result["execution_id"]}))
    assert detail["flow_id"] == "flw_e2e"
    assert detail["status"] == "completed"
    assert [h["node_id"] for h in detail["execution_history"]] == ["start", "cond", "pass", "end"]


def test_tool_surface_declarations() -> None:
    svc = make_service()
    tools = [cls(service=svc) for cls in ALL_WORKFLOW_TOOLS]
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {
        "workflow_list",
        "workflow_run",
        "workflow_status",
        "workflow_cancel",
        "workflow_pause",
        "workflow_resume",
        "workflow_save",
    }
    for tool in tools:
        assert tool.category == ToolCategory.WORKFLOW
        assert tool.feature_group == "workflow"
        assert isinstance(tool.parameters, dict)
    assert by_name["workflow_list"].is_read_only is True
    assert by_name["workflow_status"].is_read_only is True
    assert by_name["workflow_run"].is_read_only is False
    assert by_name["workflow_save"].is_read_only is False
    assert "flow_data" in by_name["workflow_save"].parameters["properties"]
    assert "flow_id" in by_name["workflow_save"].parameters["properties"]
    assert "execution_id" in by_name["workflow_status"].parameters["properties"]

    chat = ChatWorkflowTool(registry=ToolRegistry())
    assert chat.name == "chat_workflow"
    assert chat.category == ToolCategory.WORKFLOW
    assert chat.feature_group == "workflow"
    assert chat.is_read_only is True
    assert "steps" in chat.parameters["properties"]


def test_save_flow_persists_readable_flow() -> None:
    store = WorkflowStore(":memory:")
    svc = make_service(store=store)
    saved = svc.save_flow("报表流程", build_conditional_doc(), publish=True)
    assert saved["name"] == "报表流程"
    assert saved["status"] == "published"
    assert saved["node_count"] == 5
    assert len(saved["digest"]) == 64
    assert saved["flow_id"]

    flow = store.get_flow(saved["flow_id"])
    assert flow is not None
    assert flow.status == FlowStatus.PUBLISHED
    assert flow.flow_type == FlowType.WORKFLOW
    assert flow.data == build_conditional_doc()
    assert [f["id"] for f in svc.list_flows(status=FlowStatus.PUBLISHED)] == [saved["flow_id"]]


def test_save_flow_rejects_invalid_graph_no_partial_write() -> None:
    svc = make_service()
    bad = {"nodes": [{"id": "x", "type": "NoSuchNode"}], "edges": [], "start_node": "x"}
    result = svc.save_flow("坏图", bad)
    assert result.get("ok") is False
    assert "Invalid workflow graph" in (result.get("error") or "")
    assert svc.list_flows() == []

    empty = svc.save_flow("空图", {"nodes": [], "edges": []})
    assert empty.get("ok") is False


def test_workflow_save_tool_saves_and_updates_in_place() -> None:
    store = WorkflowStore(":memory:")
    svc = make_service(store=store)
    tool = WorkflowSaveTool(service=svc)
    saved = asyncio.run(
        tool.execute({"name": "待发布", "flow_data": build_conditional_doc(), "publish": False})
    )
    assert saved["status"] == "draft"
    assert saved["node_count"] == 5
    flow_id = saved["flow_id"]

    updated = asyncio.run(
        tool.execute({"name": "已发布", "flow_data": build_conditional_doc(), "flow_id": flow_id})
    )
    assert updated["flow_id"] == flow_id
    flow = store.get_flow(flow_id)
    assert flow is not None
    assert flow.name == "已发布"
    assert flow.status == FlowStatus.PUBLISHED

    invalid = asyncio.run(
        tool.execute(
            {
                "name": "坏",
                "flow_data": {
                    "nodes": [{"id": "x", "type": "Nope"}],
                    "edges": [],
                    "start_node": "x",
                },
            }
        )
    )
    assert invalid.get("ok") is False


class _DestructiveStubTool(BaseTool):
    name: str = "boom_tool"
    description: str = "destructive stub"
    is_destructive: bool = True
    category: ToolCategory = ToolCategory.UTIL

    async def execute(self, params: dict, context=None):
        return {"success": True, "result": "done"}


def test_chat_workflow_tool_validates_steps_and_digest() -> None:
    from bandi.tools.glob_tool import GlobTool

    reg = ToolRegistry()
    reg.register_instance(GlobTool())
    tool = ChatWorkflowTool(registry=reg)
    steps = [
        {
            "id": "s1",
            "label": "搜索源码",
            "hint": "按 glob 找文件",
            "action": {"kind": "tool", "tool_id": "glob", "arguments": {"pattern": "**/*.py"}},
        }
    ]
    out = asyncio.run(
        tool.execute({"version": 1, "title": "例行搜索", "summary": "每日", "steps": steps})
    )
    assert out.get("success") is True
    assert out["workflow"]["title"] == "例行搜索"
    assert out["workflow"]["steps"][0]["action"]["tool_id"] == "glob"
    assert len(out["digest"]) == 64


def test_chat_workflow_tool_unknown_tool_fail_closed() -> None:
    tool = ChatWorkflowTool(registry=ToolRegistry())
    steps = [
        {"id": "s1", "label": "x", "action": {"kind": "tool", "tool_id": "nope", "arguments": {}}}
    ]
    out = asyncio.run(tool.execute({"title": "t", "steps": steps}))
    assert out.get("ok") is False
    assert "nope" in (out.get("error") or "")


def test_chat_workflow_tool_duplicate_step_ids_rejected() -> None:
    reg = ToolRegistry()
    tool = ChatWorkflowTool(registry=reg)
    steps = [
        {"id": "s1", "label": "A", "action": {"kind": "tool", "tool_id": "glob", "arguments": {}}},
        {"id": "s1", "label": "B", "action": {"kind": "tool", "tool_id": "glob", "arguments": {}}},
    ]
    out = asyncio.run(tool.execute({"title": "t", "steps": steps}))
    assert out.get("ok") is False
    assert "重复" in (out.get("error") or "")


def test_chat_workflow_tool_blocks_destructive_tool() -> None:
    reg = ToolRegistry()
    reg.register_instance(_DestructiveStubTool())
    tool = ChatWorkflowTool(registry=reg)
    steps = [
        {
            "id": "s1",
            "label": "危险",
            "action": {"kind": "tool", "tool_id": "boom_tool", "arguments": {}},
        }
    ]
    out = asyncio.run(tool.execute({"title": "t", "steps": steps}))
    assert out.get("ok") is False
    assert "destructive" in (out.get("error") or "").lower() or "破坏性" in (out.get("error") or "")


def test_factory_wiring_tool_node_end_to_end(monkeypatch) -> None:
    tmp_dir = tempfile.mkdtemp(prefix="bandi_workflow_tool_")
    # 隔离环境（conftest Path.home 隔离）下默认 workspace 目录不存在且
    from pathlib import Path as _Path

    from bandi.tools import fs_tool as fs_mod

    workspace = _Path.home() / ".bandi" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fs_mod, "_WORKSPACE_ROOT", workspace)
    db_path = str(pathlib.Path(tmp_dir) / "workflow.db")
    provider = MockProvider()
    registry = build_tool_registry(
        feature_flags={"workflow": True},
        workflow_db_path=db_path,
        subagent_provider=provider,
    )
    assert registry.get("workflow_save") is not None
    assert registry.get("chat_workflow") is not None

    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            {
                "id": "t0",
                "type": "ToolNode",
                "config": {"tool_name": "glob", "params": {"pattern": "${inputs.pattern}"}},
            },
            tnode("final", "完成-${outputs.t0}"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "t0"), edge("e2", "t0", "final"), edge("e3", "final", "end")],
        "start_node": "start",
        "end_node": "end",
    }
    save_flow(WorkflowStore(db_path), "flw_tool", doc)

    run_tool = registry.get("workflow_run")
    result = asyncio.run(
        run_tool.execute({"flow_id": "flw_tool", "inputs": {"pattern": "**/glob_tool.py"}})
    )
    assert result["status"] == "completed"
    assert isinstance(result["outputs"]["t0"], str)
    assert result["outputs"]["final"].startswith("完成-")


def test_tool_node_invalid_params_fail_closed() -> None:
    """工具节点参数非法必须 fail-closed：不执行工具，工作流进入 failed。"""
    tmp_dir = tempfile.mkdtemp(prefix="bandi_workflow_toolval_")
    db_path = str(pathlib.Path(tmp_dir) / "workflow.db")
    provider = MockProvider()
    registry = build_tool_registry(
        feature_flags={"workflow": True},
        workflow_db_path=db_path,
        subagent_provider=provider,
    )
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            {
                "id": "t0",
                "type": "ToolNode",
                "config": {"tool_name": "glob", "params": {"pattern": 123}},
            },
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "t0"), edge("e2", "t0", "end")],
        "start_node": "start",
        "end_node": "end",
    }
    save_flow(WorkflowStore(db_path), "flw_badparam", doc)

    run_tool = registry.get("workflow_run")
    result = asyncio.run(run_tool.execute({"flow_id": "flw_badparam"}))
    assert result["status"] == "failed", f"参数非法必须 failed，实际 {result['status']}"
    assert "参数校验失败" in " ".join(result.get("errors") or []), (
        f"错误必须含参数校验失败：{result.get('errors')}"
    )


def test_resume_can_be_paused_again_during_run() -> None:
    """resume 续跑期间可再次暂停：service 必须把 should_pause 透传 engine，返回 paused 并保留 checkpoint。"""
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            {
                "id": "t0",
                "type": "ToolNode",
                "config": {"tool_name": "boom_tool", "params": {}},
            },
            tnode("n3", "${outputs.t0}-done"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "t0"), edge("e2", "t0", "n3"), edge("e3", "n3", "end")],
        "start_node": "start",
        "end_node": "end",
    }
    from bandi.tools import base as _tools_base

    class _StubTool(_tools_base.BaseTool):
        name: str = "boom_tool"
        description: str = "stub"

        async def execute(self, params, context=None):
            return {"success": True, "result": "ok"}

    holder: dict[str, Any] = {}

    async def pausing_runner(tool_name: str, params: dict) -> Any:
        await holder["svc"].pause_execution(holder["rid"])
        return {"success": True, "result": "token"}

    store = WorkflowStore(":memory:")
    save_flow(store, "flw_1", doc)
    svc = make_service(store=store, tool_runner=pausing_runner)
    holder["svc"] = svc
    checkpoint = {
        "resume_node": "t0",
        "outputs": {"start": {}},
        "variables": {},
        "history": [{"node_id": "start", "status": "completed", "output": {}, "next_node": "t0"}],
    }
    running = WorkflowExecution(
        execution_id="ex_rp", flow_id="flw_1", status=ExecutionStatus.RUNNING, inputs={}
    )
    store.create_execution(running)
    assert asyncio.run(svc.pause_execution("ex_rp")) is True
    store.update_execution("ex_rp", state_json=checkpoint)
    holder["rid"] = "ex_rp"
    resumed = asyncio.run(svc.resume_execution("ex_rp", "flw_1"))
    assert resumed["status"] == "paused", (
        f"resume 期间再暂停必须返回 paused，实际 {resumed['status']}"
    )
    stored = asyncio.run(svc.get_execution("ex_rp"))
    assert stored["status"] == "paused"
    stored_record = store.get_execution("ex_rp")
    assert stored_record is not None
    assert stored_record.state_json is not None, "再暂停必须保留 checkpoint 供二次 resume"


def test_factory_chat_workflow_validates_against_live_registry() -> None:
    tmp_dir = tempfile.mkdtemp(prefix="bandi_workflow_chat_")
    db_path = str(pathlib.Path(tmp_dir) / "workflow.db")
    provider = MockProvider()
    registry = build_tool_registry(
        feature_flags={"workflow": True},
        workflow_db_path=db_path,
        subagent_provider=provider,
    )
    chat = registry.get("chat_workflow")
    assert chat is not None
    steps = [
        {
            "id": "s1",
            "label": "列目录",
            "action": {"kind": "tool", "tool_id": "list_dir", "arguments": {}},
        }
    ]
    out = asyncio.run(chat.execute({"title": "目录检查", "steps": steps}))
    assert out.get("success") is True
    assert out["workflow"]["steps"][0]["action"]["tool_id"] == "list_dir"


def test_save_flow_rejects_fanout_silent_drop() -> None:
    """审查修复：fan-out（非 condition 多出边）保存期拒绝——禁止静默丢分支。"""
    svc = make_service()
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            {"id": "taskA", "type": "ToolNode", "config": {"tool_name": "glob", "params": {}}},
            {"id": "taskB", "type": "ToolNode", "config": {"tool_name": "glob", "params": {}}},
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [
            edge("e1", "start", "taskA"),
            edge("e2", "start", "taskB"),
            edge("e3", "taskA", "end"),
            edge("e4", "taskB", "end"),
        ],
        "start_node": "start",
    }
    result = svc.save_flow("并行图", doc)
    assert result.get("ok") is False
    assert "Invalid workflow graph" in (result.get("error") or "")


def test_execute_fanout_fails_loudly_not_silent_completed() -> None:
    """直接 execute fan-out 图必须 FAILED + 显式错误（不能静默 completed 丢分支）。"""
    from bandi.tools.workflow.engine import WorkflowEngine

    engine = WorkflowEngine()
    ran: list[str] = []

    async def runner(name, params):
        ran.append(name)

    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            {"id": "taskA", "type": "ToolNode", "config": {"tool_name": "alpha", "params": {}}},
            {"id": "taskB", "type": "ToolNode", "config": {"tool_name": "beta", "params": {}}},
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [
            edge("e1", "start", "taskA"),
            edge("e2", "start", "taskB"),
            edge("e3", "taskA", "end"),
            edge("e4", "taskB", "end"),
        ],
        "start_node": "start",
    }
    result = asyncio.run(engine.execute(doc, tool_runner=runner))
    assert result.status == ExecutionStatus.FAILED.value
    assert result.errors, "fan-out 必须显式报错"
    assert ran == [], "校验失败不得执行任何节点"


def test_save_flow_rejects_cycle_at_validation() -> None:
    """审查修复：环 DAG 保存期拒绝（运行期才 fail 已晚——save 前静态拒绝）。"""
    svc = make_service()
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            {"id": "n2", "type": "TransformNode", "config": {"template": "${inputs.x}"}},
            {"id": "n3", "type": "TransformNode", "config": {"template": "${inputs.x}"}},
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [
            edge("e1", "start", "n2"),
            edge("e2", "n2", "n3"),
            edge("e3", "n3", "n2"),  # 环
            edge("e4", "n3", "end"),
        ],
        "start_node": "start",
    }
    result = svc.save_flow("环图", doc)
    assert result.get("ok") is False
    assert "Invalid workflow graph" in (result.get("error") or "")
    assert svc.list_flows() == [], "环图不得部分落库"


def test_save_flow_rejects_dangling_edge() -> None:
    """审查修复：悬空边（target/source 不在节点集）保存期拒绝。"""
    svc = make_service()
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            {"id": "n2", "type": "TransformNode", "config": {"template": "hi"}},
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "n2"), edge("e2", "n2", "ghost_node")],
        "start_node": "start",
    }
    result = svc.save_flow("悬空边图", doc)
    assert result.get("ok") is False
    assert "Invalid workflow graph" in (result.get("error") or "")


def test_condition_branch_still_allowed_two_edges() -> None:
    """回归护栏：condition 节点双出边（true/false）是合法 fan-out，不得误拒。"""
    svc = make_service()
    result = svc.save_flow("条件图", build_conditional_doc(), publish=False)
    assert result.get("ok") is not False, f"条件双分支必须可保存: {result}"
    assert result.get("status") == "draft", "成功保存返回 status=draft"
