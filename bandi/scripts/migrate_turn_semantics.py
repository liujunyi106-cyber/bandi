"""功能：历史会话轮次语义迁移。

旧库的 session_entries.turn_index 实际是「模型步」（每次 THINKING 自增），
新语义要求它是「用户轮」。迁移策略：

1. 在 session_entries.metadata 打 `turn_semantics: "step_indexed"` 标记，
   表明该行的 turn_index 属于旧口径；
2. 按消息顺序重建真实轮次：每条 user 消息开启新一轮，其后的 assistant/tool
   归该轮，写入 `derived_turn`（metadata 内），不改动原 turn_index 列
   （避免破坏按 (session, turn) 的既有幂等键）。

幂等：已带标记的行跳过；重复执行结果一致。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB = Path.home() / ".bandi" / "data" / "bandi.db"
MARK = "step_indexed"


def migrate(db_path: Path, *, dry_run: bool = False) -> dict[str, int]:
    con = sqlite3.connect(str(db_path), timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute(
        "SELECT id, session_id, role, turn_index, metadata, created_at"
        " FROM session_entries ORDER BY session_id, created_at, rowid"
    )
    rows = cur.fetchall()

    stats = {"sessions": 0, "rows": 0, "marked": 0, "multi_turn": 0}
    current_session = None
    derived_turn = 0
    pending: list[tuple[int, str]] = []

    for row in rows:
        sid = row["session_id"]
        if sid != current_session:
            current_session = sid
            derived_turn = 0
            stats["sessions"] += 1
        stats["rows"] += 1

        if row["role"] == "user":
            derived_turn += 1

        meta = {}
        try:
            meta = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        if meta.get("turn_semantics") == MARK:
            continue

        meta["turn_semantics"] = MARK
        meta["derived_turn"] = max(1, derived_turn)
        # 旧 turn_index 保留在 metadata 供对照，便于回滚与核查
        meta.setdefault("legacy_turn_index", int(row["turn_index"] or 0))
        pending.append((row["id"], json.dumps(meta, ensure_ascii=False)))
        if int(row["turn_index"] or 0) != max(1, derived_turn):
            stats["multi_turn"] += 1

    stats["marked"] = len(pending)
    if dry_run:
        con.close()
        return stats

    with con:
        cur.executemany(
            "UPDATE session_entries SET metadata = ? WHERE id = ?",
            [(m, rid) for rid, m in pending],
        )
    con.close()
    return stats


if __name__ == "__main__":
    import sys

    dry = "--dry-run" in sys.argv
    if not DB.exists():
        raise SystemExit(f"库不存在: {DB}")
    result = migrate(DB, dry_run=dry)
    print(("DRY-RUN " if dry else "") + f"迁移结果: {result}")
