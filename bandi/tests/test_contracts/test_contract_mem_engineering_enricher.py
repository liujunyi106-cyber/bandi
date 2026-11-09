"""
本体图只读增强契约+ 工程增强修复契约：

extract_entities 纯文本抽取三类（引号专名/路径/英文驼峰）——不访问图
find_facts 实体名→图内事实文本（name 优先、description 回退），去重 + 上限
find_contradictions 共享实体 + 低相似（token Jaccard < 0.15）+ 长度 ≥20；
 高相似/短文本/无共享实体不报
search_unified 增强只写 metadata（enriched_entities），不改 value/key/排序
contradiction_with 双向标注另一条命中 key
装配（_build_memory_manager / run_agent_turn）把本体只读适配注入 enricher
MemoryContextSource.collect 消费增强字段：相关实体/矛盾后缀；无字段逐字不变
作用域：EntityGraphEnricher(project_id=...) 只召回本项目 + 全局实体
脏类型防御：输入/图返回脏类型不炸链、不写字符拆分
组合排序：reliability × question 倍率 × intent 加法 × 软保底；
 缺省 reliability 与「无 factor」逐条一致（兼容 B 组把乘子移到最终分的改法）
增强只作用于截断后返回集（limit=1 时 extract 调用恰 1 次）

破坏者断言：
- 图方法抛异常 → search_unified 仍返回未增强结果（降级不炸）
- enricher 自身抛异常 → 同样降级
- 假图写方法（add_entity/add_relation/save/delete）在增强全流程零调用（只读证明）
- enricher=None → 零行为（既有 facade 契约不回归）
- 实体无 project_id 字段（旧库形态）→ 作用域过滤不得误杀
- find_related 已删除（零生产调用 DoD-11 实锤）——契约引用同步删除"""

from pathlib import Path
from typing import Any

import pytest


class _FakeEntity:
    """确定性实体替身——仅暴露 enricher 消费字段。"""

    def __init__(
        self,
        entity_id: str,
        name: str,
        description: str = "",
        project_id: str | None = None,
    ) -> None:
        self.id = entity_id
        self.name = name
        self.description = description
        self.type = "note"
        self.project_id = project_id


class _FakeGraph:
    """假图：只读 API 确定性返回 + 写方法炮台（只读证明）。"""

    def __init__(
        self,
        entities: dict[str, _FakeEntity] | None = None,
        fail_on: str = "",
    ) -> None:
        self.entities = entities or {}
        self.fail_on = fail_on
        self.calls: dict[str, list[Any]] = {"find_entities": []}
        self.project_calls: list[Any] = []
        self.writes: list[str] = []

    def find_entities(
        self,
        entity_type: Any = None,
        query: str = "",
        tags: Any = None,
        limit: int = 50,
        project_id: str | None = None,
    ) -> list[_FakeEntity]:
        """功能：镜像实体检索作用域语义（项目命中 + 全局实体）。"""
        self.calls["find_entities"].append(query)
        self.project_calls.append(project_id)
        if self.fail_on == "find_entities":
            raise RuntimeError("graph unavailable")
        key = (query or "").strip().lower()
        rows = [
            e
            for e in self.entities.values()
            if key and (key in e.name.lower() or key in e.description.lower())
        ]
        if project_id:
            rows = [e for e in rows if getattr(e, "project_id", None) in (None, "", project_id)]
        return rows[:limit]

    # 功能：写方法炮台——被 enricher 调用即炸（只读证明）

    def add_entity(self, *args: Any, **kwargs: Any) -> None:
        self.writes.append("add_entity")
        raise AssertionError("enricher 不得调用图写方法 add_entity")

    def add_relation(self, *args: Any, **kwargs: Any) -> None:
        self.writes.append("add_relation")
        raise AssertionError("enricher 不得调用图写方法 add_relation")

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.writes.append("save")
        raise AssertionError("enricher 不得调用图写方法 save")

    def delete(self, *args: Any, **kwargs: Any) -> None:
        self.writes.append("delete")
        raise AssertionError("enricher 不得调用图写方法 delete")


class _DirtyGraph:
    """破坏者：find_entities 返回脏类型（str/int/坏 tuple 行/任意对象）。"""

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def find_entities(self, **kwargs: Any) -> Any:
        return self.payload


class _FakeLongTerm:
    """确定性 long_term 替身——预置命中，零 IO。"""

    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def search_fts_ranked(self, query: str, limit: int = 5, **kwargs: Any) -> list[Any]:
        return self.items[:limit]

    def count(self) -> int:
        return len(self.items)

    def close(self) -> None:
        pass


class _BrokenEnricher:
    """破坏者：任何调用都抛异常——验证 manager 层兜底。"""

    def extract_entities(self, text: str) -> list[Any]:
        raise RuntimeError("enricher down")

    def find_facts(self, entities: list[Any], *, limit: int = 5) -> list[str]:
        raise RuntimeError("enricher down")

    def find_contradictions(self, candidates: list[str]) -> list[tuple[str, str]]:
        raise RuntimeError("enricher down")


def _mk_item(item_id: str, content: str, importance: float = 0.5) -> Any:
    from bandi.memory.long_term import MemoryItem

    return MemoryItem(item_id=item_id, content=content, importance=importance)


def _mk_manager(items: list[Any], graph: Any | None = None, project_id: str | None = None) -> Any:
    from bandi.memory.enricher import EntityGraphEnricher
    from bandi.memory.memory_provider import MemoryManager

    enricher = EntityGraphEnricher(graph, project_id=project_id) if graph is not None else None
    return MemoryManager(long_term=_FakeLongTerm(items), enricher=enricher)


_LEFT = "「蓝鲸项目」的验收标准是并发安全与崩溃恢复能力需量化"
_RIGHT = "「蓝鲸项目」今天下雨了所以延期交付吧另外还有别的事情"


# ── extract_entities 纯文本抽取 ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("把「蓝鲸项目」归档到知识库", "蓝鲸项目"),
        ("打开 bandi/memory/enricher.py 查看", "bandi/memory/enricher.py"),
        ("交给 MemoryEnricher 处理", "MemoryEnricher"),
    ],
)
def test_extract_entities_covers_three_patterns(text: str, expected: str) -> None:
    """契约（族级）：引号专名 / 路径 / 英文驼峰三类均被抽取。"""
    from bandi.memory.enricher import EntityGraphEnricher

    enricher = EntityGraphEnricher(_FakeGraph())
    names = {ref.name for ref in enricher.extract_entities(text)}
    assert expected in names, f"应抽取 {expected}（实际 {sorted(names)}）"


def test_extract_entities_does_not_touch_graph() -> None:
    """契约（P1b）：纯文本抽取零图访问（图不可用时仍可抽取）。"""
    from bandi.memory.enricher import EntityGraphEnricher

    graph = _FakeGraph(fail_on="find_entities")
    enricher = EntityGraphEnricher(graph)
    names = {ref.name for ref in enricher.extract_entities("「蓝鲸项目」MemoryEnricher")}
    assert "蓝鲸项目" in names and "MemoryEnricher" in names
    assert graph.calls["find_entities"] == [], "extract_entities 不得访问图"


def test_extract_entities_dedupes_preserving_first_seen() -> None:
    """契约（P1c）：同一名称重复出现只产出一条（按首见顺序）。"""
    from bandi.memory.enricher import EntityGraphEnricher

    enricher = EntityGraphEnricher(_FakeGraph())
    names = [ref.name for ref in enricher.extract_entities("「蓝鲸项目」和「蓝鲸项目」")]
    assert names == ["蓝鲸项目"]


# ── 只读事实查询 ───────────────────────────────────────────────────────────


def test_injected_find_entities_is_used() -> None:
    """契约（注入面）：find_entities 可显式注入（graph=None 亦可工作）。"""
    from bandi.memory.enricher import EntityGraphEnricher, EntityRef, MemoryEnricher

    calls: list[str] = []

    def find_entities(**kwargs: Any) -> list[_FakeEntity]:
        calls.append(str(kwargs.get("query", "")))
        return [_FakeEntity("e1", "蓝鲸项目")]

    enricher = EntityGraphEnricher(graph=None, find_entities=find_entities)
    facts = enricher.find_facts([EntityRef(name="蓝鲸项目")])

    assert facts == ["蓝鲸项目"]
    assert calls == ["蓝鲸项目"], f"注入函数必须被实际调用（实际 {calls}）"
    assert isinstance(enricher, MemoryEnricher), "实现必须满足 MemoryEnricher Protocol"


def test_find_facts_prefers_name_falls_back_to_description_and_caps() -> None:
    """契约：事实文本 name 优先、description 回退；去重且总上限 limit。"""
    from bandi.memory.enricher import EntityGraphEnricher, EntityRef

    graph = _FakeGraph(
        entities={
            "e1": _FakeEntity("e1", "蓝鲸项目", "描述甲"),
            "e2": _FakeEntity("e2", "蓝鲸项目", "描述乙"),
            "e3": _FakeEntity("e3", "", "蓝鲸项目旧描述"),
        }
    )
    enricher = EntityGraphEnricher(graph)
    assert enricher.find_facts([EntityRef(name="蓝鲸项目")], limit=5) == [
        "蓝鲸项目",
        "蓝鲸项目旧描述",
    ]
    assert enricher.find_facts([EntityRef(name="蓝鲸项目")], limit=1) == ["蓝鲸项目"]
    assert enricher.find_facts([EntityRef(name="")], limit=5) == []


# ── 作用域：跨项目实体不泄漏 ───────────────────────────────────────────────


def test_find_facts_scopes_to_project_and_keeps_global() -> None:
    """契约：project_id 非空 → 仅本项目 + 全局实体；跨项目实体不得泄漏。"""
    from bandi.memory.enricher import EntityGraphEnricher, EntityRef

    graph = _FakeGraph(
        entities={
            "a": _FakeEntity("a", "蓝鲸项目甲文档", project_id="proj-a"),
            "b": _FakeEntity("b", "蓝鲸项目乙文档", project_id="proj-b"),
            "g": _FakeEntity("g", "蓝鲸项目全局文档"),
        }
    )
    scoped = EntityGraphEnricher(graph, project_id="proj-a")
    facts = scoped.find_facts([EntityRef(name="蓝鲸项目")], limit=10)

    assert "蓝鲸项目甲文档" in facts, "本项目实体必须命中"
    assert "蓝鲸项目全局文档" in facts, "全局（无 project_id）实体不得误杀"
    assert "蓝鲸项目乙文档" not in facts, "跨项目实体必须被作用域过滤"
    assert graph.project_calls == ["proj-a"], "作用域必须透传到图查询"

    unscoped = EntityGraphEnricher(graph)
    all_facts = unscoped.find_facts([EntityRef(name="蓝鲸项目")], limit=10)
    assert "蓝鲸项目乙文档" in all_facts, "缺省 project_id=None 必须零行为"
    assert graph.project_calls[-1] is None


def test_find_facts_with_real_triple_store(tmp_path: Any) -> None:
    """契约（P9b 生产形态）：真实 OntologyStore 上增强端到端生效。

    语义变化：三元组本体实体是全局知识层（无 project 维度），lookup 的
    project_id 形参保留但不再过滤——跨项目实体隔离由记忆条目的 scope 承担，
    不再由实体层承担。"""
    from bandi.memory.enricher import EntityGraphEnricher, EntityRef
    from bandi.ontology.lookup import OntologyEntityLookup
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore

    store = OntologyStore(open_ontology(str(tmp_path / "ontology.db")))
    store.define_class({"id": "Note"}, "2026-01-01T00:00:00")
    for eid, label in (
        ("a1", "蓝鲸项目甲文档"),
        ("b1", "蓝鲸项目乙文档"),
        ("g1", "蓝鲸项目全局文档"),
    ):
        store.assert_entity({"id": eid, "classes": ["Note"], "label": label}, "2026-01-01T00:00:00")

    enricher = EntityGraphEnricher(OntologyEntityLookup(store), project_id="proj-a")
    facts = enricher.find_facts([EntityRef(name="蓝鲸项目")], limit=10)
    assert "蓝鲸项目甲文档" in facts
    assert "蓝鲸项目全局文档" in facts
    assert "蓝鲸项目乙文档" in facts, "实体层全局：同名前缀实体均命中（隔离在记忆 scope）"


def test_search_unified_enrichment_respects_project_scope() -> None:
    """契约（P9c 主链）：增强产物经 search_unified 元数据回读时同样受作用域约束。"""
    items = [_mk_item("lt-1", "「蓝鲸项目」的部署文档需要评审")]
    graph = _FakeGraph(
        entities={
            "a": _FakeEntity("a", "蓝鲸项目甲文档", project_id="proj-a"),
            "b": _FakeEntity("b", "蓝鲸项目乙文档", project_id="proj-b"),
        }
    )

    scoped = _mk_manager(items, graph=graph, project_id="proj-a").search_unified(
        "蓝鲸项目", limit=5
    )
    unscoped = _mk_manager(items, graph=graph).search_unified("蓝鲸项目", limit=5)

    assert scoped[0].metadata.get("enriched_entities") == ["蓝鲸项目甲文档"]
    assert set(unscoped[0].metadata.get("enriched_entities") or []) == {
        "蓝鲸项目甲文档",
        "蓝鲸项目乙文档",
    }


# ── 矛盾对 ─────────────────────────────────────────────────────────────────


def test_find_contradictions_flags_shared_entity_low_similarity() -> None:
    """契约：共享实体 + Jaccard < 0.15 + 长度均 ≥20 → 一对。"""
    from bandi.memory.enricher import EntityGraphEnricher

    enricher = EntityGraphEnricher(_FakeGraph())
    assert len(_LEFT) >= 20 and len(_RIGHT) >= 20
    assert enricher.find_contradictions([_LEFT, _RIGHT]) == [(_LEFT, _RIGHT)]


def test_find_contradictions_skips_similar_short_or_unshared() -> None:
    """契约（P4b 破坏者）：高相似 / 过短 / 无共享实体均不报。"""
    from bandi.memory.enricher import EntityGraphEnricher

    enricher = EntityGraphEnricher(_FakeGraph())
    similar = _LEFT + "并且要求回归验证"
    short = "「蓝鲸项目」短句"
    unshared = "完全不提项目名的这一条内容很长很长很长很长"
    assert enricher.find_contradictions([_LEFT, similar]) == []
    assert enricher.find_contradictions([_LEFT, short]) == []
    assert enricher.find_contradictions([_LEFT, unshared]) == []


# ── /search_unified 增强挂点 ────────────────────────────────────────────


def test_search_unified_enriches_metadata_without_touching_order() -> None:
    """契约：增强只写 metadata；value/key/排序不变；矛盾双向标注。"""
    items = [_mk_item("lt-1", _LEFT, 0.9), _mk_item("lt-2", _RIGHT, 0.5)]
    graph = _FakeGraph(entities={"e1": _FakeEntity("e1", "蓝鲸项目", "实体描述")})

    plain_hits = _mk_manager(items).search_unified("蓝鲸项目验收", limit=5)
    hits = _mk_manager(items, graph=graph).search_unified("蓝鲸项目验收", limit=5)

    assert [h.key for h in hits] == [h.key for h in plain_hits], "增强不得改变排序/key"
    by_key = {h.key: h for h in hits}
    plain_by_key = {h.key: h for h in plain_hits}
    assert by_key["lt-1"].metadata.get("enriched_entities") == ["蓝鲸项目"]
    assert by_key["lt-1"].value == plain_by_key["lt-1"].value
    assert by_key["lt-1"].metadata.get("contradiction_with") == ["lt-2"]
    assert by_key["lt-2"].metadata.get("contradiction_with") == ["lt-1"]


def test_search_unified_without_enricher_zero_behavior() -> None:
    """契约（P5b）：enricher=None → 零增强字段（既有 façade 契约不回归）。"""
    items = [_mk_item("lt-1", _LEFT)]
    hits = _mk_manager(items).search_unified("蓝鲸项目", limit=5)
    assert [h.key for h in hits] == ["lt-1"]
    assert "enriched_entities" not in hits[0].metadata
    assert "contradiction_with" not in hits[0].metadata


def test_search_unified_survives_graph_exception() -> None:
    """契约（破坏者）：图方法抛异常 → 返回未增强结果，不炸检索链。"""
    items = [_mk_item("lt-1", _LEFT)]
    graph = _FakeGraph(entities={"e1": _FakeEntity("e1", "蓝鲸项目")}, fail_on="find_entities")
    hits = _mk_manager(items, graph=graph).search_unified("蓝鲸项目", limit=5)
    assert [h.key for h in hits] == ["lt-1"]
    assert "enriched_entities" not in hits[0].metadata


def test_search_unified_survives_broken_enricher() -> None:
    """契约（P7b 破坏者）：enricher 自身抛异常 → manager 层兜底返回原结果。"""
    from bandi.memory.memory_provider import MemoryManager

    items = [_mk_item("lt-1", _LEFT)]
    manager = MemoryManager(long_term=_FakeLongTerm(items), enricher=_BrokenEnricher())
    hits = manager.search_unified("蓝鲸项目", limit=5)
    assert [h.key for h in hits] == ["lt-1"]


# ── 脏类型防御（不炸链 / 不写字符拆分） ──────────────────────────────────


def test_enricher_methods_survive_dirty_inputs() -> None:
    """契约：四个入参面收到脏类型 → 返回类型安全空结果，不抛不字符拆分。"""
    from bandi.memory.enricher import EntityGraphEnricher, EntityRef

    enricher = EntityGraphEnricher(_FakeGraph())
    assert enricher.extract_entities(None) == []  # type: ignore[arg-type]
    assert enricher.extract_entities(123) == []  # type: ignore[arg-type]
    assert enricher.find_facts("蓝鲸项目") == []  # type: ignore[arg-type]
    assert enricher.find_facts(None) == []  # type: ignore[arg-type]
    assert enricher.find_facts([EntityRef(name="x")], limit=0) == []
    assert enricher.find_contradictions("不是列表") == []  # type: ignore[arg-type]
    assert enricher.find_contradictions(None) == []  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", ["蓝鲸项目", 123, [("蓝鲸项目", "描述")], None, object()])
def test_find_facts_dirty_rows_never_char_split(payload: Any) -> None:
    """契约（P10b 破坏者）：图返回 str/int/坏 tuple 行 → find_facts 仍返回干净 list。"""
    from bandi.memory.enricher import EntityGraphEnricher, EntityRef

    enricher = EntityGraphEnricher(_DirtyGraph(payload))
    facts = enricher.find_facts([EntityRef(name="蓝鲸项目")], limit=5)

    assert isinstance(facts, list), f"必须返回 list（实际 {type(facts)}）"
    assert all(isinstance(fact, str) and len(fact) > 1 for fact in facts), (
        f"不得写入字符拆分（实际 {facts}）"
    )


@pytest.mark.parametrize("payload", ["蓝鲸项目", 123, [("蓝鲸项目", "描述")]])
def test_search_unified_dirty_graph_metadata_never_char_split(payload: Any) -> None:
    """契约（P10c 主链破坏者）：脏图返回 → metadata 增强字段不得被逐字符拆分。"""
    items = [_mk_item("lt-1", "「蓝鲸项目」的部署流程")]
    hits = _mk_manager(items, graph=_DirtyGraph(payload)).search_unified("蓝鲸项目", limit=5)

    assert [h.key for h in hits] == ["lt-1"]
    facts = hits[0].metadata.get("enriched_entities")
    if facts is not None:
        assert isinstance(facts, list)
        assert all(isinstance(fact, str) and len(fact) > 1 for fact in facts), (
            f"metadata 被字符拆分（实际 {facts}）"
        )


# ── 组合排序（reliability × question × intent × 软保底） ─────────────────


# 时间戳不可解析（NaN）→ _recency_factor 中立 0.5：消除时钟噪声，场景算术精确确定
_NEUTRAL_TS = float("nan")


def _lt_item(item_id: str, rank: float | None, *, reliability: float | None = None) -> Any:
    """功能：融合排序场景的 long_term 条目（中性时间戳 → recency 恒 0.5 确定性）。"""
    from bandi.memory.long_term import MemoryItem

    metadata: dict[str, Any] = {}
    if rank is not None:
        metadata["_fts_rank"] = rank
    if reliability is not None:
        metadata["reliability"] = reliability
    return MemoryItem(
        item_id=item_id,
        content=f"{item_id} 蓝鲸内容",
        importance=0.5,
        metadata=metadata,
        timestamp=_NEUTRAL_TS,
    )


class _LtStub:
    """功能：long_term 替身——忽略 limit 返回全部预置条目（截断由上层管）。"""

    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def search_fts_ranked(self, query: str, limit: int = 5, **kwargs: Any) -> list[Any]:
        return list(self.items)

    def count(self) -> int:
        return len(self.items)

    def close(self) -> None:
        pass


class _WorkingStub:
    """功能：working 替身——恒定返回一条 (ScratchHit, score)。"""

    def __init__(self, value: str, score: float) -> None:
        self._value = value
        self._score = score

    def search(self, query: str, limit: int = 5, session_id: str = "") -> list[Any]:
        from bandi.memory.working import ScratchHit

        return [(ScratchHit(kind="kv", key="w1", value=self._value), self._score)]

    def close(self) -> None:
        pass


def _scene_base() -> tuple[list[Any], Any]:
    return [_lt_item("lt-hi", -49.0), _lt_item("lt-mid", -9.0)], None


def _scene_question() -> tuple[list[Any], Any]:
    # lt-hi base=0.666；working base=0.63，×1.2 倍率后 0.756 → 反超
    return [_lt_item("lt-hi", -49.0)], _WorkingStub("蓝鲸 任务草稿", 0.9)


def _scene_intent() -> tuple[list[Any], Any]:
    # lt-mid base=0.63；working base=0.6525 → 无 intent 时 working 领先
    return [_lt_item("lt-mid", -9.0)], _WorkingStub("蓝鲸 任务草稿", 0.95)


def _scene_reliability() -> tuple[list[Any], Any]:
    return [
        _lt_item("lt-rel-hi", -49.0, reliability=1.0),
        _lt_item("lt-rel-lo", -49.0, reliability=0.0),
    ], None


def _scene_soft_floor() -> tuple[list[Any], Any]:
    # 三者融合分精确相等（中性时间戳 + 同分同重要性）——分类优先级与软保底接管；
    # 同分序由 key tie-breaker 确定（lt2 主序在前 → 被替换，保留 lt1）
    return [_lt_item("lt1", None), _lt_item("lt2", None)], _WorkingStub("蓝鲸 任务草稿", 0.5)


@pytest.mark.parametrize(
    ("scene", "question_hint", "intent_hint", "limit", "expected"),
    [
        (_scene_base, None, None, 5, ["lt-hi", "lt-mid"]),
        (_scene_question, "task_state", None, 5, ["kv:w1", "lt-hi"]),
        (_scene_intent, None, None, 5, ["kv:w1", "lt-mid"]),
        (_scene_intent, None, "plan", 5, ["lt-mid", "kv:w1"]),
        (_scene_reliability, None, None, 5, ["lt-rel-hi", "lt-rel-lo"]),
        (_scene_soft_floor, None, None, 2, ["kv:w1", "lt1"]),
        (_scene_base, "execute", None, 5, ["lt-hi", "lt-mid"]),
        (_scene_base, "task_state", None, 5, ["lt-hi", "lt-mid"]),
    ],
    ids=[
        "base-order",
        "question-task-state-flips-working",
        "intent-off-control",
        "intent-plan-flips-long-term",
        "reliability-tiebreak-high-first",
        "soft-floor-promotes-working",
        "unknown-question-zero-behavior",
        "irrelevant-question-no-flip",
    ],
)
def test_search_unified_signal_combination_order(
    scene: Any,
    question_hint: str | None,
    intent_hint: str | None,
    limit: int,
    expected: list[str],
) -> None:
    """契约：reliability/question/intent/软保底组合下最终 key 顺序稳定；
    各场景均与「B 组把 reliability 乘子移到最终分」的改法兼容。"""
    from bandi.memory.memory_provider import MemoryManager

    items, working = scene()
    manager = MemoryManager(long_term=_LtStub(items), working=working)
    keys = [
        hit.key
        for hit in manager.search_unified(
            "蓝鲸", limit=limit, question_hint=question_hint, intent_hint=intent_hint
        )
    ]
    assert keys == expected, f"最终 key 顺序不符（实际 {keys}）"


def test_search_unified_intent_plan_flips_long_term() -> None:
    """契约（P11b）：intent=plan 加法偏置在缺省 reliability 下抬升 long_term
    超过基分更高的 working（control 无 intent 时 working 在前）。"""
    from bandi.memory.memory_provider import MemoryManager

    items, working = _scene_intent()  # lt-mid base=0.63, working base=0.6525
    manager = MemoryManager(long_term=_LtStub(items), working=working)

    control = [h.key for h in manager.search_unified("蓝鲸", limit=5)]
    assert control == ["kv:w1", "lt-mid"], f"无 intent 时 working 基分更高（实际 {control}）"

    biased = [h.key for h in manager.search_unified("蓝鲸", limit=5, intent_hint="plan")]
    assert biased == ["lt-mid", "kv:w1"], f"plan 必须抬升 long_term（实际 {biased}）"


def test_default_reliability_matches_explicit_neutral() -> None:
    """契约（P11c 兼容 B 组改法）：缺省 reliability 与显式 0.5 的组合排序逐条一致
    （均匀缩放对排序零影响，无论乘子落在融合分还是最终分）。"""
    from bandi.memory.memory_provider import MemoryManager

    def keys(items: list[Any], **kwargs: Any) -> list[str]:
        return [
            hit.key
            for hit in MemoryManager(long_term=_LtStub(items)).search_unified("蓝鲸", **kwargs)
        ]

    plain = [_lt_item("lt-hi", -49.0), _lt_item("lt-mid", -9.0)]
    neutral = [
        _lt_item("lt-hi", -49.0, reliability=0.5),
        _lt_item("lt-mid", -9.0, reliability=0.5),
    ]

    assert keys(plain) == keys(neutral) == ["lt-hi", "lt-mid"]
    assert keys(plain, question_hint="task_state") == keys(neutral, question_hint="task_state")


# ── 增强只作用于截断后返回集 ─────────────────────────────────────────────


class _SpyEnricher:
    """计数替身：记录增强调用输入，验证截断边界。"""

    def __init__(self) -> None:
        self.extract_values: list[str] = []
        self.contradiction_batches: list[list[str]] = []

    def extract_entities(self, text: str) -> list[Any]:
        self.extract_values.append(text)
        return []

    def find_facts(self, entities: list[Any], *, limit: int = 5) -> list[str]:
        return []

    def find_contradictions(self, candidates: list[str]) -> list[tuple[str, str]]:
        self.contradiction_batches.append(list(candidates))
        return []


def test_enrichment_only_applies_to_truncated_result() -> None:
    """契约：limit=1 时增强只处理最终返回的 1 条——extract 恰 1 次。"""
    from bandi.memory.memory_provider import MemoryManager

    items = [_lt_item("lt-1", -49.0), _lt_item("lt-2", -9.0), _lt_item("lt-3", -4.0)]
    spy = _SpyEnricher()
    manager = MemoryManager(long_term=_LtStub(items), enricher=spy)

    out = manager.search_unified("蓝鲸", limit=1)

    assert len(out) == 1
    assert spy.extract_values == [out[0].value], "增强只作用于截断后返回集"
    assert spy.contradiction_batches == [[out[0].value]]


# ── MemoryContextSource 消费增强字段（主链可见） ──────────────────────────


def _mk_hit(key: str, value: str, **meta: Any) -> Any:
    from bandi.memory.memory_provider import MemoryHit

    return MemoryHit(key=key, value=value, metadata={"category": "long_term", **meta})


class _HitManager:
    """返回固定 hit 列表的检索替身。"""

    def __init__(self, hits: list[Any]) -> None:
        self.hits = hits

    def search_unified(self, query: str, **kw: Any) -> list[Any]:
        return list(self.hits)


@pytest.fixture()
def _home_isolated(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """seen 持久化路径 tmp + env 双保险，禁止写穿真实 ~/.bandi。"""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("BANDI_MEMORY_PENDING_DIR", str(tmp_path / "memory-pending"))
    return tmp_path


def _mk_source(hits: list[Any]) -> Any:
    from bandi.context.sources.memory_source import MemoryContextSource

    return MemoryContextSource(memory_manager=_HitManager(hits))


@pytest.mark.asyncio
async def test_collect_appends_enriched_entities_suffix(_home_isolated: Any) -> None:
    """契约：enriched_entities 非空 → content 追加「（相关实体：…）」
    （去重、上限 3）。"""
    from bandi.core.message import Message, MessageRole

    hit = _mk_hit(
        "k1",
        "蓝鲸项目验收标准",
        enriched_entities=["部署文档", "部署文档", "值班表", "额外实体"],
    )
    items = await _mk_source([hit]).collect(Message(role=MessageRole.USER, content="蓝鲸"), "s1")

    assert len(items) == 1
    assert items[0].content == (
        "[long_term] 蓝鲸项目验收标准（相关实体：部署文档、值班表、额外实体）"
    )


@pytest.mark.asyncio
async def test_collect_appends_contradiction_suffix(_home_isolated: Any) -> None:
    """契约（P8b）：contradiction_with 非空 → content 追加矛盾后缀。"""
    from bandi.core.message import Message, MessageRole

    hit = _mk_hit("k1", "蓝鲸项目验收标准", contradiction_with=["k2"])
    items = await _mk_source([hit]).collect(Message(role=MessageRole.USER, content="蓝鲸"), "s1")

    assert items[0].content == "[long_term] 蓝鲸项目验收标准（与另一条记忆存在矛盾）"


@pytest.mark.asyncio
async def test_collect_entities_suffix_precedes_contradiction_suffix(_home_isolated: Any) -> None:
    """契约（P8c）：双字段同时命中 → 实体后缀在前、矛盾后缀在后。"""
    from bandi.core.message import Message, MessageRole

    hit = _mk_hit("k1", "蓝鲸项目验收标准", enriched_entities=["甲"], contradiction_with=["k2"])
    items = await _mk_source([hit]).collect(Message(role=MessageRole.USER, content="蓝鲸"), "s1")

    assert items[0].content.endswith("（相关实体：甲）（与另一条记忆存在矛盾）")


@pytest.mark.asyncio
async def test_collect_without_enrichment_fields_unchanged(_home_isolated: Any) -> None:
    """契约（P8d 零回归）：无增强字段 → content 与现状逐字一致。"""
    from bandi.core.message import Message, MessageRole

    hit = _mk_hit("k1", "蓝鲸项目验收标准")
    items = await _mk_source([hit]).collect(Message(role=MessageRole.USER, content="蓝鲸"), "s1")

    assert items[0].content == "[long_term] 蓝鲸项目验收标准"


@pytest.mark.asyncio
async def test_collect_enrichment_still_truncated_at_item_chars(_home_isolated: Any) -> None:
    """契约（P8e）：超长 value + 后缀整体仍受 _MEMORY_ITEM_CHARS 截断。"""
    from bandi.context.sources import memory_source as ms
    from bandi.core.message import Message, MessageRole

    value = "很长的记忆正文" * 40
    hit = _mk_hit("k1", value, enriched_entities=["甲"])
    items = await _mk_source([hit]).collect(Message(role=MessageRole.USER, content="蓝鲸"), "s1")

    assert len(items) == 1
    assert items[0].content == f"[long_term] {value[: ms._MEMORY_ITEM_CHARS]}"
    assert len(items[0].content) == len("[long_term] ") + ms._MEMORY_ITEM_CHARS


@pytest.mark.asyncio
async def test_collect_ignores_dirty_enrichment_metadata(_home_isolated: Any) -> None:
    """契约（P8f 破坏者）：增强字段脏类型（str/非 str 列表项）→ 不追加、不拆分。"""
    from bandi.core.message import Message, MessageRole

    hits = [
        _mk_hit("k1", "蓝鲸项目验收标准", enriched_entities="甲", contradiction_with=[]),
        _mk_hit("k2", "蓝鲸项目验收标准乙", enriched_entities=[None, 5, ""]),
    ]
    items = await _mk_source(hits).collect(Message(role=MessageRole.USER, content="蓝鲸"), "s1")

    assert len(items) == 2
    assert all("（相关实体" not in item.content for item in items)
    assert all("（与另一条记忆存在矛盾）" not in item.content for item in items)


# ── 只读证明 + 装配 ────────────────────────────────────────────────────────


def test_enricher_never_invokes_graph_writers() -> None:
    """契约（只读证明）：增强全流程零图写方法调用（写炮台不炸）。"""
    from bandi.memory.enricher import EntityGraphEnricher, EntityRef

    graph = _FakeGraph(entities={"e1": _FakeEntity("e1", "蓝鲸项目")})
    enricher = EntityGraphEnricher(graph)

    enricher.extract_entities(_LEFT)
    enricher.find_facts([EntityRef(name="蓝鲸项目")])
    enricher.find_contradictions([_LEFT, _RIGHT])

    assert graph.writes == [], f"只读增强不得调用写方法（实际 {graph.writes}）"


def test_build_memory_manager_binds_optional_enricher(tmp_path: Any) -> None:
    """契约（P7c 装配）：entity_graph 非空注入 EntityGraphEnricher；缺省 None 零行为。"""
    from bandi.core.agent_runner import _build_memory_manager
    from bandi.memory.enricher import EntityGraphEnricher, EntityRef

    graph = _FakeGraph(entities={"e1": _FakeEntity("e1", "蓝鲸项目")})
    wired = _build_memory_manager(tmp_path / "wired", entity_lookup=graph)
    try:
        assert isinstance(wired.enricher, EntityGraphEnricher)
        assert wired.enricher.find_facts([EntityRef(name="蓝鲸项目")]) == ["蓝鲸项目"]
    finally:
        wired.close()

    bare = _build_memory_manager(tmp_path / "bare")
    try:
        assert bare.enricher is None
    finally:
        bare.close()


@pytest.mark.asyncio
async def test_run_agent_turn_binds_entity_graph_enricher(tmp_path: Any) -> None:
    """契约（P7d 端到端装配）：run_agent_turn 用生产本体只读适配绑定 enricher。"""
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.config import BandiConfig
    from bandi.core.message import LLMResponse, TokenUsage
    from bandi.llm.mock import MockProvider
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.session.store import SessionStore
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore

    manager = MemoryManager(long_term=_FakeLongTerm([]))
    store = SessionStore(str(tmp_path / "session.db"))
    sid = store.create_session(mode="execute")
    entity_store = OntologyStore(open_ontology(str(tmp_path / "ontology.db")))

    cfg = BandiConfig()
    cfg.llm.api_key = ""
    provider = MockProvider()
    provider.add_response(LLMResponse(content="ok", usage=TokenUsage(2, 1), finish_reason="stop"))

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            await run_agent_turn(
                user_input="装配验证",
                session_id=sid,
                session_store=store,
                triple_ontology_store=entity_store,
                bandi_config=cfg,
                memory_manager=manager,
            )

    assert manager.enricher is not None, "run_agent_turn 必须以本体只读适配绑定 enricher"
