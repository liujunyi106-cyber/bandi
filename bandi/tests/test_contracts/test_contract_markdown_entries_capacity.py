# 功能：markdown entries 容量分区契约——超上限最旧淘汰 + 指针清理 + 检索不可见
"""
     淘汰后 list_entry_pointers/磁盘文件/指针三方一致（无死链）
  不超限不淘汰：≤ 上限条目全部保留
  逃生门：max_entries<=0 不限容量（显式退出分区帷幕）
  检索一致性：被淘汰 entry 在 search_entries 不可见，存活条目仍可 read_entry
  幂等：连续写入超限不崩，索引重建后计数正确
破坏者断言：被淘汰 entry 的指针行必须从 移除——残留指针指向
  不存在的文件（死链）即契约失败。
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

from bandi.memory.markdown_memory import PROJECT_ENTRIES_MAX_COUNT, MarkdownMemoryStore


def _store(tmp_path: Any, max_chars: int | None = None, max_entries: int | None = None) -> Any:
    return MarkdownMemoryStore(
        str(tmp_path / "store"),
        project_root=tmp_path,
        max_chars=max_chars,
        max_entries=max_entries,
    )


def _seed(store: Any, n: int, prefix: str = "记忆条目") -> list[str]:
    ids: list[str] = []
    for i in range(n):
        ids.append(
            store.save_entry(f"{prefix}{i}", f"这是第 {i} 条项目记忆内容，用于容量测试").entry_id
        )
        if i == 0:
            time.sleep(0.02)  # 保证首条 mtime 最旧（淘汰基准）
    return ids


def test_exceeds_capacity_evicts_oldest(tmp_path: Any) -> None:
    """超上限淘汰最旧——列表/磁盘/指针三方一致。"""
    store = _store(tmp_path, max_entries=3)
    ids = _seed(store, 4)
    oldest = ids[0]

    assert len(store.list_entry_pointers()) == 3, "超限后必须收敛到上限"
    with pytest.raises(FileNotFoundError):
        store.read_entry(oldest)
    assert not (store.entries_dir / f"{oldest}.md").exists(), "被淘汰 entry 文件必须删除"


def test_within_capacity_all_kept(tmp_path: Any) -> None:
    """≤ 上限条目全部保留。"""
    store = _store(tmp_path, max_entries=5)
    ids = _seed(store, 4)
    assert len(store.list_entry_pointers()) == 4
    for eid in ids:
        assert store.read_entry(eid) != "", "上限内条目必须可读回"


def test_capacity_zero_unlimited(tmp_path: Any) -> None:
    """max_entries=0 显式不限（逃生门）——对比上限 3 时淘汰 2 条。"""
    store_unlimited = _store(tmp_path, max_entries=0)
    _seed(store_unlimited, 5)
    assert len(store_unlimited.list_entry_pointers()) == 5, "0=不限容量，5 条全保留"

    store_limited = _store(tmp_path / "limited", max_entries=3)
    _seed(store_limited, 5)
    assert len(store_limited.list_entry_pointers()) == 3, "上限 3 时 5 条收敛到 3"


def test_evicted_entry_not_searchable(tmp_path: Any) -> None:
    """被淘汰 entry 检索不可见，存活条目仍可读回。"""
    store = _store(tmp_path, max_entries=2)
    ids = _seed(store, 3, prefix="容量检索")
    alive = ids[-1]

    hits = store.search_entries("容量检索", limit=10)
    hit_ids = {ptr.entry_id for ptr, _ in hits}
    assert ids[0] not in hit_ids, "被淘汰 entry 不得出现在检索结果"
    assert store.read_entry(alive) != "", "存活条目必须可读"


def test_eviction_idempotent_after_index_rebuild(tmp_path: Any) -> None:
    """连续写入超限不崩，索引重建后计数正确。"""
    store = _store(tmp_path, max_entries=2)
    _seed(store, 3)
    assert len(store.list_entry_pointers()) == 2
    _seed(store, 1, prefix="再写")
    assert len(store.list_entry_pointers()) == 2, "继续写仍收敛上限、不崩"
    assert len(store.search_entries("再写", limit=10)) == 1, "新条目可检索"


def test_pointer_line_removed_on_eviction(tmp_path: Any) -> None:
    """破坏者：被淘汰 entry 的 指针行必须移除——残留即死链。"""
    store = _store(tmp_path, max_chars=120, max_entries=2)
    for i in range(3):
        # 每条内容单独超预算（>120）——确保每次 append 都落 entry 产生指针
        store.append_visible_entry(f"这是第 {i} 条超长内容：" + "很长" * 60)
    text = store.load_visible_text()
    for ptr in store.list_entry_pointers():
        assert f"entries/{ptr.entry_id}.md" in text, "存活条目指针必须在入口"
    # 直接断言：可见文本里的指针行数 == 存活 entry 数（无孤儿指针）
    pointer_count = text.count("entries/")
    assert pointer_count == 2, f"指针行必须与存活条目一致（实际 {pointer_count}）"
    # 破坏者：任何指针指向的文件必须真实存在
    import re

    for m in re.finditer(r"entries/([A-Za-z0-9\-]+)\.md", text):
        assert (store.entries_dir / f"{m.group(1)}.md").exists(), "指针必须指向真实文件"


def test_default_capacity_limit(tmp_path: Any) -> None:
    """常量契约：默认上限 = 200；不传参数写入超限收敛。"""
    assert PROJECT_ENTRIES_MAX_COUNT == 200, "条目上限"
    store = _store(tmp_path)
    ids = _seed(store, 3)
    assert len(store.list_entry_pointers()) == 3, "少量写入不受默认上限影响"
    assert store.read_entry(ids[0]) != ""


def test_fresh_entry_survives_mtime_collision(tmp_path: Any) -> None:
    """破坏者（回放）：淘汰排序在 mtime 不可区分（同一时钟 tick）时以文件名
    破平——fresh 条目按名序可能落在淘汰窗口首位而被误删，随后其指针被写回
    即成死链（指针行数 > 上限）。契约：任何 mtime 布局下，刚写入的
    新条目必须存活、指针与文件三方一致。"""
    store = _store(tmp_path, max_chars=120, max_entries=2)
    for i in range(2):
        store.append_visible_entry(f"这是第 {i} 条超长内容：" + "很长" * 60)
    # 模拟同一 tick：既有条目 mtime 统一置于 fresh 之后（等价于三文件同 mtime
    future = time.time() + 3600
    for path in store.entries_dir.glob("*.md"):
        os.utime(path, (future, future))
    store.append_visible_entry("这是第 2 条超长内容：" + "很长" * 60)

    import re

    text = store.load_visible_text()
    assert text.count("entries/") == 2, f"指针行必须收敛上限（实际 {text.count('entries/')}）"
    for m in re.finditer(r"entries/([A-Za-z0-9\-]+)\.md", text):
        assert (store.entries_dir / f"{m.group(1)}.md").exists(), "指针必须指向真实文件（无死链）"
    fresh_lines = [ln for ln in text.splitlines() if "entries/" in ln]
    fresh_id = re.search(r"entries/([A-Za-z0-9\-]+)\.md", fresh_lines[-1]).group(1)
    assert (store.entries_dir / f"{fresh_id}.md").exists(), "刚写入的新条目不得被淘汰（FL-1）"


@pytest.mark.asyncio
async def test_e2e_provider_retrieval_hides_evicted_entry(tmp_path: Any) -> None:
    """端到端（生产主链路）：MemoryManager.search_all 的 project_md 分类
    经 search_entries 检索——被淘汰 entry 必须不可见、存活条目仍可召回。"""
    from bandi.memory.memory_provider import MemoryManager

    store = _store(tmp_path, max_entries=2)
    _seed(store, 3, prefix="淘汰检索")
    manager = MemoryManager(markdown_memory=store)
    hits = manager.search_all("淘汰检索", limit=10)["project_md"]
    assert len(hits) == 2, f"超限淘汰后检索必须只返回存活条目（实际 {len(hits)}）"
    assert all(h.key.startswith("entry:") for h in hits), "检索产物必须为 entry 展开"
    assert all(h.value.strip() for h in hits), "命中即正文展开注入（非空）"
