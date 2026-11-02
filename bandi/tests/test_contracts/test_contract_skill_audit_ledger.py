# 功能：契约——技能审计账本：技能落盘记 before/after sha256；
# 账本读写往返 + editor / employee_evolution 生产写链路必须记账（破坏者：漏记即红）。
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bandi.skills.audit_ledger import SkillAuditLedger, content_sha256
from bandi.skills.card import SkillCard


def _card(skill_id: str = "sk_x", name: str = "测试技能") -> SkillCard:
    return SkillCard(
        **{
            "skill_id": skill_id,
            "name": name,
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


def _entries(home: Path) -> list[dict[str, Any]]:
    path = home / ".bandi" / "skills_audit.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


class TestLedgerRoundtrip:
    def test_record_and_list_roundtrip(self, tmp_path: Path) -> None:
        ledger = SkillAuditLedger(tmp_path / "audit.jsonl")
        entry = ledger.record(action="rewrite", skill_id="sk_x", before="a", after="b", source="t")
        assert entry["changed"] is True
        assert entry["before_sha256"] == content_sha256("a")
        assert entry["after_sha256"] == content_sha256("b")
        rows = ledger.list_entries()
        assert len(rows) == 1
        assert rows[0]["skill_id"] == "sk_x"
        assert rows[0]["source"] == "t"

    def test_same_content_records_changed_false(self, tmp_path: Path) -> None:
        ledger = SkillAuditLedger(tmp_path / "audit.jsonl")
        entry = ledger.record(action="rewrite", skill_id="sk_x", before="same", after="same")
        assert entry["changed"] is False

    def test_multiple_entries_append(self, tmp_path: Path) -> None:
        ledger = SkillAuditLedger(tmp_path / "audit.jsonl")
        ledger.record(action="a", skill_id="s1", before="", after="1")
        ledger.record(action="b", skill_id="s2", before="1", after="2")
        rows = ledger.list_entries()
        assert [r["action"] for r in rows] == ["a", "b"]


class TestEditorRecordsChange:
    @pytest.mark.asyncio
    async def test_rewrite_and_persist_records_ledger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """editor 生产写链路：改写落盘必须记 before/after（漏记即红）。"""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        from bandi.skills.editor import SkillEditor, SkillRewriteResponse
        from bandi.skills.serializer import save_card

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        before_card = _card()
        save_card(before_card, str(skills_dir))

        editor = SkillEditor(provider=object(), skills_dir=str(skills_dir))
        after_card = _card(name="测试技能（改）")

        async def fake_rewrite(
            self: Any, request: Any, model_config: Any = None, *, session_id: str | None = None
        ) -> Any:
            return SkillRewriteResponse(draft_skill=after_card)

        monkeypatch.setattr(SkillEditor, "rewrite", fake_rewrite)
        response = await editor.rewrite_and_persist("sk_x", "改名")

        rows = _entries(tmp_path)
        assert len(rows) == 1, f"改写落盘必须记一条账：{rows}"
        assert rows[0]["action"] == "rewrite"
        assert rows[0]["source"] == "editor"
        assert rows[0]["changed"] is True
        assert rows[0]["before_sha256"] == content_sha256(before_card.model_dump_json())
        assert rows[0]["after_sha256"] == content_sha256(response.draft_skill.model_dump_json())


class TestEvolutionRecordsChange:
    @pytest.mark.asyncio
    async def test_reflection_update_records_ledger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """员工反思沉淀生产写链路：技能更新必须记账。"""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        from bandi.company.employee_evolution import EmployeeEvolutionManager

        skills_dir = tmp_path / "skills"
        manager = EmployeeEvolutionManager(home_dir=tmp_path / "evo", skills_dir=str(skills_dir))
        skill_md = skills_dir / "sk_learned" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        existing = "---\nname: sk_learned\nversion: 2\n---\n\n已有正文。\n"
        skill_md.write_text(existing, encoding="utf-8")

        updated = manager._update_skill_md("sk_learned", "新经验：先校验再提交。")
        assert updated is True

        rows = _entries(tmp_path)
        assert len(rows) == 1, f"反思沉淀更新必须记账：{rows}"
        assert rows[0]["action"] == "reflection_update"
        assert rows[0]["source"] == "employee_evolution"
        assert rows[0]["skill_id"] == "sk_learned"
        assert rows[0]["changed"] is True
        assert rows[0]["before_sha256"] == content_sha256(existing)

    @pytest.mark.asyncio
    async def test_reflection_create_records_ledger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """破坏者对照组：技能文件缺失时重建也必须记账（before 为空）。"""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        from bandi.company.employee_evolution import EmployeeEvolutionManager

        skills_dir = tmp_path / "skills"
        manager = EmployeeEvolutionManager(home_dir=tmp_path / "evo", skills_dir=str(skills_dir))

        updated = manager._update_skill_md("sk_new", "全新经验。")
        assert updated is True
        rows = _entries(tmp_path)
        assert len(rows) == 1
        assert rows[0]["action"] == "reflection_create"
        assert rows[0]["before_sha256"] == content_sha256("")
        assert rows[0]["changed"] is True
