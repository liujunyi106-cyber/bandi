# 功能：契约——技能分支版本化（per-owner 分支：fork / 更新 / 同步 / 晋升 / 回滚 + semver）。
# 钉行为不钉实现：契约断言「最终产物」——分支卡内容与版本号取值、sync_state 语义
# 快照追加与回滚内容逐字恢复、owner 隔离、晋升后全局卡可读回（DoD-12）
# 卡片错配/缺失分支/缺失版本/越界 owner 一律显式失败且不改分支（破坏者）。
# 生产路径族：SkillBranching（fork/update/sync/promote/rollback/versions）
# + 文件落盘 load_branch/save_branch（分支状态新生产消费路径）。
# AgentSkillBranchVersion + ensure/update/sync/promote/rollback + next_*_version。
# 分支对象为 SkillCard / 五件套 + semver / 新建独立模块（不改 EmployeeEvolutionManager）。
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.skills.branching import (
    SkillBranchConflictError,
    SkillBranchError,
    SkillBranching,
    SkillBranchMismatchError,
    SkillBranchNotFoundError,
    SkillBranchSyncState,
)
from bandi.skills.card import SkillCard
from bandi.skills.serializer import load_card, save_card


def _card(
    skill_id: str = "sk_ship_order", version: str = "1.0.0", name: str = "发货 SOP"
) -> SkillCard:
    """功能：契约基准卡——两节点线性 SOP（collect_order → reply_final_result 终端）。"""
    return SkillCard(
        **{
            "skill_id": skill_id,
            "name": name,
            "version": version,
            "required_info": ["订单号"],
            "nodes": [
                {
                    "node_id": "collect_order",
                    "name": "收集订单",
                    "type": "collect_info",
                    "instruction": "收集订单号。",
                    "expected_user_info": ["订单号"],
                },
                {
                    "node_id": "reply_final_result",
                    "name": "反馈结果",
                    "type": "response",
                    "instruction": "给出最终回复。",
                },
            ],
            "edges": [
                {
                    "source_node_id": "collect_order",
                    "next_node_id": "reply_final_result",
                    "priority": 0,
                }
            ],
            "start_node_id": "collect_order",
            "terminal_node_ids": ["reply_final_result"],
        }
    )


@pytest.fixture
def branching(tmp_path: Path) -> SkillBranching:
    """功能：分支管理器（技能库与分支目录均落 tmp，互不污染）。"""
    return SkillBranching(
        skills_dir=str(tmp_path / "skills"),
        branches_dir=str(tmp_path / "branches"),
    )


def _renamed(card: SkillCard, name: str) -> SkillCard:
    """功能：改卡内容（模拟分支本地改写），版本字段由管理器接管。"""
    return card.model_copy(update={"name": name})


def test_b1_fork_creates_synced_branch(branching: SkillBranching) -> None:
    card = _card()

    branch = branching.fork("agent-a", card)

    assert branch.owner_id == "agent-a"
    assert branch.skill_id == "sk_ship_order"
    assert branch.base_version == "1.0.0"
    assert branch.head_version == "1.0.0"
    assert branch.sync_state == SkillBranchSyncState.SYNCED
    assert branch.card.name == "发货 SOP"
    assert [snapshot.version for snapshot in branch.versions] == ["1.0.0"]
    assert branch.versions[0].change_summary


def test_b2_fork_is_idempotent(branching: SkillBranching) -> None:
    card = _card()
    first = branching.fork("agent-a", card)

    second = branching.fork("agent-a", _card(name="改名了但应被忽略"))

    assert second.model_dump(mode="json") == first.model_dump(mode="json")
    assert len(second.versions) == 1


def test_b3_update_bumps_version_and_marks_diverged(branching: SkillBranching) -> None:
    branching.fork("agent-a", _card())

    updated = branching.update("agent-a", "sk_ship_order", _renamed(_card(), "发货 SOP（改）"))

    assert updated.head_version == "1.1.0"
    assert updated.base_version == "1.0.0"
    assert updated.sync_state == SkillBranchSyncState.DIVERGED
    assert updated.card.name == "发货 SOP（改）"
    assert updated.card.version == "1.1.0"
    assert [snapshot.version for snapshot in updated.versions] == ["1.0.0", "1.1.0"]


def test_b4_update_rejects_mismatched_card(branching: SkillBranching) -> None:
    branch = branching.fork("agent-a", _card())
    before = branch.model_dump(mode="json")

    with pytest.raises(SkillBranchMismatchError):
        branching.update("agent-a", "sk_ship_order", _card(skill_id="sk_other_flow"))

    reloaded = branching.load_branch("agent-a", "sk_ship_order")
    assert reloaded is not None
    assert reloaded.model_dump(mode="json") == before


def test_b5_operations_on_missing_branch_raise(branching: SkillBranching) -> None:
    with pytest.raises(SkillBranchNotFoundError):
        branching.update("agent-a", "sk_ghost", _card(skill_id="sk_ghost"))
    with pytest.raises(SkillBranchNotFoundError):
        branching.sync("agent-a", "sk_ghost", _card(skill_id="sk_ghost"))
    with pytest.raises(SkillBranchNotFoundError):
        branching.rollback("agent-a", "sk_ghost", "1.0.0")


def test_b6_sync_overwrites_local_changes(branching: SkillBranching) -> None:
    branching.fork("agent-a", _card())
    branching.update("agent-a", "sk_ship_order", _renamed(_card(), "本地改写"))

    synced = branching.sync("agent-a", "sk_ship_order", _card(version="2.0.0", name="全局新版"))

    assert synced.base_version == "2.0.0"
    assert synced.head_version == "2.0.0"
    assert synced.sync_state == SkillBranchSyncState.SYNCED
    assert synced.card.name == "全局新版"
    assert [snapshot.version for snapshot in synced.versions] == ["1.0.0", "1.1.0", "2.0.0"]


def test_b7_sync_rejects_mismatched_card(branching: SkillBranching) -> None:
    branch = branching.fork("agent-a", _card())
    before = branch.model_dump(mode="json")

    with pytest.raises(SkillBranchMismatchError):
        branching.sync("agent-a", "sk_ship_order", _card(skill_id="sk_other_flow"))

    reloaded = branching.load_branch("agent-a", "sk_ship_order")
    assert reloaded is not None
    assert reloaded.model_dump(mode="json") == before


def test_b8_promote_writes_global_and_resyncs(branching: SkillBranching, tmp_path: Path) -> None:
    save_card(_card(), tmp_path / "skills")
    branching.fork("agent-a", _card())
    branching.update("agent-a", "sk_ship_order", _renamed(_card(), "分支的更好版本"))

    promoted, branch = branching.promote("agent-a", "sk_ship_order")

    assert promoted.version == "1.1.0"
    assert promoted.name == "分支的更好版本"
    on_disk = load_card(tmp_path / "skills" / "sk_ship_order.card.json")
    assert on_disk.name == "分支的更好版本"
    assert on_disk.version == "1.1.0"
    assert branch.base_version == "1.1.0"
    assert branch.head_version == "1.1.0"
    assert branch.sync_state == SkillBranchSyncState.SYNCED


def test_b9_rollback_restores_snapshot(branching: SkillBranching) -> None:
    branching.fork("agent-a", _card())
    branching.update("agent-a", "sk_ship_order", _renamed(_card(), "第二版"))
    branching.update("agent-a", "sk_ship_order", _renamed(_card(), "第三版"))

    rolled = branching.rollback("agent-a", "sk_ship_order", "1.1.0")

    assert rolled.head_version == "1.1.0"
    assert rolled.card.name == "第二版"
    assert rolled.sync_state == SkillBranchSyncState.DIVERGED
    assert [snapshot.version for snapshot in rolled.versions] == ["1.0.0", "1.1.0", "1.2.0"]


def test_b10_rollback_unknown_version_raises(branching: SkillBranching) -> None:
    branch = branching.fork("agent-a", _card())
    before = branch.model_dump(mode="json")

    with pytest.raises(SkillBranchNotFoundError):
        branching.rollback("agent-a", "sk_ship_order", "9.9.9")

    reloaded = branching.load_branch("agent-a", "sk_ship_order")
    assert reloaded is not None
    assert reloaded.model_dump(mode="json") == before


def test_b11_consecutive_updates_keep_versions_unique(branching: SkillBranching) -> None:
    branching.fork("agent-a", _card())

    first = branching.update("agent-a", "sk_ship_order", _renamed(_card(), "v2"))
    second = branching.update("agent-a", "sk_ship_order", _renamed(_card(), "v3"))

    assert first.head_version == "1.1.0"
    assert second.head_version == "1.2.0"
    versions = [snapshot.version for snapshot in second.versions]
    assert versions == ["1.0.0", "1.1.0", "1.2.0"]
    assert len(set(versions)) == len(versions)


def test_b12_owner_id_traversal_is_rejected(branching: SkillBranching, tmp_path: Path) -> None:
    with pytest.raises(SkillBranchError) as excinfo:
        branching.fork("../evil", _card())

    assert "owner_id" in str(excinfo.value)
    assert not (tmp_path / "evil").exists()

    with pytest.raises(SkillBranchError) as load_excinfo:
        branching.load_branch("../evil", "sk_ship_order")
    assert "owner_id" in str(load_excinfo.value)


def test_b13_branch_roundtrip_and_promote_end_to_end(
    branching: SkillBranching, tmp_path: Path
) -> None:
    save_card(_card(), tmp_path / "skills")
    branching.fork("agent-a", _card())
    updated = branching.update("agent-a", "sk_ship_order", _renamed(_card(), "端到端版本"))

    reloaded = branching.load_branch("agent-a", "sk_ship_order")

    assert reloaded is not None
    assert reloaded.model_dump(mode="json") == updated.model_dump(mode="json")

    promoted, _ = branching.promote("agent-a", "sk_ship_order")
    on_disk = load_card(tmp_path / "skills" / "sk_ship_order.card.json")
    assert on_disk.model_dump(mode="json") == promoted.model_dump(mode="json")


def test_b14_branches_are_isolated_per_owner(branching: SkillBranching) -> None:
    branching.fork("agent-a", _card())
    branching.fork("agent-b", _card())
    branching.update("agent-a", "sk_ship_order", _renamed(_card(), "A 的版本"))

    branch_a = branching.load_branch("agent-a", "sk_ship_order")
    branch_b = branching.load_branch("agent-b", "sk_ship_order")

    assert branch_a is not None and branch_b is not None
    assert branch_a.card.name == "A 的版本"
    assert branch_a.head_version == "1.1.0"
    assert branch_b.card.name == "发货 SOP"
    assert branch_b.head_version == "1.0.0"
    assert branch_b.sync_state == SkillBranchSyncState.SYNCED


def test_b15_repeated_sync_does_not_duplicate_snapshot(branching: SkillBranching) -> None:
    branching.fork("agent-a", _card())
    global_v2 = _card(version="2.0.0", name="全局 v2")

    first = branching.sync("agent-a", "sk_ship_order", global_v2)
    second = branching.sync("agent-a", "sk_ship_order", global_v2)

    assert [item.version for item in first.versions] == ["1.0.0", "2.0.0"]
    assert [item.version for item in second.versions] == ["1.0.0", "2.0.0"]
    assert second.head_version == "2.0.0"
    assert second.sync_state == SkillBranchSyncState.SYNCED
    assert second.card.name == "全局 v2"


def test_b16_promote_rejects_diverged_global_baseline(
    branching: SkillBranching, tmp_path: Path
) -> None:
    save_card(_card(), tmp_path / "skills")
    branching.fork("agent-a", _card())
    branching.update("agent-a", "sk_ship_order", _renamed(_card(), "分支改名"))
    save_card(_card(version="1.2.0", name="他人改的全局"), tmp_path / "skills")

    with pytest.raises(SkillBranchConflictError):
        branching.promote("agent-a", "sk_ship_order")

    on_disk = load_card(tmp_path / "skills" / "sk_ship_order.card.json")
    assert on_disk.name == "他人改的全局"
    assert on_disk.version == "1.2.0"


def test_b17_snapshot_limit_is_fail_closed(
    branching: SkillBranching, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bandi.skills import branching as branching_module

    monkeypatch.setattr(branching_module, "MAX_BRANCH_SNAPSHOTS", 2)
    branching.fork("agent-a", _card())
    branching.update("agent-a", "sk_ship_order", _renamed(_card(), "v2"))
    before = branching.load_branch("agent-a", "sk_ship_order")

    with pytest.raises(SkillBranchError):
        branching.update("agent-a", "sk_ship_order", _renamed(_card(), "v3"))

    after = branching.load_branch("agent-a", "sk_ship_order")
    assert before is not None and after is not None
    assert after.model_dump(mode="json") == before.model_dump(mode="json")


def test_b18_promote_at_snapshot_limit_is_atomic(
    branching: SkillBranching, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bandi.skills import branching as branching_module

    monkeypatch.setattr(branching_module, "MAX_BRANCH_SNAPSHOTS", 2)
    save_card(_card(), tmp_path / "skills")
    branching.fork("agent-a", _card())
    global_v2 = _card(version="2.0.0", name="全局 v2")
    save_card(global_v2, tmp_path / "skills")
    branching.sync("agent-a", "sk_ship_order", global_v2)  # 快照 [1.0.0, 2.0.0] 达上限
    before = branching.load_branch("agent-a", "sk_ship_order")

    with pytest.raises(SkillBranchError):
        branching.promote("agent-a", "sk_ship_order")

    on_disk = load_card(tmp_path / "skills" / "sk_ship_order.card.json")
    assert on_disk.version == "2.0.0", "promote 失败不得留下已提升的全局卡（先写后抛=非原子）"
    assert on_disk.name == "全局 v2"
    after = branching.load_branch("agent-a", "sk_ship_order")
    assert before is not None and after is not None
    assert after.model_dump(mode="json") == before.model_dump(mode="json")


def test_b19_load_global_card_error_semantics(branching: SkillBranching, tmp_path: Path) -> None:
    with pytest.raises(SkillBranchNotFoundError):
        branching.load_global_card("ghost_skill")

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "bad_skill.card.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SkillBranchError) as excinfo:
        branching.load_global_card("bad_skill")
    assert "不存在" not in str(excinfo.value), "存在但非法的卡不得报'不存在'（消息必须区分）"
