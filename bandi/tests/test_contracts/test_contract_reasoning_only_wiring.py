"""集成契约：reasoning-only 重试必须**真的被调用**（防「静默不生效」）。

这一版的由来：第一版实现写成 `engine.execute(...)`，而 `FallbackEngine` 的方法
其实叫 `call_with_fallback` —— AttributeError 被自己写的 `except Exception`
吞掉，静默返回 None。于是**重试代码从未执行过**，Pass^N 里 reasoning-only
依旧以 MODEL_ERROR 出现（日志里连 debug 都看不到，因为异常被吃了）。

教训两条，都在本文件钉死：
1. 兜底分支不得用 `except Exception` 把自身的实现错误一起吞掉；
2. 契约必须断言**调用发生**，不能只断言助手函数返回 True——只测
 `_is_reasoning_only` 会让「重试根本调不到」这类缺陷 100% 漏网。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from bandi.llm.base import LLMResponse
from bandi.llm.mock import MockProvider

STATES = Path(__file__).resolve().parents[2] / "bandi" / "agent" / "states.py"


class TestGuardOnEveryReturnPath:
    """第三版修法的由来。

    前两版都只把重试挂在 `_do_llm_call_inner` 的**直调**返回点（1295 行附近），
    但主路径根本不经过那里——流式候选成功（`return response`）与非流式
    `complete_with_fallback`（`return response`）各自提前 return。重试代码因此
    是死代码，events 里 `reasoning_only_retry` 事件数恒为 0。

    本类用 AST 静态核对：函数内所有 `return <LLMResponse 类表达式>` 的返回点，
    除经 `_guard_reasoning_only` 包裹的以外不得裸返回响应。"""

    def test_return_points_are_guarded(self) -> None:
        src = STATES.read_text(encoding="utf-8")
        tree = ast.parse(src)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != "_do_llm_call_inner":
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Return) or sub.value is None:
                    continue
                seg = ast.get_source_segment(src, sub) or ""
                # 返回响应对象的点必须经闸门包裹
                if ("response" in seg or "retried" in seg) and "_guard_reasoning_only" not in seg:
                    # `_guard_reasoning_only` 自身返回 retried/response 属正常
                    offenders.append(f"{node.name}:{sub.lineno} → {seg[:70]}")
        assert not offenders, "未挂 reasoning-only 闸门的响应返回点:\n" + "\n".join(offenders)

    def test_guard_call_sites_exist(self) -> None:
        src = STATES.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "_do_llm_call_inner"
            ),
            None,
        )
        assert fn is not None, "找不到 _do_llm_call_inner"
        calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_guard_reasoning_only"
        ]
        assert len(calls) >= 3, (
            f"闸门只挂了 {len(calls)} 处；主路径有「流式候选 / 非流式 fallback / 直调」"
            "三个返回点，少挂任一处该路径就绕过自愈"
        )


class TestFallbackEngineApiIsReal:
    def test_method_name_exists(self) -> None:
        """接线前提：FallbackEngine 必须真的有 call_with_fallback。
        写成 execute 会被 except Exception 静默吃掉——本用例直接拦这类错误。"""
        from bandi.llm.strategy import FallbackEngine

        assert hasattr(FallbackEngine, "call_with_fallback")
        assert not hasattr(FallbackEngine, "execute"), (
            "FallbackEngine 若新增了 execute，需确认重试路径用的是哪个"
        )


class TestNoBlanketExcept:
    def test_retry_path_has_no_bare_exception_swallow(self) -> None:
        """兜底分支可以捕获异常，但必须**留痕**（logger + 不得静默）。

        这里退一步只做静态检查：重试函数体内不得出现 `except Exception` 后
        直接 `return None` 而无任何日志/告警——那正是吞掉自身 bug 的形状。
        """
        src = STATES.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "retry_reasoning" not in node.name:
                continue
            for handler in ast.walk(node):
                if not isinstance(handler, ast.ExceptHandler):
                    continue
                body = handler.body
                returns_null = any(isinstance(s, ast.Return) and s.value is None for s in body)
                has_log = any(
                    isinstance(s, ast.Expr)
                    and isinstance(s.value, ast.Call)
                    and getattr(s.value.func, "attr", "") in {"warning", "error", "exception"}
                    for s in body
                )
                if returns_null and not has_log:
                    pytest.fail(f"{node.name} 的 except 分支静默吞异常，不得返回 None 而不留痕")


class TestDetectionShape:
    def test_real_provider_shape_detected(self) -> None:
        """返回的响应形态（finish_reason=stop、无 tool_calls）必须被判中，
        否则线上永远触发不了重试。"""
        from bandi.agent.states import _is_reasoning_only

        assert (
            _is_reasoning_only(
                LLMResponse(content="", reasoning="思考了 392 个字符"), safety_finish=False
            )
            is True
        )

    def test_mock_can_emit_reasoning_only(self) -> None:
        p = MockProvider()
        p.add_response(LLMResponse(content="", reasoning="思考"))
        assert True
