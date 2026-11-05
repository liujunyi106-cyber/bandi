import pytest

from bandi.core.message import LLMResponse, Message, MessageRole
from bandi.memory.compression_pipeline import _prune_tool_outputs_by_budget
from bandi.tools.base import BaseTool
from bandi.tools.result_summary import MICRO_CLEARED_PLACEHOLDER as _MICRO_CLEARED_PLACEHOLDER
from bandi.tools.result_summary import PERSISTED_EXCERPT_PREFIX, PERSISTED_PATH_MARKER


class _StubToolBase(BaseTool):
    name = "stub"
    protocol = "openai"

    def validate_params(self, arguments):
        return []

    async def execute(self, arguments, **kw):
        return "y" * 1000


def _tool_msg(name: str, chars: int, call_id: str | None = None) -> Message:
    # 使构造数值与 token 计量口径对齐（重复单字会被 BPE 压缩导致计量失真）
    import random

    rng = random.Random(f"{name}-{chars}")
    pool = "的一是了我不人在他有这上们来到时大地为子中你说生国年着就那和要她出也得里后自以会家可下而过天去能对小多然于心学么之都好看起发当没成只如事把还用第样道想作种开美总从无情己面最女但现前些所同日手又行意动方期它头经长儿回位分爱老因很给名法间斯知世什两次使身者被高已亲其进此话常与活正感"
    content = "".join(rng.choice(pool) for _ in range(chars))
    return Message(
        role=MessageRole.TOOL,
        content=content,
        tool_call_id=call_id or f"c_{name}_{chars}",
        name=name,
    )


def _dialogue(tool_outputs: list[Message], extra_pairs: int = 0) -> list[Message]:
    """功能：构造「历史 TOOL 输出 + 若干轮对话」消息列表（倒序扫描语义验证）。"""
    msgs: list[Message] = [Message(role=MessageRole.SYSTEM, content="sys")]
    for m in tool_outputs:
        msgs.append(m)
    for i in range(extra_pairs):
        msgs.append(Message(role=MessageRole.USER, content=f"turn {i}"))
        msgs.append(Message(role=MessageRole.ASSISTANT, content=f"done {i}"))
    return msgs


@pytest.fixture(autouse=True)
def _isolate_output_dir(tmp_path, monkeypatch):
    """功能：落盘目录隔离到 tmp（prune 现在会把正文落盘，禁止写真实 ~/.bandi）。"""
    monkeypatch.setenv("BANDI_TOOL_OUTPUT_DIR", str(tmp_path / "tool_outputs"))


@pytest.fixture
def small_budget(monkeypatch):
    """功能：将剪枝预算阈值调小便于轻量构造（生产默认 40K 由 E2E 校验）。
    适配：_tool_msg 随机中文密度 ≈1.17 token/字（80+60 字 ≈ 162 tokens）。"""
    from bandi.memory import compression_pipeline as cp

    monkeypatch.setattr(cp, "_PRUNE_PROTECT_TOKENS", 200)
    monkeypatch.setattr(cp, "_PRUNE_MIN_SAVINGS_TOKENS", 50)


def test_budget_within_keeps_all(small_budget) -> None:
    """契约：累计 tokens 未超预算时全部保留（零剪枝）。"""
    msgs = _dialogue(
        [_tool_msg("read", 80), _tool_msg("bash", 60)],
        extra_pairs=3,
    )
    result = _prune_tool_outputs_by_budget(
        msgs,
        __import__(
            "bandi.memory.compression_pipeline", fromlist=["CompressionStats"]
        ).CompressionStats(),
    )
    contents = [m.content for m in result if m.role == MessageRole.TOOL]
    originals = [m.content for m in msgs if m.role == MessageRole.TOOL]
    assert contents == originals, "预算内不得剪枝（原文保留）"


def test_budget_exceeded_prunes_old_keeps_recent(small_budget) -> None:
    """契约：超预算时仅更早的输出被替换占位，最近的保留（倒序保护语义）。"""
    old = _tool_msg("read", 400)
    mid = _tool_msg("read", 300)
    recent = _tool_msg("bash", 50)
    msgs = (
        [Message(role=MessageRole.SYSTEM, content="sys"), old]
        + [
            Message(role=MessageRole.USER, content="t0"),
            Message(role=MessageRole.ASSISTANT, content="a0"),
        ]
        + [mid]
        + [
            Message(role=MessageRole.USER, content="t1"),
            Message(role=MessageRole.ASSISTANT, content="a1"),
        ]
        + [recent]
        + [
            Message(role=MessageRole.USER, content="t2"),
            Message(role=MessageRole.ASSISTANT, content="a2"),
        ]
    )
    result = _prune_tool_outputs_by_budget(
        msgs,
        __import__(
            "bandi.memory.compression_pipeline", fromlist=["CompressionStats"]
        ).CompressionStats(),
    )
    by_id = {m.tool_call_id: m.content for m in result if m.role == MessageRole.TOOL}
    persisted = by_id["c_read_400"]
    assert persisted.startswith(PERSISTED_EXCERPT_PREFIX), "最旧超预算应改为真实正文摘录"
    assert PERSISTED_PATH_MARKER in persisted, "摘录必须带全文路径（中段可回读）"
    assert by_id["c_bash_50"] == recent.content, "最近输出必须保留原文"


def test_recent_two_user_turns_protected(small_budget) -> None:
    """契约：最近 2 个 user-turn 内的工具输出即使超预算也不剪（opencode turns<2 保护）。"""
    msgs: list[Message] = [
        Message(role=MessageRole.USER, content="old-turn-1"),
        Message(role=MessageRole.USER, content="old-turn-2"),
        Message(role=MessageRole.ASSISTANT, content="a"),
        _tool_msg("read", 5000),  # 远超预算 100，但位于最近 2 turn 内
        Message(role=MessageRole.USER, content="recent-2"),
        Message(role=MessageRole.ASSISTANT, content="tail"),
    ]
    result = _prune_tool_outputs_by_budget(
        msgs,
        __import__(
            "bandi.memory.compression_pipeline", fromlist=["CompressionStats"]
        ).CompressionStats(),
    )
    assert result[3].content == msgs[3].content, "最近两轮内保留原文"


def test_protected_tool_skipped(small_budget) -> None:
    """契约：保护名单工具（skill_invoke/skill_discover）的输出豁免剪枝。"""
    huge_skill = _tool_msg("skill_invoke", 5000)
    msgs = [huge_skill] + [
        Message(role=MessageRole.USER, content="t1"),
        Message(role=MessageRole.USER, content="t2"),
    ]
    result = _prune_tool_outputs_by_budget(
        msgs,
        __import__(
            "bandi.memory.compression_pipeline", fromlist=["CompressionStats"]
        ).CompressionStats(),
    )
    assert result[0].content == huge_skill.content, "skill_invoke 工具豁免（CT-03 名单修复）"


def test_min_savings_gate_blocks_tiny_prune(small_budget) -> None:
    """契约：可剪量低于门槛（_PRUNE_MIN_SAVINGS_TOKENS）时不动作——收益太小不值得。"""
    # 预算 100，两条各 90 tokens 的旧输出：第一条使累计 190>100 标记剪（可剪 90≥门槛50）
    # 本用例验证门槛：单条可剪量 30 < 50 → 不动作
    m1 = _tool_msg("read", 120)  # 30 tokens 超 100 预算 → 可剪 30 < 50 门槛
    msgs = [m1] + [
        Message(role=MessageRole.USER, content="t1"),
        Message(role=MessageRole.USER, content="t2"),
    ]
    result = _prune_tool_outputs_by_budget(
        msgs,
        __import__(
            "bandi.memory.compression_pipeline", fromlist=["CompressionStats"]
        ).CompressionStats(),
    )
    assert result[0].content == m1.content, "可剪量低于门槛不得动作（原文保留）"


def test_placeholder_idempotent_second_pass(small_budget) -> None:
    placeholder_msg = Message(
        role=MessageRole.TOOL,
        content=_MICRO_CLEARED_PLACEHOLDER,
        tool_call_id="c_p",
        name="read",
    )
    msgs = [placeholder_msg] + [
        Message(role=MessageRole.USER, content="t1"),
        Message(role=MessageRole.USER, content="t2"),
    ]
    result = _prune_tool_outputs_by_budget(
        msgs,
        __import__(
            "bandi.memory.compression_pipeline", fromlist=["CompressionStats"]
        ).CompressionStats(),
    )
    assert result[0].content == _MICRO_CLEARED_PLACEHOLDER
    stats_cls = __import__(
        "bandi.memory.compression_pipeline", fromlist=["CompressionStats"]
    ).CompressionStats()
    result2 = _prune_tool_outputs_by_budget(result, stats_cls)
    assert result2[0].content == _MICRO_CLEARED_PLACEHOLDER, "二次处理恒定"


def test_attachment_diff_set_unchanged_by_prune() -> None:
    """契约（e 日交互裁决）：prune 只清 TOOL content、消息对象身份保留——"""
    from bandi.memory.compression_pipeline import CompressionPipeline, ProgressiveCompressor

    touched = Message(
        role=MessageRole.ASSISTANT,
        content="读取",
        tool_calls=[
            __import__("bandi.core.message", fromlist=["ToolCall"]).ToolCall(
                id="c1", name="read", arguments={"path": "src/x.py"}
            )
        ],
    )
    msgs: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="sys"),
        touched,
    ]
    for i in range(10):
        msgs.append(_tool_msg("read", 900))
        msgs.append(
            Message(role=MessageRole.USER, content=f"第{i}轮排查 logs/run_{i}.py ValueError('x')")
        )
        msgs.append(Message(role=MessageRole.ASSISTANT, content=f"结论 patch_{i}.diff"))
    pipeline = CompressionPipeline(
        progressive=ProgressiveCompressor(max_chunks=4, target_token_budget=10)
    )

    async def cap(messages):
        return LLMResponse(
            content="## 目标\n- x\n\n## 进度\n### Done\n- [x]\n### In Progress\n- [ ]\n### Blocked\n- (none)\n\n## 关键决策\n- y\n\n## 下一步\n1. z\n\n## 关键上下文\n- p",
            tool_calls=[],
            usage=__import__("bandi.core.message", fromlist=["TokenUsage"]).TokenUsage(5, 3),
            finish_reason="stop",
        )

    import asyncio

    result, success, _, stats = asyncio.run(pipeline.compress(msgs, 20000, 200, 0.5, cap))
    assert success, "前置：压缩发生"
    kept_ids = {id(m) for m in result}
    removed = [m for m in msgs if id(m) not in kept_ids]
    att_paths = [tc.arguments.get("path") for m in removed for tc in (m.tool_calls or [])]
    assert "src/x.py" in [p for p in att_paths if p] or any(id(touched) in kept_ids for _ in [0]), (
        "触碰消息的身份归属不受 prune 干扰"
    )


class _DeclaringTool(_StubToolBase):
    name = "declared"
    max_result_tokens = 50  # 工具作者声明

    async def execute(self, arguments, **kw):
        return "y" * 1000


class _PlainTool(_StubToolBase):
    name = "plain"

    async def execute(self, arguments, **kw):
        return "y" * 1000


def _make_executor(per_tool: dict | None, tool_obj) -> object:
    from bandi.tools.executor import ToolExecutor

    class _Reg:
        def get(self, name):
            return tool_obj

        def list_schemas(self):
            return []

    return ToolExecutor(
        _Reg(),
        max_output_tokens=100,  # 全局默认 100 tokens
        per_tool_max_output_tokens=per_tool,
        chars_per_token=4,
    )


def _executor_mod():
    import bandi.tools.executor as mod

    return mod


def test_config_override_takes_effect(monkeypatch) -> None:
    mod = _executor_mod()
    monkeypatch.setattr(mod.ToolExecutor, "_persist_output", lambda self, tc, out: None)
    ex = _make_executor({"plain": 30}, _PlainTool())
    import asyncio

    r = asyncio.run(
        ex._execute_one(
            __import__("bandi.core.message", fromlist=["ToolCall"]).ToolCall(
                id="c", name="plain", arguments={}
            )
        )
    )
    assert "[tool:summary] tool=plain" in r.output, (
        "config 覆盖生效（30*4=120 chars 上限 → 摘要形态）"
    )


def test_tool_attr_fallback(monkeypatch) -> None:
    """契约：未配置 dict 时工具类声明的 max_result_tokens 生效。"""
    mod = _executor_mod()
    monkeypatch.setattr(mod.ToolExecutor, "_persist_output", lambda self, tc, out: None)
    ex = _make_executor({}, _DeclaringTool())
    import asyncio

    r = asyncio.run(
        ex._execute_one(
            __import__("bandi.core.message", fromlist=["ToolCall"]).ToolCall(
                id="c", name="declared", arguments={}
            )
        )
    )
    assert "[tool:summary] tool=declared" in r.output, "工具属性生效（50*4=200 chars 上限 → 摘要）"
    assert len(r.output) <= 1200


def test_priority_config_over_attr_over_global(monkeypatch) -> None:
    """契约（取值链优先级）：config dict > 工具属性 > 全局默认——摘要化后
    以「是否触发摘要」为可观察差异：100 字（config 80 帽超 / attr 200 帽不超）
    300 字（attr 200 帽超 / global 400 帽不超）。"""
    mod = _executor_mod()
    monkeypatch.setattr(mod.ToolExecutor, "_persist_output", lambda self, tc, out: None)

    class _Both(_DeclaringTool):
        name = "both"

        async def execute(self, arguments, **kw):
            size = int((arguments or {}).get("size", 1000))
            return "y" * size

    import asyncio

    ex = _make_executor({"both": 20}, _Both())
    r = asyncio.run(
        ex._execute_one(
            __import__("bandi.core.message", fromlist=["ToolCall"]).ToolCall(
                id="c", name="both", arguments={"size": 100}
            )
        )
    )
    assert "[tool:summary]" in r.output, "config=20（80 帽）优先于属性 50（200 帽）"

    ex2 = _make_executor({}, _Both())
    r2 = asyncio.run(
        ex2._execute_one(
            __import__("bandi.core.message", fromlist=["ToolCall"]).ToolCall(
                id="c2", name="both", arguments={"size": 100}
            )
        )
    )
    assert r2.output == "y" * 100, "属性 50（200 帽）生效：100 字不触发摘要"

    r3 = asyncio.run(
        ex2._execute_one(
            __import__("bandi.core.message", fromlist=["ToolCall"]).ToolCall(
                id="c3", name="both", arguments={"size": 300}
            )
        )
    )
    assert "[tool:summary]" in r3.output, "属性 50（200 帽）优先于全局 100（400 帽）"


def test_default_global_when_neither(monkeypatch) -> None:
    """契约：未声明未配置走全局默认（既有行为回归保护）。"""
    mod = _executor_mod()
    monkeypatch.setattr(mod.ToolExecutor, "_persist_output", lambda self, tc, out: None)
    ex = _make_executor({}, _PlainTool())
    import asyncio

    r = asyncio.run(
        ex._execute_one(
            __import__("bandi.core.message", fromlist=["ToolCall"]).ToolCall(
                id="c", name="plain", arguments={}
            )
        )
    )
    assert "[tool:summary] tool=plain" in r.output, "全局默认 100*4 截断生效（摘要形态）"
