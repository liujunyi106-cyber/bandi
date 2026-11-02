# 功能：契约——员工产出会话写回按「同卡最新覆盖」语义
# 失败轮终止文案先占幂等键，成功重试的写回被同卡判重静默跳过 ⇒ 会话里永远
# 只有失败文案、成功回复刷新即丢）。同 (session, work_item) 恰一条；后写覆盖前写。
from __future__ import annotations

import sqlite3

from bandi.memory.session.store import SessionStore


def _store(tmp_path) -> SessionStore:
    return SessionStore(str(tmp_path / "sessions.db"))


def _entries(db_path, session_id: str):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT rowid, content, tool_call_id, metadata FROM session_entries "
        "WHERE session_id = ? ORDER BY rowid",
        (session_id,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def test_first_write_inserts_employee_reply(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_session("s1")
    rid = store.append_employee_reply(
        "s1",
        content="首版回复",
        sender_role="deep_research",
        work_item_id="wi-1",
        sender_name="深度研究专家",
    )
    assert rid is not None
    rows = _entries(tmp_path / "sessions.db", "s1")
    assert len(rows) == 1 and rows[0]["content"] == "首版回复"


def test_same_work_item_latest_reply_replaces_in_place(tmp_path) -> None:
    """破坏者：失败文案先写、成功重试后写——必须覆盖（同一条、不新增）。"""
    store = _store(tmp_path)
    store.create_session("s1")
    rid1 = store.append_employee_reply(
        "s1",
        content="模型响应超时（流式调用超过 240 秒未完成）",
        sender_role="industry_opportunity",
        work_item_id="wi-1",
    )
    rid2 = store.append_employee_reply(
        "s1",
        content="老板，先给判断：这是最终规划全文。",
        sender_role="industry_opportunity",
        work_item_id="wi-1",
        next_assignee={"role_id": "deep_research", "card": "取证"},
    )
    assert rid2 == rid1, "同卡覆盖必须落同一条目（位置稳定，不新增）"
    rows = _entries(tmp_path / "sessions.db", "s1")
    assert len(rows) == 1, "同卡任意次重写都必须恰一条"
    assert "最终规划全文" in rows[0]["content"], "最新产出必须覆盖失败文案"
    assert rows[0]["tool_call_id"] == "employee-reply:wi-1"


def test_replace_updates_metadata_snapshot(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_session("s1")
    store.append_employee_reply("s1", content="v1", sender_role="r", work_item_id="wi-1")
    store.append_employee_reply(
        "s1",
        content="v2",
        sender_role="r",
        work_item_id="wi-1",
        sender_name="新名字",
        next_assignee={"role_id": "data_analyst", "card": "数据处理"},
    )
    rows = _entries(tmp_path / "sessions.db", "s1")
    import json

    meta = json.loads(rows[0]["metadata"])
    assert meta["sender_name"] == "新名字"
    assert meta["next_assignee"]["role_id"] == "data_analyst"
    assert meta["content"] if "content" in meta else True  # 兼容字段不存在亦可


def test_distinct_work_items_stay_separate(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_session("s1")
    store.append_employee_reply("s1", content="A", sender_role="r", work_item_id="wi-a")
    store.append_employee_reply("s1", content="B", sender_role="r", work_item_id="wi-b")
    rows = _entries(tmp_path / "sessions.db", "s1")
    assert [r["content"] for r in rows] == ["A", "B"], "不同卡各自一条，互不覆盖"


def test_empty_content_still_skipped(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_session("s1")
    assert (
        store.append_employee_reply("s1", content="   ", sender_role="r", work_item_id="wi-1")
        is None
    )
