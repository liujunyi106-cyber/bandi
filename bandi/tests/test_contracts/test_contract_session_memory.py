"""
  摘要拼装：working scratchpad turn 快照 → [上下文摘要] 结构化摘要；
  keep index 算法：尾部窗口扩展（min 双下限/max 上限/boundary floor/
     tool_use+tool_result 对不拆散）
  接线回退链：_production_autocompact 先试 SM——有快照走 stage="session_memory"
     且零 LLM 调用；无快照/无 memory_manager/无 session_id 回退现有 pipeline
     摘要恒一条（旧 SM 摘要被入口剥离）
     （boundary 标记 stage=session_memory 最终产物）

破坏者断言：tool 结果在保留区而其 assistant tool_calls 被裁 = API 孤儿必须防；
不传 session_id 时 SM 路径不得启用（fail-closed 回退）。
"""

from typing import Any

import pytest

from bandi.core.config import BandiConfig
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage, ToolCall


def _store(tmp_path: Any) -> Any:
    from bandi.memory.working import ScratchpadStore

    return ScratchpadStore(str(tmp_path / "working.db"))


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text)


def _assistant(text: str, tool_calls: list[ToolCall] | None = None) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=text, tool_calls=tool_calls)


def _tool_result(call_id: str, output: str = "ok") -> Message:
    return Message(role=MessageRole.TOOL, content=output, tool_call_id=call_id)


# ── 摘要拼装 ──────────────────────────────────────────────────────────────


def test_summary_from_scratchpad_snapshots(tmp_path: Any) -> None:
    """契约（P1a）：turn 快照拼装为结构化摘要，带 [上下文摘要] 前缀协议
    按 turn 序号排序、内容保真。"""
    from bandi.memory.session_memory import build_session_memory_summary

    store = _store(tmp_path)
    store.set("turn:0", "用户: 排查 seed 报错 → 回答: 定位缺失参数", session_id="s1")
    store.set("turn:2", "用户: 继续修复 → 回答: 已给出补丁", session_id="s1")
    store.set("turn:1", "用户: 查配置 → 回答: config.yaml 第 3 行", session_id="s1")
    store.set("other:key", "非 turn 快照不进摘要", session_id="s1")

    summary = build_session_memory_summary(store, session_id="s1")

    assert summary is not None
    assert summary.startswith("[上下文摘要]"), "必须复用摘要前缀协议"
    assert summary.index("seed 报错") < summary.index("查配置") < summary.index("继续修复"), (
        "必须按 turn 序号排序"
    )
    assert "非 turn 快照" not in summary, "非 turn:* 键不得进摘要"


def test_summary_returns_none_without_snapshots(tmp_path: Any) -> None:
    """
    isSessionMemoryEmpty）；其他会话的快照不算数。"""
    from bandi.memory.session_memory import build_session_memory_summary

    store = _store(tmp_path)
    store.set("turn:0", "别的会话内容", session_id="sess-other")

    assert build_session_memory_summary(store, session_id="sess-mine") is None


def test_summary_capped_by_total_token_budget(tmp_path: Any) -> None:
    """契约（P1c，审查项3）：摘要总 token 必须受上限约束（防海量 turn 快照把
    摘要撑到失控，进 SYSTEM 消息挤爆上下文）——超过预算时保留最近 turn（尾部
    优先，会话记忆新近性），内容不丢可发现性（至少最新快照进入摘要）。"""
    from bandi.memory.session_memory import build_session_memory_summary
    from bandi.utils.token_counter import TokenCounter

    store = _store(tmp_path)
    # 30 个 turn，每个约 40 字符 → 单条≈40 tokens，30 条总额≈1200 ≫ 200 预算
    for i in range(30):
        store.set(f"turn:{i}", f"用户: 第{i}轮排查 {('明细' * 10)} → 回答: 已处理", session_id="s1")

    summary = build_session_memory_summary(store, session_id="s1", max_tokens=200)

    assert summary is not None
    kept = [ln for ln in summary.splitlines() if ln.strip().startswith("- turn")]
    total_tokens = TokenCounter.count(summary)
    assert total_tokens <= 200, f"摘要 token {total_tokens} 必须落在预算内"
    assert len(kept) < 30, "快照超预算时必须裁掉部分旧快照"
    assert any("turn 29" in ln for ln in kept), "最新 turn 快照必须保留（尾部优先）"


# ── keep index 算法 ───────────────────────────────────────────────────────


def _config(**kwargs: Any) -> Any:
    from bandi.memory.session_memory import SessionMemoryCompactConfig

    return SessionMemoryCompactConfig(**kwargs)


def test_keep_index_keeps_all_when_under_minimums() -> None:
    """契约（P2a）：消息量低于双下限 → 全保（index=0）。"""
    from bandi.memory.session_memory import calculate_keep_index

    msgs = [_user(f"消息 {i}") for i in range(3)]

    idx = calculate_keep_index(msgs, config=_config(min_tokens=100_000))

    assert idx == 0


def test_keep_index_caps_at_max_tokens() -> None:
    """契约（P2b）：扩展撞 max_tokens 上限即停——保留区 token 不超上限。"""
    from bandi.memory.session_memory import calculate_keep_index
    from bandi.utils.token_counter import TokenCounter

    big = "长" * 8000  # 单条 estimate≈8000 tokens（tiktoken 中文 ≈1 字/token）
    msgs = [_user(big) for _ in range(30)]
    cfg = _config(min_tokens=500_000, max_tokens=25_000)

    idx = calculate_keep_index(msgs, config=cfg)

    kept_tokens = sum(TokenCounter.estimate(m.content or "") for m in msgs[idx:])
    assert kept_tokens <= cfg.max_tokens + TokenCounter.estimate(big), (
        f"保留区不得超过 max_tokens 一条余量（实际 {kept_tokens}）"
    )
    assert idx > 0, "超上限时必须裁掉头部部分消息"


def test_keep_index_floors_at_compact_boundary() -> None:
    """契约（P2c）：向前扩展不得越过最后一个 [压缩边界] 标记（floor 不变量）。"""
    from bandi.memory.compression_pipeline import build_boundary_message
    from bandi.memory.session_memory import calculate_keep_index

    boundary = build_boundary_message("micro", 20, 5, 4000, 900)
    msgs = [_user("边界前的老消息")] * 8 + [boundary] + [_user(f"新消息 {i}") for i in range(4)]

    idx = calculate_keep_index(
        msgs,
        config=_config(min_tokens=500_000, max_tokens=1_000_000),
    )

    assert idx >= msgs.index(boundary) + 1, (
        "floor=最后 boundary 之后——boundary 之前的消息不得进保留区"
    )


def test_keep_index_preserves_tool_pair() -> None:
    """契约（P2d+破坏者）：TOOL 结果在保留区时其 assistant(tool_calls) 必须同在
    （拆对 = API 孤儿 tool_result）。"""
    from bandi.memory.session_memory import calculate_keep_index

    call = ToolCall(id="tc-1", name="bash", arguments={"command": "ls"})
    pair_assistant = _assistant("执行命令", tool_calls=[call])
    msgs = [
        _user("最早的"),
        *(_user(f"填充 {i}") for i in range(6)),
        pair_assistant,
        _tool_result("tc-1"),
        _user("收尾输入"),
    ]
    cfg = _config(min_tokens=1, min_text_messages=1, max_tokens=1)

    idx = calculate_keep_index(msgs, config=cfg)

    kept_roles = [(m.role, m.tool_call_id, bool(m.tool_calls)) for m in msgs[idx:]]
    has_tool_result = any(r == MessageRole.TOOL for r, _, _ in kept_roles)
    if has_tool_result:
        assert any(tc_list for _, _, tc_list in kept_roles), (
            f"保留区含 TOOL 结果时必须含其 assistant(tool_calls)（实际 {kept_roles}）"
        )


# ── 接线回退链 ────────────────────────────────────────────────────────────


def _mk_ctx(
    tmp_path: Any,
    *,
    store: Any | None = None,
    session_id: str | None = "s1",
    messages: list[Message] | None = None,
) -> Any:
    """构造最小 ExecutionContext 形状（SimpleNamespace 鸭子类型）。"""
    from types import SimpleNamespace

    memory_manager = None
    if store is not None:
        from bandi.memory.memory_provider import MemoryManager

        memory_manager = MemoryManager(working=store)
    ctx = SimpleNamespace(
        meta={
            "memory_manager": memory_manager,
            "session_id": session_id,
            "provider_registry": None,
            "bandi_config": None,
        },
        messages=messages or [],
        total_tokens=999_999,
        config=BandiConfig().agent,
        provider=None,
        compress=SimpleNamespace(pipeline=None),
    )
    return ctx


@pytest.mark.asyncio
async def test_autocompact_prefers_session_memory_zero_llm(tmp_path: Any) -> None:
    """契约（P3a）：有快照时 autocompact 走 SM 路径——stage=session_memory 且
    provider.complete 零调用（Sentinel 断言零 LLM 成本）。"""
    from bandi.agent.deps import make_default_deps

    class _SentinelProvider:
        complete_calls = 0

        def count_tokens(self, text: str) -> int:
            return len(text)

        async def complete(self, *a: Any, **k: Any) -> LLMResponse:  # pragma: no cover
            _SentinelProvider.complete_calls += 1
            raise AssertionError("SM 路径严禁调用 LLM")

    store = _store(tmp_path)
    store.set("turn:0", "用户: 排查 seed → 回答: 定位完成", session_id="s1")
    ctx = _mk_ctx(tmp_path, store=store)
    ctx.provider = _SentinelProvider()
    # 前 12 条 ti=0（已被 turn:0 快照覆盖）；「当前输入」ti=1（本轮未快照必须保留）
    ctx.messages = [_user(f"历史 {i}") for i in range(12)] + [
        Message(role=MessageRole.USER, content="当前输入", turn_index=1)
    ]

    outcome = await make_default_deps().autocompact(ctx)

    assert outcome.success
    assert outcome.stats.stage == "session_memory", (
        f"有快照必须走 SM 路径（实际 stage={outcome.stats.stage!r}）"
    )
    assert _SentinelProvider.complete_calls == 0, "SM 路径零 LLM 调用"
    joined = "\n".join((m.content or "") for m in outcome.messages)
    assert "seed" in joined and "[上下文摘要]" in joined, "摘要必须注入产物"
    assert "当前输入" in joined, "快照边界之后的当前轮消息必须保留"


@pytest.mark.asyncio
async def test_autocompact_falls_back_without_session_memory(tmp_path: Any) -> None:
    """契约（P3b）：无快照 → 回退现有 pipeline（stage 非 session_memory）
    行为与 j 日契约钉住的 autocompact 兼容不回归。"""
    from bandi.agent.deps import make_default_deps

    empty_ctx = _mk_ctx(tmp_path, store=_store(tmp_path))  # working 无快照
    empty_ctx.provider = _FakeCountingProvider()
    empty_ctx.messages = [_user(f"历史 {i}") for i in range(6)]

    outcome = await make_default_deps().autocompact(empty_ctx)

    assert outcome.stats.stage != "session_memory", "无快照必须回退 pipeline"

    no_mm_ctx = _mk_ctx(tmp_path, store=None)  # 无 memory_manager
    no_mm_ctx.provider = _FakeCountingProvider()
    no_mm_ctx.messages = list(empty_ctx.messages)
    outcome2 = await make_default_deps().autocompact(no_mm_ctx)
    assert outcome2.stats.stage != "session_memory"

    no_sid_ctx = _mk_ctx(tmp_path, store=_store(tmp_path), session_id=None)  # 无 session_id
    no_sid_ctx.provider = _FakeCountingProvider()
    no_sid_ctx.messages = list(empty_ctx.messages)
    outcome3 = await make_default_deps().autocompact(no_sid_ctx)
    assert outcome3.stats.stage != "session_memory", "无 session_id 必须 fail-closed 回退"


class _FakeCountingProvider:
    """最小 provider——计数用 token 估算 + 兜底 complete（progressive 分支兜底
    不产生真实摘要语义）。"""

    def count_tokens(self, text: str) -> int:
        return len(text)

    async def complete(self, messages: Any, tools: Any = None) -> LLMResponse:
        return LLMResponse(
            content="压缩摘要",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
            finish_reason="stop",
        )


@pytest.mark.asyncio
async def test_second_sm_compact_keeps_single_summary(tmp_path: Any) -> None:
    """
    （旧摘要在被裁历史/或入口剥离，不得双摘要并存）。"""
    from bandi.agent.deps import make_default_deps

    store = _store(tmp_path)
    store.set("turn:0", "用户: 第一轮任务 → 回答: 完成", session_id="s1")
    ctx = _mk_ctx(tmp_path, store=store)
    ctx.provider = _FakeCountingProvider()
    old_summary = Message(
        role=MessageRole.USER,
        content="[上下文摘要] 更早轮次的旧摘要内容",
        meta={"synthetic": True},
    )
    ctx.messages = [
        _user("远古消息"),
        old_summary,
        *(_user(f"中间历史 {i}") for i in range(8)),
        _user("最新输入"),
    ]

    first = await make_default_deps().autocompact(ctx)
    assert first.stats.stage == "session_memory"
    ctx.messages = first.messages
    ctx.total_tokens = sum(len(m.content or "") for m in ctx.messages)

    second = await make_default_deps().autocompact(ctx)
    if second.stats.stage == "session_memory":
        summaries = [m for m in second.messages if (m.content or "").startswith("[上下文摘要]")]
        assert len(summaries) == 1, f"二次 SM 压缩后摘要必须恒一条（实际 {len(summaries)}）"


@pytest.mark.timing
@pytest.mark.asyncio
async def test_run_agent_turn_sm_compact_end_to_end(tmp_path: Any, monkeypatch: Any) -> None:
    """
    SM 路径生效（boundary 带 stage=session_memory 最终产物）→ 回复正常产出。
    第二轮依赖第一轮 scratchpad 快照（跨轮闭环）。"""
    from pathlib import Path
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    provider = MockProvider()
    # 窗口后压缩生效、发送量变小，不再反复触发压缩
    # /tiktoken 计量校准：r0 usage 升至内容真实量级（count 口径 ~19.5k token）
    # 使 r1 切 16000 窗口后 usage 口径越线触发、SM 压后 count 可落在预算内
    # （旧 12000 窗口在 tiktoken 计量下无解：system+当前 raw 不可裁部分已超线）
    usages = [(8000, 100), (100, 30), (100, 30), (100, 30)]
    for i in range(4):
        p, c = usages[i]
        provider.add_response(
            LLMResponse(
                content=f"第{i}轮处理完成，结论已沉淀",
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=p, completion_tokens=c),
                finish_reason="stop",
            )
        )

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        # 未收录模型：显式 context_window 作为兜底生效（放开语义下目录优先，
        # 本用例需要可控窗口，用未收录模型名让配置兜底路径生效）
        cfg.llm.default_model = "unknown-e2e-model"
        # max_iterations=3：压缩往返（THINKING→COMPRESSING→THINKING）会消耗
        cfg.agent.max_iterations = 3
        # ——total_tokens 必超阈值触发正常档压缩，此时快照已存在 → SM 路径生效
        cfg.agent.context_window = 128_000

        import bandi.agent.states as states_mod

        stages: list[str] = []
        orig_get_deps = states_mod.get_query_deps

        def _spy_deps(ctx: Any) -> Any:
            deps = orig_get_deps(ctx)
            inner_autocompact = deps.autocompact
            inner_microcompact = deps.microcompact

            async def spy_autocompact(c: Any) -> Any:
                outcome = await inner_autocompact(c)
                stages.append(outcome.stats.stage)
                return outcome

            async def spy_microcompact(c: Any) -> Any:
                outcome = await inner_microcompact(c)
                stages.append(f"m:{outcome.stats.stage}")
                return outcome

            import dataclasses

            return dataclasses.replace(
                deps, autocompact=spy_autocompact, microcompact=spy_microcompact
            )

        monkeypatch.setattr(states_mod, "get_query_deps", _spy_deps)

        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            replies = []
            for i in range(4):
                if i == 1:
                    # 切压缩窗口：usage 口径 16200 越 0.85*16000；SM 重放未覆盖快照
                    # 的当前轮消息（turn_index 归属修正后 keep_idx 钳在
                    # summarized_boundary 后）+ 摘要，count 口径收敛至预算内
                    # 压缩后原始用户输入仍在保留区 → 连续锚点免注入，仅一次压缩
                    # 窗口 32000 经「预留 20K 下限 + 窗口 50% 钳制」→
                    # effective=16000（与旧 16000 等效——阈值边界场景保持可测）
                    # 工具面 Top-N 裁剪后 request 口径变小（~41k -> ~25k），窗口同步下调
                    # 让压缩第二闸（request >= 窗口*0.92）继续越线
                    cfg.agent.context_window = 24000
                r = await run_agent_turn(
                    user_input=f"第{i}轮请继续排查 seed 参数问题并补充细节说明" * 300,
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                )
                replies.append(r.reply)

        assert all(replies), f"每轮必须有回复（实际 {replies}）"
        assert "session_memory" in stages, (
            f"生产链路必须至少一次走 SM 压缩路径（实际 stages={stages}）"
        )
        working_db = tmp_path / ".bandi" / "data" / "working.db"
        assert working_db.exists()


# ── 轮次快照结构化（目标 / 终态 / 结果 / 待办 / 产物）──────────────────────


def test_turn_snapshot_has_five_slots_single_line() -> None:
    from bandi.memory.session_memory import build_turn_snapshot

    written = Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id="c1", name="write", arguments={"file_path": "/p/index.html"})],
        turn_index=3,
    )
    snapshot = build_turn_snapshot(
        raw_input="做一个待办 App",
        final_message="基础列表已完成",
        terminal="COMPLETED",
        messages=[written],
        turn_index=3,
        ledger_items=[{"content": "加筛选", "status": "pending"}],
    )
    assert "目标: 做一个待办 App" in snapshot
    assert "终态: done" in snapshot
    assert "结果: 基础列表已完成" in snapshot
    assert "待办: 1 项" in snapshot and "加筛选" in snapshot
    assert "产物: /p/index.html" in snapshot
    assert "\n" not in snapshot


def test_turn_snapshot_marks_unfinished_turn() -> None:
    from bandi.memory.session_memory import build_turn_snapshot

    snapshot = build_turn_snapshot(
        raw_input="把导出功能做完",
        final_message="",
        terminal="MAX_TURNS",
        messages=[],
        turn_index=5,
        ledger_items=[],
    )
    assert "终态: partial" in snapshot
    assert "未完成" in snapshot
    assert "产物" not in snapshot


def test_turn_snapshot_is_length_bounded() -> None:
    from bandi.memory.session_memory import build_turn_snapshot

    snapshot = build_turn_snapshot(
        raw_input="目" * 4000,
        final_message="结" * 8000,
        terminal="TOOL_ERROR",
        messages=[],
        turn_index=9,
        ledger_items=[{"content": "待" * 500, "status": "in_progress"}],
    )
    assert len(snapshot) <= 700
    assert "终态: blocked" in snapshot


def test_turn_snapshot_ignores_other_turn_artifacts() -> None:
    from bandi.memory.session_memory import build_turn_snapshot

    old = Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id="c9", name="write", arguments={"file_path": "/old/x.py"})],
        turn_index=1,
    )
    snapshot = build_turn_snapshot(
        raw_input="继续",
        final_message="done",
        terminal="COMPLETED",
        messages=[old],
        turn_index=2,
        ledger_items=[],
    )
    assert "产物" not in snapshot, "其他轮次的产物不得混入本轮快照"


def test_turn_snapshot_extracts_preview_path_from_reply() -> None:
    """真实形态：产物路径常出现在回复正文（预览路径：`canvas/x/index.html`），
    而非 write 工具参数——快照必须能从回复补出产物槽。"""
    from bandi.memory.session_memory import build_turn_snapshot

    snapshot = build_turn_snapshot(
        raw_input="做个待办 App",
        final_message="已创建完成！预览路径：`canvas/待办事项-app/index.html`",
        terminal="COMPLETED",
        messages=[],
        turn_index=2,
        ledger_items=[],
    )
    assert "产物: canvas/待办事项-app/index.html" in snapshot


def test_turn_snapshot_ignores_urls_as_artifacts() -> None:
    from bandi.memory.session_memory import build_turn_snapshot

    snapshot = build_turn_snapshot(
        raw_input="看下这个",
        final_message="参考 https://example.com/a/index.html 就够了",
        terminal="COMPLETED",
        messages=[],
        turn_index=1,
        ledger_items=[],
    )
    assert "产物" not in snapshot, "URL 不是本地产物"
