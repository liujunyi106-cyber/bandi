"""功能：无人值守路径的记忆晋升必须先进草稿池，不得直插审批队列。

真机现象（`~/.bandi/memory/pending/memory` 183 条 review）：

    background 151 条（promote_episode）  value_score 全部 = 0
    foreground  32 条（store_long_term）  value_score 全部 >= 0.7

后台自动化任务每跑一次就产出一条晋升候选，直接进审批队列且价值分为 0，
于是队列无限堆积——用户看到「记忆审批两百多条」，逐条点开全是后台自己
跑出来的过程记录。

对照 `agent_runner` 里的前台写入（正确模式）：

    gate=GATE_REVIEW if conflict else GATE_DRAFT
    value_score=value_score
    promote_memory_drafts()      # 由草稿池按价值分晋级

契约钉住后台两条路径与前台同构：进 draft、带上价值分、不绕过晋级逻辑。
"""

from __future__ import annotations

from bandi.memory import consolidation, maintenance
from bandi.memory import staging as staging_module
from bandi.memory.staging import DEFAULT_DRAFT_MIN_SCORE, GATE_DRAFT


class _Episode:
    """功能：情节替身——只带晋升需要的字段。"""

    def __init__(self, importance: float = 0.9) -> None:
        self.episode_id = "ep-test"
        self.importance = importance
        self.project_id = None
        self.employee_id = None


class _Item:
    def __init__(self) -> None:
        self.content = "结论：按含税口径出报表。"
        self.metadata: dict = {}
        self.importance = 0.9

    def to_dict(self) -> dict:
        return {"id": "it-1", "content": self.content}


class _CapturingStore:
    """功能：记录 stage() 收到的字段，不落盘。"""

    def __init__(self) -> None:
        self.fields: dict | None = None
        self.persisted = True

    def stage(self, fields: dict) -> object:
        self.fields = fields

        class _Rec:
            persisted = True

        return _Rec()


def test_consolidation_stages_into_draft(monkeypatch) -> None:
    """功能：整合晋升必须落 draft，不能直接落 review。"""
    store = _CapturingStore()
    monkeypatch.setattr(staging_module, "get_pending_store", lambda: store)
    consolidation._stage_consolidation(_Item(), _Episode())  # type: ignore[arg-type]
    assert store.fields is not None, "未调用 stage()"
    assert store.fields.get("gate") == GATE_DRAFT, (
        f"整合晋升 gate={store.fields.get('gate')!r}，应为 draft"
    )


def test_maintenance_stages_into_draft() -> None:
    """功能：维护晋升（真机 151 条的来源）必须落 draft。"""
    store = _CapturingStore()
    maintenance._stage_promotion(store, _Item(), _Episode())  # type: ignore[arg-type]
    assert store.fields is not None, "未调用 stage()"
    assert store.fields.get("gate") == GATE_DRAFT, (
        f"维护晋升 gate={store.fields.get('gate')!r}，应为 draft"
    )


def test_maintenance_records_value_score() -> None:
    """功能：维护晋升必须带上情节价值分，否则草稿池无法按分筛选。"""
    store = _CapturingStore()
    maintenance._stage_promotion(store, _Item(), _Episode(importance=0.85))  # type: ignore[arg-type]
    assert store.fields is not None
    assert float(store.fields.get("value_score") or 0.0) == 0.85, (
        f"value_score={store.fields.get('value_score')!r}，应取情节 importance"
    )


def test_consolidation_records_value_score(monkeypatch) -> None:
    """功能：整合晋升同样必须带价值分（真机这 151 条全是 0）。"""
    store = _CapturingStore()
    monkeypatch.setattr(staging_module, "get_pending_store", lambda: store)
    consolidation._stage_consolidation(_Item(), _Episode(importance=0.72))  # type: ignore[arg-type]
    assert store.fields is not None
    assert float(store.fields.get("value_score") or 0.0) == 0.72


def test_background_paths_never_use_review_gate() -> None:
    """功能：后台路径不得出现直插 review 的写法（破坏者视角：绕过草稿池）。"""
    import inspect

    for module, func in (
        (consolidation, consolidation._stage_consolidation),
        (maintenance, maintenance._stage_promotion),
    ):
        source = inspect.getsource(func)
        assert "GATE_REVIEW" not in source, (
            f"{module.__name__} 仍在后台路径里直插 review"
        )


def test_draft_threshold_is_below_typical_importance() -> None:
    """功能：草稿池阈值必须能被真实情节价值分跨过，否则晋升永远卡死在草稿。"""
    assert DEFAULT_DRAFT_MIN_SCORE < 0.9, (
        f"min_score={DEFAULT_DRAFT_MIN_SCORE} 高于常见 importance，草稿将永不晋级"
    )
