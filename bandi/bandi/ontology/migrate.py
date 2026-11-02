"""旧实体-关系库 → 三元组本体迁移（一次性，B4d）。

把 `bandi.db` 的 `entities` / `relations` 两表迁进 `ontology.db` 的三表：
实体类型 → 类词表、实体 → `onto_entities`、关系类型 → 关系词表、有向边 → 断言事实。

设计取舍：
- **直读 SQL，不导入旧 `ontology.*` 模块**——迁移脚本必须在旧模块删除后仍可运行
  （删旧批次在 B4 之后），依赖旧模块等于让迁移脚本随删旧一起失效。
  对应边不迁并在报告里点名；旧实体的自由 `properties` 不进三元组（三元组没有
  属性袋，硬塞成字面量事实只会制造无人使用的噪音）。
- **目标库非空即拒绝**：迁移拒绝覆盖已有本体——半迁状态比不迁更难收拾。
- **迁移前备份**：目标库存在则复制为 `.bak-<时间戳>`，`rollback_migration()` 可整体回滚。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bandi.ontology.spec import open_ontology
from bandi.ontology.triple_store import OntologyStore
from bandi.ontology.types import ID_PATTERN
from bandi.utils.db import connect

_ID_RE = re.compile(ID_PATTERN)

# 内置类种子（来自旧 `ontology/registry.py` 的 5 个内置实体类型）——迁移后的库
# 仍认得这些自然类名，标签与旧界面一致。
BUILTIN_CLASSES: tuple[tuple[str, str], ...] = (
    ("project", "项目"),
    ("task", "任务"),
    ("note", "笔记"),
    ("contact", "联系人"),
    ("knowledge", "知识"),
)


@dataclass
class MigrationReport:
    """功能：一次迁移的核对报告——旧库计数与迁入计数逐项对照。"""

    legacy_entities: int = 0
    legacy_relations: int = 0
    migrated_entities: int = 0
    migrated_facts: int = 0
    classes: list[str] = field(default_factory=list)
    relation_terms: list[str] = field(default_factory=list)
    skipped_entities: list[str] = field(default_factory=list)
    skipped_relations: list[str] = field(default_factory=list)
    backup: str | None = None

    @property
    def counts_match(self) -> bool:
        """功能：计数逐位核对——迁入实体数 = 旧实体数 − 跳过数，事实同理。"""
        return self.migrated_entities == self.legacy_entities - len(
            self.skipped_entities
        ) and self.migrated_facts == self.legacy_relations - len(self.skipped_relations)

    def summary(self) -> str:
        """功能：一行核对摘要（CLI/日志用）。"""
        return (
            f"实体 {self.migrated_entities}/{self.legacy_entities}"
            f"（跳过 {len(self.skipped_entities)}）、"
            f"事实 {self.migrated_facts}/{self.legacy_relations}"
            f"（跳过 {len(self.skipped_relations)}）、"
            f"类 {len(self.classes)}、关系 {len(self.relation_terms)}、"
            f"备份 {self.backup or '无'}"
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_legacy(legacy_db: Path) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """功能：直读旧两表——返回 (实体行, 关系行)；表缺失即空（旧库从无实体数据）。"""
    if not legacy_db.exists():
        return [], []
    conn = connect(legacy_db, query_only=True)
    try:
        entities: list[tuple[str, str, str]] = []
        relations: list[tuple[str, str, str]] = []
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "entities" in tables:
            entities = [
                (str(row[0]), str(row[1]), str(row[2] or ""))
                for row in conn.execute("SELECT id, type, name FROM entities").fetchall()
            ]
        if "relations" in tables:
            relations = [
                (str(row[0]), str(row[1]), str(row[2]))
                for row in conn.execute(
                    "SELECT source_id, target_id, type FROM relations"
                ).fetchall()
            ]
        return entities, relations
    finally:
        conn.close()


def migrate_legacy(
    legacy_db: str | Path,
    ontology_db: str | Path,
    *,
    backup: bool = True,
) -> MigrationReport:
    """功能：把旧实体-关系库迁进三元组本体——目标非空拒绝，迁移前备份。

    失败（含计数不符）时不抛出污染态：调用方拿报告判定，库可经 `rollback_migration`
    整体回退。
    """
    legacy_path = Path(legacy_db)
    target_path = Path(ontology_db)
    report = MigrationReport()
    legacy_entities, legacy_relations = _read_legacy(legacy_path)
    report.legacy_entities = len(legacy_entities)
    report.legacy_relations = len(legacy_relations)

    # 目标非空即拒绝——半迁状态比不迁更难收拾
    if target_path.exists():
        probe = open_ontology(str(target_path))
        try:
            existing = probe.all_entities()
        finally:
            probe.close()
        if existing:
            raise ValueError(
                f"目标本体库非空（{len(existing)} 个实体），拒绝覆盖迁移：{target_path}"
            )
        if backup:
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            backup_path = target_path.with_name(target_path.name + f".bak-{stamp}")
            shutil.copy2(target_path, backup_path)
            report.backup = str(backup_path)

    store = OntologyStore(open_ontology(str(target_path)))
    try:
        now = _now_iso()
        _seed_classes(store, report, now)
        _seed_relation_terms(store, report, now, legacy_relations)
        _migrate_entities(store, report, now, legacy_entities)
        _migrate_relations(store, report, now, legacy_relations)
    finally:
        store.close()
    return report


def _seed_classes(store: OntologyStore, report: MigrationReport, now: str) -> None:
    """功能：种内置 5 类（旧 registry 的实体类型）+ 数据里出现的自定义类型。"""
    for class_id, label in BUILTIN_CLASSES:
        store.define_class({"id": class_id, "label": label}, now)
        report.classes.append(class_id)


def _seed_relation_terms(
    store: OntologyStore, report: MigrationReport, now: str, relations: list[tuple[str, str, str]]
) -> None:
    """功能：为每个合 id 语法的关系类型建词表项；不合语法者对应边整体跳过。"""
    seen: set[str] = set()
    for _source, _target, rel_type in relations:
        if rel_type in seen:
            continue
        seen.add(rel_type)
        if not _ID_RE.fullmatch(rel_type):
            continue
        store.define_relation({"id": rel_type}, now)
        report.relation_terms.append(rel_type)


def _migrate_entities(
    store: OntologyStore, report: MigrationReport, now: str, entities: list[tuple[str, str, str]]
) -> None:
    """功能：实体迁入——类型即类；类名不合 id 语法时跳过该实体（点名进报告）。"""
    for entity_id, entity_type, name in entities:
        if not _ID_RE.fullmatch(entity_id) or not _ID_RE.fullmatch(entity_type):
            report.skipped_entities.append(entity_id)
            continue
        if entity_type not in report.classes:
            store.define_class({"id": entity_type}, now)
            report.classes.append(entity_type)
        outcome = store.assert_entity(
            {"id": entity_id, "classes": [entity_type], "label": name or entity_id}, now
        )
        if not outcome.ok:
            report.skipped_entities.append(entity_id)
            continue
        report.migrated_entities += 1


def _migrate_relations(
    store: OntologyStore, report: MigrationReport, now: str, relations: list[tuple[str, str, str]]
) -> None:
    """功能：有向边迁入为断言事实；关系类型或端点不合规即跳过（点名进报告）。"""
    migrated_ids = {e for e in store.graph().entities}
    for source, target, rel_type in relations:
        label = f"{source} {rel_type} {target}"
        if (
            not _ID_RE.fullmatch(rel_type)
            or source not in migrated_ids
            or target not in migrated_ids
        ):
            report.skipped_relations.append(label)
            continue
        outcome = store.assert_fact(
            {"subject": source, "predicate": rel_type, "object": target}, now
        )
        if not outcome.ok:
            report.skipped_relations.append(label)
            continue
        report.migrated_facts += 1


def rollback_migration(backup_path: str | Path, ontology_db: str | Path) -> None:
    """功能：从备份整体回滚——覆盖目标库并清理 WAL/SHM 附属文件。

    覆盖前不校验备份内容：备份由 `migrate_legacy` 自己产出，损坏时复制品同样损坏，
    多一层校验只会掩盖问题。
    """
    backup = Path(backup_path)
    if not backup.exists():
        raise FileNotFoundError(f"备份不存在：{backup}")
    target = Path(ontology_db)
    shutil.copy2(backup, target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            sidecar.unlink()


__all__ = [
    "BUILTIN_CLASSES",
    "MigrationReport",
    "migrate_legacy",
    "rollback_migration",
]
