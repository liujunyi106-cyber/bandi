#
# ① 首次 `record()` 会付一次性 ~0.6s 索引解析 ⇒ 改为启动后台预热 + 双检锁只解析一次；
# 纯函数降级为兜底；**两条路径必须逐字节一致**，否则同一份台账出现两个数字；
# ③ 修复前的历史行留「编造金额」⇒ 提供幂等的重算工具（干跑默认 + 自动备份）。
#
# 本契约的重点是**防漂移**：SQL 与纯函数、记账公式与重算公式，都必须同口径。

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from bandi.llm import models_dev
from bandi.observability import cost_store as cs
from bandi.observability.cost_store import CostRecord, CostStore, _aggregate_rows
from bandi.observability.reprice import plan_reprice, reprice_ledger

# ── 夹具：一份「形态齐全」的台账 ────────────────────────────────────────────


def _record(
    session_id: str,
    model: str,
    *,
    provider_name: str = "",
    role: str = "dev",
    agent_id: str = "",
    prompt: int = 1000,
    completion: int = 100,
    cache_tokens: int = 0,
    cost: float = 0.5,
    breakdown: dict[str, Any] | None = None,
    event_id: str = "",
) -> CostRecord:
    return CostRecord(
        session_id=session_id,
        provider_name=provider_name,
        model=model,
        agent_role=role,
        agent_id=agent_id,
        input_tokens=prompt,
        output_tokens=completion,
        cache_tokens=cache_tokens,
        total_cost_cny=cost,
        event_id=event_id or f"evt-{session_id}-{model}-{prompt}-{cache_tokens}",
        breakdown=breakdown or {},
    )


@pytest.fixture
def seeded_store(tmp_path: Path) -> CostStore:
    store = CostStore(tmp_path / "cost.db")
    store.enqueue(
        _record(
            "s1",
            "deepseek-v4.1-flash",
            role="dev",
            prompt=1000,
            cache_tokens=800,
            cost=0.25,
            breakdown={
                "input_tokens_total": 1000,
                "cache_creation_tokens": 0,
                "pricing_source": "catalog",
            },
        )
    )
    # 归一总量与列值不同：必须用 breakdown 的 1000，而不是列里的 500
    store.enqueue(
        _record(
            "s1",
            "deepseek-v4.1-flash",
            role="reviewer",
            prompt=500,
            completion=50,
            cost=0.1,
            breakdown={
                "input_tokens_total": 1000,
                "cache_creation_tokens": 128,
                "pricing_source": "catalog",
            },
        )
    )
    store.enqueue(
        _record(
            "s2",
            "my-custom-llm-v9",
            role="dev",
            prompt=200,
            cost=0.0,
            breakdown={"unpriced": True, "pricing_source": "unpriced"},
        )
    )
    # 空 model/role：聚合必须落到 'unknown' / 'unassigned'（两条路径同口径）
    store.enqueue(_record("s3", "", role="", prompt=10, cost=0.0, breakdown={}))
    store.flush()
    return store


def _raw_rows(store: CostStore) -> list[Any]:
    return list(
        store._conn.execute(
            "SELECT model, agent_role, session_id, input_tokens, output_tokens, cache_tokens,"
            " total_cost_cny, breakdown FROM llm_request_log ORDER BY rowid"
        ).fetchall()
    )


# ── ① SQL 路径 与 纯函数降级路径 必须一致 ───────────────────────────────────


def test_sql_aggregate_matches_pure_function(seeded_store: CostStore) -> None:
    """契约①（防漂移）：同一份台账，SQL 聚合与纯函数聚合必须**完全相等**。

    两者分别服务「生产主路径」与「降级兜底」，任何一处改动另一处不跟 ⇒ 同一用户
    在不同环境看到两个花费数字。这条把二者钉死。
    """
    sql_result = seeded_store.aggregate_usage()
    pure_result = _aggregate_rows(_raw_rows(seeded_store))
    assert sql_result == pure_result


def test_sql_path_is_the_primary(seeded_store: CostStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """契约①：**证明 SQL 路径真的在跑**——把纯函数炸掉，聚合仍必须正确。

    若 SQL 路径其实没被走到（例如异常被吞后总是回落），本用例会红。
    """
    expected = seeded_store.aggregate_usage()

    def _boom(*_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("纯函数路径不应被调用（说明 SQL 路径没生效）")

    monkeypatch.setattr(cs, "_aggregate_rows", _boom)
    assert seeded_store.aggregate_usage() == expected


def test_fallback_when_sql_path_fails(
    seeded_store: CostStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约①（破坏者）：SQL 侧异常时**必须**降级到纯函数，且结果一致（观测面绝不 500）。"""
    expected = seeded_store.aggregate_usage()

    def _boom(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("simulated: no JSON1 / broken query")

    monkeypatch.setattr(CostStore, "_aggregate_usage_sql", _boom)
    assert seeded_store.aggregate_usage() == expected


def test_malformed_breakdown_does_not_break_sql_path(seeded_store: CostStore) -> None:
    """契约①（破坏者）：**坏 JSON 行不得让整条聚合查询报错**（json_valid 守卫）。

    手工改库/旧版本写入都可能留下坏 breakdown；一旦 SQL 报错就整页失败。
    """
    seeded_store._conn.execute("UPDATE llm_request_log SET breakdown = 'not-json' WHERE model = ''")
    seeded_store._conn.commit()
    result = seeded_store.aggregate_usage()
    assert result["total"]["requests"] == 4
    assert result["by_model"]["unknown"]["requests"] == 1
    # 坏 JSON 行的归一总量回落列值（10）
    assert result["by_model"]["unknown"]["input_tokens"] == 10


def test_empty_model_and_role_fall_into_buckets(seeded_store: CostStore) -> None:
    """契约①：空 model/agent_role 归到 'unknown' / 'unassigned'（前端分组不得丢行）。"""
    result = seeded_store.aggregate_usage()
    assert "unknown" in result["by_model"]
    assert "unassigned" in result["by_role"]


def test_aggregate_empty_ledger_is_zero_state(tmp_path: Path) -> None:
    """契约①：空台账返回零值三桶（前端首次打开不得 500 / 不得缺键）。"""
    store = CostStore(tmp_path / "empty.db")
    result = store.aggregate_usage()
    assert result["total"]["requests"] == 0
    assert result["total"]["cache_hit_rate"] == 0.0
    assert result["by_model"] == {} and result["by_role"] == {}


def test_aggregate_range_filter(seeded_store: CostStore) -> None:
    """契约①：`days>0` 只统计窗口内的行（把该行改成很旧 ⇒ 7 天窗口必须排除它）。"""
    seeded_store._conn.execute(
        "UPDATE llm_request_log SET created_at = '2000-01-01 00:00:00' WHERE session_id = 's3'"
    )
    seeded_store._conn.commit()
    assert seeded_store.aggregate_usage(days=7)["total"]["requests"] == 3
    assert seeded_store.aggregate_usage(days=0)["total"]["requests"] == 4


# ── ② 计价公式唯一实现（记账 与 重算 同口径）──────────────────────────────


def test_compute_cost_matches_record(tmp_path: Path) -> None:
    """契约②（破坏者）：`compute_cost_cny`（重算用）与 `record()`（记账用）必须同值。

    重算若自写一套公式，两处迟早漂移——「重算出来的钱」与「新记账的钱」必须一致。
    """
    from bandi.llm.cost_tracker import CostTracker, compute_cost_cny
    from bandi.llm.model_catalog import ModelCatalog

    catalog = ModelCatalog()
    spec = catalog.get("deepseek-v4.1-flash")
    assert spec is not None
    tracker = CostTracker(catalog, budget_limit=-1.0)
    entry = tracker.record(
        session_id="s",
        provider="deepseek",
        model="deepseek-v4.1-flash",
        prompt_tokens=1000,
        completion_tokens=100,
        cache_read_tokens=400,
        cache_creation_tokens=100,
        input_tokens_total=1000,
    )
    expected = compute_cost_cny(
        spec.pricing,
        input_tokens_total=1000,
        cache_read_tokens=400,
        cache_creation_tokens=100,
        completion_tokens=100,
    )
    assert entry.cost_cny == pytest.approx(expected, abs=1e-12)


# ── ③ 历史台账重算：幂等 + 干跑 + 备份 ──────────────────────────────────────


def _legacy_db(tmp_path: Path) -> Path:
    """造一份「修复前」台账：无 pricing_source、金额是编造兜底价算出来的。"""
    path = tmp_path / "legacy.db"
    store = CostStore(path)
    store.enqueue(
        _record(
            "s-legacy",
            "xiaomi/mimo-v2.6-flash",
            provider_name="openrouter",
            role="user",
            prompt=31734,
            completion=12,
            cost=0.05032368,  # 旧硬编码兜底价 1.584/4.752 的结果
            breakdown={},
            event_id="evt-legacy",
        )
    )
    # 修复后的行（带 pricing_source）必须被跳过
    store.enqueue(
        _record(
            "s-new",
            "deepseek-v4.1-flash",
            cost=0.01,
            breakdown={"pricing_source": "catalog"},
            event_id="evt-new",
        )
    )
    # 已标记未定价的行也必须被跳过（没有金额可纠）
    store.enqueue(
        _record(
            "s-unpriced",
            "no-such-model",
            cost=0.0,
            breakdown={"unpriced": True},
            event_id="evt-unpriced",
        )
    )
    store.flush()
    store.close()
    return path


def test_plan_reprice_targets_only_legacy_rows(tmp_path: Path) -> None:
    """契约③：只挑「修复前」的行；已带 pricing_source 与已标记 unpriced 的一律跳过。"""
    from bandi.utils.db import connect_app_db

    path = _legacy_db(tmp_path)
    conn = connect_app_db(path, row_factory=True)
    try:
        rows = list(conn.execute("SELECT * FROM llm_request_log ORDER BY rowid").fetchall())
    finally:
        conn.close()

    seen: list[tuple[str, str]] = []

    def _resolver(provider: str, model: str) -> tuple[Any, str]:
        from bandi.llm.cost_tracker import CostTracker
        from bandi.llm.model_catalog import ModelCatalog

        seen.append((provider, model))
        return CostTracker._resolve_pricing(provider, model, ModelCatalog().get(model))

    plan = plan_reprice(rows, _resolver)
    assert plan.stats["scanned"] == 3
    assert plan.stats["skipped_reprised"] == 1
    assert plan.stats["skipped_unpriced"] == 1
    assert plan.stats["changed"] == 1
    assert [i.event_id for i in plan.items] == ["evt-legacy"]
    # 解析器只对「需要重算」的行调用（不浪费解析）
    assert seen == [("openrouter", "xiaomi/mimo-v2.6-flash")]
    item = plan.items[0]
    assert item.old_cost == pytest.approx(0.05032368, abs=1e-9)
    assert item.new_cost is not None and item.new_cost > 0


def test_reprice_dry_run_then_apply_with_backup(tmp_path: Path) -> None:
    """契约③：干跑不写库；--apply 才写，且**先备份**；再次 apply 幂等（无行可改）。"""
    path = _legacy_db(tmp_path)
    dry = reprice_ledger(path, apply=False)
    assert dry["applied"] is False and dry["changed"] == 1 and dry["backup"] == ""

    from bandi.utils.db import connect_app_db

    conn = connect_app_db(path, row_factory=True)
    try:
        before = conn.execute(
            "SELECT total_cost_cny FROM llm_request_log WHERE event_id = 'evt-legacy'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert before == pytest.approx(0.05032368, abs=1e-9), "干跑不得写库"

    applied = reprice_ledger(path, apply=True)
    assert applied["applied"] is True
    assert applied["backup"], "写库前必须留下备份路径"
    assert Path(applied["backup"]).exists()

    conn = connect_app_db(path, row_factory=True)
    try:
        row = conn.execute(
            "SELECT total_cost_cny, breakdown FROM llm_request_log WHERE event_id = 'evt-legacy'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == pytest.approx(applied["samples"][0]["new_cost_cny"], abs=1e-9)
    assert json.loads(row[1]).get("pricing_source"), "重算后必须留下定价来源（供审计）"

    again = reprice_ledger(path, apply=True)
    assert again["changed"] == 0, "重算必须幂等（第二次无行可改）"


# ── ④ 实时价索引：启动预热 + 并发只解析一次 ─────────────────────────────────


def test_index_is_parsed_once_under_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约④（破坏者）：并发调用**只允许解析一次** 5MB 缓存（双检锁 + 构建锁）。

    预热线程与首次查询同时到达时若各解析一遍，等于把一次性开销翻倍且浪费 CPU。
    """
    cache = tmp_path / "m.json"
    cache.write_text(
        json.dumps(
            {
                "providers": {"openrouter": {"models": {"m": {"cost": {"input": 1.0}}}}},
                "fetched_at": "2026-09-27T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    calls = {"n": 0}
    real_load = models_dev.load_cache

    def _counting_load(path: Any) -> dict[str, Any] | None:
        calls["n"] += 1
        import time as _t

        _t.sleep(0.05)  # 放大竞态窗口
        return real_load(path)

    monkeypatch.setattr(models_dev, "load_cache", _counting_load)
    monkeypatch.setattr(models_dev, "_INDEX_CACHE", None)

    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda: results.append(models_dev.provider_price_index(cache)))
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls["n"] == 1, f"缓存被解析 {calls['n']} 次（应只 1 次）"
    assert all(result is results[0] for result in results)


def test_agent_id_attribution_and_old_ledger_migration(tmp_path: Path) -> None:
    """agent_id 必须可归因，且旧台账启动时自动补列（不丢历史行）。"""
    import sqlite3

    old_db = tmp_path / "old-cost.db"
    old_schema = cs._SCHEMA.replace("    agent_id             TEXT    NOT NULL DEFAULT '',\n", "")
    assert "agent_id" not in old_schema
    conn = sqlite3.connect(old_db)
    conn.execute(old_schema)
    conn.commit()
    conn.close()

    store = CostStore(old_db)
    try:
        store.enqueue(
            _record(
                "s-agent",
                "deepseek-v4.1-flash",
                agent_id="engineer@s-agent",
                prompt=10,
                completion=5,
                cost=0.1,
            )
        )
        store.flush()
        rows = store.list_records(limit=1)
        assert rows and rows[0]["agent_id"] == "engineer@s-agent"
        aggregate = store.aggregate_usage()
        assert aggregate["by_agent"]["engineer@s-agent"]["requests"] == 1
    finally:
        store.close()


def test_warm_provider_price_index_starts_background_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """契约④：预热只**启动后台线程**，绝不阻塞调用方。"""
    done = threading.Event()

    def _fake_index(*_: Any, **__: Any) -> dict[str, Any]:
        done.set()
        return {}

    monkeypatch.setattr(models_dev, "provider_price_index", _fake_index)
    models_dev.warm_provider_price_index()  # 立即返回（不等待解析）
    assert done.wait(timeout=5), "预热线程未执行"
