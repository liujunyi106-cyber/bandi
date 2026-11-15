# 功能：WorkflowEngine 单测——DAG 执行/条件分支/模板/LLM/工具节点/环检测/暂停恢复
import asyncio

from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider
from bandi.tools.workflow.engine import NodeExecutionError, WorkflowEngine


def run_engine(doc, **kwargs):
    engine = WorkflowEngine()
    return asyncio.run(engine.execute(doc, **kwargs))


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


def test_transform_resolves_input_template() -> None:
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            tnode("n2", {"msg": "收到：${inputs.text}，字数 ${inputs.n}"}),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "n2"), edge("e2", "n2", "end")],
        "start_node": "start",
        "end_node": "end",
    }
    result = run_engine(doc, inputs={"text": "你好", "n": 3})
    assert result.status == "completed"
    assert result.outputs["n2"] == {"msg": "收到：你好，字数 3"}
    assert result.errors == []


def test_transform_refs_previous_node_output() -> None:
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            tnode("pre", "${inputs.x}", "step"),
            tnode("final", "${outputs.pre}-tail"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [
            edge("e1", "start", "pre"),
            edge("e2", "pre", "final"),
            edge("e3", "final", "end"),
        ],
        "start_node": "start",
        "end_node": "end",
    }
    result = run_engine(doc, inputs={"x": "前缀"})
    assert result.status == "completed"
    assert result.outputs["pre"] == "前缀"
    assert result.outputs["final"] == "前缀-tail"


def test_condition_true_false_branches() -> None:
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            {
                "id": "cond",
                "type": "ConditionNode",
                "name": "cond",
                "config": {
                    "condition": {"left": "${inputs.amount}", "operator": "gt", "right": 100}
                },
            },
            tnode("good", "${inputs.amount}-大"),
            tnode("bad", "${inputs.amount}-小"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [
            edge("e1", "start", "cond"),
            edge("e2", "cond", "good", "true"),
            edge("e3", "cond", "bad", "false"),
            edge("e4", "good", "end"),
            edge("e5", "bad", "end"),
        ],
        "start_node": "start",
        "end_node": "end",
    }
    big = run_engine(doc, inputs={"amount": 500})
    assert big.status == "completed"
    assert big.outputs["good"] == "500-大"
    assert "bad" not in big.outputs
    small = run_engine(doc, inputs={"amount": 50})
    assert small.status == "completed"
    assert small.outputs["bad"] == "50-小"
    assert "good" not in small.outputs


def test_condition_operator_in_contains() -> None:
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            {
                "id": "cond",
                "type": "ConditionNode",
                "config": {
                    "condition": {"left": "${inputs.tag}", "operator": "in", "right": ["a", "b"]}
                },
            },
            tnode("yes", "Y"),
            tnode("no", "N"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [
            edge("e1", "start", "cond"),
            edge("e2", "cond", "yes", "true"),
            edge("e3", "cond", "no", "false"),
            edge("e4", "yes", "end"),
            edge("e5", "no", "end"),
        ],
        "start_node": "start",
        "end_node": "end",
    }
    assert run_engine(doc, inputs={"tag": "b"}).outputs["yes"] == "Y"
    assert run_engine(doc, inputs={"tag": "z"}).outputs["no"] == "N"


def test_cycle_detected_fails_without_hang() -> None:
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            tnode("n2", "${inputs.x}"),
            tnode("n3", "${inputs.x}"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [
            edge("e1", "start", "n2"),
            edge("e2", "n2", "n3"),
            edge("e3", "n3", "n2"),
            edge("e4", "n2", "end"),
        ],
        "start_node": "start",
        "end_node": "end",
    }
    result = run_engine(doc, inputs={"x": 1})
    assert result.status == "failed"
    assert any("环" in e or "cycle" in e.lower() for e in result.errors)


def test_start_end_synthesized_when_missing() -> None:
    doc = {"nodes": [tnode("n2", "${inputs.x}")], "edges": [], "start_node": None, "end_node": None}
    result = run_engine(doc, inputs={"x": 1})
    assert result.status == "completed"
    assert result.outputs["n2"] == 1


def test_llm_node_uses_provider_and_resolves_prompt() -> None:
    provider = MockProvider()
    provider.add_response(LLMResponse(content="翻译结果"))
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            {
                "id": "llm",
                "type": "LLMCallNode",
                "config": {"prompt": "翻译成中文：${inputs.text}"},
            },
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "llm"), edge("e2", "llm", "end")],
        "start_node": "start",
        "end_node": "end",
    }
    result = run_engine(doc, inputs={"text": "hello"}, provider=provider)
    assert result.status == "completed"
    assert result.outputs["llm"] == "翻译结果"
    assert provider.call_count == 1


def test_llm_node_without_provider_fails_explicitly() -> None:
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            {"id": "llm", "type": "LLMCallNode", "config": {"prompt": "hi"}},
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "llm"), edge("e2", "llm", "end")],
        "start_node": "start",
        "end_node": "end",
    }
    result = run_engine(doc)
    assert result.status == "failed"
    assert any("provider" in e.lower() for e in result.errors)


def test_pause_probe_checkpoint_and_resume() -> None:
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            tnode("n2", "${inputs.x}-2"),
            tnode("n3", "${outputs.n2}-3"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "n2"), edge("e2", "n2", "n3"), edge("e3", "n3", "end")],
        "start_node": "start",
        "end_node": "end",
    }

    async def paused_run():
        engine = WorkflowEngine()
        calls = {"count": 0}

        async def should_pause() -> bool:
            calls["count"] += 1
            return calls["count"] == 2

        return await engine.execute(doc, inputs={"x": "A"}, should_pause=should_pause)

    paused = asyncio.run(paused_run())
    assert paused.status == "paused"
    assert paused.checkpoint is not None
    assert paused.checkpoint["resume_node"] == "n2"

    resumed = run_engine(doc, inputs={"x": "A"}, resume_state=paused.checkpoint)
    assert resumed.status == "completed"
    assert resumed.outputs["n3"] == "A-2-3"
    assert [h["node_id"] for h in resumed.execution_history] == ["start", "n2", "n3", "end"]


def toolnode(nid, tool_name, params=None, output_var=None):
    node = {
        "id": nid,
        "type": "ToolNode",
        "name": nid,
        "config": {"tool_name": tool_name, "params": params or {}},
    }
    if output_var:
        node["config"]["output_var"] = output_var
    return node


def test_tool_node_invokes_runner_with_resolved_params() -> None:
    async def runner(tool_name, params):
        assert tool_name == "search"
        assert params == {"query": "中医", "limit": 2}
        return {"total": 2, "items": ["a", "b"]}

    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            toolnode("t0", "search", {"query": "${inputs.q}", "limit": 2}),
            tnode("final", "${outputs.t0.total}-尾"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "t0"), edge("e2", "t0", "final"), edge("e3", "final", "end")],
        "start_node": "start",
        "end_node": "end",
    }
    result = run_engine(doc, inputs={"q": "中医"}, tool_runner=runner)
    assert result.status == "completed"
    assert result.outputs["t0"] == {"total": 2, "items": ["a", "b"]}
    assert result.outputs["final"] == "2-尾"
    assert result.errors == []


def test_tool_node_output_var_preserves_typed_result() -> None:
    async def runner(tool_name, params):
        return 42

    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            toolnode("t0", "calc", output_var="num"),
            tnode("final", "${outputs.t0}-${variables.num}"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "t0"), edge("e2", "t0", "final"), edge("e3", "final", "end")],
        "start_node": "start",
        "end_node": "end",
    }
    result = run_engine(doc, tool_runner=runner)
    assert result.status == "completed"
    assert result.outputs["final"] == "42-42"


def test_tool_node_without_runner_fails_explicitly() -> None:
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            toolnode("t0", "x"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "t0"), edge("e2", "t0", "end")],
        "start_node": "start",
        "end_node": "end",
    }
    result = run_engine(doc)
    assert result.status == "failed"
    assert any("tool_runner" in e.lower() for e in result.errors)


def test_tool_node_runner_exception_fails_workflow() -> None:
    async def boom(tool_name, params):
        raise RuntimeError("工具服务不可用")

    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            toolnode("t0", "x"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "t0"), edge("e2", "t0", "end")],
        "start_node": "start",
        "end_node": "end",
    }
    result = run_engine(doc, tool_runner=boom)
    assert result.status == "failed"
    assert any("工具服务不可用" in e for e in result.errors)


def test_validate_counts_nodes_and_rejects_bad_graph() -> None:
    engine = WorkflowEngine()
    good = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            tnode("n2", "x"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "n2"), edge("e2", "n2", "end")],
        "start_node": "start",
        "end_node": "end",
    }
    assert engine.validate(good) == 3
    bad_graphs = (
        {"nodes": [{"id": "x", "type": "NoSuchNode"}], "edges": [], "start_node": "x"},
        {"nodes": [], "edges": []},
    )
    for bad in bad_graphs:
        try:
            engine.validate(bad)
            raise AssertionError("应抛 NodeExecutionError")
        except NodeExecutionError:
            pass


def test_cancel_probe_returns_cancelled() -> None:
    doc = {
        "nodes": [
            {"id": "start", "type": "StartNode"},
            tnode("n2", "${inputs.x}"),
            {"id": "end", "type": "EndNode"},
        ],
        "edges": [edge("e1", "start", "n2"), edge("e2", "n2", "end")],
        "start_node": "start",
        "end_node": "end",
    }

    async def should_cancel() -> bool:
        return True

    result = run_engine(doc, should_cancel=should_cancel)
    assert result.status == "cancelled"
