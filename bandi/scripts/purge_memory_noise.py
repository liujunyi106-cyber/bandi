# 功能：清理记忆库中的审批等待/回执/寒暄等噪声条目（默认预演，--apply 才删除）
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from bandi.memory.episodic import EpisodicStore
from bandi.memory.ingest_review import is_noise_memory
from bandi.memory.long_term import LongTermMemory
from bandi.memory.staging import MemoryPendingStore


def _iter_long_term(store: LongTermMemory) -> list[Any]:
    items: list[Any] = []
    for project_id, employee_id in store.list_scopes():
        offset = 0
        while True:
            page = store.list_items(
                project_id=project_id, employee_id=employee_id, limit=500, offset=offset
            )
            items.extend(page)
            if len(page) < 500:
                break
            offset += len(page)
    return items


def _iter_episodes(store: EpisodicStore) -> list[Any]:
    episodes: list[Any] = []
    for project_id, employee_id in store.list_scopes():
        episodes.extend(
            store.list_for_maintenance(
                project_id=project_id,
                employee_id=employee_id,
                oldest_first=True,
                limit=100_000,
            )
        )
    return episodes


def _backup(data_dir: Path, pending_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = data_dir.parent / "backups" / f"memory-noise-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    for name in (
        "long_term.db",
        "long_term.db-wal",
        "long_term.db-shm",
        "episodic.db",
        "episodic.db-wal",
        "episodic.db-shm",
    ):
        source = data_dir / name
        if source.exists():
            shutil.copy2(source, backup / name)
    if pending_dir.exists():
        shutil.copytree(pending_dir, backup / "pending", dirs_exist_ok=True)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".bandi" / "data")
    parser.add_argument("--store-dir", type=Path, default=Path.home() / ".bandi" / "memory")
    parser.add_argument("--apply", action="store_true", help="实际删除；缺省只预演")
    args = parser.parse_args()

    data_dir: Path = args.data_dir
    store_dir: Path = args.store_dir
    pending_dir = store_dir / "pending"
    if not data_dir.exists():
        raise SystemExit(f"data dir not found: {data_dir}")

    backup = None
    if args.apply:
        backup = _backup(data_dir, pending_dir)
        print(f"backup: {backup}")

    long_term = LongTermMemory(str(data_dir / "long_term.db"))
    episodic = EpisodicStore(str(data_dir / "episodic.db"))
    pending = MemoryPendingStore(store_dir)
    report: dict[str, Any] = {
        "long_term": {"scanned": 0, "deleted": 0, "samples": []},
        "episodic": {"scanned": 0, "deleted": 0, "samples": []},
        "pending": {"scanned": 0, "deleted": 0, "samples": []},
        "apply": bool(args.apply),
        "backup": str(backup) if backup else "",
    }
    try:
        for item in _iter_long_term(long_term):
            report["long_term"]["scanned"] += 1
            reason = is_noise_memory(item.content)
            if reason is None:
                continue
            if len(report["long_term"]["samples"]) < 8:
                report["long_term"]["samples"].append(
                    {"id": item.id, "reason": reason, "content": item.content[:120]}
                )
            if args.apply:
                long_term.delete(item.id)
            report["long_term"]["deleted"] += 1

        for episode in _iter_episodes(episodic):
            report["episodic"]["scanned"] += 1
            reason = is_noise_memory(episode.summary)
            if reason is None:
                continue
            if len(report["episodic"]["samples"]) < 8:
                report["episodic"]["samples"].append(
                    {"id": episode.episode_id, "reason": reason, "summary": episode.summary[:160]}
                )
            if args.apply:
                episodic.delete(episode.episode_id)
            report["episodic"]["deleted"] += 1

        for record in pending.list_pending():
            report["pending"]["scanned"] += 1
            payload = record.payload or {}
            item = payload.get("item") if isinstance(payload, dict) else None
            content = ""
            if isinstance(item, dict):
                content = str(item.get("content") or "")
            reason = is_noise_memory(content or record.summary)
            if reason is None:
                continue
            if len(report["pending"]["samples"]) < 8:
                report["pending"]["samples"].append(
                    {"id": record.id8, "reason": reason, "summary": record.summary[:160]}
                )
            if args.apply:
                pending.discard_pending(record.id8)
            report["pending"]["deleted"] += 1
    finally:
        for closer in (long_term, episodic):
            try:
                closer.close()
            except Exception:  # noqa: BLE001
                pass

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
