# 功能：契约测试——search_unified 跨分类 score 归一化
"""契约：MemoryManager.search_unified 跨分类统一排序前
long_term/episodic 的 score 含未限幅 importance（可 >1），其量纲上限高而系统性
压过 working/project_md（恒为 relevance∈[0,1]）——即使后者查询相关度满格。
归一化后同量纲下比较，破坏者（importance 高但 relevance 低的 episodic）不得
压过满相关 working。

说明：此契约验证的是 classified 排序路径（_unit_score 归一 + _CATEGORY_PRIORITY
破平）的确定性语义——两分类 score 归一后同分，由分类优先级打破平手。self 自
起默认 scoring="fusion"，fusion 主排序键为 _fusion_score（importance 参与加
权，episodic 在此例中合法压过 working），故本契约显式传入 scoring="classified"
以钉住其声明的那条路径。"""

from pathlib import Path

import pytest

from bandi.memory.memory_provider import MemoryManager


def test_high_importance_low_relevance_episodic_does_not_outrank_full_relevance_working(
    tmp_path: Path,
) -> None:
    """契约（classified 路径）：episodic 因 importance 超 1 量纲抬到 1.5 的 score
    归一化到 [0,1]（_unit_score(1.5)=1.0）后与满相关 working（_unit_score(1.0)=1.0）
    同分，由 _CATEGORY_PRIORITY（working=3 > episodic=2）打破，working 在前。"""
    from bandi.memory.episodic import Episode
    from bandi.memory.long_term import LongTermMemory
    from bandi.memory.working import ScratchHit

    lt = LongTermMemory(str(tmp_path / "lt.db"))

    class _Episodic:
        def search(self, q, limit=5, project_id=None, employee_id=None):
            ep = Episode(
                summary="高重要性但低相关的冷门经历摘要",
                episode_id="ep-high-imp",
                importance=3.0,
            )
            # importance=3 → relevance(0.5)*importance(3)*decay(1.0)=1.5 超额量纲
            return [(ep, 1.5)]

        def note_recall(self, episode_id: str, query: str | None = None) -> None:
            return None

    class _Working:
        def search(self, q, limit=5, session_id=""):
            # 满相关 relevance=1.0，无 importance 放大
            return [(ScratchHit("kv", "kw1", "满相关工作草稿片段内容", ""), 1.0)]

    mgr = MemoryManager(long_term=lt, episodic=_Episodic(), working=_Working())
    out = mgr.search_unified("蓝鲸", session_id="s1", scoring="classified")
    values = [getattr(x, "value", "") for x in out]

    ep_idx = next(i for i, v in enumerate(values) if "冷门经历" in v)
    wk_idx = next(i for i, v in enumerate(values) if "工作草稿" in v)
    assert wk_idx < ep_idx, "满相关 working 必须排在量纲抬高的 episodic 前（归一化后 score 可比值）"


def test_unit_score_clamps_negative_and_over_one(tmp_path: Path) -> None:
    """契约：_unit_score 把任意量纲 score 夹逼到 [0,1]，负值/超 1 均归一。"""
    from bandi.memory.memory_provider import _unit_score

    assert _unit_score(1.5) == 1.0
    assert _unit_score(-0.5) == 0.0
    assert _unit_score(0.42) == pytest.approx(0.42)


def test_fusion_default_episodic_importance_can_outrank_working(tmp_path: Path) -> None:
    """契约（fusion 默认路径）：默认 scoring="fusion" 下 _fusion_score 参与 importance
    加权，高重要性 episodic 可合法压过满相关 working——与 classified 契约互斥且并存；
    防「测试按 classified 语义写、默认却走 fusion」这类陈旧默认错位。"""
    from bandi.memory.episodic import Episode
    from bandi.memory.long_term import LongTermMemory
    from bandi.memory.working import ScratchHit

    lt = LongTermMemory(str(tmp_path / "lt.db"))

    class _Episodic:
        def search(self, q, limit=5, project_id=None, employee_id=None):
            ep = Episode(
                summary="高重要性但低相关的冷门经历摘要",
                episode_id="ep-high-imp",
                importance=3.0,
            )
            return [(ep, 1.5)]

        def note_recall(self, episode_id: str, query: str | None = None) -> None:
            return None

    class _Working:
        def search(self, q, limit=5, session_id=""):
            return [(ScratchHit("kv", "kw1", "满相关工作草稿片段内容", ""), 1.0)]

    mgr = MemoryManager(long_term=lt, episodic=_Episodic(), working=_Working())
    out = mgr.search_unified("蓝鲸", session_id="s1")
    values = [getattr(x, "value", "") for x in out]

    ep_idx = next(i for i, v in enumerate(values) if "冷门经历" in v)
    wk_idx = next(i for i, v in enumerate(values) if "工作草稿" in v)
    assert ep_idx < wk_idx, "fusion 默认下高重要性 episodic 压过满相关 working（importance 加权）"
