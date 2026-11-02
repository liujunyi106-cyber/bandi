# 功能：技能晋升审批契约——后台晋升只进暂存区 / 用户批准后落盘 / 拒绝零写入
# 契约原则：钉最终产物（pending 文件 / 技能目录 SKILL.md 全文 / 审批状态码），不钉实现。
from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bandi.memory.staging import (
    ORIGIN_BACKGROUND,
    MemoryPendingStore,
    MemoryWriteRecord,
    get_skill_pending_store,
    replay_skill_record,
    reset_skill_pending_store,
    stage_skill_write,
)

_SKILL_MD = "---\nname: cache-playbook\nversion: 1\n---\n\n# 缓存优化 playbook\n\n- 先测量再优化\n"


@pytest.fixture()
def skill_store(tmp_path: Path) -> Iterator[MemoryPendingStore]:
    store = MemoryPendingStore(str(tmp_path / "root"), subsystem="skills")
    reset_skill_pending_store(store)
    yield store
    reset_skill_pending_store()


def _stage_skill(
    name: str = "cache-playbook",
    *,
    summary: str = "技能待批准",
) -> MemoryWriteRecord | None:
    return stage_skill_write(
        skill_name=name,
        content=_SKILL_MD,
        origin=ORIGIN_BACKGROUND,
        summary=summary,
        reason="reflection threshold reached",
    )


def test_promotion_does_not_write_skills_dir_before_approval(skill_store, tmp_path) -> None:
    """契约：暂存技能写入落在 pending/skills，技能库零直写。"""
    record = _stage_skill()
    assert record is not None and record.persisted is True
    assert record.subsystem == "skills"
    assert record.origin == "background"
    assert record.action == "stage_skill"
    assert record.payload == {
        "skill_name": "cache-playbook",
        "content": _SKILL_MD,
        "reason": "reflection threshold reached",
    }
    assert (tmp_path / "root" / "pending" / "skills" / f"{record.id8}.json").exists()
    assert not (tmp_path / "root" / "pending" / "memory").exists()
    assert not (tmp_path / "skills").exists()


def test_same_skill_name_supersedes_previous_pending(skill_store) -> None:
    """契约：同 skill_name 重复晋升只保留最新一条（去重语义）。"""
    first = _stage_skill()
    second = _stage_skill(summary="第二版教训")
    assert first is not None and second is not None
    records = skill_store.list_pending()
    assert [record.id8 for record in records] == [second.id8]
    assert skill_store.get_pending(first.id8) is None
    assert skill_store.pending_count() == 1


def test_same_skill_name_tie_created_at_keeps_later_write(skill_store, monkeypatch) -> None:
    """破坏者（同刻 tie）：created_at 相同（Windows time.time() 粒度 ~15.6ms）时
    胜负不得由随机 id8 决定——后写入者必须存活（原实现 ~50% 概率旧记录留存）。"""
    import bandi.memory.staging as staging_mod

    class _ZeroUuid:
        hex = "00000000" + "0" * 24

    fields = {
        "action": "stage_skill",
        "origin": "background",
        "summary": "第一版",
        "created_at": 1000.0,
        "payload": {"skill_name": "cache-playbook", "content": "# v1\n"},
    }
    first = skill_store.stage(dict(fields))
    monkeypatch.setattr(staging_mod, "uuid4", lambda: _ZeroUuid())
    second = skill_store.stage({**fields, "summary": "第二版"})
    assert second.id8.endswith("00000000"), (
        f"uuid 注入后缀（HB-P1 M6 后 id=时间前缀+uuid8），实际 {second.id8}"
    )
    records = skill_store.list_pending()
    assert [record.id8 for record in records] == [second.id8], (
        "created_at 同刻时必须由写序（seq）决定胜负，后写入者存活"
    )
    assert skill_store.get_pending(first.id8) is None
    assert skill_store.pending_count() == 1


def test_distinct_skill_names_coexist(skill_store) -> None:
    _stage_skill("alpha-playbook")
    _stage_skill("beta-playbook")
    assert skill_store.pending_count() == 2


def test_unsafe_skill_names_rejected_without_pending(skill_store) -> None:
    """破坏者：路径穿越/盘符/保留名/斜杠/超长技能名一律拒收，不得留下待批准记录。"""
    for bad in ("../evil", "..\\evil", "C:evil", "CON", "", "a/b", ".", "..", "a" * 65):
        assert _stage_skill(bad) is None, f"非法技能名必须拒收: {bad!r}"
    assert skill_store.pending_count() == 0


def test_staging_failure_is_safe_failure(tmp_path) -> None:
    """破坏者：暂存写盘失败不得回落直写技能目录，也不得假装已暂存。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    store = MemoryPendingStore(str(blocked), subsystem="skills")
    reset_skill_pending_store(store)
    try:
        record = _stage_skill()
        assert record is not None and record.persisted is False
        assert store.pending_count() == 0
        assert not (tmp_path / "skills").exists()
    finally:
        reset_skill_pending_store()


class TestSkillReplay:
    def test_replay_writes_utf8_skill_md(self, skill_store, tmp_path) -> None:
        record = _stage_skill(name="缓存优化")
        assert record is not None
        skills_dir = tmp_path / "skills"
        assert replay_skill_record(record, skills_dir=skills_dir) is True
        target = skills_dir / "缓存优化" / "SKILL.md"
        assert target.read_text(encoding="utf-8") == _SKILL_MD
        assert list(skills_dir.rglob("*.tmp")) == [], "原子写不得遗留临时文件"

    def test_replay_rejects_traversal_payload(self, tmp_path) -> None:
        """破坏者：绕过 stage 直接塞坏 payload，重放也必须拒写目录外。"""
        record = MemoryWriteRecord(
            id8="deadbeef",
            subsystem="skills",
            action="stage_skill",
            payload={"skill_name": "../../evil", "content": "# x\n"},
        )
        skills_dir = tmp_path / "skills"
        assert replay_skill_record(record, skills_dir=skills_dir) is False
        assert not (tmp_path / "evil").exists()

    def test_replay_empty_content_fails(self, tmp_path) -> None:
        record = MemoryWriteRecord(
            id8="deadbeef",
            subsystem="skills",
            action="stage_skill",
            payload={"skill_name": "empty-playbook", "content": "   "},
        )
        assert replay_skill_record(record, skills_dir=tmp_path / "skills") is False

    def test_replay_lone_surrogate_fails_without_tmp_leftover(self, tmp_path) -> None:
        """破坏者：内容含 lone surrogate（不可 UTF-8 编码）→ 失败返回 False，不得裸抛。"""
        record = MemoryWriteRecord(
            id8="deadbeef",
            subsystem="skills",
            action="stage_skill",
            payload={"skill_name": "surrogate-playbook", "content": "前置\ud800后置\n"},
        )
        skills_dir = tmp_path / "skills"

        assert replay_skill_record(record, skills_dir=skills_dir) is False
        assert list(skills_dir.rglob("*.tmp")) == [], "失败路径不得残留临时文件"


class TestSkillPendingApi:
    def test_list_fields_and_masked_summary(self, skill_store) -> None:
        app_mod = importlib.import_module("bandi.api.app")
        secret = "sk-live-abc123def456"
        _stage_skill(summary=f"技能密钥 {secret} 更新")
        rows = asyncio.run(app_mod.list_skills_pending())["pending"]
        assert len(rows) == 1
        assert set(rows[0]) == {
            "id",
            "skill_name",
            "action",
            "summary",
            "origin",
            "created_at",
        }
        assert rows[0]["skill_name"] == "cache-playbook"
        assert rows[0]["origin"] == "background"
        assert secret not in rows[0]["summary"], "列表摘要不得泄漏密钥原文"

    def test_approve_writes_skill_md_and_discards(self, skill_store, tmp_path, monkeypatch) -> None:
        app_mod = importlib.import_module("bandi.api.app")
        record = _stage_skill()
        assert record is not None
        monkeypatch.setattr(app_mod, "_skill_write_overrides", {"skills_dir": tmp_path / "skills"})
        result = asyncio.run(app_mod.approve_skill_pending(record.id8))
        assert result["approved"] is True and result["skill_name"] == "cache-playbook"
        target = tmp_path / "skills" / "cache-playbook" / "SKILL.md"
        assert target.read_text(encoding="utf-8") == _SKILL_MD
        assert skill_store.pending_count() == 0

    def test_approve_idempotent_after_discard(self, skill_store, tmp_path, monkeypatch) -> None:
        """契约：批准出队后重复批准 404；同记录重复重放内容不变不堆积。"""
        app_mod = importlib.import_module("bandi.api.app")
        record = _stage_skill()
        assert record is not None
        monkeypatch.setattr(app_mod, "_skill_write_overrides", {"skills_dir": tmp_path / "skills"})
        asyncio.run(app_mod.approve_skill_pending(record.id8))
        with pytest.raises(HTTPException) as missing:
            asyncio.run(app_mod.approve_skill_pending(record.id8))
        assert missing.value.status_code == 404
        assert replay_skill_record(record, skills_dir=tmp_path / "skills") is True
        dirs = [path for path in (tmp_path / "skills").iterdir() if path.is_dir()]
        assert [path.name for path in dirs] == ["cache-playbook"]
        assert (dirs[0] / "SKILL.md").read_text(encoding="utf-8") == _SKILL_MD

    def test_reject_discards_without_write(self, skill_store, tmp_path, monkeypatch) -> None:
        app_mod = importlib.import_module("bandi.api.app")
        record = _stage_skill()
        assert record is not None
        monkeypatch.setattr(app_mod, "_skill_write_overrides", {"skills_dir": tmp_path / "skills"})
        result = asyncio.run(app_mod.reject_skill_pending(record.id8))
        assert result["rejected"] is True
        assert not (tmp_path / "skills").exists(), "拒绝必须零落盘"
        assert skill_store.pending_count() == 0

    def test_failed_replay_keeps_pending_and_returns_500(
        self, skill_store, tmp_path, monkeypatch
    ) -> None:
        """破坏者：坏 payload 重放失败必须 500 且保留待批准记录（不得静默丢弃）。"""
        app_mod = importlib.import_module("bandi.api.app")
        broken = skill_store.stage(
            {
                "action": "stage_skill",
                "origin": "background",
                "summary": "坏技能",
                "payload": {"skill_name": "../../evil", "content": "# x\n"},
            }
        )
        monkeypatch.setattr(app_mod, "_skill_write_overrides", {"skills_dir": tmp_path / "skills"})
        with pytest.raises(HTTPException) as failed:
            asyncio.run(app_mod.approve_skill_pending(broken.id8))
        assert failed.value.status_code == 500
        assert skill_store.get_pending(broken.id8) is not None

    def test_invalid_id_400_and_missing_404(self, skill_store) -> None:
        app_mod = importlib.import_module("bandi.api.app")
        for handler in (app_mod.approve_skill_pending, app_mod.reject_skill_pending):
            with pytest.raises(HTTPException) as invalid:
                asyncio.run(handler("not-a-hex"))
            assert invalid.value.status_code == 400, "非法 id 格式必须 400"
            with pytest.raises(HTTPException) as missing:
                asyncio.run(handler("deadbeef"))
            assert missing.value.status_code == 404, "合法格式但不存在必须 404"


class TestPromotionGoesThroughStaging:
    def _reflection_round(self, manager, index: int) -> list[dict]:
        delivery = SimpleNamespace(
            id=f"del-{index}", project_id="p1", phase="done", status="done", metadata={}
        )
        task = SimpleNamespace(
            id=f"wi-{index}",
            title="task",
            phase="done",
            status="done",
            assignee_role="engineer",
            project_id="p1",
            tags=[],
            metadata={
                "employee_assignment": {
                    "employee_id": "e1",
                    "role_id": "engineer",
                    "name": "Alice",
                    "domains": [],
                },
                "work_item_summary_for_downstream": f"implemented feature {index}",
            },
            result=None,
            assigned_external_agent="",
        )
        return manager.record_project_reflections(delivery, [task])

    def test_promotion_stages_then_approve_lands_skill(self, tmp_path, monkeypatch) -> None:
        """端到端：反思达阈值 → pending/skills（零直写）→ 审批 → SKILL.md 落盘可读回。"""
        from bandi.company.employee_evolution import EmployeeEvolutionManager

        app_mod = importlib.import_module("bandi.api.app")
        manager = EmployeeEvolutionManager(home_dir=tmp_path, skills_dir=str(tmp_path / "skills"))
        results: list[dict] = []
        for index in range(2):
            results = self._reflection_round(manager, index)

        assert not (tmp_path / "skills").exists(), "审批前技能库必须零写入"
        records = get_skill_pending_store().list_pending()
        assert len(records) == 1
        record = records[0]
        assert record.subsystem == "skills"
        assert record.origin == "background"
        assert record.id8 in results[0]["learned_skill_ref"]
        assert results[0]["learned_skill_ref"] == f"pending:{record.id8}"
        skill_name = str(record.payload["skill_name"])
        assert skill_name in record.summary, "摘要必须含技能名"

        profile = manager.load_evolution_profile()
        employee = profile["employees"]["e1"]
        assert not employee.get("learned_skill_refs"), "审批前档案不得假装已学技能"
        assert not employee["patterns"]["engineer|general"].get("learned_skill_ref")

        monkeypatch.setattr(app_mod, "_skill_write_overrides", {"skills_dir": tmp_path / "skills"})
        asyncio.run(app_mod.approve_skill_pending(record.id8))
        skill_md = tmp_path / "skills" / skill_name / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8")
        assert f"name: {skill_name}" in text
        assert "Break work into explicit deliverables" in text, "技能正文必须含反思教训"
        assert get_skill_pending_store().pending_count() == 0

    def test_repeated_reflection_supersedes_single_pending(self, tmp_path) -> None:
        """契约：连续反思重复命中阈值——同技能 pending 保持一条（覆盖最新）。"""
        from bandi.company.employee_evolution import EmployeeEvolutionManager

        manager = EmployeeEvolutionManager(home_dir=tmp_path, skills_dir=str(tmp_path / "skills"))
        for index in range(3):
            self._reflection_round(manager, index)
        records = get_skill_pending_store().list_pending()
        assert len(records) == 1
        assert not (tmp_path / "skills").exists()

    def test_approved_skill_written_back_then_merge_updates(self, tmp_path, monkeypatch) -> None:
        """端到端：批准回写档案真实技能名——再反思不重复入队
        同名技能走合并更新（version 递增 + history），且可回滚到 v1。"""
        from bandi.company.employee_evolution import EmployeeEvolutionManager

        app_mod = importlib.import_module("bandi.api.app")
        manager = EmployeeEvolutionManager(home_dir=tmp_path, skills_dir=str(tmp_path / "skills"))
        for index in range(2):
            self._reflection_round(manager, index)

        record = get_skill_pending_store().list_pending()[0]
        skill_name = str(record.payload["skill_name"])
        monkeypatch.setattr(app_mod, "_skill_write_overrides", {"skills_dir": tmp_path / "skills"})
        asyncio.run(app_mod.approve_skill_pending(record.id8))

        employee = manager.load_evolution_profile()["employees"]["e1"]
        assert skill_name in employee["learned_skill_refs"], "批准后档案必须记录真实技能名"
        assert employee["patterns"]["engineer|general"]["learned_skill_ref"] == skill_name
        assert get_skill_pending_store().pending_count() == 0

        self._reflection_round(manager, 2)
        assert get_skill_pending_store().pending_count() == 0, "已批准技能不得重新入队"
        updated = (tmp_path / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")
        assert "version: 2" in updated, "再反思必须进入合并更新分支（version 递增）"
        history = tmp_path / "skill_history" / skill_name / "SKILL.v1.md"
        assert history.is_file(), "旧版本必须备份到 history（回滚面）"
        assert manager.rollback_skill(skill_name, 1) is True


class TestApprovedSkillWriteBackConcurrency:
    def test_concurrent_attach_approved_skill_keeps_both_refs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """契约（-β，破坏者）：双线程并发批准两个技能 → 档案含两个技能引用——
        读-改-写临界区必须串行化，不得丢更新（原红：后写覆盖先写只剩一个）。"""
        from bandi.company.employee_evolution import EmployeeEvolutionManager

        profile_path = tmp_path / "company" / "evolution" / "employees.json"
        profile_path.parent.mkdir(parents=True)
        base_profile = {
            "employees": {
                "e1": {
                    "role_id": "engineer",
                    "patterns": {"p1": {}, "p2": {}},
                    "learned_skill_refs": [],
                }
            }
        }
        profile_path.write_text(
            json.dumps(base_profile, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        original_load = EmployeeEvolutionManager._load_json
        first_load_done = threading.Event()

        def slow_first_load(self: object, path: Path) -> dict:
            data = original_load(self, path)
            if not first_load_done.is_set():
                first_load_done.set()
                time.sleep(0.3)
            return data

        monkeypatch.setattr(EmployeeEvolutionManager, "_load_json", slow_first_load)
        results: list[bool] = []

        def worker(pattern_key: str, skill_name: str) -> None:
            results.append(
                EmployeeEvolutionManager.attach_approved_skill(
                    profile_path,
                    employee_id="e1",
                    pattern_key=pattern_key,
                    skill_name=skill_name,
                )
            )

        first = threading.Thread(target=worker, args=("p1", "alpha-playbook"))
        second = threading.Thread(target=worker, args=("p2", "beta-playbook"))
        first.start()
        second.start()
        first.join()
        second.join()
        assert results == [True, True]
        saved = json.loads(profile_path.read_text(encoding="utf-8"))
        refs = saved["employees"]["e1"]["learned_skill_refs"]
        assert set(refs) == {"alpha-playbook", "beta-playbook"}
