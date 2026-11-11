"""本体存储 schema：三张表的持久化声明（复刻 dsh-ontology `src/spec.ts`）。

参考源用 zod + 插件存储域，这里用 SQLite DDL——同一套记录形状，换一个介质。
三张表对应 TBox 词表 / ABox 实体 / ABox 断言事实，事实按三元组复合键存储，
使重复断言成为幂等覆盖而非重复行。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from bandi.ontology.types import (
    ClassTerm,
    Entity,
    Fact,
    GraphView,
    RelationTerm,
    Term,
)
from bandi.utils.db import connect

# 存储域版本——记录形状不兼容变更时递增
ONTOLOGY_DOMAIN_VERSION = 1

TERM_TABLE = "onto_terms"
ENTITY_TABLE = "onto_entities"
FACT_TABLE = "onto_facts"

_DDL = (
    f"""
    CREATE TABLE IF NOT EXISTS {TERM_TABLE} (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,
        payload     TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {ENTITY_TABLE} (
        id          TEXT PRIMARY KEY,
        payload     TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {FACT_TABLE} (
        fact_key    TEXT PRIMARY KEY,
        subject     TEXT NOT NULL,
        predicate   TEXT NOT NULL,
        object      TEXT NOT NULL,
        object_kind TEXT NOT NULL,
        payload     TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_onto_facts_subject ON {FACT_TABLE}(subject)",
    f"CREATE INDEX IF NOT EXISTS idx_onto_facts_predicate ON {FACT_TABLE}(predicate)",
    f"CREATE INDEX IF NOT EXISTS idx_onto_facts_object ON {FACT_TABLE}(object)",
)


@dataclass
class OntologyTables:
    """功能：本体三表的读写门面——记录形状与 `types.py` 一一对应。"""

    conn: sqlite3.Connection
    path: Path

    def table_names(self) -> list[str]:
        """功能：已建的本体表名清单。"""
        return [TERM_TABLE, ENTITY_TABLE, FACT_TABLE]

    def close(self) -> None:
        """功能：关闭连接。"""
        self.conn.close()

    # -- 词表 ------------------------------------------------------------

    def put_term(self, term: Term, now: str) -> None:
        """功能：写入词表项（整体替换，键为 term.id）。"""
        self.conn.execute(
            f"INSERT OR REPLACE INTO {TERM_TABLE} (id, kind, payload, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                term.id,
                term.kind,
                json.dumps(_term_payload(term), ensure_ascii=False),
                term.created_at,
                term.updated_at,
            ),
        )

    def delete_term(self, term_id: str) -> None:
        """功能：删除词表项。"""
        self.conn.execute(f"DELETE FROM {TERM_TABLE} WHERE id = ?", (term_id,))

    def get_term(self, term_id: str) -> Term | None:
        """功能：取词表项。"""
        row = self.conn.execute(
            f"SELECT kind, payload FROM {TERM_TABLE} WHERE id = ?", (term_id,)
        ).fetchone()
        return _term_from_row(row) if row else None

    def raw_terms(self) -> list[tuple]:
        """功能：词表原始行（迁移计数核对用）。"""
        return self.conn.execute(
            f"SELECT id, kind, created_at, updated_at FROM {TERM_TABLE} ORDER BY id"
        ).fetchall()

    def all_terms(self) -> dict[str, Term]:
        """功能：全部词表项（id → 项）。"""
        return {
            term_id: term
            for term_id, term in (
                (row[0], _term_from_row((row[1], row[2])))
                for row in self.conn.execute(
                    f"SELECT id, kind, payload FROM {TERM_TABLE}"
                ).fetchall()
            )
            if term is not None
        }

    # -- 实体 ------------------------------------------------------------

    def put_entity(self, entity: Entity, now: str) -> None:
        """功能：写入实体（整体替换）。"""
        self.conn.execute(
            f"INSERT OR REPLACE INTO {ENTITY_TABLE} (id, payload, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (
                entity.id,
                json.dumps(_entity_payload(entity), ensure_ascii=False),
                entity.created_at,
                entity.updated_at,
            ),
        )

    def delete_entity(self, entity_id: str) -> None:
        """功能：删除实体。"""
        self.conn.execute(f"DELETE FROM {ENTITY_TABLE} WHERE id = ?", (entity_id,))

    def get_entity(self, entity_id: str) -> Entity | None:
        """功能：取实体。"""
        row = self.conn.execute(
            f"SELECT payload FROM {ENTITY_TABLE} WHERE id = ?", (entity_id,)
        ).fetchone()
        return _entity_from_json(row[0]) if row else None

    def all_entities(self) -> dict[str, Entity]:
        """功能：全部实体（id → 实体）。"""
        out: dict[str, Entity] = {}
        for (payload,) in self.conn.execute(f"SELECT payload FROM {ENTITY_TABLE}").fetchall():
            entity = _entity_from_json(payload)
            if entity is not None:
                out[entity.id] = entity
        return out

    # -- 事实 ------------------------------------------------------------

    def put_fact(self, fact_key: str, fact: Fact) -> None:
        """功能：写入断言事实（键 = 复合键，故重复断言即覆盖）。"""
        self.conn.execute(
            f"INSERT OR REPLACE INTO {FACT_TABLE}"
            " (fact_key, subject, predicate, object, object_kind, payload, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                fact_key,
                fact.subject,
                fact.predicate,
                fact.object,
                fact.object_kind,
                json.dumps(_fact_payload(fact), ensure_ascii=False),
                fact.created_at,
            ),
        )

    def delete_fact(self, fact_key: str) -> None:
        """功能：删除事实（键命中才计为已删除）。"""
        self.conn.execute(f"DELETE FROM {FACT_TABLE} WHERE fact_key = ?", (fact_key,))

    def delete_facts_by_entity(self, entity_id: str) -> int:
        """功能：级联删除提及某实体的事实——主语匹配，或 entity 值宾语匹配。

        字面量宾语只是值不是实体引用，故不匹配（否则删一个人会连带删掉全库
        恰好与他同值的年龄）。
        """
        cursor = self.conn.execute(
            f"DELETE FROM {FACT_TABLE} WHERE subject = ?"
            " OR (object = ? AND object_kind = 'entity')",
            (entity_id, entity_id),
        )
        return cursor.rowcount

    def all_facts(self) -> dict[str, Fact]:
        """功能：全部断言事实（复合键 → 事实）。派生态不在此表。"""
        out: dict[str, Fact] = {}
        for key, payload in self.conn.execute(
            f"SELECT fact_key, payload FROM {FACT_TABLE}"
        ).fetchall():
            fact = _fact_from_json(payload)
            if fact is not None:
                out[key] = fact
        return out

    def raw_facts(self) -> list[tuple]:
        """功能：事实原始行（迁移计数核对用）。"""
        return self.conn.execute(
            f"SELECT fact_key, subject, predicate, object, object_kind FROM {FACT_TABLE}"
            " ORDER BY fact_key"
        ).fetchall()

    # -- 事务 ------------------------------------------------------------

    def commit(self) -> None:
        """功能：提交一次写入。"""
        self.conn.commit()

    def rollback(self) -> None:
        """功能：回滚未提交写入。"""
        self.conn.rollback()


def open_ontology(path: str | Path) -> OntologyTables:
    """功能：打开（或新建）本体库并建表——建表幂等，可反复调用。"""
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(resolved, wal=True, check_same_thread=False, foreign_keys=True)
    for statement in _DDL:
        conn.execute(statement)
    conn.commit()
    return OntologyTables(conn=conn, path=resolved)


# ---------------------------------------------------------------------------
# 记录形状 <-> JSON
# ---------------------------------------------------------------------------


def _term_payload(term: Term) -> dict:
    """功能：词表项 → JSON 可存形状。"""
    if isinstance(term, ClassTerm):
        return {
            "kind": "class",
            "id": term.id,
            "label": term.label,
            "comment": term.comment,
            "subClassOf": list(term.sub_class_of),
            "createdAt": term.created_at,
            "updatedAt": term.updated_at,
        }
    return {
        "kind": "relation",
        "id": term.id,
        "label": term.label,
        "comment": term.comment,
        "domain": list(term.domain),
        "rangeKind": term.range_kind,
        "range": list(term.range),
        "characteristics": list(term.characteristics),
        "inverseOf": term.inverse_of,
        "createdAt": term.created_at,
        "updatedAt": term.updated_at,
    }


def _term_from_row(row: tuple) -> Term | None:
    """功能：(kind, payload_json) → 词表项；形状不认识则返回 None。"""
    kind, payload = row
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if kind == "class" or data.get("kind") == "class":
        return ClassTerm(
            kind="class",
            id=str(data.get("id") or ""),
            label=data.get("label"),
            comment=data.get("comment"),
            sub_class_of=tuple(data.get("subClassOf") or ()),
            created_at=str(data.get("createdAt") or ""),
            updated_at=str(data.get("updatedAt") or ""),
        )
    return RelationTerm(
        kind="relation",
        id=str(data.get("id") or ""),
        label=data.get("label"),
        comment=data.get("comment"),
        domain=tuple(data.get("domain") or ()),
        range_kind=str(data.get("rangeKind") or "entity"),
        range=tuple(data.get("range") or ()),
        characteristics=tuple(data.get("characteristics") or ()),
        inverse_of=data.get("inverseOf"),
        created_at=str(data.get("createdAt") or ""),
        updated_at=str(data.get("updatedAt") or ""),
    )


def _entity_payload(entity: Entity) -> dict:
    """功能：实体 → JSON 可存形状。"""
    return {
        "id": entity.id,
        "classes": list(entity.classes),
        "label": entity.label,
        "comment": entity.comment,
        "createdAt": entity.created_at,
        "updatedAt": entity.updated_at,
    }


def _entity_from_json(payload: str) -> Entity | None:
    """功能：JSON → 实体；形状不认识则返回 None。"""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return Entity(
        id=str(data.get("id") or ""),
        classes=tuple(data.get("classes") or ()),
        label=data.get("label"),
        comment=data.get("comment"),
        created_at=str(data.get("createdAt") or ""),
        updated_at=str(data.get("updatedAt") or ""),
    )


def _fact_payload(fact: Fact) -> dict:
    """功能：事实 → JSON 可存形状。"""
    return {
        "subject": fact.subject,
        "predicate": fact.predicate,
        "object": fact.object,
        "objectKind": fact.object_kind,
        "source": fact.source,
        "confidence": fact.confidence,
        "violations": list(fact.violations),
        "createdAt": fact.created_at,
    }


def _fact_from_json(payload: str) -> Fact | None:
    """功能：JSON → 事实；形状不认识则返回 None。"""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return Fact(
        subject=str(data.get("subject") or ""),
        predicate=str(data.get("predicate") or ""),
        object=str(data.get("object") or ""),
        object_kind=str(data.get("objectKind") or "entity"),
        source=data.get("source"),
        confidence=data.get("confidence"),
        violations=tuple(data.get("violations") or ()),
        created_at=str(data.get("createdAt") or ""),
    )


def build_graph_view(tables: OntologyTables) -> GraphView:
    """功能：三表投影成只读图视图——纯逻辑只吃这个，故规则可脱离存储单测。"""
    return GraphView(
        terms=tables.all_terms(),
        entities=tables.all_entities(),
        facts=tables.all_facts(),
    )


__all__ = [
    "ENTITY_TABLE",
    "FACT_TABLE",
    "ONTOLOGY_DOMAIN_VERSION",
    "TERM_TABLE",
    "OntologyTables",
    "build_graph_view",
    "open_ontology",
]
