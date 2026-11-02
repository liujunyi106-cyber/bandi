"""契约族：长期记忆 × 本体知识库的隔离（计划附录 A：记忆↔本体两系统隔离）。

行为不变式（用户点名验收）：
1. **写入路径分离**——直写（long_term）/ 草稿（staging 暂存）/ 本地（项目 entries）
   / 本体（三元组）四路各写各库，互不串写：
   - `ontology_assert` 只写本体库，long_term 零新增；
   - `memory_assert` 只写长期记忆，本体库零新增；
   - 自然语言轮次的直写只落 long_term（显式「记住」/可记事实），本体零写入；
   - 草稿 ≠ 直写：暂存只进 pending store，批准回放（replay）才落 long_term。
2. **检索召回分离**：
   - `memory_search` 只召回记忆条目，不返回本体事实；
   - `ontology_query` 只查图事实，不返回记忆条目；
   - 增强（enricher）只读：召回增强不产生任何写入（两库计数不变）；
   - 同名内容两库各自独立：本体撤回不影响记忆；记忆遗忘不影响本体。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.ontology.engine import stats_of
from bandi.ontology.lookup import OntologyEntityLookup
from bandi.ontology.spec import open_ontology
from bandi.ontology.triple_store import OntologyStore, StoreLimits

# 两库使用**可区分的独有词元**：任何交叉串写都会让断言立即暴露
MEMORY_MARK = "记忆独有词元-QX"
ONTOLOGY_MARK = "寰宇控股"


@pytest.fixture()
def long_term(tmp_path: Path) -> Any:
    store = LongTermMemory(str(tmp_path / "long_term.db"))
    yield store
    store.close()


@pytest.fixture()
def ontology(tmp_path: Path) -> Any:
    store = OntologyStore(open_ontology(str(tmp_path / "ontology.db")), StoreLimits())
    yield store
    store.close()


def _ontology_tools(store: OntologyStore) -> dict[str, Any]:
    from bandi.ontology.tools import OntologyToolContext
    from bandi.tools.ontology_tool_adapter import build_ontology_tools

    return {t.name: t for t in build_ontology_tools(OntologyToolContext(store=store))}


def _seed_ontology(store: OntologyStore) -> None:
    """本体侧种子：类 Customer + 实体「明远科技」+ 关系事实（独有词元寰宇控股）。"""
    now = "2026-10-14T00:00:00Z"
    store.define_class({"id": "Customer", "label": "客户"}, now)
    store.define_relation({"id": "parent_of", "domain": ["Customer"], "range": ["Customer"]}, now)
    store.assert_entity({"id": "mingyuan", "classes": ["Customer"], "label": "明远科技"}, now)
    store.assert_entity({"id": "huanyu", "classes": ["Customer"], "label": ONTOLOGY_MARK}, now)
    store.assert_fact({"subject": "mingyuan", "predicate": "parent_of", "object": "huanyu"}, now)


def _seed_memory(store: LongTermMemory) -> MemoryItem:
    """记忆侧种子：直写条目含独有词元（内容与本体事实刻意不同）。

    实体名带「」引号：enricher 抽取器对裸中文名保守（防误报），引号形态是
    其识别约定（记忆增强按此触发）。
    """
    item = MemoryItem(
        item_id="turn:test:1",
        content=f"「明远科技」是华东区重点客户（{MEMORY_MARK}）",
        importance=0.5,
        metadata={"evidence": "用户陈述", "memory_type": "business_fact"},
    )
    store.store(item)
    return item


# ── 1. 写入路径分离 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ontology_write_never_touches_long_term(long_term: Any, ontology: Any) -> None:
    """预期：ontology_define/assert 只落本体三表；long_term 计数为 0。

    实体与事实成对断言——只引入实体不引入事实会触发孤立实体守卫（无确认
    通道时回滚），成对形态才是本用例要验证的「本体写入」路径。
    """
    tools = _ontology_tools(ontology)
    await tools["ontology_define"].execute(
        {
            "classes": [{"id": "Customer", "label": "客户"}],
            "relations": [{"id": "parent_of", "domain": ["Customer"], "range": ["Customer"]}],
        }
    )
    output = await tools["ontology_assert"].execute(
        {
            "entities": [
                {"id": "mingyuan", "classes": ["Customer"], "label": "明远科技"},
                {"id": "huanyu", "classes": ["Customer"], "label": ONTOLOGY_MARK},
            ],
            "facts": [{"subject": "mingyuan", "predicate": "parent_of", "object": "huanyu"}],
        }
    )
    assert "2 个实体" in output, f"本体断言应成功：{output}"
    assert stats_of(ontology.graph()).entities == 2
    assert long_term.count() == 0, "本体写入不得在长期记忆落任何条目"


@pytest.mark.asyncio
async def test_memory_assert_never_touches_ontology(long_term: Any, ontology: Any) -> None:
    """预期：memory_assert（直写三元组）只落 long_term；本体计数全 0。"""
    from bandi.tools.memory_tools import MemoryAssertTool

    tool = MemoryAssertTool(long_term=long_term)
    result = await tool.execute(
        {"subject": "明远科技", "predicate": "owned_by", "object": "华东区"}
    )
    assert result.get("asserted") is True, f"记忆三元组应断言成功：{result}"
    assert long_term.count() == 1
    stats = stats_of(ontology.graph())
    assert (stats.classes, stats.relations, stats.entities, stats.facts) == (0, 0, 0, 0), (
        "记忆写入不得在本体库落任何类/关系/实体/事实"
    )


@pytest.mark.asyncio
async def test_natural_turn_direct_write_stays_in_long_term(tmp_path: Path) -> None:
    """预期（生产主链路）：自然语言显式「记住」→ 仅 long_term +1（turn: 前缀）；本体零写入。

    镜像真实内核装配：run_agent_turn 同时挂本体 store 与真实 long_term——
    直写规则（B1）只在用户输入侧触发，且只写记忆库。
    """
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.message import LLMResponse
    from bandi.llm.mock import MockProvider
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore

    class _Rec(MockProvider):
        async def complete(self, messages: Any, tools: Any = None, **kw: Any) -> Any:
            return await super().complete(messages, tools=tools, **kw)

    long_term = LongTermMemory(str(tmp_path / "long_term.db"))
    ontology = OntologyStore(open_ontology(str(tmp_path / "ontology.db")), StoreLimits())
    manager = MemoryManager(long_term=long_term)
    session = SessionStore(str(tmp_path / "session.db"))
    sid = session.create_session(mode="execute")

    provider = _Rec()
    provider.add_response(LLMResponse(content="好的，已记住。"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        await run_agent_turn(
            user_input="记住：明远科技是华东区的重点客户",
            session_id=sid,
            session_store=session,
            memory_manager=manager,
            triple_ontology_store=ontology,
        )

    assert long_term.count() == 1, "显式「记住」应直写 1 条长期记忆"
    items = long_term.list_items()
    assert items and items[0].id.startswith("turn:"), f"直写条目 id 前缀应为 turn:：{items}"
    assert stats_of(ontology.graph()).entities == 0, "自然语言直写不得写入本体库"
    long_term.close()
    ontology.close()


@pytest.mark.asyncio
async def test_plain_turn_never_direct_writes(tmp_path: Path) -> None:
    """预期（B1 规则）：普通自然语言描述（无「记住」、非可记事实）零直写——两库均 0。"""
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.message import LLMResponse
    from bandi.llm.mock import MockProvider
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore

    long_term = LongTermMemory(str(tmp_path / "long_term.db"))
    ontology = OntologyStore(open_ontology(str(tmp_path / "ontology.db")), StoreLimits())
    manager = MemoryManager(long_term=long_term)
    session = SessionStore(str(tmp_path / "session.db"))
    sid = session.create_session(mode="execute")

    provider = MockProvider()
    provider.add_response(LLMResponse(content="我看看这个项目。"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        await run_agent_turn(
            user_input="帮我看看这个项目的目录结构",
            session_id=sid,
            session_store=session,
            memory_manager=manager,
            triple_ontology_store=ontology,
        )

    assert long_term.count() == 0, "普通消息不得直写长期记忆"
    assert stats_of(ontology.graph()).entities == 0
    long_term.close()
    ontology.close()


# ── 1b. 本体写入轮的记忆副作用隔离────────────────────


def _no_ask_config() -> Any:
    """测试配置：清空 ask 审批（工具直执行）——审批链另有契约覆盖。"""
    from bandi.core.config import BandiConfig

    cfg = BandiConfig()
    cfg.permissions.ask_tools = []
    cfg.permissions.ask_risk_levels = []
    return cfg


def _tool_call_response(tool_name: str, arguments: dict[str, Any]) -> Any:
    from bandi.core.message import LLMResponse, ToolCall

    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=f"tc-{tool_name}", name=tool_name, arguments=arguments)],
        finish_reason="tool_calls",
    )


def _final_response(text: str) -> Any:
    from bandi.core.message import LLMResponse

    return LLMResponse(content=text, finish_reason="stop")


@pytest.mark.asyncio
async def test_ontology_write_turn_skips_auto_direct_write(tmp_path: Path) -> None:
    """预期：本体写入轮——用户输入为可记事实（含数字）时**不自动直写**
    长期记忆（防同一内容「本体一份 + 长期记忆一份」重复；写本体=只写本体）。"""
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore

    long_term = LongTermMemory(str(tmp_path / "long_term.db"))
    ontology = OntologyStore(open_ontology(str(tmp_path / "ontology.db")), StoreLimits())
    manager = MemoryManager(long_term=long_term)
    session = SessionStore(str(tmp_path / "session.db"))
    sid = session.create_session(mode="execute")

    provider = MockProvider()
    provider.add_response(
        _tool_call_response("ontology_define", {"classes": [{"id": "Customer", "label": "客户"}]})
    )
    provider.add_response(_final_response("已把 Customer 类写进本体。"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        await run_agent_turn(
            user_input="我的习惯是 Customer 这类客户走专属流程，编号 001",
            session_id=sid,
            session_store=session,
            memory_manager=manager,
            triple_ontology_store=ontology,
            bandi_config=_no_ask_config(),
        )

    assert stats_of(ontology.graph()).classes == 1, "本体写入应生效"
    assert long_term.count() == 0, "本体写入轮不得自动直写长期记忆（写本体=只写本体）"
    long_term.close()
    ontology.close()


@pytest.mark.asyncio
async def test_ontology_write_turn_keeps_explicit_remember(tmp_path: Path) -> None:
    """回归护栏：显式「记住」是用户主动指令——本体写入轮仍直写（不因隐式规则失效）。"""
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore

    long_term = LongTermMemory(str(tmp_path / "long_term.db"))
    ontology = OntologyStore(open_ontology(str(tmp_path / "ontology.db")), StoreLimits())
    manager = MemoryManager(long_term=long_term)
    session = SessionStore(str(tmp_path / "session.db"))
    sid = session.create_session(mode="execute")

    provider = MockProvider()
    provider.add_response(
        _tool_call_response("ontology_define", {"classes": [{"id": "Customer", "label": "客户"}]})
    )
    provider.add_response(_final_response("已记住并写进本体。"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        await run_agent_turn(
            user_input="记住：Customer 是客户类型，编号 002；同时把它写进本体",
            session_id=sid,
            session_store=session,
            memory_manager=manager,
            triple_ontology_store=ontology,
            bandi_config=_no_ask_config(),
        )

    assert stats_of(ontology.graph()).classes == 1, "本体写入应生效"
    assert long_term.count() == 1, "显式「记住」不应被本体轮守卫拦截"
    long_term.close()
    ontology.close()


@pytest.mark.asyncio
async def test_ontology_query_turn_keeps_auto_direct_write(tmp_path: Path) -> None:
    """破坏者：本体**读取**轮（ontology_query）不跳过——只有写入工具才拦。"""
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore

    long_term = LongTermMemory(str(tmp_path / "long_term.db"))
    ontology = OntologyStore(open_ontology(str(tmp_path / "ontology.db")), StoreLimits())
    manager = MemoryManager(long_term=long_term)
    session = SessionStore(str(tmp_path / "session.db"))
    sid = session.create_session(mode="execute")

    provider = MockProvider()
    provider.add_response(_tool_call_response("ontology_query", {"mode": "stats"}))
    provider.add_response(_final_response("本体当前为空。"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        await run_agent_turn(
            user_input="我的习惯是 Customer 这类客户走专属流程，编号 003",
            session_id=sid,
            session_store=session,
            memory_manager=manager,
            triple_ontology_store=ontology,
            bandi_config=_no_ask_config(),
        )

    assert long_term.count() == 1, "本体读取轮不得拦自动直写（只有写入工具才拦）"
    long_term.close()
    ontology.close()


@pytest.mark.parametrize(
    ("messages_spec", "expected"),
    [
        (["user", ("tool", "ontology_define"), "assistant"], True),
        (["user", ("tool", "ontology_assert"), ("tool", "bash"), "assistant"], True),
        (["user", ("tool", "ontology_retract"), "assistant"], True),
        (["user", ("tool", "ontology_query"), "assistant"], False),
        (["user", ("tool", "bash"), "assistant"], False),
        (["user", ("tool", "ontology_assert"), "assistant", "user", "assistant"], False),
        (["user", "assistant"], False),
    ],
)
def test_ontology_write_turn_detector(messages_spec: list, expected: bool) -> None:
    """检测器参数化：本用户轮含本体写入工具消息 → True；读工具/非本体/历史轮 → False。"""
    from types import SimpleNamespace

    from bandi.core.agent_runner import _ontology_write_turn
    from bandi.core.message import Message, MessageRole

    role_map = {"user": MessageRole.USER, "assistant": MessageRole.ASSISTANT}
    messages = []
    for item in messages_spec:
        if isinstance(item, tuple):
            messages.append(
                Message(
                    role=MessageRole.TOOL,
                    content="(output)",
                    tool_call_id="t1",
                    name=item[1],
                    turn_index=1,
                )
            )
        else:
            messages.append(Message(role=role_map[item], content="x", turn_index=1))
    ctx = SimpleNamespace(messages=messages)
    assert _ontology_write_turn(ctx) is expected


@pytest.mark.asyncio
async def test_ontology_write_turn_episode_tagged(tmp_path: Path) -> None:
    """预期：本体写入轮的 episode 带 ontology_turn 标记（提名通道据此跳过）。"""
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.consolidation import ONTOLOGY_TURN_TAG
    from bandi.memory.episodic import EpisodicStore
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore

    long_term = LongTermMemory(str(tmp_path / "long_term.db"))
    episodic = EpisodicStore(str(tmp_path / "episodic.db"))
    manager = MemoryManager(long_term=long_term, episodic=episodic)
    session = SessionStore(str(tmp_path / "session.db"))
    sid = session.create_session(mode="execute")

    provider = MockProvider()
    provider.add_response(
        _tool_call_response("ontology_define", {"classes": [{"id": "Customer", "label": "客户"}]})
    )
    provider.add_response(_final_response("已写进本体。"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        await run_agent_turn(
            user_input="我的习惯是 Customer 这类客户走专属流程，编号 004",
            session_id=sid,
            session_store=session,
            memory_manager=manager,
            triple_ontology_store=None,
            bandi_config=_no_ask_config(),
        )

    episodes = episodic.list_recent(limit=10)
    tagged = [ep for ep in episodes if ONTOLOGY_TURN_TAG in (ep.tags or [])]
    assert tagged, f"本体写入轮 episode 应带标记：{[(e.episode_id, e.tags) for e in episodes]}"
    long_term.close()
    episodic.close()


def test_ontology_turn_episode_skipped_by_consolidation(tmp_path: Path) -> None:
    """预期：带本体轮标记的 episode 不进晋升通道（对照组正常 episode 照常提名）。"""
    from bandi.memory.consolidation import ONTOLOGY_TURN_TAG, consolidate_episodes
    from bandi.memory.episodic import Episode, EpisodicStore
    from bandi.memory.staging import MemoryPendingStore, reset_pending_store

    pending = MemoryPendingStore(str(tmp_path / "pending"))
    reset_pending_store(pending)
    try:
        episodic = EpisodicStore(str(tmp_path / "episodic.db"))
        long_term = LongTermMemory(str(tmp_path / "long_term.db"))
        episodic.record(
            Episode(
                episode_id="ep:norm",
                summary="华东区重点客户的年度预算评审流程已完成梳理",
                session_id="s1",
                importance=0.9,
                recall_count=3,
            )
        )
        episodic.record(
            Episode(
                episode_id="ep:onto",
                summary="本体操作：写入实体明远科技并等待人工审批",
                session_id="s1",
                importance=0.9,
                recall_count=3,
                tags=[ONTOLOGY_TURN_TAG],
            )
        )
        consolidate_episodes(episodic, long_term)
        staged = pending.list_pending()
        blob = str([r.payload for r in staged])
        assert "ep:norm" in blob, f"对照组应被提名：{blob[:300]}"
        assert "ep:onto" not in blob, f"本体轮 episode 不得被提名：{blob[:300]}"
        long_term.close()
        episodic.close()
    finally:
        reset_pending_store(None)


def test_staged_draft_not_in_long_term_until_replayed(
    long_term: Any, ontology: Any, tmp_path: Path
) -> None:
    """预期：草稿（staging）≠ 直写——暂存只进 pending store；批准回放才落 long_term。"""
    from bandi.memory.staging import (
        MemoryPendingStore,
        replay_record,
        reset_pending_store,
        stage_long_term_write,
    )

    pending = MemoryPendingStore(str(tmp_path / "pending"))
    reset_pending_store(pending)
    try:
        item = MemoryItem(
            item_id="turn:draft:1",
            content=f"草稿内容（{MEMORY_MARK}）",
            importance=0.5,
            metadata={"evidence": "草稿", "memory_type": "business_fact"},
        )
        staged = stage_long_term_write(
            item,
            project_id=None,
            employee_id=None,
            action="store_long_term",
            summary="草稿摘要",
            origin="agent",
        )
        assert staged is True
        assert len(pending.list_pending()) == 1, "暂存应进 pending store"
        assert long_term.count() == 0, "草稿阶段不得直写 long_term"
        assert stats_of(ontology.graph()).entities == 0, "草稿不得触碰本体库"

        record = pending.list_pending()[0]
        assert replay_record(record, long_term=long_term) is True
        assert long_term.count() == 1, "批准回放后才落 long_term"
        assert stats_of(ontology.graph()).entities == 0
    finally:
        reset_pending_store(None)


def test_local_entry_write_stays_in_entries(long_term: Any, ontology: Any, tmp_path: Path) -> None:
    """预期：本地（项目 entries/*.md）写入只落 markdown 记忆库——long_term/本体零变化。"""
    from bandi.memory.markdown_memory import MarkdownMemoryStore

    markdown = MarkdownMemoryStore(tmp_path / "memory_dir")
    markdown.save_visible_text(f"本地条目内容（{MEMORY_MARK}）")
    assert long_term.count() == 0, "本地 entries 写入不得直写 long_term"
    assert stats_of(ontology.graph()).entities == 0, "本地 entries 写入不得触碰本体库"


# ── 2. 检索召回分离 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_search_returns_memory_not_ontology(long_term: Any, ontology: Any) -> None:
    """预期：memory_search 只召回记忆条目（scope=long_term）——本体独有词元不得出现。"""
    from bandi.tools.memory_tools import MemorySearchTool

    _seed_memory(long_term)
    _seed_ontology(ontology)
    tool = MemorySearchTool(long_term=long_term)
    result = await tool.execute({"query": "明远科技"})
    hits = result.get("hits") or []
    assert hits, f"记忆检索应命中直写条目：{result}"
    assert all(hit.get("scope") == "long_term" for hit in hits), f"不得混入其他来源：{hits}"
    blob = str(result)
    assert MEMORY_MARK in blob, "记忆独有词元应被召回"
    assert ONTOLOGY_MARK not in blob, "本体事实不得从记忆检索出口泄漏"


@pytest.mark.asyncio
async def test_ontology_query_returns_graph_not_memory(long_term: Any, ontology: Any) -> None:
    """预期：ontology_query 只查图事实
    记忆独有词元不得出现。"""
    _seed_memory(long_term)
    _seed_ontology(ontology)
    tools = _ontology_tools(ontology)
    output = await tools["ontology_query"].execute({"mode": "facts"})
    assert "parent_of" in output and "huanyu" in output, f"本体事实应可查：{output}"
    assert MEMORY_MARK not in output, "记忆条目不得从本体查询出口泄漏"


def test_enrichment_is_read_only_for_both_stores(long_term: Any, ontology: Any) -> None:
    """预期：召回增强（enricher）只读——增强可见（enriched_entities）且两库计数不变。"""
    from bandi.memory.enricher import EntityGraphEnricher
    from bandi.memory.memory_provider import MemoryManager

    _seed_memory(long_term)
    _seed_ontology(ontology)
    manager = MemoryManager(
        long_term=long_term,
        enricher=EntityGraphEnricher(OntologyEntityLookup(ontology)),
    )
    before_lt = long_term.count()
    before_stats = stats_of(ontology.graph())

    results = manager.search_unified("明远科技", limit=5)
    assert results, "召回应有结果"
    enriched = results[0].metadata.get("enriched_entities") or []
    assert "明远科技" in enriched, f"本体实体应进入增强元数据：{results[0].metadata}"

    assert long_term.count() == before_lt, "增强不得写入长期记忆"
    assert stats_of(ontology.graph()) == before_stats, "增强不得写入本体库"


@pytest.mark.asyncio
async def test_same_name_stays_in_own_store(long_term: Any, ontology: Any) -> None:
    """预期：同名两库各自独立——本体撤回不动记忆条目；记忆遗忘不动本体。"""
    from bandi.tools.memory_tools import MemoryAssertTool, MemoryForgetTool

    _seed_ontology(ontology)
    memory_tool = MemoryAssertTool(long_term=long_term)
    result = await memory_tool.execute(
        {"subject": "明远科技", "predicate": "owned_by", "object": "华东区"}
    )
    assert result.get("asserted") is True
    item_id = str(result["id"])

    tools = _ontology_tools(ontology)
    retract = await tools["ontology_retract"].execute({"entities": ["mingyuan"]})
    assert "mingyuan" not in ontology.graph().entities, f"本体实体应被撤回：{retract}"
    assert long_term.count() == 1, "本体撤回不得动记忆条目"
    assert any(it.id == item_id for it in long_term.list_items())

    stats_after_retract = stats_of(ontology.graph())
    forget = MemoryForgetTool(long_term=long_term)
    forgotten = await forget.execute({"scope": "long_term", "id": item_id})
    assert (
        forgotten.get("forgotten") is True
        or forgotten.get("deleted") is True
        or (forgotten.get("ok") is True)
    ), f"记忆遗忘应成功：{forgotten}"
    assert long_term.count() == 0, "记忆遗忘应删除条目"
    assert stats_of(ontology.graph()) == stats_after_retract, "记忆遗忘不得动本体库"
