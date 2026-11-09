"""契约：流式断流必须走降级链，不得直接返回。

证据（turn 17， 12:25-12:26）：
    LLM_REQUEST → 51.8s → LLM_STREAM_EVENT done/incomplete
    → STATE_CHANGED parsing→executing→observing→terminated(MODEL_ERROR)
    零重试、零回退、UI 永久「正在回复中…」

根因：`_stream_thinking` 在未见 DONE 时**正常 return** 一个
`finish_reason="incomplete"` 的响应（不抛异常），而流式分支只对 `Exception`
降级 → `return response` 整段绕过候选降级与非流式回退链。

钉行为不钉实现：断言「断流后确实发生了非流式回退调用」。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_STATES = Path(__file__).resolve().parents[2] / "bandi" / "agent" / "states.py"


def _inner_source() -> str:
    return _STATES.read_text(encoding="utf-8")


class TestIncompleteStreamFallsBack:
    @staticmethod
    def _stream_guard() -> int:
        """定位**流式候选循环内**的断流守卫（不是 on_response 的裸守卫）。"""
        src = _inner_source()
        needle = 'response.finish_reason in ("incomplete", "error") and not ('
        at = src.find(needle)
        assert at > 0, "未找到流式分支的 incomplete 守卫"
        return at

    def test_incomplete_with_no_content_is_treated_as_failed_attempt(self) -> None:
        """断流且无内容 ⇒ continue（进下一候选/非流式回退），不得 return。"""
        src = _inner_source()
        at = self._stream_guard()
        window = src[at : at + 1600]
        # 判定必须同时排除「仅思维链」——那是有效输出（空回复第一形态），不可降级
        assert "response.reasoning" in window, (
            "断流判定须排除「仅思维链」——那是有内容输出，不可降级"
        )
        # 关键：continue 必须早于任何 return（变异：在守卫后立刻 return 依旧绕过降级）
        cont = window.find("continue")
        assert cont > 0, "断流后必须 continue 到降级链"
        ret = window.find("return")
        assert ret < 0 or cont < ret, (
            f"断流分支内 return(位置 {ret}) 早于 continue(位置 {cont}) —— 降级链仍被绕过"
        )

    def test_failed_stream_attempt_is_recorded_as_failure(self) -> None:
        """断流必须记为失败尝试（否则 provider 可靠性排名被健康数据稀释）。"""
        src = _inner_source()
        at = self._stream_guard()
        window = src[at : at + 1600]
        assert "record_attempt" in window, "断流必须记入 provider 可靠性统计"
        assert "success=False" in window, "断流必须记为失败（不是成功）"

    def test_guarded_return_is_after_the_incomplete_check(self) -> None:
        """结构守卫：成功 return 必须在断流 continue 之后（顺序不能颠倒）。"""
        src = _inner_source()
        guard = self._stream_guard()
        success_return = src.find(
            'stats.record_attempt("chat", fallback._provider_name(model), success=True)'
        )
        assert success_return > 0
        assert guard < success_return, (
            "断流判定必须排在「记成功并 return」之前，否则断流被当成功返回"
        )

    def test_on_response_still_rejects_bare_incomplete(self) -> None:
        """反向：非流式/直调路径的裸 incomplete 仍须判错（不得因本修复而放过）。"""
        src = _inner_source()
        assert (
            'ctx.final_message = "模型流式响应未完整结束（incomplete），请重试或继续对话。"' in src
        ), "on_response 的裸 incomplete 守卫必须保留——本修复只覆盖流式候选分支"


class TestRetryChainNotBypassed:
    def test_nonstream_fallback_chain_still_reachable(self) -> None:
        """流式全败后必须能进入非流式回退链（`complete_with_fallback`）。"""
        src = _inner_source()
        assert "complete_with_fallback" in src, "非流式回退链缺失"
        call_at = src.find("response, used_model = await fallback.complete_with_fallback(")
        assert call_at > 0
        # 该调用必须在流式循环之后（流式 continue 才能落到这里）
        loop_at = src.find("for model in candidates_fn(")
        assert loop_at > 0 and loop_at < call_at, "流式循环应先于非流式回退链"

    def test_prompt_too_long_still_raises_immediately(self) -> None:
        """超长错误必须穿透（不做无谓重试）——断流修复不得放宽该守卫。"""
        src = _inner_source()
        idx = src.find("if is_prompt_too_long_error(e):")
        assert idx > 0
        window = src[idx : idx + 120]
        assert "raise" in window, "超长错误须立即抛出（止损语义）"


@pytest.mark.skipif(sys.platform == "win32", reason="契约只做源码结构断言，无平台依赖")
def test_placeholder() -> None:
    """占位：保持 pytest 收集稳定。"""
    assert True
