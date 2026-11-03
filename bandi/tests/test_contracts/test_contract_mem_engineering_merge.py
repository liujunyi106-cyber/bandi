"""契约：同类语义合并（批次原子 + 最终态预算 + Jaccard 分组 + 整合接线）。

生产调用路径：
1. LongTermMemory.apply_batch——单连接事务内 upsert+delete；最终态按
   (project, employee, kind) 分区计数校验（>300 拒绝整批，不裁剪）
2. merge.suggest_merges——同 scope 同 memory_type 候选 Jaccard 分组（offset 翻页
   扫描全量，单页 limit 不得截断扫描面）；target_id 稳定 = merged:<sha1(排序成员 id 串)[:12]>
3. merge.apply_merges / merge_similar_memories——确定性合并内容（最长为准 +
   去重句子/实体拼接 ≤2000），成员并集 metadata + merged_from
4. consolidation.consolidate_episodes 末尾直接应用（维护类数据整理，不经暂存门）
   报告 merged 与 merged_sources（来源 id 可还原）

破坏者断言：
- delete 目标不存在 → 整批零写入（含 upsert 不能半写）
- 分区最终态 301 → ValueError 零写入；恰好 300 → 成功
- 重复 apply 同批 → 无副作用（幂等）
- 不同 memory_type / 低相似 / 空候选不合并
- 中文数字/数量词不同（百分之五十↔百分之六十、五十台↔六十台、三成↔八成）不得自动合并
- >500 条旧条目中的近义对仍在扫描面
- 整合晋升暂存摘要已打码；合并报告携带来源列表
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

import pytest

from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.merge import (
    MergeSuggestion,
    apply_merges,
    merge_similar_memories,
    suggest_merges,
)


def _mk_item(
    item_id: str,
    content: str,
    *,
    memory_type: str | None = "business_fact",
    importance: float = 0.5,
    timestamp: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> MemoryItem:
    meta: dict[str, Any] = dict(metadata or {})
    if memory_type is not None:
        meta["memory_type"] = memory_type
    return MemoryItem(
        item_id=item_id,
        content=content,
        metadata=meta,
        timestamp=timestamp,
        importance=importance,
    )


# ── 批次原子 + 最终态预算 ─────────────────────────────────────────────────────


def test_apply_batch_upserts_and_deletes_in_one_shot(tmp_path: Any) -> None:
    """契约：一批 upsert+delete 全部生效（主表与 FTS 同步；删除项不可再检索）。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(MemoryItem("keep", "保留的旧记忆"), "p1", "e1")
    lt.store(MemoryItem("drop", "将被合并的旧记忆"), "p1", "e1")

    lt.apply_batch(
        upserts=[MemoryItem("new", "合并后的新记忆")],
        deletes=["drop"],
        project_id="p1",
        employee_id="e1",
    )

    assert lt.get("new") is not None and lt.get("keep") is not None
    assert lt.get("drop") is None
    assert lt.count() == 2
    assert lt.search_fts("合并后的新记忆", limit=5, project_id="p1", employee_id="e1"), (
        "新写入必须可经 FTS 检索（索引同步）"
    )
    assert not lt.search_fts("将被合并的旧记忆", limit=5, project_id="p1", employee_id="e1"), (
        "已删除项不得残留 FTS 索引"
    )
    lt.close()


def test_apply_batch_missing_delete_rejects_whole_batch(tmp_path: Any) -> None:
    """契约（破坏者）：删除目标不存在 → 整批零写入——upsert 也不得半写。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(MemoryItem("keep", "原始记录"), "p1", "e1")

    with pytest.raises(ValueError):
        lt.apply_batch(
            upserts=[MemoryItem("half", "绝不该落库的半写内容")],
            deletes=["missing-id"],
            project_id="p1",
            employee_id="e1",
        )

    assert lt.get("half") is None, "整批拒绝后 upsert 不得残留"
    assert lt.get("keep") is not None and lt.count() == 1
    assert not lt.search_fts("绝不该落库的半写内容", limit=5, project_id="p1", employee_id="e1")
    lt.close()


def test_apply_batch_budget_final_state_and_per_kind(tmp_path: Any) -> None:
    """契约：最终态预算按 (scope, kind) 分区——恰好 300 可、301 拒；kind 互相独立。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    for i in range(300):
        lt.store(MemoryItem(f"turn:p1:e1:{i}", f"会话记录 {i}"), "p1", "e1")

    lt.apply_batch(
        upserts=[MemoryItem("turn:p1:e1:new", "净增为零的合并结果")],
        deletes=["turn:p1:e1:0"],
        project_id="p1",
        employee_id="e1",
    )
    assert lt.count() == 300 and lt.get("turn:p1:e1:new") is not None, (
        "恰好 300 且最终态不超限的批次必须通过"
    )

    lt.apply_batch(
        upserts=[MemoryItem("compaction:p1:e1:1", "压缩摘要独立分区")],
        deletes=[],
        project_id="p1",
        employee_id="e1",
    )
    assert lt.get("compaction:p1:e1:1") is not None, "不同 kind 分区互不占额"

    with pytest.raises(ValueError):
        lt.apply_batch(
            upserts=[MemoryItem("turn:p1:e1:new2", "第 301 条会话记录")],
            deletes=[],
            project_id="p1",
            employee_id="e1",
        )
    assert lt.get("turn:p1:e1:new2") is None, "超限批次必须零写入"
    assert lt.count() == 301
    lt.close()


# ── 分组合并建议 ─────────────────────────────────────────────────────────────


def test_suggest_merges_groups_near_duplicates_chinese(tmp_path: Any) -> None:
    """契约：同类型中文近义句式 Jaccard 达标成组；无关条目不入组；内容去重拼接。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    a = "项目决定采用方案A：迁移到新数据库，降低维护成本"
    b = "项目决定采用方案A：迁移到新数据库以降低维护成本"
    lt.store(_mk_item("m1", a, memory_type="process_conclusion", timestamp=100.0))
    lt.store(_mk_item("m2", b, memory_type="process_conclusion", timestamp=200.0))
    lt.store(_mk_item("m3", "部署服务器需要配置防火墙规则", memory_type="process_conclusion"))

    suggestions = suggest_merges(lt)

    assert len(suggestions) == 1, f"恰应成组一对（实际 {suggestions}）"
    suggestion = suggestions[0]
    assert suggestion.source_ids == ["m1", "m2"]
    assert suggestion.memory_type == "process_conclusion"
    assert "方案A" in suggestion.merged_content
    assert len(suggestion.merged_content) <= 2000
    expected = "merged:" + hashlib.sha1("|".join(["m1", "m2"]).encode()).hexdigest()[:12]
    assert suggestion.target_id == expected, "target_id 必须为成员 id 串的稳定哈希"
    lt.close()


def test_suggest_merges_respects_type_and_threshold(tmp_path: Any) -> None:
    """契约（破坏者）：不同 memory_type 不合并；相似度低于阈值不合并。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    content = "用户偏好使用简洁的风格回答"
    lt.store(_mk_item("p1", content, memory_type="preference"))
    lt.store(_mk_item("p2", content, memory_type="business_fact"))
    assert suggest_merges(lt) == [], "跨 memory_type 即使内容相同也不得合并"

    lt.store(_mk_item("p3", "用户偏好使用简洁风格回答", memory_type="preference"))
    assert suggest_merges(lt), "同类型高相似必须成组"
    assert suggest_merges(lt, similarity_threshold=1.0) == [], "阈值 1.0 时仅完全一致可合并"
    lt.close()


def test_suggest_merges_scoped_and_stable_target(tmp_path: Any) -> None:
    """契约：候选按 scope 隔离；重复建议 target_id/内容完全一致（确定性）。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(_mk_item("s1", "蓝鲸是地球上现存最大的动物"), "proj", "emp")
    lt.store(_mk_item("s2", "蓝鲸是地球上现存最大的动物，生活在海洋中"), "proj", "emp")
    lt.store(_mk_item("o1", "蓝鲸是地球上现存最大的动物"), "other", "emp")

    first = suggest_merges(lt, project_id="proj", employee_id="emp")
    second = suggest_merges(lt, project_id="proj", employee_id="emp")
    assert first and [s.target_id for s in first] == [s.target_id for s in second]
    assert [s.merged_content for s in first] == [s.merged_content for s in second]
    assert first[0].source_ids == ["s1", "s2"], "跨 scope 条目不得入组"
    assert suggest_merges(lt, project_id="other", employee_id="emp") == [], (
        "另一 scope 单条候选必须无建议"
    )
    lt.close()


def test_merge_content_bounded_and_deterministic_across_calls(tmp_path: Any) -> None:
    """契约：长内容合并总长 ≤2000 且跨调用字节级稳定。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    shared = "".join(f"条目{i}" for i in range(300))
    expanded = shared + "".join(f"补充{i}" for i in range(160))
    lt.store(_mk_item("long-a", shared))
    lt.store(_mk_item("long-b", expanded))

    first = suggest_merges(lt)
    second = suggest_merges(lt)
    assert first and first[0].source_ids == ["long-a", "long-b"]
    assert len(first[0].merged_content) == 2000, "超长合并必须截断到 2000 字符"
    assert first[0].merged_content == second[0].merged_content
    lt.close()


# ── 合并落库 ─────────────────────────────────────────────────────────────────


def test_apply_merges_replaces_sources_and_preserves_scope(tmp_path: Any) -> None:
    """契约：合并写入 target（importance=最大、timestamp=最新、metadata 并集 + merged_from）
    删除全部源，且作用域不变。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(
        _mk_item("g1", "蓝鲸是地球上现存最大的动物", importance=0.3, timestamp=100.0),
        "proj",
        "emp",
    )
    lt.store(
        _mk_item(
            "g2",
            "蓝鲸是地球上现存最大的动物，生活在海洋中",
            importance=0.9,
            timestamp=200.0,
            metadata={"entities": ["蓝鲸"], "source": "agent_turn"},
        ),
        "proj",
        "emp",
    )

    suggestions = suggest_merges(lt, project_id="proj", employee_id="emp")
    applied = apply_merges(lt, suggestions)

    assert applied == 1
    target_id = suggestions[0].target_id
    target = lt.get(target_id)
    assert target is not None
    assert target.importance == pytest.approx(0.9)
    assert target.timestamp == pytest.approx(200.0)
    assert target.metadata["merged_from"] == ["g1", "g2"]
    assert target.metadata["memory_type"] == "business_fact"
    assert target.metadata["entities"] == ["蓝鲸"]
    assert lt.get("g1") is None and lt.get("g2") is None
    assert lt.scope_of(target_id) == ("proj", "emp"), "合并结果必须留在原 scope"
    kept = lt.list_items(project_id="proj", employee_id="emp")
    assert [item.id for item in kept] == [target_id]
    lt.close()


def test_apply_merges_idempotent_on_repeat(tmp_path: Any) -> None:
    """契约（破坏者）：同批建议重复 apply → 第二次零副作用；再建议为空。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(_mk_item("i1", "蓝鲸是地球上现存最大的动物"))
    lt.store(_mk_item("i2", "蓝鲸是地球上现存最大的动物，生活在海洋中"))
    suggestions = suggest_merges(lt)

    assert apply_merges(lt, suggestions) == 1
    before = lt.count()
    assert apply_merges(lt, suggestions) == 0, "源已消失的建议必须跳过而非报错"
    assert lt.count() == before
    assert suggest_merges(lt) == [], "合并完成后不得再产出同组建议"
    lt.close()


def test_merge_similar_memories_dry_run_and_apply(tmp_path: Any) -> None:
    """契约：dry_run=True 只返回建议不落库；dry_run=False 应用并返回已应用项。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(_mk_item("d1", "蓝鲸是地球上现存最大的动物"))
    lt.store(_mk_item("d2", "蓝鲸是地球上现存最大的动物，生活在海洋中"))

    preview = merge_similar_memories(lt, dry_run=True)
    assert preview and lt.count() == 2, "dry_run 不得写库"

    applied = merge_similar_memories(lt)
    assert applied and [s.target_id for s in applied] == [s.target_id for s in preview]
    assert lt.count() == 1
    assert merge_similar_memories(lt) == []
    lt.close()


def test_apply_merges_all_sources_gone_is_noop(tmp_path: Any) -> None:
    """契约（破坏者）：全部源已消失的手工建议 → 0 且零写入（不抛异常）。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    stale = MergeSuggestion(
        target_id="merged:deadbeef0000",
        source_ids=["ghost-a", "ghost-b"],
        merged_content="幽灵合并内容",
        memory_type="business_fact",
    )

    assert apply_merges(lt, [stale]) == 0
    assert lt.count() == 0 and lt.get("merged:deadbeef0000") is None
    lt.close()


# ── 生产接线（consolidation 报告） ────────────────────────────────────────────


def test_consolidation_merges_directly_without_staging_gate(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约：consolidate_episodes 末尾直接应用合并（维护类口径，不经暂存门）并报告
    merged——即使写入门对任何来源都判 stage，合并仍应直落长期库。"""
    from bandi.memory.consolidation import consolidate_episodes
    from bandi.memory.episodic import Episode, EpisodicStore
    from bandi.memory.staging import MemoryWriteGate, get_pending_store

    monkeypatch.setattr(MemoryWriteGate, "evaluate", staticmethod(lambda origin: "stage"))

    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(_mk_item("c1", "蓝鲸是地球上现存最大的动物", timestamp=100.0))
    lt.store(_mk_item("c2", "蓝鲸是地球上现存最大的动物，生活在海洋中", timestamp=200.0))
    episodic = EpisodicStore(str(tmp_path / "ep.db"))
    episodic.record(
        Episode(episode_id="ep_hi", summary="结论：高价值经验", importance=0.9, recall_count=2)
    )

    try:
        report = consolidate_episodes(episodic, lt)
    finally:
        episodic.close()

    assert report.staged == ["ep_hi"], "晋升走暂存门"
    assert report.merged, "consolidation 必须报告合并结果（merged 字段）"
    targets = report.merged
    assert all(item_id.startswith("merged:") for item_id in targets)
    assert lt.get(targets[0]) is not None, "合并必须直接落长期库（不经暂存门）"
    assert lt.get("c1") is None and lt.get("c2") is None
    assert lt.get("consolidated:ep_hi") is None, "晋升仍应停在暂存区"
    pending = get_pending_store().list_pending()
    assert [record.action for record in pending] == ["consolidate_episode"]
    assert lt.count() == 1
    lt.close()


def test_list_items_scope_isolation_and_order(tmp_path: Any) -> None:
    """契约：list_items 只读、按 timestamp DESC、按 scope 隔离。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(MemoryItem("old", "旧记录", timestamp=100.0), "p1", "e1")
    lt.store(MemoryItem("new", "新记录", timestamp=300.0), "p1", "e1")
    lt.store(MemoryItem("mid", "中记录", timestamp=200.0), "p1", "e1")
    lt.store(MemoryItem("foreign", "其他项目", timestamp=400.0), "p2", "e1")
    lt.store(MemoryItem("solo", "无维度全局层", timestamp=500.0))

    scoped = lt.list_items(project_id="p1", employee_id="e1")
    assert [item.id for item in scoped] == ["new", "mid", "old"]
    assert lt.list_items(project_id="p2", employee_id="e1")[0].id == "foreign"
    assert lt.list_items()[0].id == "solo", "未传维度只看全局层（与检索口径一致）"
    lt.close()


# ── 实质差异保护（数值/引号专名） ─────────────────────────────────────────


def test_suggest_merges_rejects_numeric_and_quoted_name_differences(tmp_path: Any) -> None:
    """契约（破坏者）：模板相同仅数值/引号专名不同——Jaccard 高但语义不同
    不得自动合并删源（50%→60%、100→200 dollars、「蓝海」→「蓝天」）。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(_mk_item("num-cn-a", "第三季度完成率 50%，达到既定目标", memory_type="metric"))
    lt.store(_mk_item("num-cn-b", "第三季度完成率 60%，达到既定目标", memory_type="metric"))
    lt.store(
        _mk_item(
            "num-en-a",
            "annual revenue was 100 dollars, exceeding expectations",
            memory_type="report",
        )
    )
    lt.store(
        _mk_item(
            "num-en-b",
            "annual revenue was 200 dollars, exceeding expectations",
            memory_type="report",
        )
    )
    lt.store(_mk_item("quote-a", "签约客户是「蓝海科技」的年度框架合同", memory_type="contract"))
    lt.store(_mk_item("quote-b", "签约客户是「蓝天科技」的年度框架合同", memory_type="contract"))

    assert suggest_merges(lt) == [], "数值/引号专名实质不同的记忆不得自动合并"
    lt.close()


def test_suggest_merges_keeps_equal_number_near_duplicates(tmp_path: Any) -> None:
    """契约：数值相同（50% = 50%）的近义改写仍可合并——保护只拦实质差异。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(
        _mk_item("same-a", "迁移到新数据库可降低维护成本 50%，团队已确认", memory_type="conclusion")
    )
    lt.store(
        _mk_item(
            "same-b", "迁移到新数据库可降低维护成本 50% 并提升稳定性", memory_type="conclusion"
        )
    )

    suggestions = suggest_merges(lt)
    assert suggestions and suggestions[0].source_ids == ["same-a", "same-b"]
    lt.close()


def test_suggest_merges_rejects_chinese_number_differences(tmp_path: Any) -> None:
    """契约（破坏者）：中文数字/数量词不同——「百分之五十↔百分之六十」「五十台↔六十台」
    「三成↔八成」「一万↔两万」，模板近义但语义不同，不得自动合并删源。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(_mk_item("cn-pct-a", "第三季度完成率百分之五十，达到既定目标", memory_type="metric"))
    lt.store(_mk_item("cn-pct-b", "第三季度完成率百分之六十，达到既定目标", memory_type="metric"))
    lt.store(_mk_item("cn-count-a", "仓库里还有五十台服务器可以调配", memory_type="inventory"))
    lt.store(_mk_item("cn-count-b", "仓库里还有六十台服务器可以调配", memory_type="inventory"))
    lt.store(_mk_item("cn-rate-a", "预计本季度产能提升三成左右", memory_type="forecast"))
    lt.store(_mk_item("cn-rate-b", "预计本季度产能提升八成左右", memory_type="forecast"))
    lt.store(_mk_item("cn-money-a", "追加预算一万用于扩容", memory_type="budget"))
    lt.store(_mk_item("cn-money-b", "追加预算两万用于扩容", memory_type="budget"))

    assert suggest_merges(lt) == [], "中文数字实质不同的记忆不得自动合并"
    lt.close()


def test_suggest_merges_keeps_equal_chinese_number_near_duplicates(tmp_path: Any) -> None:
    """契约：中文数字相同（百分之五十 = 百分之五十）的近义改写仍可合并。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(_mk_item("cn-same-a", "第三季度完成率百分之五十，达到既定目标", memory_type="metric"))
    lt.store(
        _mk_item("cn-same-b", "第三季度完成率百分之五十，达成了既定目标", memory_type="metric")
    )

    suggestions = suggest_merges(lt)
    assert suggestions and suggestions[0].source_ids == ["cn-same-a", "cn-same-b"]
    lt.close()


# ── metadata 并集 / TOCTOU / 前缀排除 ──────────────────────────────


def test_apply_merges_metadata_union_lineage_reliability(tmp_path: Any) -> None:
    """契约（破坏者）：metadata 并集——entities 去重并集、merged_from 递归展开
    reliability 取 max、recall_count 求和、last_recalled_at 取最新。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(
        _mk_item(
            "u1",
            "蓝鲸是地球上现存最大的动物",
            timestamp=100.0,
            metadata={
                "entities": ["蓝鲸", "海洋"],
                "reliability": 0.9,
                "recall_count": 2,
                "last_recalled_at": "T00:00:00+00:00",
            },
        ),
        "proj",
        "emp",
    )
    lt.store(
        _mk_item(
            "u2",
            "蓝鲸是地球上现存最大的动物，生活在海洋中",
            timestamp=200.0,
            metadata={
                "entities": ["蓝鲸", "哺乳动物"],
                "reliability": 0.3,
                "recall_count": 3,
                "last_recalled_at": "T00:00:00+00:00",
                "merged_from": ["older-1", "older-2"],
            },
        ),
        "proj",
        "emp",
    )

    suggestions = suggest_merges(lt, project_id="proj", employee_id="emp")
    assert apply_merges(lt, suggestions) == 1
    target = lt.get(suggestions[0].target_id)
    assert target is not None
    assert target.metadata["entities"] == ["哺乳动物", "海洋", "蓝鲸"], "entities 必须并集去重"
    assert target.metadata["merged_from"] == ["older-1", "older-2", "u1", "u2"], (
        "merged_from 必须递归展开历史谱系"
    )
    assert target.metadata["reliability"] == pytest.approx(0.9), "reliability 取 max"
    assert target.metadata["recall_count"] == 5, "recall_count 求和"
    assert target.metadata["last_recalled_at"] == "T00:00:00+00:00", "last_recalled_at 取最新"
    lt.close()


def test_apply_merges_stale_suggestion_skipped_without_delete(tmp_path: Any) -> None:
    """契约（破坏者 TOCTOU）：suggest 后成员被改写 → apply 必须跳过（stale 计数）
    不得合并、不得删除改动后的成员。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(_mk_item("c1", "蓝鲸是地球上现存最大的动物", timestamp=100.0), "proj", "emp")
    lt.store(
        _mk_item("c2", "蓝鲸是地球上现存最大的动物，生活在海洋中", timestamp=200.0), "proj", "emp"
    )
    suggestions = suggest_merges(lt, project_id="proj", employee_id="emp")
    assert suggestions, "前置：必须成组"

    lt.store(
        MemoryItem(
            "c1",
            "用户已改写的全新内容：部署方案评审记录",
            timestamp=150.0,
            metadata={"memory_type": "business_fact"},
        ),
        "proj",
        "emp",
    )
    stale: list[str] = []
    applied = apply_merges(lt, suggestions, stale_targets=stale)

    assert applied == 0, "成员内容已变，不得再应用该建议"
    assert stale == [suggestions[0].target_id], f"必须报告 stale 建议（实际 {stale}）"
    assert lt.get("c1") is not None and lt.get("c2") is not None, "改动后的成员不得被删除"
    assert lt.get(suggestions[0].target_id) is None, "不得写出合并目标"
    lt.close()


def test_suggest_merges_excludes_compaction_but_includes_turn_and_consolidated(
    tmp_path: Any,
) -> None:
    """契约（破坏者）：`compaction:` 是压缩快照内部产物，不得参与合并；
    `turn:` 与 `consolidated:` 都要参与——它们是同一份事实的两次落库，
    留着两份会让「记忆×草稿去重」预期落空（计划 -2 已放开 `consolidated:`）。

    `turn:` 另有两道保护：未被召回 + 够老（`_turn_merge_eligible`），本例
    两条都刚写入，故仍应被排除——排除理由是年龄，不是前缀。
    """
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    for item_id in ("compaction:s:1", "compaction:s:2"):
        lt.store(_mk_item(item_id, "压缩摘要：部署方案与数据库迁移计划", memory_type="summary"))
    for item_id in ("consolidated:ep1", "consolidated:ep2"):
        lt.store(_mk_item(item_id, "晋升经验：部署方案与数据库迁移计划", memory_type="experience"))

    suggestions = suggest_merges(lt)
    sources = {sid for s in suggestions for sid in s.source_ids}
    assert any(s.startswith("consolidated:") for s in sources), (
        f"consolidated: 必须参与合并，实际来源 {sources}"
    )
    assert not any(s.startswith("compaction:") for s in sources), "compaction: 仍须排除"
    lt.close()


def test_new_turn_items_not_merged_until_stale(tmp_path: Any) -> None:
    """`turn:` 的合并门槛是「未被召回 + 够老」，新近对话仍可能被后续轮次引用。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    for item_id in ("turn:s:1", "turn:s:2"):
        lt.store(_mk_item(item_id, "用户询问部署方案与数据库迁移计划", memory_type="turn_summary"))

    assert suggest_merges(lt) == [], "刚写入的 turn: 条目不得被合并"
    lt.close()


def test_merge_target_kind_matches_members(tmp_path: Any) -> None:
    """契约：合并目标 kind 与成员一致（普通成员保持 other 与 merged: 命名）。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(_mk_item("k1", "蓝鲸是地球上现存最大的动物"), "proj", "emp")
    lt.store(_mk_item("k2", "蓝鲸是地球上现存最大的动物，生活在海洋中"), "proj", "emp")

    suggestions = suggest_merges(lt, project_id="proj", employee_id="emp")
    assert suggestions
    kind = LongTermMemory._kind_of(suggestions[0].target_id)
    assert kind == LongTermMemory._kind_of(suggestions[0].source_ids[0]) == "other"
    lt.close()


# ── delete scope 校验 / 净减放行 ──────────────────────────────────────


def test_apply_batch_delete_scope_mismatch_rejects_whole_batch(tmp_path: Any) -> None:
    """契约（破坏者）：delete 行不属于参数 scope → 整批拒绝（防跨 scope 误删）。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(MemoryItem("foreign", "其他项目记录"), "p2", "e1")

    with pytest.raises(ValueError):
        lt.apply_batch(
            upserts=[MemoryItem("half", "绝不该落库的内容")],
            deletes=["foreign"],
            project_id="p1",
            employee_id="e1",
        )

    assert lt.get("foreign") is not None, "跨 scope 目标不得被删"
    assert lt.get("half") is None, "整批拒绝不得半写"
    lt.close()


def test_apply_merges_cross_scope_manual_suggestion_rejected(tmp_path: Any) -> None:
    """契约（破坏者）：手工建议把不同 scope 成员放进同一组 → apply_batch 校验
    scope 不一致整批拒绝，不得跨项目误删。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(MemoryItem("x1", "项目一的内容记录"), "p1", "e1")
    lt.store(MemoryItem("x2", "项目二的内容记录"), "p2", "e1")
    manual = MergeSuggestion(
        target_id="merged:crossscope",
        source_ids=["x1", "x2"],
        merged_content="手工合并内容",
        memory_type="business_fact",
    )

    with pytest.raises(ValueError):
        apply_merges(lt, [manual])

    assert lt.get("x1") is not None and lt.get("x2") is not None, "跨 scope 成员都不得被删"
    assert lt.get("merged:crossscope") is None, "整批拒绝不得写出目标"
    lt.close()


def test_apply_batch_net_decrease_allows_over_budget_partition(tmp_path: Any) -> None:
    """契约（破坏者）：分区已超预算时净减批次仍放行（只拦净增）。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    for i in range(302):
        lt.store(MemoryItem(f"turn:p:e:{i}", f"会话记录 {i}"), "p", "e")

    lt.apply_batch(
        upserts=[],
        deletes=["turn:p:e:0"],
        project_id="p",
        employee_id="e",
    )

    assert lt.count() == 301 and lt.get("turn:p:e:0") is None, "净减批次必须放行"
    lt.close()


# ── consolidation 全分区合并 / 维护漏传抑制 ───────────────────────────


def test_consolidation_merges_scoped_memories(tmp_path: Any) -> None:
    """契约：consolidation 必须遍历 distinct (project, employee) 分区执行合并——
    先前只合并全局层，带维度近义对永不合并。"""
    from bandi.memory.consolidation import consolidate_episodes
    from bandi.memory.episodic import EpisodicStore

    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(_mk_item("sc1", "蓝鲸是地球上现存最大的动物", timestamp=100.0), "proj", "emp")
    lt.store(
        _mk_item("sc2", "蓝鲸是地球上现存最大的动物，生活在海洋中", timestamp=200.0),
        "proj",
        "emp",
    )
    episodic = EpisodicStore(str(tmp_path / "ep.db"))
    try:
        report = consolidate_episodes(episodic, lt)
    finally:
        episodic.close()

    assert report.merged, "带 (project, employee) 维度的近义对必须在 consolidation 中被合并"
    assert lt.get("sc1") is None and lt.get("sc2") is None
    assert lt.get(report.merged[0]) is not None
    assert lt.scope_of(report.merged[0]) == ("proj", "emp")
    lt.close()


def test_merge_does_not_count_scope_miss(tmp_path: Any) -> None:
    """契约：维护类合并写入不得污染维度漏传计数。"""
    from bandi.memory.long_term import reset_scope_strict_misses, scope_strict_misses

    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(_mk_item("gm1", "蓝鲸是地球上现存最大的动物", timestamp=100.0))
    lt.store(_mk_item("gm2", "蓝鲸是地球上现存最大的动物，生活在海洋中", timestamp=200.0))

    reset_scope_strict_misses()
    applied = merge_similar_memories(lt)
    assert applied, "前置：全局层近义对必须合并"
    assert all(value == 0 for value in scope_strict_misses().values()), (
        f"合并属维护类写入，不得计为维度漏传：{scope_strict_misses()}"
    )
    lt.close()


def test_suggest_merges_scans_beyond_first_page(tmp_path: Any) -> None:
    """契约：候选扫描必须覆盖全部分页——>500 条旧条目中的近义对仍可建议
    （单页 limit=500 截断不得缩小合并扫描面）。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    for i in range(598):
        digest = hashlib.sha1(f"archive-{i}".encode()).hexdigest()
        lt.store(
            _mk_item(f"bulk-{i:04d}", f"archive record {i} {digest}", timestamp=2000.0 + i),
            "p1",
            "e1",
        )
    lt.store(_mk_item("near-a", "蓝鲸是地球上现存最大的动物", timestamp=1.0), "p1", "e1")
    lt.store(
        _mk_item("near-b", "蓝鲸是地球上现存最大的动物，生活在海洋中", timestamp=1.0),
        "p1",
        "e1",
    )

    suggestions = suggest_merges(lt, project_id="p1", employee_id="e1")

    assert suggestions, "第 500 条之后的近义对必须仍在合并扫描面内"
    assert suggestions[0].source_ids == ["near-a", "near-b"]
    lt.close()


def test_consolidation_staged_summary_masks_secret(tmp_path: Any) -> None:
    """契约：整合晋升写进暂存区的摘要必须在模块出口打码——密钥原文不得
    出现在 pending 记录（不得只依赖 API 列表层二次遮挡）。"""
    from bandi.memory.consolidation import consolidate_episodes
    from bandi.memory.episodic import Episode, EpisodicStore
    from bandi.memory.staging import MemoryPendingStore, reset_pending_store

    secret = "sk-live-abc123def456"
    pending = MemoryPendingStore(str(tmp_path / "memory"))
    reset_pending_store(pending)
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    episodic = EpisodicStore(str(tmp_path / "ep.db"))
    try:
        episodic.record(
            Episode(
                episode_id="ep_secret",
                summary=f"结论：配置密钥 {secret} 已更新到环境变量",
                importance=0.9,
                recall_count=2,
            )
        )
        report = consolidate_episodes(episodic, lt)

        assert report.staged == ["ep_secret"], "前置：高价值情节必须进暂存区"
        records = pending.list_pending()
        assert len(records) == 1
        assert secret not in records[0].summary, "暂存摘要必须在写入时即打码（模块出口脱敏）"
    finally:
        reset_pending_store()
        episodic.close()
        lt.close()


def test_consolidation_report_exposes_merged_sources(tmp_path: Any) -> None:
    """契约：报告必须给出每个合并目标的来源 id 列表——合并应用后仍可还原
    被合并源（审计信息充分）。"""
    from bandi.memory.consolidation import consolidate_episodes
    from bandi.memory.episodic import EpisodicStore

    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(_mk_item("src-1", "蓝鲸是地球上现存最大的动物", timestamp=100.0), "proj", "emp")
    lt.store(
        _mk_item("src-2", "蓝鲸是地球上现存最大的动物，生活在海洋中", timestamp=200.0),
        "proj",
        "emp",
    )
    episodic = EpisodicStore(str(tmp_path / "ep.db"))
    try:
        report = consolidate_episodes(episodic, lt)
    finally:
        episodic.close()

    assert report.merged, "前置：近义对必须被合并"
    target = report.merged[0]
    assert report.merged_sources[target] == ["src-1", "src-2"], (
        f"报告必须携带被合并来源列表（实际 {report.merged_sources}）"
    )
    assert report.to_dict()["merged_sources"][target] == ["src-1", "src-2"], (
        "序列化报告同样必须携带来源列表"
    )
    lt.close()


# ── turn: 前缀合并双保护─────────────────────

_TURN_OLD_TS = time.time() - 30 * 86400.0
_TURN_TEXT = "竞品分析报告：三家主要对手的价格与渠道对比结论已经确认"


class TestTurnMergeGuard:
    """契约：turn: 记忆只在「未召回 + 够老（≥7 天）」时参与合并。

    背景：turn: 是逐轮对话快照，冗余主要来源；放开合并的同时必须防「把不同轮次的
    重要对话糊成一条」——被召回过的说明仍被引用，未够老的说明可能还会被引用。
    """

    def test_fresh_turn_memory_not_merged(self, tmp_path: Any) -> None:
        lt = LongTermMemory(str(tmp_path / "lt.db"))
        lt.store(_mk_item("turn:s1:1", _TURN_TEXT, timestamp=time.time()), "p1", "e1")
        lt.store(_mk_item("turn:s1:2", _TURN_TEXT, timestamp=time.time()), "p1", "e1")
        assert suggest_merges(lt, project_id="p1", employee_id="e1") == [], (
            "新近 turn:（<7 天）不得参与合并"
        )
        lt.close()

    def test_aged_unrecalled_turn_memory_is_merged(self, tmp_path: Any) -> None:
        lt = LongTermMemory(str(tmp_path / "lt.db"))
        lt.store(_mk_item("turn:s1:1", _TURN_TEXT, timestamp=_TURN_OLD_TS), "p1", "e1")
        lt.store(_mk_item("turn:s1:2", _TURN_TEXT, timestamp=_TURN_OLD_TS), "p1", "e1")
        suggestions = suggest_merges(lt, project_id="p1", employee_id="e1")
        assert len(suggestions) == 1, "够老 + 未召回的 turn: 应当参与合并"
        lt.close()

    def test_recalled_turn_memory_not_merged(self, tmp_path: Any) -> None:
        lt = LongTermMemory(str(tmp_path / "lt.db"))
        lt.store(
            _mk_item("turn:s1:1", _TURN_TEXT, timestamp=_TURN_OLD_TS, metadata={"recall_count": 3}),
            "p1",
            "e1",
        )
        lt.store(_mk_item("turn:s1:2", _TURN_TEXT, timestamp=_TURN_OLD_TS), "p1", "e1")
        assert suggest_merges(lt, project_id="p1", employee_id="e1") == [], (
            "已召回的 turn: 不得参与合并（仍在被引用）"
        )
        lt.close()

    def test_compaction_prefix_still_excluded(self, tmp_path: Any) -> None:
        lt = LongTermMemory(str(tmp_path / "lt.db"))
        lt.store(_mk_item("compaction:x:1", _TURN_TEXT, timestamp=_TURN_OLD_TS), "p1", "e1")
        lt.store(_mk_item("compaction:x:2", _TURN_TEXT, timestamp=_TURN_OLD_TS), "p1", "e1")
        assert suggest_merges(lt, project_id="p1", employee_id="e1") == [], (
            "compaction: 前缀仍排除在合并之外"
        )
        lt.close()

    def test_non_turn_memory_unaffected_by_guard(self, tmp_path: Any) -> None:
        # 非 turn: 记忆不受双保护影响（新近也可合并——回归护栏）
        lt = LongTermMemory(str(tmp_path / "lt.db"))
        lt.store(_mk_item("mem:1", _TURN_TEXT, timestamp=time.time()), "p1", "e1")
        lt.store(_mk_item("mem:2", _TURN_TEXT, timestamp=time.time()), "p1", "e1")
        assert len(suggest_merges(lt, project_id="p1", employee_id="e1")) == 1
        lt.close()


# ── 写入侧跨轮去重（is_semantic_duplicate）─────────────────────────────────


class TestSemanticDuplicate:
    """契约：同 scope 近期记忆与新内容高相似 → 判重（写入侧防线）。"""

    def test_similar_content_detected(self, tmp_path: Any) -> None:
        from bandi.memory.merge import is_semantic_duplicate

        lt = LongTermMemory(str(tmp_path / "lt.db"))
        lt.store(_mk_item("mem:old", _TURN_TEXT), "p1", "e1")
        assert is_semantic_duplicate(
            lt, _TURN_TEXT, "turn:s2:9", project_id="p1", employee_id="e1"
        ), "完全相同内容应判重"
        lt.close()

    def test_different_content_not_duplicate(self, tmp_path: Any) -> None:
        from bandi.memory.merge import is_semantic_duplicate

        lt = LongTermMemory(str(tmp_path / "lt.db"))
        lt.store(_mk_item("mem:old", _TURN_TEXT), "p1", "e1")
        assert not is_semantic_duplicate(
            lt,
            "完全无关的另一段内容：客户退款流程需要财务复核",
            "turn:s2:9",
            project_id="p1",
            employee_id="e1",
        )
        lt.close()

    def test_self_item_skipped(self, tmp_path: Any) -> None:
        # 同 id（同轮重放）不算重复——那是幂等覆盖语义
        from bandi.memory.merge import is_semantic_duplicate

        lt = LongTermMemory(str(tmp_path / "lt.db"))
        lt.store(_mk_item("turn:s1:1", _TURN_TEXT), "p1", "e1")
        assert not is_semantic_duplicate(
            lt, _TURN_TEXT, "turn:s1:1", project_id="p1", employee_id="e1"
        )
        lt.close()

    def test_scope_isolated(self, tmp_path: Any) -> None:
        # 别的 project 的相似内容不影响本 scope 判重
        from bandi.memory.merge import is_semantic_duplicate

        lt = LongTermMemory(str(tmp_path / "lt.db"))
        lt.store(_mk_item("mem:other", _TURN_TEXT), "p2", "e1")
        assert not is_semantic_duplicate(
            lt, _TURN_TEXT, "turn:s2:9", project_id="p1", employee_id="e1"
        )
        lt.close()

    def test_empty_content_not_duplicate(self, tmp_path: Any) -> None:
        from bandi.memory.merge import is_semantic_duplicate

        lt = LongTermMemory(str(tmp_path / "lt.db"))
        lt.store(_mk_item("mem:old", _TURN_TEXT), "p1", "e1")
        assert not is_semantic_duplicate(lt, "", "turn:s2:9", project_id="p1", employee_id="e1")
        lt.close()
