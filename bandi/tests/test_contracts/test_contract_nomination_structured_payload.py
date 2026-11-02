# 功能：契约——提名卡结构化（2026-10-17 真机反馈：提名消息须字段化并说清
# 「涉及文件/目标员工/建议技能」）。钉行为：_touched_files 从工具参数提路径
# （去重/上限 5/无参空）；propose extra 透传 payload 且不可覆盖核心键；
# rework 提名端到端 payload 含结构化字段。
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bandi.agent.states import _nominate_team_lesson, _touched_files


@pytest.fixture
def store_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from bandi.memory import staging as staging_mod

    monkeypatch.setattr(staging_mod, "default_memory_store_dir", lambda: tmp_path / "pending")
    return tmp_path / "pending"


def _ctx(**overrides: Any) -> SimpleNamespace:
    base = {
        "meta": {
            "team_id": "team-a",
            "project_id": "p1",
            "employee_id": "e1",
            "session_id": "s1",
        },
        "messages": [],
        "tool_results": [],
        "turn_index": 3,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _verdict(
    action: str, reason: str = "缺索引", analysis: str = "先看执行计划"
) -> SimpleNamespace:
    return SimpleNamespace(action=action, reason=reason, analysis=analysis)


def _call(**arguments: Any) -> SimpleNamespace:
    return SimpleNamespace(name="edit", arguments=arguments)


def _msg(calls: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(role="assistant", tool_calls=calls)


class TestTouchedFiles:
    def test_extracts_dedup_caps_five(self) -> None:
        ctx = _ctx(
            messages=[
                _msg(
                    [
                        _call(file_path="a.py"),
                        _call(file_path="a.py"),
                        _call(path="b.md"),
                        _call(file="c.txt"),
                        _call(target="d.bin"),
                        _call(output_path="e.log"),
                        _call(file_path="f.py"),
                    ]
                )
            ]
        )
        assert _touched_files(ctx) == ["a.py", "b.md", "c.txt", "d.bin", "e.log"]

    def test_empty_without_calls(self) -> None:
        assert _touched_files(_ctx()) == []


class TestStructuredPayload:
    def test_rework_payload_has_fields(self, store_dir: Path) -> None:
        from bandi.memory.team_staging import TeamMemoryStaging

        ctx = _ctx(messages=[_msg([_call(file_path="sql/idx.sql")])])
        _nominate_team_lesson(ctx, "优化慢查询", _verdict("rework"), output="已加索引")
        pending = TeamMemoryStaging(store_dir).list_pending()
        assert len(pending) == 1
        payload = pending[0].payload
        assert payload["verdict_action"] == "rework"
        assert payload["files"] == ["sql/idx.sql"]
        assert payload["suggested_employee"] == "e1"
        assert payload["suggested_skill_slug"]
        assert str(payload["skill_target"]).endswith("/SKILL.md")

    def test_extra_cannot_override_core(self, store_dir: Path) -> None:
        from bandi.memory.team_staging import TeamMemoryStaging

        TeamMemoryStaging(store_dir).propose(
            summary="任务：x",
            project_id="p1",
            team_id="team-a",
            employee_id="e1",
            session_id="s1",
            turn_index=1,
            extra={"team_id": "hijack", "files": ["a.py"]},
        )
        payload = TeamMemoryStaging(store_dir).list_pending()[0].payload
        assert payload["team_id"] == "team-a", "extra 不得覆盖核心键"
        assert payload["files"] == ["a.py"]
