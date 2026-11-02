# 功能：契约 — Judge 调优批（独立 judge 模型 + 推理前置 + few-shot 锚定）
from __future__ import annotations

from typing import Any

import pytest

from bandi.agent.verifier import VER_JUDGE_SYSTEM_PROMPT, Verifier, VerifierPacket, VerifierPolicy
from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


class _Recording(MockProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(
        self, messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any
    ) -> LLMResponse:
        self.calls.append(messages)
        return await super().complete(messages, tools=tools, **kwargs)


def _verdict_response(action: str = "pass", reason: str = "功能完成") -> LLMResponse:
    import json

    return LLMResponse(content=json.dumps({"action": action, "reason": reason}))


def _packet() -> VerifierPacket:
    return VerifierPacket(task_description="写排序函数", output="已实现快排")


class TestJudgeProviderResolution:
    def test_default_empty_follows_main_provider(self) -> None:
        """judge_model 空 = 跟随主模型——judge 调用必须打到主 provider（零回归）。"""
        from bandi.agent.factory import _resolve_judge_provider
        from bandi.core.config import BandiConfig

        cfg = BandiConfig()
        main = _Recording()
        assert _resolve_judge_provider(cfg, main) is None, (
            "judge_model 未配置必须返回 None（跟随主模型语义，由 Verifier 回落）"
        )

    def test_judge_model_builds_separate_provider(self) -> None:
        """judge_model 配置后解析出独立 provider（同模型名也必须是独立实例）。"""
        from bandi.agent.factory import _resolve_judge_provider
        from bandi.core.config import BandiConfig

        cfg = BandiConfig()
        cfg.verification.judge_model = "deepseek-v4-flash"
        main = _Recording()
        judge = _resolve_judge_provider(cfg, main)
        assert judge is not main, "配置 judge_model 后必须构建独立 provider"


class TestJudgeCallRouting:
    @pytest.mark.asyncio
    async def test_judge_prompt_carries_original_goal_and_user_messages(self) -> None:
        judge = _Recording()
        judge.add_response(_verdict_response("pass"))
        verifier = Verifier(
            policy=VerifierPolicy(enabled=True, min_todos_for_verification=1),
            provider=judge,
        )
        await verifier.evaluate(
            VerifierPacket(
                task_description="补充表格颜色",
                output="已改颜色",
                original_goal="完成竞品分析报告",
                user_messages="[1] 完成竞品分析报告\n[2] 补充表格颜色",
            )
        )
        prompt = judge.calls[-1][-1]["content"]
        assert "完成竞品分析报告" in prompt
        assert "[2] 补充表格颜色" in prompt

    @pytest.mark.asyncio
    async def test_evaluate_routes_to_judge_provider(self) -> None:
        main = _Recording()
        judge = _Recording()
        judge.add_response(_verdict_response("pass"))
        verifier = Verifier(
            policy=VerifierPolicy(enabled=True, min_todos_for_verification=1),
            provider=main,
            judge_provider=judge,
        )
        verdict = await verifier.evaluate(_packet())
        assert verdict.action == "pass"
        assert judge.calls and not main.calls, "判定调用必须走 judge provider，主 provider 零调用"

    @pytest.mark.asyncio
    async def test_no_judge_provider_falls_back_to_main(self) -> None:
        main = _Recording()
        main.add_response(_verdict_response("rework", "未完成"))
        verifier = Verifier(
            policy=VerifierPolicy(enabled=True, min_todos_for_verification=1),
            provider=main,
        )
        verdict = await verifier.evaluate(_packet())
        assert verdict.action == "rework"
        assert main.calls, "未配 judge provider 时回落主 provider（零回归）"


class TestReasoningBeforeVerdict:
    @pytest.mark.asyncio
    async def test_parses_verdict_after_prose_analysis(self) -> None:
        """judge 先给分析再给 JSON（无 fence）——解析必须容忍前置分析文本。"""
        judge = _Recording()
        judge.add_response(
            LLMResponse(
                content='分析：输出实现了快排，边界已处理，无安全风险。\n{"action": "pass", "reason": "功能完成且无安全风险"}'
            )
        )
        verifier = Verifier(
            policy=VerifierPolicy(enabled=True, min_todos_for_verification=1),
            provider=judge,
            judge_provider=judge,
        )
        verdict = await verifier.evaluate(_packet())
        assert verdict.action == "pass", f"前置分析不得破坏 verdict 解析: {verdict.reason}"

    @pytest.mark.asyncio
    async def test_analysis_captured_in_verdict(self) -> None:
        judge = _Recording()
        judge.add_response(
            LLMResponse(
                content='分析：核心报错未修复。\n{"action": "rework", "reason": "空输入未处理"}'
            )
        )
        verifier = Verifier(
            policy=VerifierPolicy(enabled=True, min_todos_for_verification=1),
            provider=judge,
            judge_provider=judge,
        )
        verdict = await verifier.evaluate(_packet())
        assert verdict.action == "rework"
        assert "核心报错未修复" in verdict.analysis, "分析文本必须捕获进 Verdict 供审计"

    @pytest.mark.asyncio
    async def test_invalid_output_still_fail_closed(self) -> None:
        judge = _Recording()
        judge.add_response(LLMResponse(content="完全无法解析的输出"))
        verifier = Verifier(
            policy=VerifierPolicy(enabled=True, min_todos_for_verification=1),
            provider=judge,
            judge_provider=judge,
        )
        verdict = await verifier.evaluate(_packet())
        assert verdict.action == "fail", "无 fail_open 时非法输出必须 fail-closed"

    @pytest.mark.asyncio
    async def test_judge_timeout_passes_without_blocking_delivery(self, monkeypatch: Any) -> None:
        """裁判基础设施超时不得把已产出回复判死并卡住 running。"""

        async def _timeout(*args: Any, **kwargs: Any) -> Any:
            raise TimeoutError("judge timed out")

        monkeypatch.setattr("bandi.llm.accounted.accounted_complete", _timeout)
        verifier = Verifier(
            policy=VerifierPolicy(enabled=True, min_todos_for_verification=1),
            provider=_Recording(),
            judge_provider=_Recording(),
        )
        verdict = await verifier.evaluate(_packet())
        assert verdict.action == "pass"
        assert "timeout" in verdict.reason


class TestPromptAnchoring:
    def test_system_prompt_has_few_shot_for_all_actions(self) -> None:
        assert "示例" in VER_JUDGE_SYSTEM_PROMPT, "提示词必须含 few-shot 示例"
        for action in ("pass", "rework", "escalate"):
            assert f'"{action}"' in VER_JUDGE_SYSTEM_PROMPT, f"缺少 {action} 示例锚定"

    def test_system_prompt_requires_analysis_before_verdict(self) -> None:
        assert "分析" in VER_JUDGE_SYSTEM_PROMPT, "提示词必须要求 verdict 前先给分析"
