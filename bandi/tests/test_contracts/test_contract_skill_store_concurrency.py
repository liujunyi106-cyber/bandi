# 功能：契约——/ 帧文件与分支文件并发写冲突检测（revision 乐观锁）。
# 钉行为不钉实现：两写者各自读同一基线后，后写者的保存必须显式报错（不得静默覆盖）
# 且盘上内容保持先写者版本逐字不变；同一对象连续保存（版本追踪）不误报；
# 首写（文件不存在）正常落盘并递增 revision。
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.skills.branching import SkillBranchConflictError, SkillBranching
from bandi.skills.card import SkillCard
from bandi.skills.frame_store import (
    SkillFrameSaveError,
    load_frames,
    save_frames,
)
from bandi.skills.runtime import SkillRuntime


def _card_two_nodes() -> SkillCard:
    return SkillCard(
        **{
            "skill_id": "sk_sync",
            "name": "并发用例",
            "version": "1.0.0",
            "description": "两节点流程。",
            "nodes": [
                {"node_id": "n1", "name": "起点", "type": "collect_info", "instruction": "开始。"},
                {"node_id": "n2", "name": "终点", "type": "response", "instruction": "结束。"},
            ],
            "edges": [{"source_node_id": "n1", "next_node_id": "n2", "priority": 0}],
            "start_node_id": "n1",
            "terminal_node_ids": ["n2"],
        }
    )


# 帧文件 stale 写入被拒——盘上保持先写者内容逐字不变
def test_frame_stale_writer_is_rejected(tmp_path: Path) -> None:
    frames_dir = str(tmp_path / "frames")
    runtime = SkillRuntime()
    stack = runtime.start(runtime.new_stack("sess-sync"), _card_two_nodes(), task_id="t1")
    save_frames(stack, frames_dir)

    writer_a = load_frames("sess-sync", frames_dir)
    writer_b = load_frames("sess-sync", frames_dir)
    assert writer_a is not None and writer_b is not None

    writer_a = runtime.start(writer_a, _card_two_nodes(), task_id="ta")
    save_frames(writer_a, frames_dir)
    expected = (Path(frames_dir) / "sess-sync.frames.json").read_text(encoding="utf-8")

    with pytest.raises(SkillFrameSaveError):
        save_frames(runtime.start(writer_b, _card_two_nodes(), task_id="tb"), frames_dir)

    assert (Path(frames_dir) / "sess-sync.frames.json").read_text(encoding="utf-8") == expected


# 同一对象连续保存不误报（保存回写版本号，追踪最新）
def test_same_object_sequential_save_is_fine(tmp_path: Path) -> None:
    frames_dir = str(tmp_path / "frames")
    runtime = SkillRuntime()
    stack = runtime.start(runtime.new_stack("sess-seq"), _card_two_nodes(), task_id="t1")
    save_frames(stack, frames_dir)
    stack = runtime.suspend(stack)

    save_frames(stack, frames_dir)

    reloaded = load_frames("sess-seq", frames_dir)
    assert reloaded is not None
    assert reloaded.model_dump(mode="json") == stack.model_dump(mode="json")


# 首写正常（文件不存在不视为冲突）且读回 revision 单调递增
def test_first_write_and_revision_advances(tmp_path: Path) -> None:
    frames_dir = str(tmp_path / "frames")
    runtime = SkillRuntime()
    stack = runtime.start(runtime.new_stack("sess-first"), _card_two_nodes(), task_id="t1")

    save_frames(stack, frames_dir)
    first = load_frames("sess-first", frames_dir)
    assert first is not None and first.revision >= 1
    first_revision = first.revision

    save_frames(first, frames_dir)
    second = load_frames("sess-first", frames_dir)
    assert second is not None and second.revision == first_revision + 1


def test_branch_stale_writer_is_rejected(tmp_path: Path) -> None:
    manager = SkillBranching(
        skills_dir=str(tmp_path / "skills"), branches_dir=str(tmp_path / "branches")
    )
    card = _card_two_nodes()
    manager.fork("owner-a", card)

    writer_a = manager.load_branch("owner-a", "sk_sync")
    writer_b = manager.load_branch("owner-a", "sk_sync")
    assert writer_a is not None and writer_b is not None

    manager.save_branch(writer_a.model_copy(update={"head_version": "1.1.0"}))
    branch_path = manager.branch_path("owner-a", "sk_sync")
    expected = branch_path.read_text(encoding="utf-8")

    with pytest.raises(SkillBranchConflictError):
        manager.save_branch(writer_b.model_copy(update={"head_version": "2.0.0"}))

    assert branch_path.read_text(encoding="utf-8") == expected


def test_branch_normal_flow_unaffected(tmp_path: Path) -> None:
    manager = SkillBranching(
        skills_dir=str(tmp_path / "skills"), branches_dir=str(tmp_path / "branches")
    )
    card = _card_two_nodes()
    from bandi.skills.serializer import save_card

    save_card(card, str(tmp_path / "skills"))
    manager.fork("owner-b", card)
    updated = manager.update(
        "owner-b",
        "sk_sync",
        card.model_copy(update={"description": "改写后。"}),
        change_summary="改写",
    )
    assert updated.head_version != card.version

    synced = manager.sync("owner-b", "sk_sync", card)
    assert synced.sync_state.value == "synced"
