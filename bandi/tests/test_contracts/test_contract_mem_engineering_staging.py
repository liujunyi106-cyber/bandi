# 功能：契约——写入暂存区 / origin 门 / 冲突标记 / 时间衰减 / 审批端点
# 契约原则：钉最终产物（落盘记录/审批重放结果/排序结果），不钉实现。
from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.staging import (
    MemoryPendingStore,
    MemoryWriteGate,
    get_write_origin,
    replay_record,
    reset_pending_store,
    reset_write_origin,
    set_write_origin,
    stage_long_term_write,
    stage_write,
)


@pytest.fixture()
def pending_store(tmp_path):
    store = MemoryPendingStore(str(tmp_path / "memory"))
    reset_pending_store(store)
    yield store
    reset_pending_store()


def _stage_fields(action: str = "store_long_term", summary: str = "示例") -> dict:
    return {
        "action": action,
        "summary": summary,
        "origin": "background",
        "payload": {"target": "long_term", "item": {}, "conflict": False},
    }


class TestPendingStore:
    def test_stage_persists_atomic_record_and_lists_ascending(self, tmp_path) -> None:
        store = MemoryPendingStore(str(tmp_path / "memory"))
        first = store.stage({**_stage_fields(), "created_at": 200.0})
        second = store.stage({**_stage_fields(), "created_at": 100.0})
        # 唯一+可排序不变式见 test_contract_hb_p1_memory.py::test_staging_new_id_has_time_prefix）
        assert len(first.id8) == 24
        assert store.pending_count() == 2
        assert [r.id8 for r in store.list_pending()] == [second.id8, first.id8]
        assert not list((tmp_path / "memory" / "pending" / "memory").glob("*.tmp"))

    def test_get_and_discard(self, tmp_path) -> None:
        store = MemoryPendingStore(str(tmp_path / "memory"))
        record = store.stage(_stage_fields(summary="待批准"))
        loaded = store.get_pending(record.id8)
        assert loaded is not None and loaded.summary == "待批准"
        assert store.discard_pending(record.id8) is True
        assert store.get_pending(record.id8) is None
        assert store.pending_count() == 0
        assert store.discard_pending(record.id8) is False

    def test_stage_write_failure_still_returns_record(self, tmp_path) -> None:
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        store = MemoryPendingStore(str(blocked))
        record = store.stage(_stage_fields())
        assert record.id8, "写盘失败仍必须返回记录（不得静默丢意图）"
        assert store.list_pending() == [], "写盘失败不得假装已暂存"

    def test_path_traversal_id_is_rejected(self, tmp_path) -> None:
        store = MemoryPendingStore(str(tmp_path / "memory"))
        assert store.get_pending("../../secret") is None
        assert store.discard_pending("..\\..\\secret") is False


class TestWriteGateAndOrigin:
    def test_gate_matrix(self) -> None:
        assert MemoryWriteGate.evaluate("foreground") == "allow"
        assert MemoryWriteGate.evaluate("background") == "stage"
        assert MemoryWriteGate.evaluate("scheduler") == "stage"
        assert MemoryWriteGate.evaluate("") == "stage"

    def test_origin_contextvar_default_and_restore(self) -> None:
        assert get_write_origin() == "foreground"
        token = set_write_origin("background")
        try:
            assert get_write_origin() == "background"
        finally:
            reset_write_origin(token)
        assert get_write_origin() == "foreground"


class TestConflictMarking:
    def test_conflict_only_when_existing_content_differs(self, tmp_path) -> None:
        mem = LongTermMemory(str(tmp_path / "lt.db"))
        mem.store(MemoryItem(item_id="turn:s:1", content="旧结论"))
        assert mem.check_conflict("turn:s:1", "新结论") is True
        assert mem.check_conflict("turn:s:1", "旧结论") is False
        assert mem.check_conflict("turn:s:missing", "任意") is False
        mem.close()


class TestDecayRanking:
    def test_decay_off_keeps_importance_order(self, tmp_path) -> None:
        mem = LongTermMemory(str(tmp_path / "lt.db"))
        now = time.time()
        mem.store(
            MemoryItem(item_id="old", content="衰减测试", importance=1.0, timestamp=now - 864000)
        )
        mem.store(MemoryItem(item_id="new", content="衰减测试", importance=0.6, timestamp=now))
        assert [h.id for h in mem.retrieve("衰减", limit=2)] == ["old", "new"]
        mem.close()

    def test_decay_on_lets_recent_overtake_stale(self, tmp_path) -> None:
        mem = LongTermMemory(str(tmp_path / "lt.db"), decay_half_life_days=1.0, decay_floor=0.05)
        now = time.time()
        mem.store(
            MemoryItem(item_id="old", content="衰减测试", importance=1.0, timestamp=now - 864000)
        )
        mem.store(MemoryItem(item_id="new", content="衰减测试", importance=0.6, timestamp=now))
        assert [h.id for h in mem.retrieve("衰减", limit=2)] == ["new", "old"]
        ranked = mem.search_fts_ranked("衰减", limit=2)
        assert [h.id for h in ranked] == ["new", "old"], "FTS 排序须与 retrieve 同衰减口径"
        mem.close()

    def test_future_timestamp_never_amplifies(self, tmp_path) -> None:
        mem = LongTermMemory(str(tmp_path / "lt.db"), decay_half_life_days=1.0, decay_floor=0.05)
        now = time.time()
        future_score = mem._rank_score(0.9, now + 86400)
        current_score = mem._rank_score(0.9, now)
        assert future_score == pytest.approx(0.9), (
            f"未来时间戳不得放大衰减因子（实际 {future_score}；放大实现会给 1.8）"
        )
        assert future_score == pytest.approx(current_score), "未来与当下必须同分（不放大不惩罚）"

        mem.store(
            MemoryItem(item_id="future", content="衰减测试", importance=0.9, timestamp=now + 86400)
        )
        mem.store(MemoryItem(item_id="current", content="衰减测试", importance=0.9, timestamp=now))
        assert [h.id for h in mem.retrieve("衰减", limit=2)] == ["future", "current"]
        mem.close()


class TestPendingApi:
    def _stage_sample(self, store: MemoryPendingStore) -> MemoryItem:
        item = MemoryItem(
            item_id="turn:s:9",
            content="结论：审批重放保留该记录。",
            metadata={"memory_type": "process_conclusion"},
        )
        assert stage_long_term_write(
            item,
            project_id=None,
            employee_id=None,
            action="store_long_term",
            summary="process_conclusion/background: 结论",
            origin="background",
        )
        return item

    def test_list_endpoint_desensitizes_payload(self, pending_store, tmp_path) -> None:
        app_mod = importlib.import_module("bandi.api.app")

        self._stage_sample(pending_store)
        payload = asyncio.run(app_mod.list_memory_pending())
        rows = payload["pending"]
        assert len(rows) == 1
        required = {
            "id",
            "subsystem",
            "action",
            "summary",
            "origin",
            "origin_source",
            "created_at",
            "conflict",
            "draft",
        }
        # 新增字段只能是显式白名单里的（content 脱敏后、structured 派生字段、冲突旧值），
        # 不允许把整条 payload / item 原样透给前端。
        allowed = required | {"content", "structured", "old_value"}
        assert required <= set(rows[0]), f"缺必回字段：{required - set(rows[0])}"
        assert set(rows[0]) <= allowed, f"出现未登记字段（可能泄漏原文）：{set(rows[0]) - allowed}"
        assert "item" not in rows[0] and "payload" not in rows[0], "不得回传原始记录"
        assert rows[0]["origin"] == "background"
        assert rows[0]["origin_source"] == ""
        assert rows[0]["conflict"] is False
        # 草稿元数据随来源门暂存自动携带（消息中心多标签数据源）
        assert rows[0]["draft"] == {"kind": "后台直写暂存", "target": "长期记忆"}

    def test_approve_replays_and_discards(self, pending_store, tmp_path, monkeypatch) -> None:
        app_mod = importlib.import_module("bandi.api.app")

        item = self._stage_sample(pending_store)
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        monkeypatch.setattr(
            app_mod,
            "_memory_replay_overrides",
            {"long_term": long_term, "episodic": None, "markdown": None},
        )
        record = pending_store.list_pending()[0]
        result = asyncio.run(app_mod.approve_memory_pending(record.id8))
        assert result["approved"] is True
        assert long_term.get(item.id) is not None, "批准必须复用写入 API 真正落库"
        assert pending_store.pending_count() == 0
        long_term.close()

    def test_reject_discards_without_write(self, pending_store, tmp_path, monkeypatch) -> None:
        app_mod = importlib.import_module("bandi.api.app")

        item = self._stage_sample(pending_store)
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        monkeypatch.setattr(
            app_mod,
            "_memory_replay_overrides",
            {"long_term": long_term, "episodic": None, "markdown": None},
        )
        record = pending_store.list_pending()[0]
        result = asyncio.run(app_mod.reject_memory_pending(record.id8))
        assert result["rejected"] is True
        assert long_term.get(item.id) is None, "拒绝不得写库"
        assert pending_store.pending_count() == 0
        long_term.close()

    def test_approve_unknown_and_failed_replay_semantics(self, pending_store, monkeypatch) -> None:
        app_mod = importlib.import_module("bandi.api.app")

        with pytest.raises(HTTPException) as missing:
            asyncio.run(app_mod.approve_memory_pending("deadbeef"))
        assert missing.value.status_code == 404
        broken = pending_store.stage(
            {"action": "store_long_term", "summary": "坏目标", "payload": {"target": "nowhere"}}
        )
        monkeypatch.setattr(
            app_mod,
            "_memory_replay_overrides",
            {"long_term": None, "episodic": None, "markdown": None},
        )
        with pytest.raises(HTTPException) as failed:
            asyncio.run(app_mod.approve_memory_pending(broken.id8))
        assert failed.value.status_code == 500
        assert pending_store.get_pending(broken.id8) is not None, "重放失败不得丢弃待批准记录"


class TestStageWriteReliability:
    def test_stage_write_reports_persist_failure(self, tmp_path) -> None:
        """契约：落盘成功 True；坏目录根 False 且不假暂存（安全失败不回落直写）。"""
        ok_store = MemoryPendingStore(str(tmp_path / "ok"))
        reset_pending_store(ok_store)
        try:
            assert (
                stage_write(
                    action="store_long_term",
                    summary="后台结论",
                    payload={"target": "long_term", "item": {"id": "turn:s:1"}},
                    origin="background",
                )
                is True
            )
            assert ok_store.pending_count() == 1
        finally:
            reset_pending_store()
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        blocked_store = MemoryPendingStore(str(blocked))
        reset_pending_store(blocked_store)
        try:
            assert (
                stage_write(
                    action="store_long_term",
                    summary="后台结论",
                    payload={"target": "long_term", "item": {"id": "turn:s:1"}},
                    origin="background",
                )
                is False
            ), "写盘失败必须返回 False（不得静默假成功）"
            assert blocked_store.pending_count() == 0, "写盘失败不得假装已暂存"
        finally:
            reset_pending_store()


class TestPendingDedup:
    def test_same_item_stage_keeps_latest_only(self, tmp_path) -> None:
        """契约：同 target/item 重复 stage（重跑/重整合）只保留最新一条。"""
        store = MemoryPendingStore(str(tmp_path / "memory"))
        fields = {
            "action": "store_long_term",
            "origin": "background",
            "payload": {
                "target": "long_term",
                "item": {"id": "turn:s:1", "content": "第一版"},
                "conflict": False,
            },
        }
        first = store.stage({**fields, "summary": "第一版"})
        second = store.stage({**fields, "summary": "第二版"})
        records = store.list_pending()
        assert [record.id8 for record in records] == [second.id8]
        assert store.get_pending(first.id8) is None, "旧待批准记录必须被新记录取代"

    def test_dedup_replay_is_idempotent(self, tmp_path) -> None:
        """契约：去重后批准重放仍幂等（REPLACE 覆盖不新增行）。"""
        store = MemoryPendingStore(str(tmp_path / "memory"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        payload = {
            "target": "long_term",
            "item": MemoryItem(item_id="turn:s:9", content="结论V2").to_dict(),
            "project_id": None,
            "employee_id": None,
            "conflict": False,
        }
        store.stage({"action": "store_long_term", "payload": payload, "origin": "background"})
        latest = store.stage(
            {"action": "store_long_term", "payload": payload, "origin": "background"}
        )
        assert len(store.list_pending()) == 1
        assert replay_record(latest, long_term=long_term) is True
        stored = long_term.get("turn:s:9")
        assert stored is not None and stored.content == "结论V2"
        assert replay_record(latest, long_term=long_term) is True
        assert long_term.count() == 1, "同一 target/item 重放必须幂等"
        long_term.close()


def _concurrent_stage(
    store: MemoryPendingStore, payload: dict, *, threads: int = 8
) -> list[BaseException]:
    """功能：8 线程同刻并发 stage 同一 payload，返回线程内异常列表。"""
    barrier = threading.Barrier(threads)
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            barrier.wait(timeout=10)
            store.stage(dict(payload))
        except BaseException as exc:  # noqa: BLE001 — 汇总返回供主线程断言
            errors.append(exc)

    workers = [threading.Thread(target=_worker) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    return errors


@pytest.mark.timing
class TestConcurrentStaging:
    """复核：stage+supersede 并发临界区——同名并发不得互删（A/B 双删清空队列）。

    `timing`：真实并发临界区 + 墙钟预算，隔离单跑 ~；与全量 `-n 4` 争 CPU 时
    预算会被压爆（负载敏感，非缺陷）——由 verify.ps1 独立低并发分组承载。
    """

    _FIELDS = {
        "action": "stage_skill",
        "origin": "background",
        "summary": "并发暂存",
        "payload": {"skill_name": "cache-playbook", "content": "# x\n"},
    }

    def test_concurrent_same_name_stage_keeps_exactly_one_per_round(self, tmp_path) -> None:
        """契约：8 线程同名 stage × 20 轮 → 每轮恰好 1 条且可读（非空）。"""
        store = MemoryPendingStore(str(tmp_path / "root"), subsystem="skills")
        for round_index in range(20):
            errors = _concurrent_stage(store, self._FIELDS)
            assert errors == [], f"第 {round_index} 轮并发 stage 抛错：{errors}"
            records = store.list_pending()
            assert len(records) == 1, (
                f"第 {round_index} 轮并发同名 stage 后应恰好 1 条，实际 {len(records)}"
            )
            assert records[0].id8 and records[0].payload.get("skill_name") == "cache-playbook"

    def test_concurrent_distinct_names_do_not_supersede_each_other(self, tmp_path) -> None:
        """契约：并发不同 skill_name 各自保留，互不影响。"""
        store = MemoryPendingStore(str(tmp_path / "root"), subsystem="skills")
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def _stage_named(name: str) -> None:
            try:
                barrier.wait(timeout=10)
                store.stage(
                    {
                        "action": "stage_skill",
                        "origin": "background",
                        "summary": name,
                        "payload": {"skill_name": name, "content": "# x\n"},
                    }
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [
            threading.Thread(target=_stage_named, args=(f"skill-{index}",)) for index in range(8)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        assert errors == []
        assert store.pending_count() == 8, "不同 skill_name 并发暂存不得互删"

    def test_failed_persist_does_not_supersede_existing(self, tmp_path, monkeypatch) -> None:
        """契约：写盘失败不得触发 supersede——已暂存记录必须保活（persisted 语义）。"""
        store = MemoryPendingStore(str(tmp_path / "root"), subsystem="skills")
        first = store.stage({**self._FIELDS, "summary": "第一版"})
        assert first.persisted is True

        original_write_text = Path.write_text

        def _failing_write(self, *args, **kwargs):
            if self.suffix == ".tmp":
                raise OSError("disk full")
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", _failing_write)
        second = store.stage({**self._FIELDS, "summary": "第二版"})

        assert second.persisted is False
        assert store.get_pending(first.id8) is not None, "写盘失败不得误删已有记录"


class TestReplayScopeMissIsolation:
    def test_replay_does_not_inflate_scope_miss_counts(self, tmp_path) -> None:
        """契约：批准重放是镜像暂存作用域的合法写，不得计为漏传维度。"""
        from bandi.memory.long_term import reset_scope_strict_misses, scope_strict_misses

        store = MemoryPendingStore(str(tmp_path / "memory"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        item = MemoryItem(item_id="turn:s-miss:1", content="结论：无维度重放")
        record = store.stage(
            {
                "action": "store_long_term",
                "payload": {
                    "target": "long_term",
                    "item": item.to_dict(),
                    "project_id": None,
                    "employee_id": None,
                    "conflict": False,
                },
            }
        )
        reset_scope_strict_misses()
        assert replay_record(record, long_term=long_term) is True
        assert all(value == 0 for value in scope_strict_misses().values()), (
            f"批准重放不得污染漏传观测：{scope_strict_misses()}"
        )
        long_term.close()


class TestPendingApiHardening:
    def test_invalid_id_format_returns_400(self, pending_store) -> None:
        """契约：非法 id 格式 400；合法但不存在的 8 位 hex 仍 404。"""
        app_mod = importlib.import_module("bandi.api.app")

        for handler in (app_mod.approve_memory_pending, app_mod.reject_memory_pending):
            with pytest.raises(HTTPException) as invalid:
                asyncio.run(handler("not-a-hex"))
            assert invalid.value.status_code == 400, "非法 id 格式必须 400"
            with pytest.raises(HTTPException) as missing:
                asyncio.run(handler("deadbeef"))
            assert missing.value.status_code == 404, "合法格式但不存在必须 404"

    def test_list_masks_secret_in_summary(self, pending_store) -> None:
        """契约：列表摘要经脱敏（防御层）——密钥原文不得出现。"""
        app_mod = importlib.import_module("bandi.api.app")

        secret = "sk-live-abc123def456"
        pending_store.stage(
            {
                "action": "store_long_term",
                "summary": f"配置密钥 {secret} 已更新",
                "origin": "background",
                "payload": {"target": "long_term", "item": {}},
            }
        )
        rows = asyncio.run(app_mod.list_memory_pending())["pending"]
        assert len(rows) == 1
        assert secret not in rows[0]["summary"], "列表摘要不得泄漏密钥原文"

    def test_conflict_row_exposes_masked_old_value(
        self, pending_store, tmp_path, monkeypatch
    ) -> None:
        """契约：仅冲突记录附旧值摘要（脱敏且 ≤60 字）；非冲突行不含该字段。"""
        app_mod = importlib.import_module("bandi.api.app")

        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        old_value = "旧结论：token=sk-ant-secret-9876543210 " + "历史内容" * 30
        long_term.store(MemoryItem(item_id="turn:s-cf:1", content=old_value))
        pending_store.stage(
            {
                "action": "store_long_term",
                "summary": "conflict/background: 新结论",
                "origin": "background",
                "payload": {
                    "target": "long_term",
                    "item": {"id": "turn:s-cf:1", "content": "新结论"},
                    "conflict": True,
                },
            }
        )
        monkeypatch.setattr(
            app_mod,
            "_memory_replay_overrides",
            {"long_term": long_term, "episodic": None, "markdown": None},
        )
        rows = asyncio.run(app_mod.list_memory_pending())["pending"]
        assert len(rows) == 1 and rows[0]["conflict"] is True
        assert "old_value" in rows[0]
        assert "sk-ant-secret-9876543210" not in rows[0]["old_value"]
        assert len(rows[0]["old_value"]) <= 60
        long_term.close()

    def test_non_conflict_row_has_no_old_value(self, pending_store) -> None:
        """契约（破坏者）：非冲突行不得附带旧值摘要字段。"""
        app_mod = importlib.import_module("bandi.api.app")

        pending_store.stage(
            {
                "action": "store_long_term",
                "summary": "无冲突",
                "origin": "background",
                "payload": {"target": "long_term", "item": {"id": "turn:s:2"}},
            }
        )
        rows = asyncio.run(app_mod.list_memory_pending())["pending"]
        assert "old_value" not in rows[0]


class TestSubsystemScoping:
    def test_subsystem_scopes_directory_and_record(self, tmp_path) -> None:
        """契约：subsystem 决定 pending/<subsystem>/ 目录，记录与目录一致且互不可见。"""
        memory_store = MemoryPendingStore(str(tmp_path / "root"))
        skill_store = MemoryPendingStore(str(tmp_path / "root"), subsystem="skills")
        memory_record = memory_store.stage(_stage_fields())
        skill_record = skill_store.stage(
            {
                "action": "stage_skill",
                "origin": "background",
                "payload": {"skill_name": "cache-playbook", "content": "# 缓存优化"},
            }
        )
        assert memory_record.subsystem == "memory"
        assert skill_record.subsystem == "skills"
        assert (tmp_path / "root" / "pending" / "memory" / f"{memory_record.id8}.json").exists()
        assert (tmp_path / "root" / "pending" / "skills" / f"{skill_record.id8}.json").exists()
        assert [record.id8 for record in memory_store.list_pending()] == [memory_record.id8]
        assert [record.id8 for record in skill_store.list_pending()] == [skill_record.id8]
        assert memory_store.pending_count() == 1
        assert skill_store.pending_count() == 1

    def test_cross_subsystem_discard_is_isolated(self, tmp_path) -> None:
        """破坏者：记忆中台不得丢弃技能中台的待批准记录。"""
        memory_store = MemoryPendingStore(str(tmp_path / "root"))
        skill_store = MemoryPendingStore(str(tmp_path / "root"), subsystem="skills")
        skill_record = skill_store.stage(
            {
                "action": "stage_skill",
                "origin": "background",
                "payload": {"skill_name": "cache-playbook", "content": "# 缓存优化"},
            }
        )
        assert memory_store.get_pending(skill_record.id8) is None
        assert memory_store.discard_pending(skill_record.id8) is False
        assert skill_store.get_pending(skill_record.id8) is not None


class TestHeadlessOriginGate:
    def test_headless_runner_stages_long_term_write(
        self, tmp_path, pending_store, monkeypatch
    ) -> None:
        """契约：headless 调度跑一轮——记忆写入进 pending 而非直写。"""
        from bandi.core import agent_runner as runner_mod

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        (tmp_path / ".bandi" / "data").mkdir(parents=True, exist_ok=True)
        app_mod = importlib.import_module("bandi.api.app")

        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        manager = SimpleNamespace(long_term=long_term, episodic=None, working=None)
        final_message = (
            "结论：以后都统一用暂存方案，因此决定把后台写入改造为待批准队列，"
            "这样任何无人值守路径都不能绕过审批门。"
        )
        ctx = SimpleNamespace(
            tool_results=None,
            final_message=final_message,
            current_content=None,
            raw_input="记住：后台巡检任务 42",
            turn_index=1,
            terminal_reason=None,
            messages=[],
        )

        async def _fake_turn(user_input: str, mode: str, **kwargs):
            await runner_mod._persist_memory(ctx, manager, session_id="s-headless")
            return SimpleNamespace(reply="后台完成")

        monkeypatch.setattr(runner_mod, "run_agent_turn", _fake_turn)
        monkeypatch.setattr(app_mod, "_task_runner", None)
        from bandi.scheduler.store import ScheduledTask

        runner = app_mod._get_task_runner()
        result = asyncio.run(
            runner(
                ScheduledTask(
                    task_id="t-headless",
                    name="后台任务",
                    cron="0 0 * * *",
                    prompt="后台整理记忆",
                )
            )
        )
        assert result == "后台完成"
        assert long_term.count() == 0, "headless 写入不得直落长期层"
        records = pending_store.list_pending()
        assert len(records) == 1
        assert records[0].origin == "background"
        assert records[0].payload["target"] == "long_term"
        assert get_write_origin() == "foreground", "跑完必须恢复前台来源"
        long_term.close()


def _seed_pending_backlog(store: MemoryPendingStore, count: int) -> None:
    """功能：直接落盘 count 条待批准记录（不同 key，模拟历史积压目录）。"""
    store._dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        record = {
            "id8": f"b{i:07d}",
            "subsystem": store._subsystem,
            "action": "store_long_term",
            "summary": "积压",
            "origin": "background",
            "created_at": 1000.0 + i,
            "payload": {"target": "long_term", "item": {"id": f"turn:s:{i}"}},
        }
        (store._dir / f"b{i:07d}.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )


def _concurrent_stage_timed(
    store: MemoryPendingStore, payload: dict, *, threads: int = 8
) -> tuple[float, list[BaseException]]:
    """功能：8 线程同刻并发 stage 同一 payload，返回（墙钟耗时, 线程异常）。"""
    barrier = threading.Barrier(threads)
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            barrier.wait(timeout=10)
            store.stage(dict(payload))
        except BaseException as exc:  # noqa: BLE001 — 汇总返回供主线程断言
            errors.append(exc)

    workers = [threading.Thread(target=_worker) for _ in range(threads)]
    start = time.perf_counter()
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    return time.perf_counter() - start, errors


@pytest.mark.timing
class TestStageBacklogScalability:
    """🟡-3（复审）：supersede 扫描/删除在写锁内 → 500 条积压下拖慢。

    修复：扫描候选与删除移出写锁（只删比当前更旧的同 key 记录，写盘/删除各自
    短临界区）；解析缓存按 (mtime_ns, size) 增量复用，不再每次全量重读 JSON。

    `timing`：本类全部用例以**墙钟预算**为断言（2s/500 条），隔离单跑 ~25-；
    全量 `-n 4` 下与 8000+ 用例争 CPU 必然压爆预算 → 由 verify.ps1 独立低并发分组承载。
    """

    _FIELDS = {
        "action": "stage_skill",
        "origin": "background",
        "summary": "积压",
        "payload": {"skill_name": "cache-playbook", "content": "# x\n"},
    }

    def test_supersede_scan_does_not_block_concurrent_write(self, tmp_path, monkeypatch) -> None:
        """破坏者：扫描期间另一线程的写盘必须立即完成（扫描不得持写锁）。"""
        store = MemoryPendingStore(str(tmp_path / "root"), subsystem="skills")
        store.stage(dict(self._FIELDS))

        entered = threading.Event()
        release = threading.Event()
        original_list_pending = store.list_pending

        def _slow_list_pending():
            if threading.current_thread().name == "supersede-scanner":
                entered.set()
                release.wait(timeout=10)
            return original_list_pending()

        monkeypatch.setattr(store, "list_pending", _slow_list_pending)
        scanner = threading.Thread(
            target=lambda: store.stage(dict(self._FIELDS)), name="supersede-scanner"
        )
        scanner.start()
        assert entered.wait(timeout=10), "扫描线程未进入 supersede 扫描"

        start = time.perf_counter()
        store.stage({**self._FIELDS, "summary": "并发写"})
        elapsed = time.perf_counter() - start

        release.set()
        scanner.join(timeout=10)
        assert elapsed < 0.5, f"写盘被 supersede 扫描阻塞 {elapsed:.2f}s（扫描仍在写锁内）"

    def test_stage_with_500_pending_backlog_under_two_seconds(self, tmp_path) -> None:
        """契约：500 条积压下单次 stage（含扫描）必须 <2s。

        阈值含 xdist 并行全量跑的 wall-clock 抖动余量；串行化回归为数量级差异，仍可捕获。
        """
        store = MemoryPendingStore(str(tmp_path / "root"), subsystem="skills")
        _seed_pending_backlog(store, 500)
        store.list_pending()  # 预热增量缓存（目录首次全量读取不计入产品路径耗时）

        start = time.perf_counter()
        store.stage(dict(self._FIELDS))
        elapsed = time.perf_counter() - start

        assert elapsed < 2.0, f"500 条积压下 stage 耗时 {elapsed:.2f}s"

    def test_concurrent_stages_with_500_backlog_under_two_seconds(self, tmp_path) -> None:
        """契约：8 线程同名 stage 在 500 条积压下不得串行化扫描（总耗时 <2s）。

        阈值含 xdist 并行全量跑的 wall-clock 抖动余量；串行化回归为数量级差异，仍可捕获。
        """
        store = MemoryPendingStore(str(tmp_path / "root"), subsystem="skills")
        _seed_pending_backlog(store, 500)
        store.list_pending()  # 预热

        elapsed, errors = _concurrent_stage_timed(store, self._FIELDS)

        assert errors == [], f"并发 stage 抛错：{errors}"
        assert elapsed < 2.0, f"500 条积压下 8 线程 stage 总耗时 {elapsed:.2f}s（扫描串行化）"
        records = store.list_pending()
        assert len(records) == 501, (
            f"积压条目不得被误删（应 500 + 同名新记录 1，实 {len(records)}）"
        )
        same_key = [r for r in records if r.payload.get("skill_name") == "cache-playbook"]
        assert len(same_key) == 1, f"同名并发 stage 后应恰 1 条，实际 {len(same_key)}"
        assert same_key[0].id8
