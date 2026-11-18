"""功能：记忆草稿提炼必须跟着全局模型走，且模型判定「不值得记」时不许退回规则。

契约钉三件行为：
  ① 判官档案解析 = 当前活跃模型（换模型即换判官，不需要单独配置）
  ② 模型说 skip 的候选被丢弃，不能被规则提炼救成一条事实
  ③ 模型不可用时退化为规则提炼，候选不丢
"""

from __future__ import annotations

from dataclasses import dataclass

from bandi.memory.distill import (
    DistilledMemory,
    DistillerSkippedError,
    build_llm_distiller,
    distill_for_episode,
)
from bandi.memory.judge_model import resolve_judge_profile


@dataclass
class _Episode:
    summary: str
    importance: float = 0.6


def test_judge_profile_follows_active_model() -> None:
    """功能：判官解析结果必须等于当前全局活跃模型。"""
    from bandi.llm.profiles import ProfileStore

    active = ProfileStore().active_profile()
    resolved = resolve_judge_profile()
    if active is None:
        # 无活跃档案时允许退到配置默认模型或 None，但不得抛异常
        assert resolved is None or resolved.get("model")
        return
    assert resolved is not None, "有活跃档案却解析不出判官"
    assert resolved["model"] == active.model, "判官模型必须与全局活跃模型一致"
    assert resolved["provider"] == active.provider


def test_distiller_marks_model_output_as_llm() -> None:
    """功能：模型提炼成功时 distilled_by 必须是 llm，不是 rule。"""

    def _complete(prompt: str) -> str:
        return '{"kind":"事实","key":"青龙端口","content":"端口 7421","importance":0.8}'

    result = distill_for_episode(_Episode("端口 7421"), build_llm_distiller(_complete))
    assert isinstance(result, DistilledMemory)
    assert result.distilled_by == "llm"
    assert result.key == "青龙端口"


def test_model_skip_is_not_rescued_by_rules() -> None:
    """功能：模型判定不值的候选必须丢弃——规则不得把它救成一条事实。"""

    def _complete(prompt: str) -> str:
        return '{"skip": true}'

    result = distill_for_episode(
        _Episode("步骤 · 读 out/a.csv，把前 3 名写到 out/b.txt"),
        build_llm_distiller(_complete),
    )
    assert result.kind == "discard", "模型说 skip 却被规则提炼成了记忆"
    assert result.distilled_by == "llm_skip"
    assert result.content == ""


def test_skip_sentinel_is_distinguishable_from_failure() -> None:
    """功能：skip 与「模型坏了」必须是两种异常，否则前者会被当成后者降级。"""

    def _skip(prompt: str) -> str:
        return '{"skip": true}'

    distiller = build_llm_distiller(_skip)
    try:
        distiller(_Episode("随便一段"))
    except DistillerSkippedError:
        pass
    else:  # pragma: no cover - 只有语义退化时才会走到
        raise AssertionError("模型 skip 必须抛 DistillerSkippedError")


def test_broken_model_falls_back_to_rules() -> None:
    """功能：模型不可用时退化为规则提炼，候选不能丢。"""

    def _boom(prompt: str) -> str:
        raise RuntimeError("network down")

    result = distill_for_episode(_Episode("端口 7421 固定不变"), build_llm_distiller(_boom))
    assert result.kind != "discard", "模型故障不该把候选判成丢弃"
    assert result.distilled_by == "rule"


def test_invalid_json_falls_back_to_rules() -> None:
    """功能：模型输出不是 JSON 时退化为规则提炼。"""

    def _junk(prompt: str) -> str:
        return "这不是 JSON"

    result = distill_for_episode(_Episode("端口 7421 固定不变"), build_llm_distiller(_junk))
    assert result.distilled_by == "rule"


def test_key_must_be_natural_chinese_phrase() -> None:
    """功能：模型抄源文字段名（如 caliber）不能当 key——应退回规则。"""

    def _complete(prompt: str) -> str:
        return '{"kind":"fact","key":"caliber","content":"x","importance":0.5}'

    result = distill_for_episode(_Episode("关税变化影响价格"), build_llm_distiller(_complete))
    assert result.key != "caliber", "字段名被当成了 key"
    assert result.distilled_by == "rule"


def test_key_accepts_chinese_phrase() -> None:
    """功能：合法中文 key 必须被接受，不能误杀。"""

    def _complete(prompt: str) -> str:
        return '{"kind":"事实","key":"美国宠物喂食器关税变化","content":"关税上调","importance":0.7}'

    result = distill_for_episode(_Episode("关税变化"), build_llm_distiller(_complete))
    assert result.distilled_by == "llm"
    assert result.key == "美国宠物喂食器关税变化"
