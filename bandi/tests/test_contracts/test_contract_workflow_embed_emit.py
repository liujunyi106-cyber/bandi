# 功能：契约——workflow_embed_emit 工作流发布卡（LeAgent chat_workflow_embed_emit 对齐）。
# 钉行为：合法 DAG 校验通过 → 结构化 payload（digest + 规范化 flow_data + title/summary/
# flow_id + node_count）；非法 DAG / 非对象 flow_data fail-closed；只读不落库；
# digest 与 workflow_save 同口径（同一文档两处 digest 一致，卡片与已存流程可对齐身份）。
from __future__ import annotations

import asyncio

from bandi.tools.workflow.service import WorkflowService
from bandi.tools.workflow.store import WorkflowStore
from bandi.tools.workflow.workflow_embed_emit import WorkflowEmbedEmitTool


def _dag() -> dict:
    """功能：契约基准——合法两节点线性 DAG。"""
    return {
        "nodes": [
            {"id": "start", "type": "start", "name": "起点", "config": {}},
            {
                "id": "step1",
                "type": "tool",
                "name": "查天气",
                "config": {"tool_name": "web_search", "params": {"query": "天气"}},
            },
            {"id": "end", "type": "end", "name": "终点", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "step1"},
            {"id": "e2", "source": "step1", "target": "end"},
        ],
        "start_node_id": None,
    }


def _run(params: dict) -> dict:
    tool = WorkflowEmbedEmitTool()
    return asyncio.run(tool.execute(params))


def test_valid_dag_returns_embed_payload() -> None:
    result = _run({"title": "发货流程", "summary": "三步", "flow_data": _dag(), "flow_id": "f-1"})

    assert result["success"] is True
    assert result["title"] == "发货流程"
    assert result["summary"] == "三步"
    assert result["flow_id"] == "f-1"
    assert result["node_count"] == 3
    assert isinstance(result["digest"], str) and len(result["digest"]) == 64
    assert result["flow_data"]["nodes"][0]["id"] == "start"


def test_invalid_dag_is_rejected() -> None:
    dag = _dag()
    dag["edges"].append({"id": "bad", "source": "step1", "target": "ghost"})

    result = _run({"title": "坏图", "flow_data": dag})

    assert result.get("success") is not True
    assert result.get("ok") is False and "error" in result


def test_non_object_flow_data_is_rejected() -> None:
    result = _run({"title": "坏输入", "flow_data": "not-a-dict"})  # type: ignore[arg-type]

    assert result.get("ok") is False


def test_digest_matches_workflow_save(tmp_path) -> None:
    dag = _dag()
    embed = _run({"title": "发货流程", "flow_data": dag})

    service = WorkflowService(WorkflowStore(str(tmp_path / "workflow.db")))
    saved = service.save_flow(name="发货流程", flow_data=dag)

    assert saved["digest"] == embed["digest"], "发布卡与保存流程的 digest 必须同口径"


def test_digest_is_stable_across_calls() -> None:
    first = _run({"title": "a", "flow_data": _dag()})
    second = _run({"title": "b", "flow_data": _dag()})

    assert first["digest"] == second["digest"]


def test_tool_is_read_only_by_contract() -> None:
    tool = WorkflowEmbedEmitTool()

    assert tool.is_read_only is True
    assert tool.feature_group == "workflow"
    assert tool.name == "workflow_embed_emit"
