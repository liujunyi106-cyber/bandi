"""契约：记忆关系词汇表（TBox）——validate, not parse。

移植 dsh-ontology 的两段式设计（`ontology_define` / `ontology_assert`）：
代码**不解析散文**去猜实体与关系，而是校验调用方显式给的三元组是否落在已声明
的词汇上；违规时返回**写给模型看的可操作消息**，让模型自己改，而不是自动改写或
悄悄放过（dsh-ontology `src/types.ts:120-126`）。

本契约钉三件事：
1. 谓词白名单——未声明的关系一律拒收（此前 `add_relation` 接受任意字符串，AR-18）
2. 违规消息可操作——必须点名可用谓词，让模型能自我修复
3. 对称/传递推理是 read-time 派生物，永不落盘（撤回前提即自动消失）
"""

from __future__ import annotations

import pytest

from bandi.memory.tbox import (
    RELATION_TERMS,
    entailed_edges,
    relation_for,
    relation_vocabulary_prompt,
    suggest_relations,
    validate_fact,
)


class TestRelationVocabulary:
    def test_predicates_must_be_declared(self) -> None:
        v = validate_fact(subject="A", predicate="随便一个关系", object_value="B")
        assert len(v) == 1
        assert v[0].code == "unknown-relation"

    def test_declared_predicate_passes(self) -> None:
        assert (
            validate_fact(subject="订单服务", predicate="depends_on", object_value="支付服务") == []
        )

    def test_violation_message_lists_available_predicates(self) -> None:
        """破坏者视角：消息必须让模型知道能写什么，否则模型只能反复试错。"""
        msg = validate_fact(subject="A", predicate="瞎写", object_value="B")[0].message
        for term_id in RELATION_TERMS:
            assert term_id in msg, f"违规消息未列出可用谓词 {term_id}"

    def test_violation_message_suggests_near_match(self) -> None:
        near = suggest_relations("依赖")
        assert "depends_on" in near

    def test_domain_mismatch_rejected(self) -> None:
        v = validate_fact(
            subject="林总",
            predicate="assigned_to",
            object_value="某任务",
            subject_type="preference",
            object_type="business_fact",
        )
        assert [x.code for x in v] == ["domain-mismatch"]

    def test_empty_subject_or_object_rejected(self) -> None:
        assert validate_fact(subject="", predicate="depends_on", object_value="B")[0].code == (
            "missing-subject"
        )
        assert validate_fact(subject="A", predicate="depends_on", object_value="")[0].code == (
            "missing-object"
        )

    def test_missing_types_do_not_block(self) -> None:
        """无类型信息时不误杀——只校验已知的约束。"""
        assert validate_fact(subject="A", predicate="assigned_to", object_value="B") == []


class TestVocabularyPrompt:
    def test_prompt_lists_all_predicates(self) -> None:
        text = relation_vocabulary_prompt()
        for term_id in RELATION_TERMS:
            assert term_id in text

    def test_prompt_marks_logical_features(self) -> None:
        text = relation_vocabulary_prompt()
        assert "传递" in text
        assert "对称" in text

    def test_relation_lookup(self) -> None:
        assert relation_for("depends_on") is not None
        assert relation_for("不存在的") is None


class TestEntailment:
    def test_transitive_closure(self) -> None:
        derived = entailed_edges([("A", "depends_on", "B"), ("B", "depends_on", "C")])
        assert ("A", "depends_on", "C") in derived

    def test_symmetric_produces_reverse(self) -> None:
        derived = entailed_edges([("A", "relates_to", "B")])
        assert ("B", "relates_to", "A") in derived

    def test_entailment_is_pure(self) -> None:
        """破坏者视角：推导不得修改入参——它是 read-time 派生物。"""
        edges = [("A", "depends_on", "B"), ("B", "depends_on", "C")]
        snapshot = list(edges)
        entailed_edges(edges)
        assert edges == snapshot

    def test_no_self_loop_from_round_trip(self) -> None:
        derived = entailed_edges([("A", "depends_on", "B"), ("B", "depends_on", "A")])
        assert ("A", "depends_on", "A") not in derived

    def test_undeclared_predicate_not_inferred(self) -> None:
        derived = entailed_edges([("A", "瞎写", "B"), ("B", "瞎写", "C")])
        assert derived == []


class TestAssertTool:
    @pytest.mark.asyncio
    async def test_undeclared_predicate_rejected_with_message(self, tmp_path) -> None:
        """端到端：工具层拒绝并把可操作消息回给模型。"""
        from bandi.memory.long_term import LongTermMemory
        from bandi.tools.memory_tools import MemoryAssertTool

        store = LongTermMemory(str(tmp_path / "lt.db"))
        tool = MemoryAssertTool(long_term=store)
        result = await tool.execute({"subject": "A", "predicate": "瞎写", "object": "B"})
        assert result["asserted"] is False
        assert result["violations"][0]["code"] == "unknown-relation"
        store.close()

    @pytest.mark.asyncio
    async def test_valid_triple_persisted(self, tmp_path) -> None:
        from bandi.memory.long_term import LongTermMemory
        from bandi.tools.memory_tools import MemoryAssertTool

        store = LongTermMemory(str(tmp_path / "lt.db"))
        tool = MemoryAssertTool(long_term=store)
        result = await tool.execute(
            {"subject": "订单服务", "predicate": "depends_on", "object": "支付服务"}
        )
        assert result["asserted"] is True
        rows = store.search_fts_ranked("订单服务", 3)
        assert rows, "三元组必须落库可查"
        assert "依赖" in rows[0].content
        store.close()

    @pytest.mark.asyncio
    async def test_reasserting_same_triple_is_idempotent(self, tmp_path) -> None:
        from bandi.memory.long_term import LongTermMemory
        from bandi.tools.memory_tools import MemoryAssertTool

        store = LongTermMemory(str(tmp_path / "lt.db"))
        tool = MemoryAssertTool(long_term=store)
        params = {"subject": "A", "predicate": "relates_to", "object": "B"}
        first = await tool.execute(params)
        second = await tool.execute(params)
        assert first["id"] == second["id"], "同一三元组必须覆盖而非堆积"
        store.close()
