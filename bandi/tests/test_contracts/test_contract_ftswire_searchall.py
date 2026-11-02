# 功能：补齐契约（项11）——MemoryManager.search_all 的 long_term 检索路径
# 必须路由到 FTS5（中文 2-gram recall + relevance），并在关键词命中时保持
# 既有重要性排序、维度隔离、空召回 LIKE 兜底等可观测不变式。
from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.memory_provider import MemoryManager


def _manager_with_lt(items: dict[str, float]) -> MemoryManager:
    lt = LongTermMemory()
    for item_id, importance in items.items():
        lt.store(
            MemoryItem(
                item_id=item_id, content=item_id + " 灰度发布回滚预案", importance=importance
            )
        )
    return MemoryManager(long_term=lt)


def test_search_all_long_term_cjk_bigram_recall() -> None:
    """项11：search_all 的 long_term 分类经 FTS5 召回中文 bigram 命中。"""
    mm = _manager_with_lt({"lt1": 0.5})
    try:
        results = mm.search_all("回滚", limit=5)
        assert "long_term" in results
        assert [h.key for h in results["long_term"]] == ["lt1"], (
            f"中文 bigram 必须经 search_all 召回（实际 {results}）"
        )
    finally:
        mm.close()


def test_search_all_long_term_fts_importance_ordering() -> None:
    """项11：FTS 召回后仍保持 importance 降序（压缩纪要 0.7 优先于普通 0.5）。"""
    mm = _manager_with_lt({"low": 0.5, "high": 0.9})
    try:
        results = mm.search_all("灰度", limit=5)
        keys = [h.key for h in results["long_term"]]
        assert keys[0] == "high", f"高重要性必须优先（实际 {keys}）"
    finally:
        mm.close()


def test_search_all_long_term_scope_isolated() -> None:
    """项11：FTS 召回同样遵守 project/employee 维度隔离（全局不泄漏维度行）。"""
    lt = LongTermMemory()
    lt.store(MemoryItem(item_id="g1", content="g1 灰度配置"))
    lt.store(MemoryItem(item_id="d1", content="d1 灰度配置"), employee_id="e1")
    mm = MemoryManager(long_term=lt)
    try:
        global_hits = mm.search_all("灰度", limit=5)["long_term"]
        assert [h.key for h in global_hits] == ["g1"], "全局检索不得命中维度行"
        dim_hits = mm.search_all("灰度", limit=5, project_id=None, employee_id="e1")["long_term"]
        assert [h.key for h in dim_hits] == ["d1"], "employee 维度检索必须命中 d1"
    finally:
        mm.close()


def test_search_all_long_term_empty_recall_falls_back() -> None:
    """项11：FTS 空召回时回退 LIKE 兜底（保召回，不丢可发现性）。"""
    lt = LongTermMemory()
    # 内容含查询但 FTS 预处理后无匹配（异常标点场景）——LIKE 兜底须召回
    lt.store(MemoryItem(item_id="x1", content="特殊^^标记内容 #tag"))
    mm = MemoryManager(long_term=lt)
    try:
        results = mm.search_all("#tag", limit=5)
        assert results["long_term"], "FTS 空召回必须回退 LIKE 兜底"
    finally:
        mm.close()


def test_fts_failure_fallback_no_deadlock() -> None:
    """项11（新增回归）：FTS MATCH 异常回退必须死锁安全——回退在锁外执行
    不二次获取非可重入锁；空查询短路由不触发 FTS 噪音。"""
    from bandi.memory.long_term import LongTermMemory, MemoryItem

    lt = LongTermMemory()
    lt.store(MemoryItem(item_id="x1", content="可检索内容"))
    try:
        assert [h.id for h in lt.search_fts("", limit=5)] == ["x1"]
        # 空查询经 search_all 干净返回（search_fts_ranked 短路，不触发 FTS 失败噪音）
        mm = MemoryManager(long_term=lt)
        try:
            assert mm.search_all("", limit=5)["long_term"] == []
        finally:
            mm.close()
    finally:
        lt.close()


def test_search_fts_ranked_available_on_long_term() -> None:
    """项11：search_all 的长期记忆路径经 search_fts_ranked 提供 FTS 召回能力。"""
    lt = LongTermMemory()
    lt.store(MemoryItem(item_id="lt1", content="lt1 灰度发布回滚预案演练"))
    try:
        hits = lt.search_fts_ranked("回滚", limit=5)
        assert [h.id for h in hits] == ["lt1"]
    finally:
        lt.close()


def test_close_is_serialized_under_lock() -> None:
    """并发崩溃契约（项11 回归）：close 必须与查询持同一把 _lock——prefetch 经
    asyncio.to_thread 并发检索时，主线程 close 若不在锁内关连接会撞上查询中的
    sqlite 连接（原生 use-after-free 崩溃）。本契约确定性地验证 close 尊重锁：
    另一线程持锁期间 close 必须阻塞（不立即关连接），锁释放后才完成。"""
    import threading

    import bandi.memory.long_term as ltmod

    lt = ltmod.LongTermMemory()
    lt.store(ltmod.MemoryItem(item_id="lt1", content="lt1 灰度发布回滚预案演练"))
    entry = threading.Event()  # 工作线程已拿到锁
    release = threading.Event()  # 主线程放行工作线程释放锁

    def _hold_lock() -> None:
        with lt._lock:  # 模拟并发查询持锁期间
            entry.set()
            release.wait(5)

    worker = threading.Thread(target=_hold_lock)
    worker.start()
    assert entry.wait(5), "工作线程未取得锁"

    closed_holder: list[bool] = []

    def _close() -> None:
        lt.close()
        closed_holder.append(True)

    closer = threading.Thread(target=_close)
    closer.start()
    closer.join(timeout=0.5)
    assert not closed_holder, (
        "close 在锁被占用期间不得返回——若不持锁将立即关连接，与并发查询竞态"
        "导致原生崩溃（应阻塞到锁释放）"
    )
    release.set()
    worker.join(timeout=5)
    closer.join(timeout=5)
    assert closed_holder, "锁释放后 close 应完成关连接"
