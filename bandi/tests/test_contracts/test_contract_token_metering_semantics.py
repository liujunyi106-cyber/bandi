"""

钉住行为族：token 计量必须 provider tokenizer（tiktoken cl100k_base）驱动
字符/4 仅作编码器缺失时的 preflight fallback——
1. 中文/JSON/XML 场景 tiktoken 驱动与字符/4 差异显著（「深度学习计划」
   6 汉字 = 9 tokens，字符/4 仅 1——低估 9 倍）
2. provider.count_tokens 与压缩链预算判定全部走 TokenCounter
3. 校准系数按模型修正、误差观测回填（estimated vs provider usage）
4. breakdown 按 role 分项计量
5. on_thinking 消费 usage 时生产接线误差回填

生产调用路径族：TokenCounter.count（utils，全计量源）→ BaseProvider.count_tokens
（llm/base.py，全 provider 族继承）→ compression_pipeline._is_under_budget /
MemoryCompactor.compact（压缩链预算判定）→ on_thinking usage 消费（误差回填）。
"""

from __future__ import annotations

from typing import Any

import pytest

from bandi.core.message import Message, MessageRole
from bandi.utils.token_counter import TokenCounter


@pytest.fixture(autouse=True)
def _isolate_token_counter():
    """功能：隔离 TokenCounter 全局校准/观测状态（测试互不污染）。"""
    TokenCounter._calibrations = {}
    TokenCounter._observations = []
    TokenCounter._active_model = ""
    yield
    TokenCounter._calibrations = {}
    TokenCounter._observations = []
    TokenCounter._active_model = ""


# ── 1. 中文场景 tiktoken 驱动（破坏者：字符/4 对中文低估数倍） ────────────────


def test_chinese_text_token_counting_driven_by_tokenizer() -> None:
    """契约（.1）：中文文本计数必须 tokenizer 驱动——显著大于字符/4 估算
    （字符/4 对中文系统性低估，压缩预算判定随之失真）。"""
    text = "深度学习计划：构建个人多Agent数字员工组织，包含状态机与压缩管道"
    actual = TokenCounter.count(text)
    legacy = len(text) // 4
    assert actual > legacy, f"中文计数 {actual} 必须大于字符/4 估算 {legacy}——仍是字符估算驱动"
    assert actual >= len(text) // 3, "中文 cl100k 计数应接近 1 字符/token 量级"


def test_fallback_when_encoder_missing() -> None:
    """契约（.1b）：编码器缺失时回落字符/4（preflight fallback，不炸）。"""
    original = TokenCounter._encoder
    TokenCounter._encoder = False
    try:
        assert TokenCounter.count("abcd") == 1
    finally:
        TokenCounter._encoder = original


# ── 2. provider.count_tokens 驱动升级 ─────────────────────────────────────────


def test_provider_count_tokens_uses_token_counter() -> None:
    """契约（.2）：BaseProvider.count_tokens 必须委托 TokenCounter（全 provider
    族继承生效）——不再内置字符/4。"""
    from bandi.llm.base import BaseProvider

    class _StubProvider(BaseProvider):
        def __init__(self) -> None:
            from bandi.core.config import LLMConfig

            super().__init__(config=LLMConfig())

        async def complete(self, messages, tools=None, **kwargs):  # type: ignore[empty-body]
            raise NotImplementedError

        async def stream(self, messages, tools=None, **kwargs):  # type: ignore[empty-body]
            raise NotImplementedError

    text = "深度学习计划"
    assert _StubProvider().count_tokens(text) == TokenCounter.count(text)
    assert _StubProvider().count_tokens(text) > len(text) // 4


# ── 3. 压缩链预算判定 token 驱动 ──────────────────────────────────────────────


def test_pipeline_budget_check_driven_by_token_counter() -> None:
    """契约（.3）：_is_under_budget 必须按 token 计量判定——中文消息在
    「字符/4 误判为预算内、tokenizer 判超预算」的场景必须正确要求压缩。"""
    from bandi.memory.compression_pipeline import CompressionPipeline

    # 350 汉字：tokenizer ≈ 350+ tokens > 预算 100（200*0.5）；字符/4 = 87 chars
    # 旧口径 `total_chars <= context_window*4*threshold` = 350 <= 400 误判「预算内」
    messages = [
        Message(role=MessageRole.USER, content="深" * 350),
    ]
    total_chars = 350
    assert total_chars <= 200 * 4 * 0.5, "构造前提：旧字符口径应误判为预算内"
    assert (
        CompressionPipeline._is_under_budget(
            messages, context_window=200, compression_threshold=0.5
        )
        is False
    ), "中文消息 tokens 超预算必须判定 needs compression（字符估算会误判为预算内）"


@pytest.mark.asyncio
async def test_compaction_target_driven_by_token_counter() -> None:
    """契约（.4）：MemoryCompactor.compact 的目标预算判定必须 token 驱动——
    中文消息 tokens 超预算（字符/4 误判预算内）时仍必须触发 LLM 摘要压缩。"""
    from bandi.core.message import LLMResponse, TokenUsage
    from bandi.memory.compaction import MemoryCompactor

    compactor = MemoryCompactor(max_summary_chars=2000, target_token_budget=50)
    # 真实多样化中文文本（重复单字会被 BPE 高效合并失真）：
    # 4 句 × ~30 字 ≈ 120+ tokens > 50 预算；字符/4 = ~150 chars，旧 target_chars=200
    # 误判预算内不压缩（构造前提暴露旧码漏判）
    sentence = "深度学习计划：构建个人多Agent数字员工组织，状态机与压缩管道协同工作"
    messages = [
        Message(role=MessageRole.SYSTEM, content="sys"),
        Message(role=MessageRole.USER, content=sentence),
        Message(role=MessageRole.ASSISTANT, content="回复：" + sentence),
        Message(role=MessageRole.USER, content=sentence + "第二步"),
    ]
    total_chars = sum(len(m.content or "") for m in messages if m.role != MessageRole.SYSTEM)
    assert total_chars <= 300, "构造前提：字符口径下应低于旧 target_chars（暴露旧码漏判）"
    assert TokenCounter.count(sentence) * 3 > 50, "构造前提：tokenizer 口径应超预算"

    calls: list[list[dict]] = []

    async def fake_complete(prompt: list[dict]) -> Any:
        calls.append(prompt)
        return LLMResponse(
            content="[压缩边界] 摘要",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )

    result, success, _ = await compactor.compact(messages, fake_complete)

    assert calls, "tokens 超预算必须触发 LLM 摘要（字符估算驱动会漏判不压缩）"
    assert success is True
    assert any("[压缩边界]" in (m.content or "") for m in result)


@pytest.mark.asyncio
async def test_compaction_split_driven_by_token_counter() -> None:
    """契约（.6，审查项1）：MemoryCompactor 切分点必须 token 驱动——旧码用
    chars_per_token=4 × 字符累计切分，对中文（≈1 token/字）消费量放大 4×
    把本应保留的较新消息也吞进摘要压缩目标。token 驱动后，最近消息留在保留区
    （result 尾部的 keep_messages 具名消息幸存），不被压缩为摘要。"""
    from bandi.core.message import LLMResponse, TokenUsage
    from bandi.memory.compaction import MemoryCompactor

    compactor = MemoryCompactor(max_summary_chars=2000, target_token_budget=40)
    # 每条中文消息 ~30 tokens；预算 40 时 split_tokens=20 → 第 1 条即达上限
    # 第 2 条起为保留区——token 驱动保留较新消息；chars_per_token=4 旧码
    # (target_chars=160, 半=80 chars) 会把全部 ~120 chars 都吞进压缩目标
    sentence = "深度学习计划：构建个人多Agent数字员工组织，状态机与压缩管道协同工作"
    messages = [
        Message(role=MessageRole.USER, content=sentence + "第一步"),
        Message(role=MessageRole.ASSISTANT, content="回复：" + sentence + "第二步"),
        Message(role=MessageRole.USER, content=sentence + "最新输入"),
    ]
    calls: list[list[dict]] = []

    async def fake_complete(prompt: list[dict]) -> Any:
        calls.append(prompt)
        return LLMResponse(
            content="[压缩边界] 摘要",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )

    result, _success, _inc = await compactor.compact(messages, fake_complete)

    joined = "\n".join(m.content or "" for m in result)
    # chars/4 旧码 (半预算=80 chars)：累计到第 3 条才越 → 只保留 [最新输入]
    # 中间 assistant 回复被吞进摘要。故「第二步」（assistant 回复）幸存与否
    # 是 token 驱动 vs chars/4 的判别点。
    assert "第二步" in joined, "token 驱动切分必须保留中间 assistant 回复（旧 chars/4 会吞掉）"
    assert "最新输入" in joined, "最新消息必须在保留区"


# ── 4. 校准系数与误差观测 ─────────────────────────────────────────────────────


def test_calibration_ratio_applied() -> None:
    """契约（.5）：按模型设置校准系数后 count 返回 raw*ratio（round）。"""
    TokenCounter.set_active_model("test-model")
    base = TokenCounter.count("hello world")
    TokenCounter.set_calibration("test-model", 1.5)
    assert TokenCounter.count("hello world") == round(base * 1.5)
    # 未设置校准的模型不受影响
    TokenCounter.set_active_model("other-model")
    assert TokenCounter.count("hello world") == base


def test_observations_suggest_calibration() -> None:
    """契约（.6）：误差观测（estimated vs provider actual）回填后
    suggested_calibration 收敛到实际比率——按模型建立校准的数据通路。"""
    for _ in range(5):
        TokenCounter.record_observation(model="m1", estimated=100, actual=120)
    suggested = TokenCounter.suggested_calibration("m1")
    assert suggested == pytest.approx(1.2)
    assert TokenCounter.suggested_calibration("m2") is None, "无观测数据的模型返回 None"


# ── 5. breakdown 分项计量 ─────────────────────────────────────────────────────


def test_breakdown_by_role_includes_tool_calls() -> None:
    """契约（.7）：分项计量按 system/user/assistant/tool 分开统计
    tool_calls 参数计入 assistant 侧，且分项之和与总量一致。"""
    messages = [
        Message(role=MessageRole.SYSTEM, content="sys prompt"),
        Message(role=MessageRole.USER, content="用户问题"),
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[]),
        Message(role=MessageRole.TOOL, content="tool output result", tool_call_id="t1"),
    ]
    breakdown = TokenCounter.breakdown(messages)
    assert set(breakdown) == {"system", "user", "assistant", "tool"}
    assert breakdown["system"] > 0 and breakdown["user"] > 0 and breakdown["tool"] > 0
    assert sum(breakdown.values()) == sum(TokenCounter.count(m.content or "") for m in messages)


# ── 6. 生产误差回填接线 ───────────────────────────────────────────────────────


def test_auto_calibration_applied_from_observation_window() -> None:
    """
    无需显式 set_calibration（生产只按 provider 设置 active model，校准值由观测
    自动收敛；否则观测数据白记，_ratio 恒 1.0 校准永不生效）。"""
    TokenCounter.set_active_model("m-auto")
    base = TokenCounter.count("深度学习计划：构建个人多Agent数字员工组织")
    for _ in range(15):
        TokenCounter.record_observation(model="m-auto", estimated=100, actual=120)
    assert TokenCounter.count("深度学习计划：构建个人多Agent数字员工组织") > base, (
        "观测 actual>estimated 时校准必须使 count 上修（自动应用，非手动 set_calibration）"
    )


def test_auto_calibration_ignores_extreme_ratios() -> None:
    """
    校准不得应用到护栏区间外——防计量被单次异常 provider usage 撑爆。"""
    TokenCounter.set_active_model("m-wild")
    for _ in range(15):
        TokenCounter.record_observation(model="m-wild", estimated=100, actual=500)
    ratio = TokenCounter.suggested_calibration("m-wild")
    assert ratio == pytest.approx(5.0)
    from bandi.utils.token_counter import _CALIB_RATIO_RANGE

    assert not (_CALIB_RATIO_RANGE[0] <= ratio <= _CALIB_RATIO_RANGE[1]), (
        "护栏外建议值不得被自动应用（构造前提：5x 超出 [0.3, 3.0]）"
    )
    assert TokenCounter._calibrations.get("m-wild") is None, "护栏外建议不得写入校准表"


def test_calibration_active_model_wired_from_provider() -> None:
    """
    provider/model_name 透传、空名/None 防御为空（校准不误命中其他模型）。"""
    from types import SimpleNamespace

    from bandi.utils.token_counter import set_active_model_for_provider

    set_active_model_for_provider(SimpleNamespace(model_name="wire-model"))
    assert TokenCounter._active_model == "wire-model"
    set_active_model_for_provider(SimpleNamespace(model_name=""))
    assert TokenCounter._active_model == ""
    set_active_model_for_provider(None)
    assert TokenCounter._active_model == ""


@pytest.mark.asyncio
async def test_usage_consumption_records_estimation_error() -> None:
    """契约（.8）：on_thinking 消费 response.usage 时必须把「估算 vs provider
    实际」写入观测窗口（校准系数的数据源生产接线）。"""
    from types import SimpleNamespace

    from bandi.agent.base import ExecutionContext
    from bandi.agent.deps import QueryDeps
    from bandi.agent.states import on_thinking
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import LLMResponse, TokenUsage

    response = LLMResponse(
        content="回答",
        tool_calls=[],
        usage=TokenUsage(prompt_tokens=500, completion_tokens=10),
        finish_reason="stop",
    )

    async def call_model(ctx: Any, messages: list[dict]) -> LLMResponse:
        return response

    async def noop_compact(ctx: Any) -> None:
        raise AssertionError("不应触发压缩链")

    deps = QueryDeps(
        call_model=call_model,
        autocompact=noop_compact,
        microcompact=noop_compact,
        new_id=lambda: "x",
    )
    provider = SimpleNamespace(model_name="stub-model", protocol="openai", supports_streaming=False)
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=provider,
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.query_deps = deps
    ctx.raw_input = "hi"

    await on_thinking(ctx)

    observations = [o for o in TokenCounter._observations if o["model"] == "stub-model"]
    assert observations, "usage 消费必须回填误差观测（estimated vs actual）"
    assert observations[0]["actual"] == 500
    assert observations[0]["estimated"] > 0
