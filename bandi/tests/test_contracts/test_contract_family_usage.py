from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.company.session_scoping import project_from_session_id, scoped_member_session_id
from bandi.observability.cost_store import CostRecord, CostStore, fold_buckets_by

# 功能：契约——用量「按项目」维度（按员工改按项目）。
#
# 为什么需要派生而非台账直接记：台账只有 session_id，项目归属有两处来源——
# ① 员工执行会话 id 约定 `role-session::<项目>::…`（`scoped_member_session_id` 生成）；
# ② 会话元数据 `metadata.project_id`（项目内会话里与秘书/员工的直接对话）。
# 故以「按会话」分桶为中间量折叠合并（会话数远小于请求数）。
#
# 钉行为：前缀与元数据两路都能归位 / 合并后命中率按总量重算（不得相加或均值）/
# 缺省项目（`default`）不得伪造出一个叫 default 的项目 / 未归属落 (unassigned) /
# 非成员会话 id 形态不误判。


class TestProjectFromSessionId:
    """① 会话 id → 项目（生成/解析同文件单一约定）。"""

    def test_member_session_roundtrip(self) -> None:
        sid = scoped_member_session_id("选品项目", "thread-key", "designer", "emp-1", "sel-team")
        assert project_from_session_id(sid) == "选品项目"

    def test_default_project_is_not_a_project(self) -> None:
        """破坏者：生成侧 project_id 为空时写 `default`——那是「无归属」而非项目名。"""
        sid = scoped_member_session_id("", "thread-key", "designer", "emp-1", "sel-team")
        assert sid.startswith("role-session::default::")
        assert project_from_session_id(sid) == ""

    def test_plain_session_and_garbage_return_empty(self) -> None:
        for raw in ("sess-abc123", "", "role-session", "role-session::", "other::x::y"):
            assert project_from_session_id(raw) == "", f"{raw!r} 不得误判为项目"


class TestFoldBucketsByProject:
    """② 折叠合并语义。"""

    @staticmethod
    def _bucket(input_tokens: int, requests: int, read: int) -> dict[str, Any]:
        return {
            "requests": requests,
            "input_tokens": input_tokens,
            "output_tokens": 0,
            "cache_read_tokens": read,
            "cache_creation_tokens": 0,
            "cache_hit_rate": 0.0,
            "cost_cny": 0.0,
        }

    def test_same_project_sessions_merge_and_recompute_hit_rate(self) -> None:
        """破坏者：命中率必须在**合并后的总量**上重算——相加(0.9)或均值(0.45)都错。"""
        by_session = {
            "role-session::项目A::staff::dev::e1": self._bucket(1000, 1, 900),
            "role-session::项目A::staff::qa::e2": self._bucket(2000, 2, 0),
        }
        folded = fold_buckets_by(by_session, project_from_session_id)

        assert set(folded) == {"项目A"}
        bucket = folded["项目A"]
        assert bucket["requests"] == 3
        assert bucket["input_tokens"] == 3000
        assert bucket["cache_read_tokens"] == 900
        assert bucket["cache_hit_rate"] == pytest.approx(900 / 3000), "命中率须按合并后总量重算"

    def test_unattributed_falls_into_sentinel_bucket(self) -> None:
        by_session = {"sess-xyz": self._bucket(500, 1, 0)}
        folded = fold_buckets_by(by_session, project_from_session_id)
        assert set(folded) == {"(unassigned)"}

    def test_fold_is_empty_safe(self) -> None:
        assert fold_buckets_by({}, project_from_session_id) == {}


class TestUsageByProjectApi:
    """③ 生产装配路径：端点返回 by_project（含会话元数据来源）。"""

    @pytest.fixture
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        import importlib

        from fastapi.testclient import TestClient

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        from bandi.observability.cost_store import reset_shared_cost_stores

        reset_shared_cost_stores()
        app_mod = importlib.import_module("bandi.api.app")
        monkeypatch.setattr(app_mod, "_API_KEY", "usage-project-test-token", raising=False)
        monkeypatch.setattr(app_mod, "_store", None, raising=False)
        # 固化路径污染（全套件 xdist 下 KeyError，单跑绿）；与其他契约测试同款硬隔离。
        monkeypatch.setattr(
            app_mod,
            "_db_path",
            tmp_path / ".bandi" / "data" / "bandi.db",
            raising=False,
        )
        yield TestClient(app_mod.app, headers={"Authorization": "Bearer usage-project-test-token"})
        reset_shared_cost_stores()

    def _write(self, tmp_path: Path, **kwargs: Any) -> None:
        from bandi.observability.cost_store import get_shared_cost_store

        store = get_shared_cost_store(tmp_path / ".bandi" / "data" / "cost.db")
        store.enqueue(CostRecord(breakdown={"input_tokens_total": 100}, **kwargs))
        store.flush()

    def test_session_metadata_project_is_attributed(self, client: Any, tmp_path: Path) -> None:
        """项目内会话（sess-xxx + metadata.project_id）的对话调用必须归到该项目。"""
        from bandi.memory.session.store import SessionStore

        db = tmp_path / ".bandi" / "data" / "bandi.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        session_store = SessionStore(str(db))
        session_store.create_session("sess-proj-1", title="项目会话", project_id="选品项目")
        session_store.close()

        self._write(tmp_path, session_id="sess-proj-1", model="m1", agent_role="user")

        body = client.get("/api/usage/stats?range=all").json()
        assert body["by_project"]["选品项目"]["requests"] == 1, "会话元数据项目归属丢失"

    def test_role_session_prefix_wins_over_session_store(self, client: Any, tmp_path: Path) -> None:
        """破坏者：员工会话按 id 前缀归位（不依赖会话库是否记录了它）。"""
        self._write(
            tmp_path,
            session_id="role-session::项目B::staff::dev::emp-9",
            model="m1",
            agent_role="dev",
        )
        body = client.get("/api/usage/stats?range=all").json()
        assert "项目B" in body["by_project"]

    def test_default_project_does_not_become_a_bucket(self, client: Any, tmp_path: Path) -> None:
        """破坏者：缺省项目不得伪造出名为 `default` 的项目桶。"""
        self._write(
            tmp_path,
            session_id="role-session::default::staff::dev::emp-9",
            model="m1",
            agent_role="dev",
        )
        body = client.get("/api/usage/stats?range=all").json()
        assert "default" not in body["by_project"]
        assert body["by_project"]["(unassigned)"]["requests"] == 1


class TestAggregateParityWithBySession:
    """④ SQL 聚合与逐行扫描在 by_session 上必须一致（防两路径漂移）。"""

    def test_sql_and_plain_agree_on_by_session(self, tmp_path: Path) -> None:
        from bandi.observability.cost_store import _aggregate_rows

        store = CostStore(tmp_path / "cost.db")
        store.enqueue(
            CostRecord(
                session_id="s1",
                model="m1",
                agent_role="dev",
                input_tokens=10,
                output_tokens=1,
                breakdown={"input_tokens_total": 10},
            )
        )
        store.enqueue(
            CostRecord(
                session_id="s2",
                model="m1",
                agent_role="dev",
                input_tokens=20,
                output_tokens=2,
                breakdown={"input_tokens_total": 20},
            )
        )
        store.flush()
        rows = list(
            store._conn.execute(
                "SELECT model, agent_role, session_id, input_tokens, output_tokens,"
                " cache_tokens, total_cost_cny, breakdown FROM llm_request_log"
            ).fetchall()
        )
        assert store.aggregate_usage() == _aggregate_rows(rows)
        assert set(store.aggregate_usage()["by_session"]) == {"s1", "s2"}


# ==================== usage_stats_api ====================
# 功能：契约 — 用量与缓存聚合端点（后端）
# 目标：设置页「用量与缓存」的数据源 = GET /api/usage/stats?range=7d||all
# 契约：① 空台账返回零值空态（首次打开设置页不得 500）；
# ② 台账写入后聚合值与写入一致，命中率 = 命中/归一总量；
# ③ range 解析（7d//其它→全部）与响应契约字段齐备；
# ④ 走生产装配路径（真实 CostStore + 真实 FastAPI app，不自造对象）。


TOTAL_KEYS = {
    "requests",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cache_hit_rate",
}


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离用户主目录 + 复位进程级共享台账（防跨用例串扰）。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from bandi.observability.cost_store import reset_shared_cost_stores

    reset_shared_cost_stores()
    yield tmp_path
    reset_shared_cost_stores()


@pytest.fixture
def client(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """鉴权：显式注入 `_API_KEY` 并带同值 Bearer——不依赖环境里的 BANDI_API_KEY
    （TestClient 的 client host 是 'testclient'，走 localhost 分支必 401）。"""
    import importlib

    from fastapi.testclient import TestClient

    app_mod = importlib.import_module("bandi.api.app")
    monkeypatch.setattr(app_mod, "_API_KEY", "usage-stats-test-token", raising=False)
    return TestClient(app_mod.app, headers={"Authorization": "Bearer usage-stats-test-token"})


def test_usage_stats_empty_ledger_is_zero_state(client: Any) -> None:
    """契约①：台账不存在/为空时必须返回零值空态而不是 500（首次打开设置页）。"""
    resp = client.get("/api/usage/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert TOTAL_KEYS <= set(body["total"]), "总量契约字段必须齐备（前端按此渲染）"
    assert body["total"]["requests"] == 0
    assert body["total"]["cache_hit_rate"] == 0.0
    assert body["by_model"] == {} and body["by_project"] == {}


def test_usage_stats_range_parsing(client: Any) -> None:
    """契约③：range=7d/解析为天数；未知值回落 all（days=0）而不是报错。"""
    assert client.get("/api/usage/stats?range=7d").json()["days"] == 7
    assert client.get("/api/usage/stats?range=30d").json()["days"] == 30
    for raw in ("all", "bogus", ""):
        body = client.get(f"/api/usage/stats?range={raw}").json()
        assert body["days"] == 0, f"range={raw!r} 应回落全部"


def test_usage_stats_reflects_ledger_write(client: Any, isolated_home: Path) -> None:
    """契约②（生产装配路径）：真实 CostStore 写入 → 端点读到同值；
    命中率 = cache_tokens ÷ 归一总量；按项目归属取员工会话 id 前缀（生产形态）。"""
    from bandi.observability.cost_store import CostRecord, get_shared_cost_store

    store = get_shared_cost_store(isolated_home / ".bandi" / "data" / "cost.db")
    store.enqueue(
        CostRecord(
            session_id="role-session::选品项目::staff::designer::emp-1",
            model="deepseek-chat",
            agent_role="dev",
            input_tokens=1000,
            output_tokens=50,
            cache_tokens=900,
            total_cost_cny=0.01,
            breakdown={"input_tokens_total": 1000, "cache_creation_tokens": 0},
        )
    )
    store.flush()

    body = client.get("/api/usage/stats?range=all").json()
    total = body["total"]
    assert total["requests"] == 1
    assert total["input_tokens"] == 1000
    assert total["output_tokens"] == 50
    assert total["cache_read_tokens"] == 900
    assert total["cache_hit_rate"] == pytest.approx(0.9)
    assert body["by_model"]["deepseek-chat"]["requests"] == 1
    assert body["by_project"]["选品项目"]["cache_read_tokens"] == 900, (
        "员工执行会话（role-session::<项目>::…）必须归到该项目"
    )


def test_aggregate_never_exceeds_full_hit_rate(client: Any, isolated_home: Path) -> None:
    """契约②（破坏者视角）：Anthropic 行「命中 >> 原生 input_tokens」不得让
    命中率越界——分母必须是 breakdown 里的归一总量。"""
    from bandi.observability.cost_store import CostRecord, get_shared_cost_store

    store = get_shared_cost_store(isolated_home / ".bandi" / "data" / "cost.db")
    store.enqueue(
        CostRecord(
            model="claude-sonnet",
            agent_role="dev",
            input_tokens=120,  # Anthropic 口径：不含已缓存部分
            output_tokens=30,
            cache_tokens=6400,
            breakdown={"input_tokens_total": 6520, "cache_creation_tokens": 0},
        )
    )
    store.flush()

    total = client.get("/api/usage/stats").json()["total"]
    assert 0.0 <= total["cache_hit_rate"] <= 1.0
    assert total["cache_hit_rate"] == pytest.approx(6400 / 6520, rel=1e-6)
    assert total["input_tokens"] == 6520, "分母须为归一总量（不得用 120）"


def test_usage_stats_is_registered_in_openapi(client: Any) -> None:
    """契约③：端点必须进入 OpenAPI（前端/openapi 快照依赖）。"""
    spec = client.get("/openapi.json").json()
    assert "/api/usage/stats" in spec["paths"], "新端点未进 OpenAPI"
    assert "get" in spec["paths"]["/api/usage/stats"]
