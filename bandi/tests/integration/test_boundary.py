# 功能：跨模块边界测试 — 15 类边界中 Day 22 覆盖的部分
# 完整 15 类
# #13 实体不存在 / #14 死循环(test_p0_verification::test_p04)
# 剩余 #3/#4/#5/#6/#8/#9/#10/#11/#12/#15 缓冲日（Day 25-27）补

from __future__ import annotations

import os
import tempfile

from bandi.perception.input_parser import InputParser


# #2 超长输入 — Perception 层不崩溃
def test_boundary_02_long_input_not_crash() -> None:
    parser = InputParser()
    long_text = "a" * 100_000  # 100K 字符
    msg = parser.parse(long_text)
    assert msg.content == long_text


# #13 实体不存在 — Ontology 查询返回 None，不崩溃
def test_boundary_13_entity_not_found_returns_none() -> None:
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore

    db = tempfile.mktemp(suffix=".db")
    try:
        store = OntologyStore(open_ontology(db))
        result = store.graph().entities.get("nonexistent-id-xyz-12345")
        assert result is None
        store.close()
    finally:
        if os.path.exists(db):
            os.unlink(db)
