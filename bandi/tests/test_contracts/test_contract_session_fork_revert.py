"""
会话 Fork/Revert 行为族（对齐 OpenCode `revert.ts` + 学习计划 Day 127：
  Stage（暂存恢复点，软标记不删数据，持久化）→ Commit（永久截断到恢复点）/
  Clear（撤销暂存，数据全量保留）；Fork 复制前缀到独立新会话并记录溯源；
  无状态预览 `resolved_branch(at=...)` = commit 前看"截断后长啥样/会丢几条"
  纯查询不写状态（替代有状态视图导航，防污染/防忘记 reset）。

生产调用路径（DoD-10 族级）：函数入口 = SessionManager.stage/commit/
  clear/fork + SessionStore.truncate_branch + SessionStore.resolved_branch；
  接线 = api `app.py` `/api/sessions/{id}/fork` + `/revert/stage|commit|clear`
  + `/api/sessions/{id}?at=` + `/api/shared/{token}?at=` + cli `app.py`
  `session fork`/`session revert`/`session show --at` + `cli/render.py`
  `render_explain`。契约钉行为结果（读回截断/保留/报错/溯源），不钉实现细节。

破坏者视角：
  - stage/fork/预览 unknown entry 必须显式报错（不能静默记坏恢复点）
  - 无恢复点直接 commit 必须报错（不能把无操作当成功）
  - 重复 stage 以最新为准，不能多点叠加
  - fork 只复制前缀 + 原会话不受影响；commit 截断只作用于目标会话
  - clear（撤销）与 commit（兑现）失败模式需区分：clear 幂等、commit 显式报错
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore
from bandi.session.manager import SessionManager


@pytest.fixture()
def store(tmp_path: Any) -> SessionStore:
    s = SessionStore(str(tmp_path / "sessions.db"))
    yield s
    s.close()


@pytest.fixture()
def manager(store: SessionStore) -> SessionManager:
    m = SessionManager(store)
    m.session_id = store.create_session(title="fork-revert")
    return m


def _append(store: SessionStore, sid: str, n: int) -> list[str]:
    """追加 n 条消息（USER/ASSISTANT 交替），返回追加的 entry_id 列表。"""
    ids: list[str] = []
    for i in range(n):
        role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
        ids.append(store.append_message(sid, Message(role=role, content=f"m{i}")))
    return ids


# ── Stage → Commit 主链路 ─────────────────────────────────────────────────


def test_stage_then_commit_truncates_past_revert_point(store: SessionStore) -> None:
    """生产路径：stage(第2条) → commit → 会话只保留恢复点之前(含恢复点)；
    commit 后新消息从恢复点之后续写，被删除历史不再可见。"""
    sid = store.create_session(title="t")
    ids = _append(store, sid, 3)
    m = SessionManager(store)
    m.session_id = sid

    m.stage(ids[1])
    m.commit()

    branch = store.get_branch(sid)
    assert [e["id"] for e in branch] == ids[:2], "commit 后必须截断到恢复点(含)"

    store.append_message(sid, Message(role=MessageRole.USER, content="续写"))
    assert [e["content"] for e in store.get_branch(sid) if e.get("entry_type") == "message"] == [
        "m0",
        "m1",
        "续写",
    ], "commit 后新消息从恢复点之后续写"


def test_stage_is_soft_marker_data_intact_before_commit(store: SessionStore) -> None:
    """Stage 只暂存恢复点：任何数据都不被删除，get_branch 可见性无影响。"""
    sid = store.create_session(title="t")
    ids = _append(store, sid, 3)
    m = SessionManager(store)
    m.session_id = sid

    m.stage(ids[1])
    assert len(store.get_branch(sid)) == 3, "stage 是软标记，不删除任何数据"


def test_commit_requires_staged_point(store: SessionStore) -> None:
    """无恢复点直接 commit：必须显式报错（不能静默把无操作当成功）。"""
    sid = store.create_session(title="t")
    _append(store, sid, 2)
    m = SessionManager(store)
    m.session_id = sid

    with pytest.raises(ValueError, match="no staged revert point"):
        m.commit()


def test_stage_persists_across_store_reopen(tmp_path: Any) -> None:
    """Stage 软标记必须持久化：重启 store 后 commit 仍能兑现（不能只在内存）。"""
    path = tmp_path / "sessions.db"
    s1 = SessionStore(str(path))
    sid = s1.create_session(title="t")
    ids = _append(s1, sid, 3)
    m = SessionManager(s1)
    m.session_id = sid
    m.stage(ids[1])
    s1.close()

    s2 = SessionStore(str(path))
    m2 = SessionManager(s2)
    m2.session_id = sid
    m2.commit()
    assert [e["id"] for e in s2.get_branch(sid)] == ids[:2], "重启后恢复点必须仍可兑现"
    s2.close()


def test_commit_clears_staged_point(store: SessionStore) -> None:
    """Commit 兑现后恢复点必须清空：再次 commit 报错，不能静默把无操作当成功。"""
    sid = store.create_session(title="t")
    ids = _append(store, sid, 3)
    m = SessionManager(store)
    m.session_id = sid

    m.stage(ids[1])
    assert m.commit() == 1

    with pytest.raises(ValueError, match="no staged revert point"):
        m.commit()


def test_re_stage_overrides_previous_point(store: SessionStore) -> None:
    """重复 stage：以最新恢复点为准，不能出现多点叠加。"""
    sid = store.create_session(title="t")
    ids = _append(store, sid, 3)
    m = SessionManager(store)
    m.session_id = sid

    m.stage(ids[1])
    m.stage(ids[0])
    m.commit()

    assert [e["id"] for e in store.get_branch(sid)] == ids[:1], "最新 stage 覆盖旧恢复点"


def test_commit_truncate_does_not_touch_sibling_session(store: SessionStore) -> None:
    """截断只作用于目标会话，不影响同库其他会话（隔离破坏者视角）。"""
    sid_a = store.create_session(title="a")
    ids_a = _append(store, sid_a, 3)
    sid_b = store.create_session(title="b")
    _append(store, sid_b, 3)
    m = SessionManager(store)
    m.session_id = sid_a

    m.stage(ids_a[1])
    m.commit()

    assert len(store.get_branch(sid_a)) == 2
    assert len(store.get_branch(sid_b)) == 3, "其他会话不受截断影响"


# ── Clear（撤销）──────────────────────────────────────────────────────────


def test_clear_cancels_revert_point_all_data_intact(store: SessionStore) -> None:
    """Stage 后 clear：撤销恢复点，数据全量保留；恢复点清空后 commit 才可再次 stage。"""
    sid = store.create_session(title="t")
    ids = _append(store, sid, 3)
    m = SessionManager(store)
    m.session_id = sid

    m.stage(ids[1])
    m.clear()
    assert len(store.get_branch(sid)) == 3, "clear 后必须恢复全量可见"

    m.stage(ids[0])
    m.commit()
    assert [e["id"] for e in store.get_branch(sid)] == ids[:1], "clear 后 stage 可正常再截断"


def test_clear_without_stage_is_noop(store: SessionStore) -> None:
    """无恢复点 clear：幂等无操作（区别于 commit 的显式报错）。"""
    sid = store.create_session(title="t")
    _append(store, sid, 2)
    m = SessionManager(store)
    m.session_id = sid

    m.clear()
    assert len(store.get_branch(sid)) == 2, "无恢复点时 clear 不产生副作用"


# ── 无状态预览 resolved_branch(at=...)（commit 前看截断后形态）─────────────


def test_resolved_branch_default_returns_full_branch(store: SessionStore) -> None:
    """不传 at：与 get_branch 等价（全量），默认行为不可因预览引入而改变。"""
    sid = store.create_session(title="t")
    ids = _append(store, sid, 3)

    resolved = store.resolved_branch(sid)
    assert [e["id"] for e in resolved] == ids, "at=None 必须返回全量分支"


def test_resolved_branch_truncates_at_inclusive_no_delete(store: SessionStore) -> None:
    """at=<entry>：截断到恢复点（含自身），数据层不删、无状态可反复查询。"""
    sid = store.create_session(title="t")
    ids = _append(store, sid, 3)

    resolved = store.resolved_branch(sid, at=ids[1])
    assert [e["id"] for e in resolved] == ids[:2], "预览以恢复点为界（含自身）"
    assert len(store.get_branch(sid)) == 3, "预览不删除任何数据"


def test_resolved_branch_unknown_at_raises_no_side_effect(store: SessionStore) -> None:
    """at 指向不存在的 entry：显式报错（不能静默回退成全量骗过用户），无副作用。"""
    sid = store.create_session(title="t")
    _append(store, sid, 2)

    with pytest.raises(ValueError, match="not found"):
        store.resolved_branch(sid, at="nonexistent-entry")

    assert len(store.get_branch(sid)) == 2, "守卫失败不得产生任何副作用"


def test_resolved_branch_commit_preview_flow(store: SessionStore) -> None:
    """生产路径：stage → commit 前用预览看截断形态 → commit 后截断兑现一致。"""
    sid = store.create_session(title="t")
    ids = _append(store, sid, 3)
    m = SessionManager(store)
    m.session_id = sid

    preview = store.resolved_branch(sid, at=ids[1])
    assert [e["id"] for e in preview] == ids[:2]

    m.stage(ids[1])
    m.commit()
    assert [e["id"] for e in store.get_branch(sid)] == ids[:2], "commit 兑现与预览一致"


def test_resolved_branch_empty_at_returns_full_branch(store: SessionStore) -> None:
    """空串 at（`?at=` / `--at ""`）：语义等同不传，返回全量，不能让 API 与 CLI 不一致。"""
    sid = store.create_session(title="t")
    ids = _append(store, sid, 3)

    resolved = store.resolved_branch(sid, at="")
    assert [e["id"] for e in resolved] == ids, "空串 at 不能当 '不存在 entry' 报错"


def test_resolved_branch_is_stable_prefix_of_get_branch(store: SessionStore) -> None:
    """预览与 get_branch 共用同一稳定排序（同 created_at 有 tie-breaker）：
    预览恒为 get_branch 的严格前缀，保证 preview 与 commit/fork 边界一致。"""
    sid = store.create_session(title="t")
    ids = _append(store, sid, 4)

    canonical = [e["id"] for e in store.get_branch(sid)]
    for at in ids:
        preview = store.resolved_branch(sid, at=at)
        assert [e["id"] for e in preview] == canonical[: canonical.index(at) + 1]


# ── Fork 行为族 ───────────────────────────────────────────────────────────


def test_fork_copies_prefix_to_independent_session(store: SessionStore) -> None:
    """fork(第2条)：新会话复制恢复点前缀；原会话数据不受影响。"""
    sid = store.create_session(title="t")
    ids = _append(store, sid, 4)
    m = SessionManager(store)
    m.session_id = sid

    new_sid = m.fork(ids[1])

    new_entries = store.get_branch(new_sid)
    assert [e["content"] for e in new_entries if e.get("entry_type") == "message"] == [
        "m0",
        "m1",
    ], "fork 只复制恢复点前缀"

    assert len(store.get_branch(sid)) == 4, "fork 不修改原会话"


def test_fork_new_session_has_provenance_metadata(store: SessionStore) -> None:
    """溯源：新会话记录父会话 id 与恢复点 entry（轻量，不建新表）。"""
    sid = store.create_session(title="t")
    ids = _append(store, sid, 2)
    m = SessionManager(store)
    m.session_id = sid

    new_sid = m.fork(ids[0])
    new_session = store.get_session(new_sid)
    assert new_session is not None
    raw = new_session.get("metadata")
    assert raw, "溯源信息必须写入新会话"
    provenance = json.loads(raw)
    assert provenance.get("parent_session_id") == sid, "溯源须记录父会话"
    assert provenance.get("forked_from_entry") == ids[0], "溯源须记录恢复点 entry"


def test_fork_inherits_scope_metadata(store: SessionStore) -> None:
    """派生必须继承归属维度（project_root/project_id/origin）——/137 metadata
    扩展后派生会话不得丢失项目/秘书归属（否则刷新后从项目面板消失、跑错分区
    工具沙箱根丢失）。"""
    sid = store.create_session(
        title="秘书项目会话",
        project_root="/tmp/proj-demo",
        project_id="p-demo",
        origin="secretary",
    )
    ids = _append(store, sid, 2)
    m = SessionManager(store)
    m.session_id = sid

    new_sid = m.fork(ids[0])
    meta = json.loads(store.get_session(new_sid)["metadata"])
    assert meta["parent_session_id"] == sid
    assert meta["forked_from_entry"] == ids[0]
    assert meta["project_id"] == "p-demo", "派生会话必须继承项目归属"
    assert meta["origin"] == "secretary", "派生会话必须继承来源标记（面板分区数据源）"
    assert meta["project_root"] == "/tmp/proj-demo", "派生会话必须继承项目根（工具沙箱）"


def test_fork_without_scope_metadata_writes_no_empty_keys(store: SessionStore) -> None:
    """破坏者视角：源会话无归属键（旧格式/普通会话）时派生 metadata 不得写入
    空值键（'' / None）——避免前端把空串当项目维度、把旧格式污染成显式空归属。"""
    sid = store.create_session(title="t")
    ids = _append(store, sid, 1)
    m = SessionManager(store)
    m.session_id = sid

    new_sid = m.fork(ids[0])
    meta = json.loads(store.get_session(new_sid)["metadata"])
    assert set(meta) == {"parent_session_id", "forked_from_entry"}, (
        f"无归属源会话的派生只写溯源键，实际 {meta}"
    )


# ── 破坏者视角：未知 entry 守卫 ───────────────────────────────────────────


@pytest.mark.parametrize("op", ["stage", "fork"])
def test_unknown_entry_raises_across_ops(store: SessionStore, op: str) -> None:
    """所有以 entry 为目标的操作用同一显式失败模式（族级契约，parametrize=2）。"""
    sid = store.create_session(title="t")
    _append(store, sid, 2)
    m = SessionManager(store)
    m.session_id = sid

    with pytest.raises(ValueError, match="not found"):
        getattr(m, op)("nonexistent-entry")

    assert len(store.get_branch(sid)) == 2, "守卫失败不得产生任何副作用"
