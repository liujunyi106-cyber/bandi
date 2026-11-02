"""契约族：记忆关系读取侧推理（B6-A2）。

行为不变式：
1. `entailed_edges` 覆盖三特征：对称 / 传递 / **逆关系**（`inverse_of`）；
2. `entailed_structured` 收敛为委托 `entailed_edges`（入口过滤 + via 标注），
   派生态保留 `inferred`/`derived_via` 标记（与断言事实可区分）；
3. 读取侧接线：召回（`search_unified`）与 `memory_search` 工具把命中的结构化
   三元组按连通分量补齐后闭包，派生结论写入 `metadata["entailed_facts"]`
   ——只读派生（撤回前提即消失）、限量、失败静默降级。
"""

from __future__ import annotations

from typing import Any

import pytest

from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.tbox import RelationTerm


def _fact_item(
    item_id: str,
    subject: str,
    predicate: str,
    obj: str,
    *,
    nested: bool = False,
) -> MemoryItem:
    """功能：造一条结构化事实记忆——nested=真时走 `structured` 嵌套形态（直写侧），
    否则走 memory_assert 的顶层 subject/predicate/object 形态。"""
    metadata: dict[str, Any] = {"memory_type": "business_fact", "source": "test"}
    if nested:
        metadata["structured"] = {
            "kind": "fact",
            "key": predicate,
            "subject": subject,
            "predicate": predicate,
            "object": obj,
        }
    else:
        metadata.update({"subject": subject, "predicate": predicate, "object": obj})
    return MemoryItem(
        item_id=item_id,
        content=f"{subject}·{predicate}·{obj}",
        importance=0.6,
        metadata=metadata,
    )


class TestEntailedEdgesInverse:
    def test_inverse_of_derives_reverse_relation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """逆关系特征：`contains` 声明 inverse_of=`part_of` 时，(甲 contains 乙) ⇒ (乙 part_of 甲)。"""
        from bandi.memory import tbox
        from bandi.memory.tbox import entailed_edges

        monkeypatch.setitem(
            tbox.RELATION_TERMS,
            "contains",
            RelationTerm(id="contains", label="包含", inverse_of="part_of"),
        )
        monkeypatch.setitem(
            tbox.RELATION_TERMS, "part_of", RelationTerm(id="part_of", label="属于")
        )
        derived = entailed_edges([("甲", "contains", "乙")])
        assert ("乙", "part_of", "甲") in derived


class TestEntailedStructuredConvergence:
    def test_delegates_to_entailed_edges(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """收敛：`entailed_structured` 必须委托 `entailed_edges` 做闭包（单一实现）。"""
        from bandi.memory import structured as structured_mod
        from bandi.memory import tbox

        calls: list[list[tuple[str, str, str]]] = []
        original = tbox.entailed_edges

        def _spy(edges: list[tuple[str, str, str]], max_rounds: int = 8) -> Any:
            calls.append(list(edges))
            return original(edges, max_rounds)

        monkeypatch.setattr(tbox, "entailed_edges", _spy)
        given = [
            {
                "kind": "fact",
                "key": "depends_on",
                "subject": "a",
                "predicate": "depends_on",
                "object": "b",
            },
            {
                "kind": "fact",
                "key": "depends_on",
                "subject": "b",
                "predicate": "depends_on",
                "object": "c",
            },
        ]
        derived = structured_mod.entailed_structured(given)
        assert calls == [[("a", "depends_on", "b"), ("b", "depends_on", "c")]], (
            "须原样把断言三元组交给 entailed_edges"
        )
        assert {tuple((d["subject"], d["predicate"], d["object"])) for d in derived} == {
            ("a", "depends_on", "c")
        }

    def test_symmetric_via_label(self) -> None:
        from bandi.memory.structured import entailed_structured

        given = [
            {
                "kind": "fact",
                "key": "relates_to",
                "subject": "A",
                "predicate": "relates_to",
                "object": "B",
            }
        ]
        derived = entailed_structured(given)
        item = next(d for d in derived if (d["subject"], d["object"]) == ("B", "A"))
        assert item["derived_via"] == "symmetric"
        assert item["inferred"] is True

    def test_inverse_via_label(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bandi.memory import tbox
        from bandi.memory.structured import entailed_structured

        monkeypatch.setitem(
            tbox.RELATION_TERMS,
            "contains",
            RelationTerm(id="contains", label="包含", inverse_of="part_of"),
        )
        monkeypatch.setitem(
            tbox.RELATION_TERMS, "part_of", RelationTerm(id="part_of", label="属于")
        )
        given = [
            {
                "kind": "fact",
                "key": "contains",
                "subject": "甲",
                "predicate": "contains",
                "object": "乙",
            }
        ]
        derived = entailed_structured(given)
        item = next(
            d
            for d in derived
            if (d["subject"], d["predicate"], d["object"]) == ("乙", "part_of", "甲")
        )
        assert item["derived_via"] == "inverse"

    def test_mixed_battery_set_equivalence(self) -> None:
        """混合图对拍：传递链 + 对称边 → 期望派生态集合（含 via 标注）。"""
        from bandi.memory.structured import entailed_structured

        given = [
            {
                "kind": "fact",
                "key": "depends_on",
                "subject": "a",
                "predicate": "depends_on",
                "object": "b",
            },
            {
                "kind": "fact",
                "key": "depends_on",
                "subject": "b",
                "predicate": "depends_on",
                "object": "c",
            },
            {
                "kind": "fact",
                "key": "relates_to",
                "subject": "x",
                "predicate": "relates_to",
                "object": "y",
            },
        ]
        derived = entailed_structured(given)
        got = {(d["subject"], d["predicate"], d["object"], d["derived_via"]) for d in derived}
        assert got == {
            ("a", "depends_on", "c", "transitive"),
            ("y", "relates_to", "x", "symmetric"),
        }


class TestReadSideWiring:
    @pytest.fixture()
    def long_term(self, tmp_path: Any) -> Any:
        store = LongTermMemory(str(tmp_path / "lt.db"))
        yield store
        store.close()

    def _seed_chain(self, store: LongTermMemory) -> None:
        store.store(_fact_item("triple:t1", "A项目", "depends_on", "B服务"))
        store.store(_fact_item("triple:t2", "B服务", "depends_on", "C组件"))
        store.store(_fact_item("triple:t3", "无关X", "relates_to", "无关Y", nested=True))

    def test_recall_attaches_transitive_entailment(self, long_term: Any) -> None:
        """预期：读取 A 得 A→C（via transitive）——召回命中携带派生结论。"""
        from bandi.memory.memory_provider import MemoryManager

        self._seed_chain(long_term)
        manager = MemoryManager(long_term=long_term)
        hits = manager.search_unified("A项目", limit=5)
        hit = next((h for h in hits if "A项目" in (h.value or "")), None)
        assert hit is not None, f"应召回 A 的断言条目：{[h.value for h in hits]}"
        facts = hit.metadata.get("entailed_facts") or []
        assert any("A项目" in f and "C组件" in f and "transitive" in f for f in facts), (
            f"A→C 传递推导应进 entailed_facts：{facts}"
        )

    def test_recall_entailment_is_read_only(self, long_term: Any) -> None:
        """破坏者：读取侧推理只读——不新增条目、不改三元组（召回计数簿记是既有行为）。"""
        from bandi.memory.memory_provider import MemoryManager

        self._seed_chain(long_term)

        def _snapshot() -> list[tuple[str, str, dict[str, Any]]]:
            rows: list[tuple[str, str, dict[str, Any]]] = []
            for item in long_term.list_items():
                meta = dict(item.metadata)
                for bookkeeping in ("recall_count", "last_recalled_at"):
                    meta.pop(bookkeeping, None)
                rows.append((item.id, item.content, meta))
            return sorted(rows)

        before = _snapshot()
        manager = MemoryManager(long_term=long_term)
        manager.search_unified("A项目", limit=5)
        assert _snapshot() == before, "读取侧推导不得写库/改条目（派生态永不落盘）"

    @pytest.mark.asyncio
    async def test_memory_search_tool_attaches_entailment(self, long_term: Any) -> None:
        """预期：memory_search 工具（模型显式读取口）同样携带派生结论。"""
        from bandi.tools.memory_tools import MemorySearchTool

        self._seed_chain(long_term)
        tool = MemorySearchTool(long_term=long_term)
        result = await tool.execute({"query": "A项目"})
        hits = result.get("hits") or []
        hit = next((h for h in hits if "A项目" in str(h.get("hook") or "")), None)
        assert hit is not None, f"应命中 A 条目：{hits}"
        facts = hit.get("entailed_facts") or []
        assert any("C组件" in f and "transitive" in f for f in facts), f"工具口应携带 A→C：{facts}"

    @pytest.mark.asyncio
    async def test_undeclared_predicate_not_expanded(self, long_term: Any) -> None:
        """破坏者：谓词不在词表——不参与推导，也不进 entailed_facts。"""
        from bandi.tools.memory_tools import MemorySearchTool

        long_term.store(_fact_item("triple:bad1", "P", "瞎写", "Q"))
        long_term.store(_fact_item("triple:bad2", "Q", "瞎写", "R"))
        tool = MemorySearchTool(long_term=long_term)
        result = await tool.execute({"query": "P"})
        for hit in result.get("hits") or []:
            assert not hit.get("entailed_facts"), f"未声明谓词不得推导：{hit}"

    @pytest.mark.asyncio
    async def test_unrelated_hit_has_no_entailment_key(self, long_term: Any) -> None:
        """破坏者：非结构化命中（偏好/散文）不携带 entailed_facts（不得硬塞空列表噪音）。"""
        from bandi.tools.memory_tools import MemorySearchTool

        long_term.store(
            MemoryItem(
                item_id="turn:t9",
                content="用户偏好报告用中文输出",
                importance=0.5,
                metadata={"memory_type": "preference"},
            )
        )
        tool = MemorySearchTool(long_term=long_term)
        result = await tool.execute({"query": "报告"})
        for hit in result.get("hits") or []:
            assert "entailed_facts" not in hit, f"非结构化命中不得带派生键：{hit}"
