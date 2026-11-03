from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.loop import AgentLoop
from bandi.core.config import AgentConfig
from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall
from bandi.eval.badcase_tracker import BadCaseTracker
from bandi.eval.hallucination_checker import check_hallucination
from bandi.ontology.lookup import OntologyEntityLookup
from bandi.ontology.spec import open_ontology
from bandi.ontology.tools import OntologyToolContext
from bandi.ontology.triple_store import OntologyStore
from bandi.tools.executor import ToolExecutor
from bandi.tools.ontology_tool_adapter import build_ontology_tools
from bandi.tools.registry import ToolRegistry
from tests.integration.helpers import MockProvider, RecordingBus

_NOW = "T00:00:00"


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _make_lookup(tmp_path: Path) -> OntologyEntityLookup:
    """功能：造一个含「Real Task」实体的三元组本体并返回只读查询适配。"""
    store = OntologyStore(open_ontology(str(tmp_path / "onto.db")))
    store.define_class({"id": "Task"}, _NOW)
    store.assert_entity({"id": "e1", "classes": ["Task"], "label": "Real Task"}, _NOW)
    return OntologyEntityLookup(store)


@pytest.mark.asyncio
async def test_hallucination_blocks_and_records(conn: sqlite3.Connection, tmp_path: Path) -> None:
    bus = RecordingBus()
    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            "I have completed the Phantom Project, Galactic Quest, Quantum Core, Infinite Loop, Sky Bridge, Dream Weaver, Mind Palace, and Star Portal"
        )
    )
    provider.add_response(
        LLMResponse(
            "I have completed the Phantom Project, Galactic Quest, Quantum Core, Infinite Loop, Sky Bridge, Dream Weaver, Mind Palace, and Star Portal (rewrite)"
        )
    )
    provider.add_response(
        LLMResponse(
            "I have completed the Phantom Project, Galactic Quest, Quantum Core, Infinite Loop, Sky Bridge, Dream Weaver, Mind Palace, and Star Portal (rewrite-2)"
        )
    )

    lookup = _make_lookup(tmp_path)
    tracker = BadCaseTracker(conn)

    async def hc_check(text: str, context: str = "") -> object:
        return await check_hallucination(text=text, graph=lookup)

    import types

    hc = types.SimpleNamespace(check=hc_check)

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="What projects are complete?")],
        provider=provider,
        config=AgentConfig(max_iterations=4),
        bus=bus,
        meta={"session_id": "sess-hallu-e2e"},
    )
    ctx.raw_input = "What projects are complete?"
    loop = AgentLoop(max_iterations=4)
    ctx.hallucination_checker = hc
    ctx.badcase_tracker = tracker
    ctx.active_tools = []
    reason = await loop.run(ctx)
    assert reason == TerminalReason.SAFETY_BLOCKED
    assert "幻觉" in (ctx.final_message or "")

    items = tracker.list()
    assert len(items) >= 1
    assert items[0].failure_type == "safety_violation"
    assert items[0].session_id == "sess-hallu-e2e"


@pytest.mark.asyncio
async def test_clean_output_passes_no_tracker(conn: sqlite3.Connection, tmp_path: Path) -> None:
    bus = RecordingBus()
    provider = MockProvider()
    provider.add_response(LLMResponse(content="Real Task is completed as requested"))
    provider.add_response(LLMResponse(content=""))
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t1", name="ontology_query", arguments={"mode": "stats"})],
        )
    )
    provider.add_response(LLMResponse(content="Done"))

    lookup = _make_lookup(tmp_path)

    async def hc_check(text: str, context: str = "") -> object:
        return await check_hallucination(text=text, graph=lookup)

    import types

    hc = types.SimpleNamespace(check=hc_check)
    tracker = BadCaseTracker(conn)

    registry = ToolRegistry()
    store = OntologyStore(open_ontology(str(tmp_path / "onto2.db")))
    query_tool = next(
        tool
        for tool in build_ontology_tools(OntologyToolContext(store=store, inference=True))
        if tool.name == "ontology_query"
    )
    registry.register_instance(query_tool)
    executor = ToolExecutor(registry=registry)

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="check Real Task progress")],
        provider=provider,
        config=AgentConfig(max_iterations=3),
        bus=bus,
        meta={"session_id": "sess-clean-e2e"},
    )
    ctx.raw_input = "check Real Task progress"
    ctx.tool_executor = executor
    ctx.active_tools = registry.list_schemas()
    ctx.hallucination_checker = hc
    ctx.badcase_tracker = tracker
    reason = await AgentLoop(max_iterations=3).run(ctx)
    assert reason == TerminalReason.COMPLETED

    items = tracker.list()
    halluc_items = [i for i in items if i.failure_type == "safety_violation"]
    assert len(halluc_items) == 0
