# 功能：WorkflowStore 单测——Flow/WorkflowExecution sqlite 持久化行为
import pytest

from bandi.tools.workflow.store import (
    ExecutionStatus,
    Flow,
    FlowStatus,
    FlowType,
    WorkflowExecution,
    WorkflowStore,
)


@pytest.fixture()
def store() -> WorkflowStore:
    return WorkflowStore(":memory:")


def build_flow(flow_id: str = "flw_1") -> Flow:
    return Flow(
        flow_id=flow_id,
        name="中文工作流",
        description="测试流程",
        status=FlowStatus.PUBLISHED,
        flow_type=FlowType.WORKFLOW,
        data={
            "nodes": [{"id": "n1", "type": "StartNode"}],
            "edges": [],
            "start_node": "n1",
            "end_node": "n1",
        },
        settings={"timeout_secs": 30},
    )


def test_save_and_get_flow_roundtrip(store: WorkflowStore) -> None:
    flow = build_flow()
    store.save_flow(flow)
    got = store.get_flow("flw_1")
    assert got is not None
    assert got.name == "中文工作流"
    assert got.data == flow.data
    assert got.settings == {"timeout_secs": 30}
    assert got.status == FlowStatus.PUBLISHED
    assert got.flow_type == FlowType.WORKFLOW
    assert got.run_count == 0
    assert got.created_at is not None
    assert got.updated_at is not None


def test_get_missing_returns_none(store: WorkflowStore) -> None:
    assert store.get_flow("missing") is None
    assert store.get_execution("missing") is None


def test_save_flow_upsert_preserves_identity(store: WorkflowStore) -> None:
    store.save_flow(build_flow("flw_1"))
    updated = build_flow("flw_1")
    updated.run_count = 5
    store.save_flow(updated)
    got = store.get_flow("flw_1")
    assert got is not None
    assert got.run_count == 5
    assert len(store.list_flows()) == 1


def test_list_flows_filter_and_limit(store: WorkflowStore) -> None:
    for fid in ("flw_1", "flw_2"):
        store.save_flow(build_flow(fid))
    store.save_flow(
        Flow(flow_id="flw_3", name="草稿", status=FlowStatus.DRAFT, flow_type=FlowType.AGENT)
    )
    assert [f.flow_id for f in store.list_flows()] == ["flw_1", "flw_2", "flw_3"]
    assert [f.flow_id for f in store.list_flows(flow_type=FlowType.WORKFLOW)] == [
        "flw_1",
        "flw_2",
    ]
    assert [f.flow_id for f in store.list_flows(status=FlowStatus.DRAFT)] == ["flw_3"]
    assert [f.flow_id for f in store.list_flows(flow_type=FlowType.AGENT)] == ["flw_3"]
    assert len(store.list_flows(limit=2)) == 2


def test_mark_flow_run_increments_count(store: WorkflowStore) -> None:
    store.save_flow(build_flow("flw_1"))
    store.mark_flow_run("flw_1")
    got = store.get_flow("flw_1")
    assert got is not None
    assert got.run_count == 1
    assert got.last_run_at is not None


def test_execution_create_get_roundtrip(store: WorkflowStore) -> None:
    execution = WorkflowExecution(
        execution_id="ex_1",
        flow_id="flw_1",
        user_id="u_1",
        status=ExecutionStatus.RUNNING,
        inputs={"text": "你好"},
        started_at="2026-09-06T10:00:00",
    )
    store.create_execution(execution)
    got = store.get_execution("ex_1")
    assert got is not None
    assert got.flow_id == "flw_1"
    assert got.user_id == "u_1"
    assert got.inputs == {"text": "你好"}
    assert got.status == ExecutionStatus.RUNNING


def test_update_execution_sets_fields(store: WorkflowStore) -> None:
    store.create_execution(WorkflowExecution(execution_id="ex_1", flow_id="flw_1"))
    store.update_execution(
        "ex_1",
        status=ExecutionStatus.COMPLETED,
        outputs={"out": 1},
        node_count=3,
        execution_history=[{"node_id": "n1"}],
        duration_ms=5,
    )
    got = store.get_execution("ex_1")
    assert got is not None
    assert got.status == ExecutionStatus.COMPLETED
    assert got.outputs == {"out": 1}
    assert got.node_count == 3
    assert got.execution_history == [{"node_id": "n1"}]
    assert got.duration_ms == 5


def test_list_executions_ordered_and_limited(store: WorkflowStore) -> None:
    for i in range(3):
        store.create_execution(WorkflowExecution(execution_id=f"ex_{i}", flow_id="flw_1"))
    executions = store.list_executions(flow_id="flw_1")
    assert [e.execution_id for e in executions] == ["ex_0", "ex_1", "ex_2"]
    assert len(store.list_executions(flow_id="flw_1", limit=2)) == 2
    assert store.list_executions(flow_id="other") == []


def test_update_execution_rejects_unknown_column() -> None:
    """审查修复 update_execution 动态列白名单——非法字段显式 ValueError。"""
    store = WorkflowStore(":memory:")
    exec_id = "ex1"
    store.create_execution(
        WorkflowExecution(
            execution_id=exec_id,
            status=ExecutionStatus.RUNNING,
        )
    )
    try:
        store.update_execution(exec_id, stauts=ExecutionStatus.COMPLETED)  # 拼写错误
        raise AssertionError("非法列必须抛 ValueError")
    except ValueError as exc:
        assert "stauts" in str(exc)
    # 合法列照常
    store.update_execution(exec_id, status=ExecutionStatus.COMPLETED.value)
    assert store.get_execution(exec_id).status == ExecutionStatus.COMPLETED
