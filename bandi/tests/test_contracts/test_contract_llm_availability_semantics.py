"""功能：LLM 可用性失败语义 + 旁路质量检查时间预算契约（2026-10-09 真机暴露）。

背景（真机取证）：
- **无可用候选**（模型名不在目录 / 无 base_url / 候选全失败）此前按 RECOVERABLE 处理
  ⇒ 重试整个回合直到阈值（10 次）⇒ `ESCALATED` + **空回复**，用户既拿不到原因也白等
  （实测 `run_agent_turn` 无链路径：provider calls=0，terminal=ESCALATED，reply=''）。
- **旁路质量检查**（验证器/裁判/幻觉校验/门禁）最坏耗时 = 上游超时 × 重试轮数
  （agna 抖动实测 60s × 3 ≈ 180s），主回复早已流完却把回合拖到 3 分钟以上。

钉行为不钉实现：
1. `LLMUnavailableError` 是 `LLMError` 子类（既有 `except LLMError` 全兼容）
2. 无可用候选 → `RecoveryManager` 判 FATAL → TERMINATE（立即 MODEL_ERROR，不重试）
3. 目录解析失败抛的就是 `LLMUnavailableError`
4. `accounted_complete(budget_sec=…)` 给调用设**总预算**（含 provider 内部重试），超时抛 TimeoutError
5. 质量检查在预算内降级返回（不把回合拖住）
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

from bandi.core.exceptions import LLMError, LLMUnavailableError


class TestExceptionTaxonomy:
    def test_unavailable_is_llm_error_subclass(self) -> None:
        # 功能：向后兼容——既有 `except LLMError` 必须仍能捕获
        assert issubclass(LLMUnavailableError, LLMError)
        assert isinstance(LLMUnavailableError("x"), LLMError)

    def test_registry_raises_unavailable_for_unresolvable_model(self) -> None:
        # 功能：目录解析失败 = 不可用（而非泛化的 LLMError）
        from bandi.core.config import BandiConfig
        from bandi.llm.registry import ProviderRegistry

        registry = ProviderRegistry(BandiConfig())
        with pytest.raises(LLMUnavailableError):
            registry.get_provider("definitely-not-a-registered-model", BandiConfig())


class TestAvailabilityIsFatal:
    @pytest.mark.asyncio
    async def test_handle_returns_terminate(self) -> None:
        # 功能：不可用 → TERMINATE（立即终止，不是 RETRY）
        from bandi.agent.recovery_manager import RecoveryAction, RecoveryManager

        manager = RecoveryManager()
        action = await manager.handle(LLMUnavailableError("no viable candidate"), "thinking")
        assert action == RecoveryAction.TERMINATE

    @pytest.mark.asyncio
    async def test_no_escalation_even_after_many_attempts(self) -> None:
        # 破坏者：反复不可用也不得走「重试到阈值 → ESCALATE」老路
        from bandi.agent.recovery_manager import RecoveryAction, RecoveryManager

        manager = RecoveryManager()
        for _ in range(15):
            action = await manager.handle(LLMUnavailableError("no viable candidate"), "thinking")
            assert action == RecoveryAction.TERMINATE, "不可用被当成可重试错误"


def _stub(config_name: str, complete: Any) -> Any:
    """功能：最小 provider 桩（补齐抽象 stream；complete 由用例注入）。"""
    from bandi.core.config import LLMConfig
    from bandi.llm.base import BaseProvider

    class _Stub(BaseProvider):
        def __init__(self) -> None:
            super().__init__(
                LLMConfig(default_model=config_name, api_key="x", base_url="http://stub")
            )

        @property
        def model_name(self) -> str:
            return config_name

        async def complete(self, messages: list[dict], **kwargs: Any) -> Any:
            return await complete(messages, kwargs)

        async def stream(self, messages: list[dict], **kwargs: Any) -> Any:
            raise NotImplementedError("stub 不提供流式")
            yield  # pragma: no cover - 使本方法成为异步生成器

    return _Stub()


class TestUpstreamStatusSurvivesWrapping:
    def test_helper_attaches_status_from_cause(self) -> None:
        # 功能：包装后的 LLMError 必须带上游状态码（判定器据此归 FATAL）
        from bandi.core.exceptions import llm_error_with_status

        class _HttpStatusError(Exception):
            status_code = 503

        err = llm_error_with_status("wrapped", _HttpStatusError())
        assert err.status_code == 503

    def test_helper_reads_response_status(self) -> None:
        # 功能：httpx 风格（status 挂在 .response 上）同样要取到
        from bandi.core.exceptions import llm_error_with_status

        class _Response:
            status_code = 429

        class _HttpStatusError(Exception):
            response = _Response()

        assert llm_error_with_status("wrapped", _HttpStatusError()).status_code == 429

    def test_helper_without_status_leaves_it_unset(self) -> None:
        # 功能：无状态可继承时不得凭空造一个
        from bandi.core.exceptions import llm_error_with_status

        assert not hasattr(llm_error_with_status("wrapped", ValueError("x")), "status_code")

    @pytest.mark.asyncio
    async def test_wrapped_upstream_5xx_is_retryable(self) -> None:
        # 语义回调（用户拍板，对齐参考源共识：opencode `server_error` 可重试；
        # deepseek-harness 只把配置类错误排除在默认重试集外，理由是「每次都会同样失败」）：
        # 上游 5xx 属**暂时故障** → 可重试；只有配置类（链上无状态 / 4xx 非 429）才终止。
        from bandi.agent.recovery_manager import RecoveryAction, RecoveryManager
        from bandi.core.exceptions import llm_error_with_status

        class _HttpStatusError(Exception):
            status_code = 503

        wrapped = llm_error_with_status("Stream connection failed: 503", _HttpStatusError())
        action = await RecoveryManager().handle(wrapped, "thinking")
        assert action != RecoveryAction.TERMINATE, "上游 5xx（暂时故障）被当成配置类错误终止"


class TestTransientVsConfigClassification:
    """功能：配置类错误 vs 暂时故障的分类契约（恢复面语义的唯一事实源）。"""

    @staticmethod
    def _unavailable_with(status: int | None):
        from bandi.core.exceptions import LLMUnavailableError

        err = LLMUnavailableError("all candidates failed")
        if status is not None:
            err.status_code = status
        return err

    @pytest.mark.asyncio
    async def test_unavailable_without_status_is_fatal(self) -> None:
        # 配置类（模型不在目录 / 无 base_url）：重试同一配置必然同样失败 → 终止
        from bandi.agent.recovery_manager import RecoveryAction, RecoveryManager

        action = await RecoveryManager().handle(self._unavailable_with(None), "thinking")
        assert action == RecoveryAction.TERMINATE

    @pytest.mark.asyncio
    async def test_unavailable_with_5xx_is_retryable(self) -> None:
        from bandi.agent.recovery_manager import RecoveryAction, RecoveryManager

        action = await RecoveryManager().handle(self._unavailable_with(503), "thinking")
        assert action != RecoveryAction.TERMINATE

    @pytest.mark.asyncio
    async def test_unavailable_with_429_is_retryable(self) -> None:
        # 429 限流属暂时故障——重试（带退避）有意义
        from bandi.agent.recovery_manager import RecoveryAction, RecoveryManager

        action = await RecoveryManager().handle(self._unavailable_with(429), "thinking")
        assert action != RecoveryAction.TERMINATE

    @pytest.mark.asyncio
    async def test_unavailable_with_404_is_fatal(self) -> None:
        from bandi.agent.recovery_manager import RecoveryAction, RecoveryManager

        action = await RecoveryManager().handle(self._unavailable_with(404), "thinking")
        assert action == RecoveryAction.TERMINATE

    @pytest.mark.asyncio
    async def test_plain_llm_error_400_is_fatal(self) -> None:
        from bandi.agent.recovery_manager import RecoveryAction, RecoveryManager
        from bandi.core.exceptions import LLMError

        err = LLMError("bad request")
        err.status_code = 400
        assert await RecoveryManager().handle(err, "thinking") == RecoveryAction.TERMINATE

    def test_upstream_status_walks_cause_chain(self) -> None:
        # 包装层新建异常对象时状态留在内层——链式解引用必须能取到
        from bandi.core.exceptions import LLMUnavailableError, upstream_status

        class _InnerError(Exception):
            status_code = 503

        inner = _InnerError()
        try:
            try:
                raise inner
            except _InnerError as exc:
                raise LLMUnavailableError("wrapped") from exc
        except LLMUnavailableError as outer:
            assert upstream_status(outer) == 503

    def test_upstream_status_reads_last_error_attribute(self) -> None:
        # ProviderChainExhaustedError 风格：内层错误挂在 `last_error` 属性上
        from bandi.core.exceptions import upstream_status

        class _InnerError(Exception):
            status_code = 500

        class _AggregateError(Exception):
            def __init__(self) -> None:
                super().__init__("chain exhausted")
                self.last_error = _InnerError()

        assert upstream_status(_AggregateError()) == 500

    def test_upstream_status_none_when_no_status_anywhere(self) -> None:
        from bandi.core.exceptions import is_transient_upstream, upstream_status

        assert upstream_status(ValueError("x")) is None
        assert is_transient_upstream(ValueError("x")) is False


class TestSidePathBudget:
    @pytest.mark.asyncio
    async def test_budget_bounds_hanging_call(self) -> None:
        # 功能：总预算封顶——挂死的 provider 不得让调用无限等
        from bandi.llm.accounted import accounted_complete

        async def _hang(messages: list[dict], kwargs: dict) -> Any:
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

        started = asyncio.get_running_loop().time()
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await accounted_complete(
                _stub("hang", _hang), [{"role": "user", "content": "hi"}], budget_sec=0.3
            )
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 5, f"预算未生效，实际等待 {elapsed:.1f}s"

    @pytest.mark.asyncio
    async def test_no_budget_keeps_original_behavior(self) -> None:
        # 功能：不传预算时不套 wait_for（保功能类旁路原有语义）
        from bandi.core.message import LLMResponse
        from bandi.llm.accounted import accounted_complete

        async def _fast(messages: list[dict], kwargs: dict) -> Any:
            return LLMResponse(content="ok", tool_calls=None, finish_reason="stop")

        resp = await accounted_complete(_stub("fast", _fast), [{"role": "user", "content": "hi"}])
        assert resp.content == "ok"

    @pytest.mark.asyncio
    async def test_quality_check_degrades_within_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 功能（端到端）：幻觉 L2 检查在上游挂死时按预算降级返回，不拖住回合
        account = importlib.import_module("bandi.llm.accounted")
        monkeypatch.setattr(account, "QUALITY_CHECK_BUDGET_SEC", 0.3)

        checker = importlib.import_module("bandi.eval.hallucination_checker")

        async def _hang(messages: list[dict], kwargs: dict) -> Any:
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

        result = await asyncio.wait_for(
            checker.l2_check(
                "模型说它完成了测试", context="没有测试输出", provider=_stub("hang", _hang)
            ),
            timeout=10,
        )
        assert result.hallucination_score == 0.0, "降级结果应为空判定"
