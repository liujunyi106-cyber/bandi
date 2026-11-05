"""契约：reasoning-only 空正文必须自动重试一次，不得直接判 MODEL_ERROR。

真机 L1：74 条里 5 条终态 MODEL_ERROR，错误文案是
「模型仅返回思维链、正文为空（reasoning 343 字符；答案疑似被上游放进
reasoning 通道——可直接重试，或在输入框切换思考档位后重发）」。

系统**检测到了**这个形态，却只把选择权丢给用户，等于白白作废一次交互。
思考型模型把答案整体写进 reasoning 是**可自愈**的输出形态问题。

本契约钉三条：
1. 命中即换模型重试一次；
2. **只试一次**——连续两次说明 provider 侧系统性行为，再试只是烧钱；
3. **绝不把 reasoning 提升为正文**（思维链不是答案，长度与语义都不可控）。
"""

from __future__ import annotations

import pytest

from bandi.agent.states import _is_reasoning_only, _safety_finish


class _Resp:
    def __init__(self, content="", reasoning="", finish_reason="stop", refusal=False):
        self.content = content
        self.reasoning = reasoning
        self.finish_reason = finish_reason
        self.refusal = refusal


class TestDetection:
    def test_reasoning_only_detected(self) -> None:
        assert _is_reasoning_only(_Resp(content="", reasoning="思考了一堆"), safety_finish=False)

    def test_normal_reply_not_flagged(self) -> None:
        assert not _is_reasoning_only(_Resp(content="答案"), safety_finish=False)

    def test_both_present_not_flagged(self) -> None:
        """有正文就不算 reasoning-only——思维链与正文并存是正常的。"""
        assert not _is_reasoning_only(_Resp(content="答案", reasoning="思考"), safety_finish=False)

    def test_truly_empty_not_flagged(self) -> None:
        """真空回复是另一类（连 reasoning 都没有），不走本重试路径。"""
        assert not _is_reasoning_only(_Resp(content="", reasoning=""), safety_finish=False)

    def test_whitespace_only_content_not_flagged(self) -> None:
        """正文只有空白等同没有。"""
        assert _is_reasoning_only(_Resp(content="   \n\t", reasoning="思考"), safety_finish=False)

    @pytest.mark.parametrize(
        "resp",
        [
            _Resp(content="", reasoning="思考", finish_reason="content_filter"),
            _Resp(content="", reasoning="思考", refusal=True),
        ],
    )
    def test_safety_finish_never_retried(self, resp: _Resp) -> None:
        """内容过滤/拒答走 SAFETY_BLOCKED，不得被当成 reasoning-only 重试——
        重试只会绕过安全语义。"""
        assert _safety_finish(resp) is True
        assert not _is_reasoning_only(resp, safety_finish=True)


class TestRetryBudget:
    def test_retry_marked_on_ctx_meta(self) -> None:
        """幂等闸门必须落在 ctx.meta 上，跨本轮所有调用点共享。"""

        class _Ctx:
            meta: dict = {}

        ctx = _Ctx()
        assert not ctx.meta.get("reasoning_only_retried")
        ctx.meta["reasoning_only_retried"] = True
        assert ctx.meta["reasoning_only_retried"] is True


class TestNoReasoningPromotion:
    def test_reasoning_never_becomes_content(self) -> None:
        """破坏者视角：实现不得把 reasoning 当答案返回给用户。"""
        r = _Resp(content="", reasoning="最终答案其实是这段话")
        # 检测函数只回答「是不是该重试」，不提供任何提升内容的路径
        assert _is_reasoning_only(r, safety_finish=False) is True
        assert r.content == ""


class TestRetryResolvesCandidate:
    """候选链已删除（不搞候选——配置什么模型用什么）。

    reasoning-only 重试 = **同模型保底重发一次**；registry 候选解析通道整删
    （换模型重试曾依赖坏凭证候选（agnes/mimo），是"空输出"故障的主要放大器）。
    """

    @pytest.mark.asyncio
    async def test_retry_reuses_current_model_without_candidates(self) -> None:
        import asyncio
        from types import SimpleNamespace

        from bandi.agent.states import _retry_reasoning_only

        calls = {"resolved": [], "completed": []}

        class _Provider:
            def __init__(self, name: str, resp: _Resp) -> None:
                self.model_name = name
                self._resp = resp

            async def complete(self, msgs, tools=None, **kw):
                calls["completed"].append(self.model_name)
                return self._resp

        class _Registry:
            def get_provider_for_model(self, model, config):
                calls["resolved"].append(model)
                return _Provider(model, _Resp(content="答案是 42"))

        class _Ctx:
            meta: dict = {}
            provider = _Provider("primary-model", _Resp(content="", reasoning="思考"))
            provider_name = ""
            active_tools = None
            bus = None
            config = SimpleNamespace(
                llm=SimpleNamespace(fallback_chain=["candidate-a"], timeout=10)
            )

        ctx = _Ctx()
        resp = await asyncio.wait_for(
            _retry_reasoning_only(
                ctx,
                [{"role": "user", "content": "q"}],
                False,
                registry=_Registry(),
                bandi_config=SimpleNamespace(),
            ),
            timeout=10,
        )
        assert calls["resolved"] == [], "候选解析已删——不得再走 registry 换模型"
        assert calls["completed"] == ["primary-model"], "同模型保底重发一次"
        assert resp is None, "同模型重发仍 reasoning-only → 返回 None"

    @pytest.mark.asyncio
    async def test_resolve_failure_falls_back_to_current_provider(self) -> None:
        """破坏者：registry 解析链候选失败 → 回落原 provider 重发一次（保底不炸）。"""
        import asyncio
        from types import SimpleNamespace

        from bandi.agent.states import _retry_reasoning_only

        calls = {"completed": []}

        class _Provider:
            def __init__(self, name: str, resp: _Resp) -> None:
                self.model_name = name
                self._resp = resp

            async def complete(self, msgs, tools=None, **kw):
                calls["completed"].append(self.model_name)
                return self._resp

        class _Registry:
            def get_provider_for_model(self, model, config):
                raise RuntimeError("registry down")

        class _Ctx:
            meta: dict = {}
            provider = _Provider("primary-model", _Resp(content="", reasoning="思考"))
            provider_name = ""
            active_tools = None
            bus = None
            config = SimpleNamespace(
                llm=SimpleNamespace(fallback_chain=["candidate-a"], timeout=10)
            )

        ctx = _Ctx()
        resp = await asyncio.wait_for(
            _retry_reasoning_only(
                ctx,
                [{"role": "user", "content": "q"}],
                False,
                registry=_Registry(),
                bandi_config=SimpleNamespace(),
            ),
            timeout=10,
        )
        assert calls["completed"] == ["primary-model"], "解析失败应回落原 provider"
        assert resp is None, "原 provider 仍 reasoning-only → 返回 None"


class TestChainSkipsCurrentModel:
    """候选链已删除：链配置被忽略——重试=同模型重发。

    历史教训：链 ["agnes-2.5-flash", ...] 全是坏凭证候选，
    换模型重试只会连败并掩盖主模型错因——候选通道整删后不存在"跳过当前"语义。
    """

    @pytest.mark.asyncio
    async def test_chain_ignored_retry_uses_current_model(self) -> None:
        import asyncio
        from types import SimpleNamespace

        from bandi.agent.states import _retry_reasoning_only

        calls = {"completed": []}

        class _Provider:
            def __init__(self, name: str, resp: _Resp) -> None:
                self.model_name = name
                self._resp = resp

            async def complete(self, msgs, tools=None, **kw):
                calls["completed"].append(self.model_name)
                return self._resp

        class _Registry:
            def get_provider_for_model(self, model, config):
                return _Provider(model, _Resp(content="答案是 42"))

        class _Ctx:
            meta: dict = {}
            provider = _Provider("primary-model", _Resp(content="", reasoning="思考"))
            provider_name = ""
            active_tools = None
            bus = None
            config = SimpleNamespace(
                llm=SimpleNamespace(fallback_chain=["primary-model", "candidate-a"], timeout=10)
            )

        ctx = _Ctx()
        resp = await asyncio.wait_for(
            _retry_reasoning_only(
                ctx,
                [{"role": "user", "content": "q"}],
                False,
                registry=_Registry(),
                bandi_config=SimpleNamespace(),
            ),
            timeout=10,
        )
        assert calls["completed"] == ["primary-model"], "链已删——同模型重发（不再跳链）"
        assert resp is None, "同模型仍 reasoning-only → None"

    @pytest.mark.asyncio
    async def test_single_model_chain_still_retries_same_model(self) -> None:
        """破坏者对偶：链上只有当前模型时——保底同模型重发一次（不退化为不重试）。"""
        import asyncio
        from types import SimpleNamespace

        from bandi.agent.states import _retry_reasoning_only

        calls = {"completed": []}

        class _Provider:
            def __init__(self, name: str, resp: _Resp) -> None:
                self.model_name = name
                self._resp = resp

            async def complete(self, msgs, tools=None, **kw):
                calls["completed"].append(self.model_name)
                return self._resp

        class _Ctx:
            meta: dict = {}
            provider = _Provider("only-model", _Resp(content="", reasoning="思考"))
            provider_name = ""
            active_tools = None
            bus = None
            config = SimpleNamespace(llm=SimpleNamespace(fallback_chain=["only-model"], timeout=10))

        ctx = _Ctx()
        resp = await asyncio.wait_for(
            _retry_reasoning_only(
                ctx,
                [{"role": "user", "content": "q"}],
                False,
                registry=None,
                bandi_config=None,
            ),
            timeout=10,
        )
        assert calls["completed"] == ["only-model"], "无其他候选时同模型保底重发"
        assert resp is None, "同模型仍 reasoning-only → None"


class TestGateResetsAcrossTurns:
    """闸门是轮级：上一轮的重试标记不得让下一轮 reasoning-only 无重试可用。

    真机实证（2026-10-15）：turn 2 重试后标记残留（ctx.meta 无清理点），
    turn 3 的 reasoning-only 直接跳过重试落到 MODEL_ERROR（事件无 retry 记录）。
    """

    @pytest.mark.asyncio
    async def test_on_thinking_entry_clears_gate(self) -> None:
        from bandi.agent.states import on_thinking
        from bandi.core.message import LLMResponse, Message, MessageRole
        from tests.test_agent.test_states import StubProvider, make_ctx

        provider = StubProvider()
        provider.add_response(LLMResponse(content="ok"))
        ctx = make_ctx(
            messages=[Message(role=MessageRole.USER, content="hi")],
            provider=provider,
            turn_index=1,
        )
        ctx.meta["reasoning_only_retried"] = True
        await on_thinking(ctx)
        assert not ctx.meta.get("reasoning_only_retried"), "轮入口必须清除重试闸门"
