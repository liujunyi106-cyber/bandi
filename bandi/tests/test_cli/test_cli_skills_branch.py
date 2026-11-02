# 功能：CLI 生产入口测试——bandi skills branch（fork/update/promote/versions）。
# 走真实命令链 + 真实分支文件与技能库落盘（断言最终产物：分支文件、晋升后的全局卡）。
# 覆盖 SkillBranching 的生产调用路径（DoD-11 接线：CLI → 分支管理器 → 文件）。
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from bandi.cli.app import app
from bandi.skills.card import SkillCard
from bandi.skills.serializer import load_card, save_card


def _card(name: str = "发货 SOP", version: str = "1.0.0") -> SkillCard:
    """功能：两节点线性 SOP（collect_order → reply_final_result 终端）。"""
    return SkillCard(
        **{
            "skill_id": "sk_ship_order",
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


def _invoke(runner: CliRunner, skills_dir: Path, branches_dir: Path, *args: str):
    return runner.invoke(
        app,
        [
            "skills",
            "branch",
            "--skills-dir",
            str(skills_dir),
            "--branches-dir",
            str(branches_dir),
            *args,
        ],
    )


def test_c5_cli_branch_fork_persists(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    branches_dir = tmp_path / "branches"
    save_card(_card(), skills_dir)
    runner = CliRunner()

    result = _invoke(
        runner,
        skills_dir,
        branches_dir,
        "--owner",
        "agent-a",
        "--action",
        "fork",
        "--skill",
        "sk_ship_order",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["sync_state"] == "synced"
    assert payload["head_version"] == "1.0.0"
    assert payload["snapshot_count"] == 1
    assert (branches_dir / "agent-a" / "sk_ship_order.branch.json").exists()


def test_c6_cli_branch_update_then_promote(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    branches_dir = tmp_path / "branches"
    save_card(_card(), skills_dir)
    updated_file = tmp_path / "updated.json"
    updated_file.write_text(
        json.dumps(_card(name="分支改名").model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    runner = CliRunner()
    _invoke(
        runner,
        skills_dir,
        branches_dir,
        "--owner",
        "agent-a",
        "--action",
        "fork",
        "--skill",
        "sk_ship_order",
    )

    updated = _invoke(
        runner,
        skills_dir,
        branches_dir,
        "--owner",
        "agent-a",
        "--action",
        "update",
        "--card-file",
        str(updated_file),
    )
    promoted = _invoke(
        runner,
        skills_dir,
        branches_dir,
        "--owner",
        "agent-a",
        "--action",
        "promote",
        "--skill",
        "sk_ship_order",
    )

    assert updated.exit_code == 0, updated.output
    assert json.loads(updated.output)["head_version"] == "1.1.0"
    assert json.loads(updated.output)["sync_state"] == "diverged"
    assert promoted.exit_code == 0, promoted.output
    assert json.loads(promoted.output)["promoted_version"] == "1.1.0"
    on_disk = load_card(skills_dir / "sk_ship_order.card.json")
    assert on_disk.name == "分支改名"
    assert on_disk.version == "1.1.0"


def test_c7_cli_branch_versions_and_errors(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    branches_dir = tmp_path / "branches"
    save_card(_card(), skills_dir)
    runner = CliRunner()
    _invoke(
        runner,
        skills_dir,
        branches_dir,
        "--owner",
        "agent-a",
        "--action",
        "fork",
        "--skill",
        "sk_ship_order",
    )

    listed = _invoke(
        runner,
        skills_dir,
        branches_dir,
        "--owner",
        "agent-a",
        "--action",
        "versions",
        "--skill",
        "sk_ship_order",
    )
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)["versions"] == ["1.0.0"]

    missing = _invoke(
        runner,
        skills_dir,
        branches_dir,
        "--owner",
        "ghost-owner",
        "--action",
        "versions",
        "--skill",
        "sk_ship_order",
    )
    assert missing.exit_code == 1, missing.output
    assert "error" in json.loads(missing.output)

    bad_action = _invoke(
        runner,
        skills_dir,
        branches_dir,
        "--owner",
        "agent-a",
        "--action",
        "ghost",
        "--skill",
        "sk_ship_order",
    )
    assert bad_action.exit_code != 0
