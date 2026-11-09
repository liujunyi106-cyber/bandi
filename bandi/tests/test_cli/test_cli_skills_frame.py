# 功能：CLI 生产入口测试——bandi skills frame（建帧/推进闭环/只读查看）。
# 走真实命令链 + 真实帧文件落盘（断言最终产物：帧文件内容与 JSON 输出）
# 覆盖 SkillRuntime 的生产调用路径（DoD-11 接线：CLI → 引擎 → frame_store）。
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from bandi.cli.app import app
from bandi.skills.card import SkillCard
from bandi.skills.frame_store import load_frames
from bandi.skills.serializer import save_card


def _card() -> SkillCard:
    """功能：三节点线性 SOP（collect_order → confirm_shipment → reply_final_result 终端）。"""
    return SkillCard(
        **{
            "skill_id": "sk_ship_order",
            "name": "发货 SOP",
            "version": "1.0.0",
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
                    "node_id": "confirm_shipment",
                    "name": "确认发货",
                    "type": "notify",
                    "instruction": "确认发货信息。",
                    "expected_user_info": ["收货地址"],
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
                    "next_node_id": "confirm_shipment",
                    "priority": 0,
                },
                {
                    "source_node_id": "confirm_shipment",
                    "next_node_id": "reply_final_result",
                    "priority": 0,
                },
            ],
            "start_node_id": "collect_order",
            "terminal_node_ids": ["reply_final_result"],
        }
    )


def _invoke(
    runner: CliRunner,
    skills_dir: Path,
    frames_dir: Path,
    *args: str,
):
    return runner.invoke(
        app,
        [
            "skills",
            "frame",
            "--skills-dir",
            str(skills_dir),
            "--frames-dir",
            str(frames_dir),
            *args,
        ],
    )


def test_c1_cli_frame_start_persists_active_frame(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    frames_dir = tmp_path / "frames"
    save_card(_card(), skills_dir)
    runner = CliRunner()

    result = _invoke(
        runner,
        skills_dir,
        frames_dir,
        "--scope",
        "s1",
        "--action",
        "start",
        "--skill",
        "sk_ship_order",
        "--slot",
        "订单号=A100",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["active"]["node_id"] == "collect_order"
    assert payload["active"]["status"] == "active"
    assert payload["active"]["awaiting_fields"] == []
    saved = load_frames("s1", str(frames_dir))
    assert saved is not None
    assert saved.active is not None
    assert saved.active.slots == {"订单号": "A100"}


def test_c2_cli_frame_advance_closes_loop_at_terminal(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    frames_dir = tmp_path / "frames"
    save_card(_card(), skills_dir)
    runner = CliRunner()
    _invoke(
        runner,
        skills_dir,
        frames_dir,
        "--scope",
        "s1",
        "--action",
        "start",
        "--skill",
        "sk_ship_order",
        "--slot",
        "订单号=A100",
    )

    mid = _invoke(runner, skills_dir, frames_dir, "--scope", "s1", "--action", "advance")
    final = _invoke(runner, skills_dir, frames_dir, "--scope", "s1", "--action", "advance")

    assert mid.exit_code == 0, mid.output
    assert json.loads(mid.output)["active"]["node_id"] == "confirm_shipment"
    assert json.loads(mid.output)["completed_count"] == 0
    assert final.exit_code == 0, final.output
    assert json.loads(final.output)["active"] is None
    assert json.loads(final.output)["completed_count"] == 1
    saved = load_frames("s1", str(frames_dir))
    assert saved is not None
    assert saved.active is None
    assert saved.completed[0].status == "completed"


def test_c3_cli_frame_status_read_only_and_bad_action_fails(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    frames_dir = tmp_path / "frames"
    runner = CliRunner()

    status = _invoke(runner, skills_dir, frames_dir, "--scope", "s1", "--action", "status")
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["active"] is None
    assert not frames_dir.exists()

    bad = _invoke(runner, skills_dir, frames_dir, "--scope", "s1", "--action", "ghost")
    assert bad.exit_code != 0


def test_c4_cli_reports_frame_store_errors_friendly(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    (frames_dir / "bad.frames.json").write_text("{ not json", encoding="utf-8")
    runner = CliRunner()

    broken = _invoke(runner, skills_dir, frames_dir, "--scope", "bad", "--action", "status")
    assert broken.exit_code == 1, broken.output
    assert "error" in json.loads(broken.output)

    traversal = _invoke(runner, skills_dir, frames_dir, "--scope", "../evil", "--action", "start")
    assert traversal.exit_code == 1, traversal.output
    assert "error" in json.loads(traversal.output)
