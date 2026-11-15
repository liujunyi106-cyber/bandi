# 功能：契约——会话用量「家族聚合」（团队会话含全员执行成本）
# 背景（真机实证）：员工执行的 token 记在各自 role-session 名下，群聊会话自身
# 零 LLM 调用 ⇒ 会话级精确查询恒 0。family_prefix 聚合本会话 + 员工会话家族。
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from bandi.observability.cost_store import CostStore


def _insert(db: Path, session_id: str, tokens_in: int, tokens_out: int, cost: float) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO llm_request_log (event_id, created_at, session_id, model,"
        " input_tokens, output_tokens, total_cost_cny)"
        " VALUES (?, ?, ?, 'deepseek-flash', ?, ?, ?)",
        (
            f"ev-{time.time_ns()}-{tokens_in}",
            "2026-10-16 12:00:00",
            session_id,
            tokens_in,
            tokens_out,
            cost,
        ),
    )
    conn.commit()
    conn.close()


def test_family_prefix_aggregates_employee_sessions(tmp_path: Path) -> None:
    db = tmp_path / "cost.db"
    store = CostStore(str(db))
    sid = "sess-market-1"
    _insert(db, sid, 100, 50, 0.01)
    _insert(db, "role-session::market-entry::staff::a::a", 2000, 300, 0.02)
    _insert(db, "role-session::market-entry::staff::b::b", 3000, 400, 0.03)
    _insert(db, "role-session::other-project::staff::c::c", 9999, 999, 9.0)

    plain = store.session_usage_detail(sid)
    assert plain is not None and plain["total_tokens"] == 150, "单会话口径只含自身"

    family = store.session_usage_detail(sid, family_prefix="role-session::market-entry::%")
    assert family is not None
    assert family["total_tokens"] == 150 + 2300 + 3400, "家族口径含本项目员工会话、不含其他项目"
    assert abs(family["total_cost_cny"] - 0.06) < 1e-9
    assert set(family["per_model"].keys()) == {"deepseek-flash"}


def test_family_prefix_default_keeps_legacy_behavior(tmp_path: Path) -> None:
    """破坏者：不传 family_prefix 时行为与旧一致（精确匹配，不聚合）。"""
    db = tmp_path / "cost.db"
    store = CostStore(str(db))
    _insert(db, "role-session::p1::a::a", 500, 100, 0.0)
    out = store.session_usage_detail("role-session::p1::a::a")
    assert out is not None and out["total_tokens"] == 600
