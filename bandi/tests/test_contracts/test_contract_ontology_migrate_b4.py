"""契约：旧实体-关系库 → 三元组本体迁移（对照表 / D7）。

行为不变式：

  1. **计数逐位核对**：迁入实体数 = 旧实体数 − 跳过数；事实同理
  2. 实体类型 → 类词表；关系类型 → 关系词表；有向边 → 断言事实（谓词保持原名）
  3. **只迁能映射的**：关系类型/实体 id 不合 id 语法 → 跳过并点名（不静默丢）
  4. **目标非空拒绝**：已有数据的本体库不允许被迁移覆盖
  5. **迁移前备份**：非空目标先备份为 `.bak-<时间戳>`
  6. **回滚**：`rollback_migration()` 从备份整体还原
  7. 旧库不存在/无表 → 空迁移（0/0），不报错
  8. 迁移可检视：报告 `counts_match` 为真才算成功

无（本项目自建能力；旧表结构见 `ontology/store.py` 历史 DDL）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bandi.ontology.migrate import MigrationReport, migrate_legacy, rollback_migration
from bandi.ontology.spec import open_ontology
from bandi.ontology.triple_store import OntologyStore

NOW = "2026-10-13T00:00:00Z"

_LEGACY_DDL = (
    """
    CREATE TABLE IF NOT EXISTS entities (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        properties TEXT DEFAULT '{}',
        tags TEXT DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS relations (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        type TEXT NOT NULL,
        properties TEXT DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(source_id, target_id, type)
    )
    """,
)


def _make_legacy(path: Path, entities: list[tuple], relations: list[tuple]) -> Path:
    """功能：造一个真实旧 schema 的迷你库（列齐全，与生产 DDL 同形）。"""
    conn = sqlite3.connect(str(path))
    for ddl in _LEGACY_DDL:
        conn.execute(ddl)
    for entity_id, entity_type, name in entities:
        conn.execute(
            "INSERT INTO entities (id, type, name, created_at, updated_at) VALUES (?,?,?,?,?)",
            (entity_id, entity_type, name, NOW, NOW),
        )
    for index, (source, target, rel_type) in enumerate(relations):
        conn.execute(
            "INSERT INTO relations (id, source_id, target_id, type, created_at) VALUES (?,?,?,?,?)",
            (f"r{index}", source, target, rel_type, NOW),
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def legacy(tmp_path: Path) -> Path:
    return _make_legacy(
        tmp_path / "bandi.db",
        [
            ("p-001", "project", "Bandi Agent"),
            ("t-001", "task", "实现图谱"),
            ("t-002", "task", "写测试"),
            ("n-001", "note", "孤立笔记"),
        ],
        [
            ("t-001", "p-001", "belongs_to"),
            ("t-002", "p-001", "belongs_to"),
            ("n-001", "t-001", "references"),
        ],
    )


class TestCountVerification:
    def test_counts_match_legacy(self, legacy: Path, tmp_path: Path) -> None:
        """核心：迁入计数与旧库逐位一致，报告自证。"""
        report = migrate_legacy(legacy, tmp_path / "ontology.db")
        assert isinstance(report, MigrationReport)
        assert report.legacy_entities == 4
        assert report.legacy_relations == 3
        assert report.migrated_entities == 4
        assert report.migrated_facts == 3
        assert report.skipped_entities == []
        assert report.skipped_relations == []
        assert report.counts_match is True

    def test_entities_land_with_their_class(self, legacy: Path, tmp_path: Path) -> None:
        migrate_legacy(legacy, tmp_path / "ontology.db")
        store = OntologyStore(open_ontology(str(tmp_path / "ontology.db")))
        try:
            graph = store.graph()
            assert set(graph.entities) == {"p-001", "t-001", "t-002", "n-001"}
            assert graph.entities["t-001"].classes == ("task",)
            assert graph.entities["p-001"].label == "Bandi Agent"
        finally:
            store.close()

    def test_edges_become_facts_with_original_predicate(self, legacy: Path, tmp_path: Path) -> None:
        migrate_legacy(legacy, tmp_path / "ontology.db")
        store = OntologyStore(open_ontology(str(tmp_path / "ontology.db")))
        try:
            facts = store.graph().facts
            assert "t-001|belongs_to|p-001" in facts
            assert "n-001|references|t-001" in facts
            assert facts["t-001|belongs_to|p-001"].object_kind == "entity"
        finally:
            store.close()

    def test_relation_terms_created(self, legacy: Path, tmp_path: Path) -> None:
        report = migrate_legacy(legacy, tmp_path / "ontology.db")
        assert set(report.relation_terms) == {"belongs_to", "references"}

    def test_builtin_classes_seeded(self, legacy: Path, tmp_path: Path) -> None:
        """旧 registry 的 5 个内置类名迁后仍认得（D4 种子）。"""
        report = migrate_legacy(legacy, tmp_path / "ontology.db")
        assert {"project", "task", "note", "contact", "knowledge"} <= set(report.classes)


class TestSkipAndReport:
    def test_invalid_relation_type_skipped_with_name(self, tmp_path: Path) -> None:
        """破坏者：自由字符串里藏着不合 id 语法的关系类型——跳过且点名，不静默丢。"""
        legacy = _make_legacy(
            tmp_path / "bandi.db",
            [("a", "task", "A"), ("b", "task", "B")],
            [("a", "b", "belongs_to"), ("a", "b", "某个中文关系")],
        )
        report = migrate_legacy(legacy, tmp_path / "ontology.db")
        assert report.migrated_facts == 1
        assert len(report.skipped_relations) == 1
        assert "某个中文关系" in report.skipped_relations[0]
        assert report.counts_match is True

    def test_invalid_entity_id_skipped(self, tmp_path: Path) -> None:
        legacy = _make_legacy(
            tmp_path / "bandi.db",
            [("ok-1", "task", "正常"), ("1bad", "task", "数字开头")],
            [],
        )
        report = migrate_legacy(legacy, tmp_path / "ontology.db")
        assert report.migrated_entities == 1
        assert report.skipped_entities == ["1bad"]
        assert report.counts_match is True

    def test_dangling_edge_skipped(self, tmp_path: Path) -> None:
        """边端点不在实体内（旧库外键失效场景）——跳过并点名。"""
        legacy = _make_legacy(
            tmp_path / "bandi.db",
            [("a", "task", "A")],
            [("a", "ghost", "references")],
        )
        report = migrate_legacy(legacy, tmp_path / "ontology.db")
        assert report.migrated_facts == 0
        assert len(report.skipped_relations) == 1
        assert report.counts_match is True


class TestEmptyAndRefusal:
    def test_missing_legacy_db_is_empty_migration(self, tmp_path: Path) -> None:
        report = migrate_legacy(tmp_path / "nope.db", tmp_path / "ontology.db")
        assert report.legacy_entities == 0
        assert report.migrated_entities == 0
        assert report.counts_match is True

    def test_legacy_without_tables_is_empty(self, tmp_path: Path) -> None:
        bare = tmp_path / "bare.db"
        sqlite3.connect(str(bare)).close()
        report = migrate_legacy(bare, tmp_path / "ontology.db")
        assert report.legacy_entities == 0
        assert report.counts_match is True

    def test_nonempty_target_refused(self, legacy: Path, tmp_path: Path) -> None:
        """破坏者：目标已有数据时拒绝迁移——半迁状态比不迁更难收拾。"""
        target = tmp_path / "ontology.db"
        store = OntologyStore(open_ontology(str(target)))
        store.define_class({"id": "Existing"}, NOW)
        store.assert_entity({"id": "e1", "classes": ["Existing"]}, NOW)
        store.close()

        with pytest.raises(ValueError, match="非空"):
            migrate_legacy(legacy, target)

    def test_rerun_after_success_refused(self, legacy: Path, tmp_path: Path) -> None:
        """迁移完再跑一次同样被拒（已非空）——天然幂等。"""
        target = tmp_path / "ontology.db"
        migrate_legacy(legacy, target)
        with pytest.raises(ValueError):
            migrate_legacy(legacy, target)


class TestBackupAndRollback:
    def test_backup_created_for_existing_target(self, legacy: Path, tmp_path: Path) -> None:
        target = tmp_path / "ontology.db"
        # 先造一个空库（存在但无实体）→ 迁移应允许并备份
        open_ontology(str(target)).close()
        report = migrate_legacy(legacy, target)
        assert report.backup is not None
        assert Path(report.backup).exists()

    def test_rollback_restores_backup(self, legacy: Path, tmp_path: Path) -> None:
        target = tmp_path / "ontology.db"
        open_ontology(str(target)).close()
        report = migrate_legacy(legacy, target)
        assert Path(report.backup or "").exists()

        rollback_migration(report.backup or "", target)
        store = OntologyStore(open_ontology(str(target)))
        try:
            assert store.graph().entities == {}, "回滚后应为迁移前状态（空库）"
        finally:
            store.close()

    def test_rollback_missing_backup_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            rollback_migration(tmp_path / "ghost.bak", tmp_path / "ontology.db")
