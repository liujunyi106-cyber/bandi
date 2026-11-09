# 功能：Memory.md 最终形态契约——持久化索引 index.json（冷启动免全扫 + 失效重建 + 损坏兜底）
"""
  索引落盘：首次构建后 entries/index.json 生成，含签名 + 完整索引（元数据+正文）。
  冷启动复用：entries 未变化（mtime+size 签名一致）时，新实例直接复用 index.json
     list/read/search 结果与全量重建完全一致（行为不变式）。
  失效重建：新增/修改 entry 使签名变化 → 不再复用，全量重建并回写 index.json。
  损坏兜底：index.json 缺失/损坏/版本不符 → 回退全量重建，功能不崩（fail-safe）。
破坏者断言：若把行为钉死成「必须逐 .md 全读」（无 index.json 缓存路径），契约失败；
  反之若签名一致却返回陈旧数据，契约失败（必须与全量重建结果一致）。
"""

from __future__ import annotations

import json
from typing import Any

from bandi.memory.markdown_memory import MarkdownMemoryStore


def _store(tmp_path: Any) -> MarkdownMemoryStore:
    return MarkdownMemoryStore(tmp_path / "store", project_root=tmp_path / "proj")


def _index_path(store: MarkdownMemoryStore) -> Any:
    return store._index_path


def test_index_json_persisted_after_first_build(tmp_path: Any) -> None:
    """首次检索后 index.json 生成，且签名+索引完整。"""
    store = _store(tmp_path)
    store.save_entry("产品定位", "个人多 Agent 数字员工组织，管理 AI 员工")
    store.save_entry("预算", "预算方向修复已完成")
    assert store.list_entry_pointers(), "应先有可索引条目"
    ip = _index_path(store)
    assert ip.exists(), "首次构建后必须生成 index.json"
    payload = json.loads(ip.read_text(encoding="utf-8"))
    assert "signature" in payload and "index" in payload
    ids = {item["entry_id"] for item in payload["index"]}
    assert len(ids) == 2, "index.json 应含全部条目"


def test_cold_start_reuses_index_and_matches_full_rebuild(tmp_path: Any) -> None:
    """签名一致时新实例复用 index.json，结果与全量重建完全一致。"""
    store = _store(tmp_path)
    store.save_entry("产品定位", "个人多 Agent 数字员工组织，管理 AI 员工")
    store.save_entry("预算", "预算方向修复已完成")
    expected_pointers = store.list_entry_pointers()
    expected_search = store.search_entries("预算", limit=5)
    assert _index_path(store).exists()

    reopened = _store(tmp_path)
    assert reopened.list_entry_pointers() == expected_pointers, (
        "冷启动复用 index.json 后指针必须与全量重建一致"
    )
    assert reopened.search_entries("预算", limit=5) == expected_search, (
        "冷启动复用 index.json 后检索结果必须与全量重建一致"
    )
    # 行为不变式：复用路径与重建路径对外等价
    assert reopened.read_entry(expected_pointers[0].entry_id) == store.read_entry(
        expected_pointers[0].entry_id
    )


def test_staleness_invalidates_and_rebuilds(tmp_path: Any) -> None:
    """新增条目使签名变化 → 不复用陈旧 index.json，重建后含新条目。"""
    store = _store(tmp_path)
    store.save_entry("产品定位", "个人多 Agent 数字员工组织")
    before = store.list_entry_pointers()
    assert _index_path(store).exists()

    store.save_entry("新增经验", "沉淀一条新经验：多 Agent 编排")
    after = store.list_entry_pointers()
    assert len(after) == len(before) + 1, "新增条目后索引必须含新条目（签名失效重建）"
    payload = json.loads(_index_path(store).read_text(encoding="utf-8"))
    assert len(payload["index"]) == len(after), "重建后 index.json 必须回写为新状态"


def test_corrupt_index_json_falls_back_to_full_scan(tmp_path: Any) -> None:
    """index.json 损坏 → 回退全量重建，功能不崩、结果正确。"""
    store = _store(tmp_path)
    store.save_entry("产品定位", "个人多 Agent 数字员工组织")
    store.save_entry("预算", "预算方向修复已完成")
    _index_path(store).write_text("{ not valid json !!!", encoding="utf-8")
    reopened = _store(tmp_path)
    assert len(reopened.list_entry_pointers()) == 2, "损坏 index.json 必须回退全量重建"
    assert reopened.search_entries("预算", limit=1), "损坏 index.json 回退后检索仍可用"
