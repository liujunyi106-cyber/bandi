"""契约：cost-detail 所有返回路径必须同形。

缺陷实证：空态早返回 `{"tool_stats": {}, "session_cost": {}}` 与正常返回的
扁平 `{session_id, total_tokens, total_cost_cny, per_model, tool_stats}` 错配
前端 `cost.total_cost_cny.toFixed(2)` 在空态读到 undefined → **整个面板崩溃**
（`Cannot read properties of undefined (reading 'toFixed')`）。

根因类别：**旁路/早返回未与主路径共用同一形状契约**。本契约按「键集合相等」
钉死，不逐字段比对数值——这样任何新增返回路径都必须显式声明同形。
"""

from __future__ import annotations

from pathlib import Path

import pytest

app_mod = __import__("importlib").import_module("bandi.api.app")

_NORMAL_KEYS = {"session_id", "total_tokens", "total_cost_cny", "per_model", "tool_stats"}


class _StubSessionCost:
    total_tokens = 123
    total_cost_cny = 0.5
    per_model = {"m": 0.5}


class _StubTracker:
    def get_session_cost(self, _session_id: str) -> _StubSessionCost:
        return _StubSessionCost()

    def tool_token_stats(self) -> dict:
        return {}


def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离主目录 + 复位共享台账——端点自 起读**落库**台账
    不隔离会读到真实 `~/.bandi/data/cost.db`（族测试污染）。"""
    from bandi.observability.cost_store import reset_shared_cost_stores

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    reset_shared_cost_stores()


def _empty_tracker_case(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, tracker_present: bool
) -> dict:
    """构造两种 tracker 状态，返回端点实际响应。

    注意：`session_cost_detail` 在**函数体内**从 `bandi.llm.cost_tracker` 局部导入
    `_shared_cost_tracker`，故须 patch 源模块属性（patch app 模块无效）。
    """
    import asyncio

    import bandi.llm.cost_tracker as ct

    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setattr(ct, "_shared_cost_tracker", _StubTracker() if tracker_present else None)
    return asyncio.run(app_mod.session_cost_detail("sess-shape-probe"))


class TestCostDetailShapeParity:
    def test_empty_state_has_same_keys_as_normal_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        empty = _empty_tracker_case(monkeypatch, tmp_path, tracker_present=False)
        normal = _empty_tracker_case(monkeypatch, tmp_path, tracker_present=True)
        assert set(empty.keys()) == set(normal.keys()), (
            f"空态与正常态键集合必须一致；空态多/少: {set(empty) ^ set(normal)}"
        )
        assert set(empty.keys()) == _NORMAL_KEYS, f"cost-detail 契约形状被改动: {sorted(empty)}"

    def test_empty_state_numeric_fields_are_numbers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """破坏者视角：把 `total_cost_cny` 留空/删掉，本组必须红。"""
        empty = _empty_tracker_case(monkeypatch, tmp_path, tracker_present=False)
        for key in ("total_tokens", "total_cost_cny"):
            assert key in empty, f"空态缺字段 {key}（前端会读到 undefined）"
            assert isinstance(empty[key], (int, float)), (
                f"空态 {key} 必须是数值，前端对其调 toFixed/number 格式化；实际 {empty[key]!r}"
            )
        assert empty["total_cost_cny"] == 0.0
        assert empty["total_tokens"] == 0
        assert empty["per_model"] == {} and empty["tool_stats"] == {}

    def test_empty_state_reports_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        empty = _empty_tracker_case(monkeypatch, tmp_path, tracker_present=False)
        assert empty["session_id"] == "sess-shape-probe", (
            "空态仍须回传 session_id（前端按此键缓存/对账）"
        )

    def test_no_legacy_nested_shape(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """钉住不再回退到嵌套 `session_cost` 形状（缺陷的直接表征）。"""
        empty = _empty_tracker_case(monkeypatch, tmp_path, tracker_present=False)
        assert "session_cost" not in empty, "不得返回嵌套 session_cost 键（FM-98 缺陷形状）"


class TestCostDetailRealEmptySession:
    @pytest.mark.asyncio
    async def test_unseen_session_returns_typed_zeros(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """未知会话（tracker 在、但无记录）也须返回可安全渲染的结构。"""
        import bandi.llm.cost_tracker as ct

        _isolated_home(monkeypatch, tmp_path)
        real = ct._shared_cost_tracker
        if real is None:
            pytest.skip("共享 tracker 未初始化（空态已由上方用例覆盖）")
        result = await app_mod.session_cost_detail("sess-never-seen-xyz")
        assert set(result.keys()) == _NORMAL_KEYS
        assert isinstance(result["total_cost_cny"], (int, float))
        assert isinstance(result["total_tokens"], (int, float))


class TestCostDetailSurvivesRestart:
    """实锤：内核重启后同一会话用量曾显示 0（内存账本归零）
    而台账里明明有记录——本组钉「落库口径优先」。"""

    @pytest.mark.asyncio
    async def test_ledger_rows_visible_with_empty_memory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import bandi.llm.cost_tracker as ct
        from bandi.observability.cost_store import CostRecord, get_shared_cost_store

        _isolated_home(monkeypatch, tmp_path)
        store = get_shared_cost_store(tmp_path / ".bandi" / "data" / "cost.db")
        store.enqueue(
            CostRecord(
                session_id="sess-restart",
                model="m1",
                agent_role="dev",
                input_tokens=1000,
                output_tokens=50,
                breakdown={"input_tokens_total": 1000},
            )
        )
        store.flush()
        monkeypatch.setattr(ct, "_shared_cost_tracker", None)  # 重启后内存为空

        result = await app_mod.session_cost_detail("sess-restart")
        assert result["total_tokens"] == 1050, "落库用量必须在内存归零后仍可读"
        assert result["per_model"]["m1"]["tokens"] == 1050
        assert set(result.keys()) == _NORMAL_KEYS
