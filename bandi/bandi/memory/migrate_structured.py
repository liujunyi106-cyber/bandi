# 功能：存量长期记忆结构化迁移——turn:/consolidated: 条目按规则补 structured
#
# 用法（从仓库根 bandi/ 执行）：
#   python -m bandi.memory.migrate_structured                # dry-run 统计
#   python -m bandi.memory.migrate_structured --apply        # 执行（自动备份）
#   python -m bandi.memory.migrate_structured --rollback     # 从最近备份恢复
#
# 语义：规则抽取能出 kind/key → 补 {kind,key,...}；抽不出 → 标注 evidence-only
# （不假装结构化）。已有 structured 的跳过（幂等重跑）。写入后逐位核对计数。
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from bandi.memory.structured import EVIDENCE_ONLY_KIND, build_structured
from bandi.utils.db import connect

_TARGET_WHERE = "id LIKE 'turn:%' OR id LIKE 'consolidated:%'"
_BACKUP_SUFFIX = ".structured-migration"


def default_db_path() -> Path:
    """功能：默认长期记忆库路径（env 不参与——迁移工具显式操作生产库）。"""
    return Path.home() / ".bandi" / "data" / "long_term.db"


@dataclass
class MigrationReport:
    """功能：迁移统计（scanned = 目标前缀全量；updated/skipped/evidence_only 分区）。"""

    scanned: int = 0
    updated: int = 0
    skipped: int = 0
    evidence_only: int = 0
    verified: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "updated": self.updated,
            "skipped": self.skipped,
            "evidence_only": self.evidence_only,
            "verified": dict(self.verified),
        }


def _read_metadata(raw: object) -> dict:
    try:
        data = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def migrate_long_term(db_path: str | Path, *, apply: bool = False) -> MigrationReport:
    """功能：执行结构化迁移——dry-run 只统计；apply 写库并逐位核对。"""
    report = MigrationReport()
    conn = connect(db_path)
    try:
        cur = conn.cursor()
        rows = cur.execute(
            f"SELECT id, content, metadata FROM long_term_memory WHERE {_TARGET_WHERE}"
        ).fetchall()
        report.scanned = len(rows)
        updates: list[tuple[str, str]] = []
        for item_id, content, metadata_json in rows:
            metadata = _read_metadata(metadata_json)
            existing = metadata.get("structured")
            if isinstance(existing, dict) and existing.get("kind"):
                report.skipped += 1
                continue
            structured = build_structured(str(content or ""))
            if structured is None:
                structured = {"kind": EVIDENCE_ONLY_KIND, "key": EVIDENCE_ONLY_KIND}
                report.evidence_only += 1
            else:
                report.updated += 1
            metadata["structured"] = structured
            updates.append((json.dumps(metadata, ensure_ascii=False), str(item_id)))
        if apply and updates:
            cur.executemany("UPDATE long_term_memory SET metadata = ? WHERE id = ?", updates)
            conn.commit()
            total = cur.execute(
                f"SELECT COUNT(*) FROM long_term_memory WHERE {_TARGET_WHERE}"
            ).fetchone()[0]
            covered = cur.execute(
                f"SELECT COUNT(*) FROM long_term_memory WHERE ({_TARGET_WHERE}) "
                "AND metadata LIKE '%\"structured\"%'"
            ).fetchone()[0]
            report.verified = {"total": int(total), "with_structured": int(covered)}
            if report.verified["total"] != report.verified["with_structured"]:
                raise RuntimeError(f"迁移计数不符：total={total} with_structured={covered}")
    finally:
        conn.close()
    return report


def backup_db(db_path: str | Path) -> Path:
    """功能：迁移前备份库文件（时间戳后缀；返回备份路径）。"""
    source = Path(db_path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = source.with_name(source.name + f"{_BACKUP_SUFFIX}-{stamp}.bak")
    shutil.copy2(source, backup)
    return backup


def latest_backup(db_path: str | Path) -> Path | None:
    """功能：找最近一次迁移备份（无则 None）。"""
    source = Path(db_path)
    candidates = sorted(source.parent.glob(source.name + f"{_BACKUP_SUFFIX}-*.bak"))
    return candidates[-1] if candidates else None


def rollback_db(db_path: str | Path) -> Path | None:
    """功能：从最近备份恢复库文件（返回所用备份路径；无备份返回 None）。"""
    backup = latest_backup(db_path)
    if backup is None:
        return None
    shutil.copy2(backup, Path(db_path))
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="存量长期记忆结构化迁移")
    parser.add_argument("--db", default=str(default_db_path()), help="long_term 库路径")
    parser.add_argument("--apply", action="store_true", help="执行写入（默认 dry-run）")
    parser.add_argument("--rollback", action="store_true", help="从最近备份恢复")
    args = parser.parse_args(argv)
    if args.rollback:
        used = rollback_db(args.db)
        if used is None:
            print("未找到迁移备份，无法回滚")
            return 1
        print(f"已从备份恢复：{used}")
        return 0
    if not Path(args.db).exists():
        print(f"库不存在：{args.db}")
        return 1
    if args.apply:
        backup = backup_db(args.db)
        print(f"已备份：{backup}")
    report = migrate_long_term(args.db, apply=args.apply)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if not args.apply:
        print("（dry-run —— 加 --apply 才写库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
