# 功能：契约——团队记忆暂存写侧过期清理（审计：暂存无生产
# 过期调用——原仅列表端点读侧清理，不访问 UI 时暂存无界堆积）。
from __future__ import annotations

import json
import time

from bandi.memory.team_staging import TeamMemoryStaging


def _propose(staging: TeamMemoryStaging, summary: str, turn: int) -> str:
    return staging.propose(
        summary=summary,
        project_id="p",
        team_id="t",
        employee_id="e",
        session_id="s1",
        turn_index=turn,
    )


def test_propose_cleans_expired_on_write(tmp_path) -> None:
    """契约（核心）：propose（写侧）必须触发过期清理——不访问列表端点
    的场景下暂存不得无界堆积（红：现状仅读侧清理，老记录保留）。"""
    staging = TeamMemoryStaging(tmp_path)
    first = _propose(staging, "旧教训", 1)
    assert first

    # 将首条记录 created_at 改老（超过默认 TTL 7 天）并重写文件
    path = tmp_path / "pending" / "team_memory" / f"{first}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["created_at"] = time.time() - 8 * 86400
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # 重新实例化，避开进程内按 (mtime, size) 的读缓存——本用例钉的是写侧清理，不是缓存失效
    staging = TeamMemoryStaging(tmp_path)
    second = _propose(staging, "新教训", 2)
    assert second

    ids = [r.id8 for r in staging.list_pending()]
    assert first not in ids, f"写侧必须清理过期项（实际 {ids}）"
    assert second in ids


def test_fresh_records_not_cleaned_by_write(tmp_path) -> None:
    """契约（对照）：未过期记录不得被写侧清理误删。"""
    staging = TeamMemoryStaging(tmp_path)
    first = _propose(staging, "新鲜教训", 1)
    second = _propose(staging, "第二条", 2)

    ids = [r.id8 for r in staging.list_pending()]
    assert first in ids and second in ids
