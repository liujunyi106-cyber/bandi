from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bandi.core.message import Message, MessageRole, ToolCall
from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.markdown_memory import (
    PROJECT_MEMORY_MAX_CHARS,
    MarkdownMemoryStore,
)
from bandi.skills.importer import SkillImporter, SkillImportError
from bandi.tools.fs_tool import resolve_within_workspace
from bandi.utils.token_counter import TokenCounter


def _make_skill(tmp_path: Path, dir_name: str, skill_name: str, **fm: str) -> Path:
    skill = tmp_path / dir_name
    skill.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {skill_name}"]
    for key, value in fm.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("# Body\n")
    (skill / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return skill


class TestImporterValidationFamily:
    """契约：导入校验必须 fail-closed——任何非法 name/description 一律拒绝，不得静默放行。"""

    @pytest.mark.parametrize(
        ("dir_name", "skill_name"),
        [
            ("a", "a/b"),
            ("a", "../escape"),
            ("a", "."),
            ("a", ".."),
            ("a", "a" * 65),
            ("a", "a\x00b"),
        ],
        ids=["slash", "traversal", "dot", "dotdot", "too_long", "control_char"],
    )
    def test_invalid_name_rejected(self, tmp_path: Path, dir_name: str, skill_name: str) -> None:
        src = _make_skill(tmp_path, dir_name, skill_name)
        with pytest.raises(SkillImportError):
            SkillImporter(skills_dir=str(tmp_path / "out")).import_skill(str(src))

    def test_blank_name_falls_back_to_folder_name(self, tmp_path: Path) -> None:
        src = _make_skill(tmp_path, "fallback-name", "   ")
        result = SkillImporter(skills_dir=str(tmp_path / "out")).import_skill(str(src))
        assert result.skill_name == "fallback-name"

    @pytest.mark.parametrize(
        "description",
        ["[todo] fill", "TODO: pending", "has <angle>", "x" * 1025],
        ids=["todo_bracket", "todo_colon", "angle_bracket", "too_long"],
    )
    def test_invalid_description_rejected(self, tmp_path: Path, description: str) -> None:
        src = _make_skill(tmp_path, "src", "valid-name", description=description)
        with pytest.raises(SkillImportError):
            SkillImporter(skills_dir=str(tmp_path / "out")).import_skill(str(src))

    def test_valid_name_and_description_accepted(self, tmp_path: Path) -> None:
        src = _make_skill(tmp_path, "src", "valid-name", description="A perfectly fine description")
        result = SkillImporter(skills_dir=str(tmp_path / "out")).import_skill(str(src))
        assert result.skill_name == "valid-name"


class TestSandboxExtraRootsFamily:
    """契约：沙箱 extra_roots 只放行白名单内路径；白名单外一律拒绝（fail-closed）。"""

    def _roots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
        ws = tmp_path / "ws"
        ws.mkdir()
        project = tmp_path / "project"
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", ws)
        return ws, project

    def test_inside_extra_root_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws, project = self._roots(tmp_path, monkeypatch)
        memory = project / ".bandi" / "memory" / "MEMORY.md"
        target = resolve_within_workspace(str(memory), extra_roots=[str(project)])
        assert target == memory.resolve()

    def test_outside_all_roots_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws, project = self._roots(tmp_path, monkeypatch)
        outside = tmp_path / "outside" / "secret.txt"
        with pytest.raises(ValueError):
            resolve_within_workspace(str(outside), extra_roots=[str(project)])

    def test_no_extra_roots_keeps_default_sandbox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws, _ = self._roots(tmp_path, monkeypatch)
        outside = tmp_path / "outside" / "secret.txt"
        with pytest.raises(ValueError):
            resolve_within_workspace(str(outside))


# ==================== day124zh ====================


# ── 连续性锚点刷新 ───────────────────────────────────────────────────


def _ctx_for_anchor(**overrides: Any) -> Any:
    """功能：构造可直调 _inject_continuity_anchor 的最小 ctx（生产形态）。"""
    from types import SimpleNamespace

    from bandi.agent.base import CompressionState

    base: dict[str, Any] = {
        "raw_input": "帮我排查 seed 参数问题",
        "messages": [
            Message(role=MessageRole.USER, content="帮我排查 seed 参数问题"),
            Message(role=MessageRole.ASSISTANT, content="开始排查"),
        ],
        "compress": CompressionState(),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _anchor_content(ctx: Any) -> list[str | None]:
    return [m.content for m in ctx.messages if m.role == MessageRole.USER]


def test_anchor_refreshes_within_same_session_after_compact() -> None:
    """契约（H1a+破坏者，真红）：压缩裁掉首轮 raw 后，同一 session（复用同一
    ctx.compress）的后续压缩必须重新注入最新 raw——不得被历史 continuity_injected
    且该标志跨轮存活于 ctx.compress → raw 被裁后永不刷新（失明）。复用同一 ctx
    （而非每次新建 CompressionState）才能真正红出旧 bug（此前测试各自新建 ctx
    flag 恒 False，旧代码也绿 = 伪绿）。"""
    from bandi.agent.states import _inject_continuity_anchor

    ctx = _ctx_for_anchor(
        raw_input="帮我排查 seed 参数问题",
        messages=[
            Message(role=MessageRole.USER, content="历史已压缩"),
            Message(role=MessageRole.ASSISTANT, content="收尾"),
        ],
    )
    _inject_continuity_anchor(ctx)
    assert "帮我排查 seed 参数问题" in _anchor_content(ctx)

    # 同一 ctx、第二轮压缩：旧 raw 已被裁，raw_input 更新为最新轮——
    ctx.messages = [
        Message(role=MessageRole.USER, content="更早历史已压缩"),
        Message(role=MessageRole.ASSISTANT, content="结论：补 codec 参数"),
    ]
    ctx.raw_input = "继续修复 codec 报错"
    _inject_continuity_anchor(ctx)
    assert any(m.content == "继续修复 codec 报错" for m in ctx.messages), (
        "同一 session 内 raw 被压缩裁掉后，最新 raw 必须重新注入为锚点"
        "（不得被跨轮 continuity_injected 永久冻结）"
    )


def test_anchor_not_duplicated_when_raw_still_present() -> None:
    """契约（H1b）：同轮 raw 仍在保留区 → 不重复注入（content-presence 门控天然防重）。"""
    from bandi.agent.states import _inject_continuity_anchor

    ctx = _ctx_for_anchor()
    _inject_continuity_anchor(ctx)
    before = len(ctx.messages)
    _inject_continuity_anchor(ctx)  # raw 未裁，仍在消息内 → 不得再注入
    assert len(ctx.messages) == before, "raw 仍在保留区时不得重复注入锚点"


def test_anchor_truncated_when_raw_oversized() -> None:
    """契约（S8 成分 A）：超长 raw 不得原样 re-inject。真正打爆的是「把压缩掉的
    filler 摘要化，锚点又塞回 16K 原文 → 压缩被完全抵消 → needs 恒 True → 假
    MAX_TURNS 空回复」。现口径为头 600 + 尾 200，任务语义与前缀、最后要求都不丢。"""
    from bandi.agent.states import (
        _ANCHOR_RAW_MAX_CHARS,
        _ANCHOR_RAW_TAIL_CHARS,
        _inject_continuity_anchor,
    )

    big = "这是填充背景内容用于撑大上下文。" * 900  # ≈14.4K chars ≈16K token
    raw = f"记住暗号是 pineapple-77。然后复述一遍下面材料要点：{big}"
    ctx = _ctx_for_anchor(
        raw_input=raw,
        messages=[
            Message(role=MessageRole.USER, content="历史已压缩为摘要"),
            Message(role=MessageRole.ASSISTANT, content="收尾"),
        ],
    )
    _inject_continuity_anchor(ctx)
    injected = [
        m.content
        for m in ctx.messages
        if m.role == MessageRole.USER and (m.content or "").startswith("记住暗号是 pineapple-77")
    ]
    assert len(injected) == 1, "raw 被裁后必须注入任务锚点"
    assert len(injected[0]) <= _ANCHOR_RAW_MAX_CHARS + _ANCHOR_RAW_TAIL_CHARS + 80, (
        "超长 raw 锚点必须截断为头+尾——完整 re-inject 会抵消压缩（S8 成分 A 假 MAX_TURNS）"
    )
    assert "原始输入中段省略" in injected[0], "中段省略必须显式标注"
    assert injected[0].endswith(big[-20:]), "尾段必须保留（最后的要求不丢）"
    assert "pineapple-77" in injected[0], "截断不得丢失任务关键前缀"


def test_anchor_skipped_when_explicitly_suppressed() -> None:
    """契约（H1c）：continuity_injected=True（显式抑制）→ 跳过锚点注入。"""
    from bandi.agent.states import _inject_continuity_anchor

    ctx = _ctx_for_anchor(continuity_injected=True)
    before = len(ctx.messages)
    _inject_continuity_anchor(ctx)
    assert len(ctx.messages) == before, "显式抑制时必须跳过锚点注入"


def test_synthetic_continue_injected_once_when_no_raw() -> None:
    """契约（H1d+破坏者）：无 raw_input → 注入 synthetic Continue；同轮不重复。"""
    from bandi.agent.states import _inject_continuity_anchor

    ctx = _ctx_for_anchor(
        raw_input="",
        messages=[
            Message(role=MessageRole.USER, content="旧消息已裁"),
            Message(role=MessageRole.ASSISTANT, content="收尾"),
        ],
    )
    _inject_continuity_anchor(ctx)
    synthetics = [
        m for m in ctx.messages if m.role == MessageRole.USER and bool(m.meta.get("synthetic"))
    ]
    assert len(synthetics) == 1, "无 raw 时必须注入一条 synthetic Continue"
    _inject_continuity_anchor(ctx)
    synthetics2 = [
        m for m in ctx.messages if m.role == MessageRole.USER and bool(m.meta.get("synthetic"))
    ]
    assert len(synthetics2) == 1, "同轮 synthetic Continue 不得重复注入"


@pytest.mark.asyncio
async def test_anchor_injected_on_truncate_fallback_path() -> None:
    """契约（H1e+破坏者，生产路径缺口修复）：on_compressing 的「截断兜底」失败分支
    （压缩失败走 CompressionPipeline.truncate）也须在裁掉最新 raw 后重新注入锚点——
    stage != 'none' 成功分支注入（states.py:1253），truncate 兜底分支无锚点。"""
    from bandi.agent.base import ExecutionContext
    from bandi.agent.deps import CompactOutcome, QueryDeps
    from bandi.agent.states import on_compressing
    from bandi.core.config import AgentConfig
    from bandi.memory.compression_pipeline import CompressionStats

    class _NoopBus:
        async def emit(self, *a: Any, **k: Any) -> None:
            return None

    provider = type(
        "P",
        (),
        {
            "protocol": "openai",
            "model_name": "s",
            "config": None,
            "count_tokens": staticmethod(lambda s: max(1, len(s or "") // 4)),
        },
    )()

    ctx = ExecutionContext(
        messages=[
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="被裁掉的最新输入"),
            Message(role=MessageRole.ASSISTANT, content="第一轮"),
            Message(role=MessageRole.USER, content="历史上下文占位" * 120),
            Message(role=MessageRole.USER, content="尾部输入"),
        ],
        provider=provider,
        config=AgentConfig(context_window=200, compression_threshold=0.5, stream_to_ui=False),
        bus=_NoopBus(),
    )
    ctx.raw_input = "被裁掉的最新输入"
    # 真实大消息（历史 user 扩容）而非手调 total_tokens——判定已统一为 live 实时值。

    async def fake_autocompact(_ctx: ExecutionContext) -> CompactOutcome:
        return CompactOutcome(
            messages=list(_ctx.messages), success=False, stats=CompressionStats(stage="none")
        )

    ctx.query_deps = QueryDeps(
        autocompact=fake_autocompact,
        microcompact=lambda _ctx: None,
        call_model=lambda _ctx, _m: None,
        new_id=lambda: "x",
    )

    from bandi.memory.compaction import MemoryCompactor

    truncated = MemoryCompactor.truncate(ctx.messages, min_keep=2, keep_ratio=0.4)
    assert "被裁掉的最新输入" not in [m.content for m in truncated], "前置：raw 已被截断裁掉"

    await on_compressing(ctx)
    assert any(m.content == "被裁掉的最新输入" for m in ctx.messages), (
        "truncate 兜底裁掉最新 raw 后必须重新注入锚点（A-4 刷新不得只覆盖成功压缩路径）"
    )


# ── 截断 tool 对保护（六场景矩阵）────────────────────────────────────


def _tool_pair_msgs(
    call_id: str = "tc-1", *, with_result: bool = True, n_results: int = 1
) -> list[Message]:
    call = ToolCall(id=call_id, name="bash", arguments={"command": "ls"})
    msgs: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="sys"),
        Message(role=MessageRole.USER, content="执行命令"),
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[call]),
    ]
    if with_result:
        for i in range(n_results):
            msgs.append(Message(role=MessageRole.TOOL, content=f"结果{i}", tool_call_id=call_id))
    msgs.append(Message(role=MessageRole.USER, content="收尾输入"))
    return msgs


def _head_roles(result: list[Message]) -> list[str]:
    return [m.role.name for m in result if m.role != MessageRole.SYSTEM]


def test_truncate_does_not_orphan_tool_result_at_tail_head() -> None:
    """契约（H2a+破坏者）：切点落在 tool_result 处 → 保留区头部不得是孤儿 TOOL"""
    from bandi.memory.compaction import MemoryCompactor  # 共用实现
    from bandi.memory.compression_pipeline import CompressionPipeline

    call = ToolCall(id="tc-1", name="bash", arguments={"command": "ls"})
    msgs = [
        Message(role=MessageRole.USER, content="u0"),
        Message(role=MessageRole.USER, content="u1"),
        Message(role=MessageRole.USER, content="u2"),
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[call]),
        Message(role=MessageRole.TOOL, content="结果", tool_call_id="tc-1"),
        Message(role=MessageRole.USER, content="u3"),
    ]
    # keep_ratio 极小 → 裸截断保留最后 2 条（tool_result + u3）：头会落在 tool_result
    for truncate in (MemoryCompactor.truncate, CompressionPipeline.truncate):
        result = truncate(msgs, min_keep=2, keep_ratio=0.2)
        kept_ids = {tc.id for m in result for tc in (m.tool_calls or [])}
        for m in result:
            if m.role == MessageRole.TOOL and m.tool_call_id:
                assert m.tool_call_id in kept_ids, (
                    f"{truncate.__qualname__}: 保留区 TOOL 结果必须有其 assistant(tool_calls)"
                    "（头不得是孤儿 TOOL——snap_autocompact_split 防 API 孤儿）"
                )


def test_truncate_keeps_assistant_pair_when_tool_result_retained() -> None:
    """契约（H2b）：保留区含 TOOL 结果 → 其 assistant(tool_calls) 必须同在（拆对=孤儿）。"""
    from bandi.memory.compaction import MemoryCompactor

    msgs = _tool_pair_msgs()
    result = MemoryCompactor.truncate(msgs, min_keep=2, keep_ratio=0.3)
    kept_ids = {tc.id for m in result for tc in (m.tool_calls or [])}
    has_tool = any(m.role == MessageRole.TOOL and m.tool_call_id for m in result)
    if has_tool:
        assert any(m.role == MessageRole.ASSISTANT and m.tool_calls for m in result), (
            "保留区含 TOOL 结果时必须含配对 assistant(tool_calls)"
        )
        assert all(
            m.tool_call_id in kept_ids
            for m in result
            if m.role == MessageRole.TOOL and m.tool_call_id
        ), "保留区内所有 TOOL 结果都必须有配对 assistant(tool_calls)"


def test_truncate_multi_result_all_paired() -> None:
    """契约（H2c）：一个 assistant 多 antool_result → 全部配对，不拆散。"""
    from bandi.memory.compaction import MemoryCompactor

    msgs = _tool_pair_msgs(n_results=3)
    result = MemoryCompactor.truncate(msgs, min_keep=2, keep_ratio=0.3)
    kept_ids = {tc.id for m in result for tc in (m.tool_calls or [])}
    results = [m for m in result if m.role == MessageRole.TOOL]
    if results:
        assert all(m.tool_call_id in kept_ids for m in results), "多结果全部需配对"


def test_truncate_no_result_no_pair_needed() -> None:
    """契约（H2d）：assistant(tool_calls) 无对应 TOOL 结果 → 不强制要求结果
    （裁剪把结果裁掉是安全的，无孤儿方向问题）。"""
    from bandi.memory.compaction import MemoryCompactor

    msgs = _tool_pair_msgs(with_result=False)
    result = MemoryCompactor.truncate(msgs, min_keep=2, keep_ratio=0.3)
    # 不抛异常、不强制成对即可——重点是不得引入孤儿 TOOL
    assert isinstance(result, list)


def test_truncate_idempotent_no_broken_pairs_across_keep_counts() -> None:
    """契约（H2e 族级）：不同 keep_count 下保留区都不出现孤儿 TOOL（参数化多切点）。"""
    from bandi.memory.compaction import MemoryCompactor

    for keep in (1, 2, 3):
        msgs = _tool_pair_msgs()
        result = MemoryCompactor.truncate(msgs, min_keep=keep, keep_ratio=0.2)
        kept_ids = {tc.id for m in result for tc in (m.tool_calls or [])}
        for m in result:
            if m.role == MessageRole.TOOL and m.tool_call_id:
                assert m.tool_call_id in kept_ids, f"keep={keep}: 不得出现孤儿 TOOL"


@pytest.mark.asyncio
async def test_progressive_keep_tail_does_not_orphan_tool_pair() -> None:
    """
    同族——保留区 head 不得是孤儿 TOOL（结果缺其 assistant.tool_calls）。"""
    from bandi.core.message import LLMResponse, TokenUsage
    from bandi.memory.compression_pipeline import ProgressiveCompressor

    async def _stub_complete(messages: Any) -> Any:
        return LLMResponse(
            content="结构化摘要：目标/进度/下一步",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
            finish_reason="stop",
        )

    call = ToolCall(id="tc-p", name="bash", arguments={"command": "ls"})
    msgs = [
        Message(role=MessageRole.USER, content="u0" * 300),
        Message(role=MessageRole.USER, content="u1" * 300),
        Message(role=MessageRole.USER, content="u2" * 300),
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[call]),
        Message(role=MessageRole.TOOL, content="结果", tool_call_id="tc-p"),
        Message(role=MessageRole.USER, content="收尾" * 300),
    ]
    comp = ProgressiveCompressor(max_chunks=2, keep_ratio=0.1)
    result, _stats = await comp.compress(msgs, _stub_complete)
    kept_ids = {tc.id for m in result for tc in (m.tool_calls or [])}
    for m in result:
        if m.role == MessageRole.TOOL and m.tool_call_id:
            assert m.tool_call_id in kept_ids, (
                "progressive 保留 tail 含 TOOL 结果时必须有配对 assistant.tool_calls"
                "（族级不变式：cut-point 不得产孤儿）"
            )


# ── SM 坐标对齐 ──────────────────────────────────────────────────────────


def _store_with_turns(tmp_path: Any) -> Any:
    from bandi.memory.working import ScratchpadStore

    store = ScratchpadStore(str(tmp_path / "working.db"))
    store.set("turn:0", "用户: 第一轮 → 回答: 完成", session_id="s1")
    return store


def test_sm_boundary_in_non_system_coordinate_ignores_system(tmp_path: Any) -> None:
    """契约（H3a+破坏者）：session_memory_boundary 必须返回 non_system 坐标——
    快照覆盖 turn:0；首个未覆盖 user（turn:1）的 non_system 坐标必须恒为 0——
    """
    from bandi.memory.session_memory import session_memory_boundary

    store = _store_with_turns(tmp_path)
    core = [
        Message(role=MessageRole.USER, content="历史1", turn_index=1),
        Message(role=MessageRole.USER, content="当前输入", turn_index=2),
    ]
    # 无 system → 首个未覆盖 user 的 non_system 坐标
    idx_no_sys = session_memory_boundary(store, "s1", core)
    # 前置 2 条 system → 坐标必须不偏移（仍指向同一 non_system 位置）
    with_sys = [
        Message(role=MessageRole.SYSTEM, content="sys1"),
        Message(role=MessageRole.SYSTEM, content="sys2"),
        *core,
    ]
    idx_with_sys = session_memory_boundary(store, "s1", with_sys)

    assert idx_no_sys == 0, "首个未覆盖 user 应为 non_system 坐标 0"
    assert idx_no_sys == idx_with_sys, (
        f"system 消息不得使 non_system 坐标偏移（无={idx_no_sys} 有={idx_with_sys}）——"
        "floor 裁剪必须覆盖全部 non_system 消息"
    )


def test_sm_boundary_all_covered_returns_length(tmp_path: Any) -> None:
    """契约（H3b）：全部消息都被快照覆盖 → 返回全部长度（无该裁的）。"""
    from bandi.memory.session_memory import session_memory_boundary

    store = _store_with_turns(tmp_path)
    messages = [
        Message(role=MessageRole.USER, content="历史", turn_index=0),
        Message(role=MessageRole.USER, content="也是历史", turn_index=0),
    ]
    assert session_memory_boundary(store, "s1", messages) == 2


# ==================== day124zi ====================


def _make_ctx(
    *, turn_index: int, final_message: str, raw_input: str = "记住：我的项目代号是 42"
) -> Any:
    """构造最小可复现 _persist_memory 的 ctx（用真实字段，不依赖完整 ExecutionContext）。

    默认 raw_input 含可记内容（数字）——记忆内容源只取用户输入（BC-004），
    源为空则直写/暂存路径整体不成立。"""
    empty_msgs: list[Any] = []
    return SimpleNamespace(
        tool_results=[],
        final_message=final_message,
        current_content=None,
        raw_input=raw_input,
        turn_index=turn_index,
        terminal_reason=SimpleNamespace(name="COMPLETED"),
        messages=empty_msgs,
    )


def _make_manager(tmp_path: Any) -> Any:
    """构造真实 MemoryManager（long_term + episodic + working 真 store）。"""
    from bandi.memory.episodic import EpisodicStore
    from bandi.memory.long_term import LongTermMemory
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.working import ScratchpadStore

    db_dir = tmp_path / "mem"
    db_dir.mkdir(exist_ok=True)
    return MemoryManager(
        long_term=LongTermMemory(":memory:"),
        episodic=EpisodicStore(str(db_dir / "episodic.db")),
        working=ScratchpadStore(str(db_dir / "working.db")),
        markdown_memory=MarkdownMemoryStore(db_dir, project_root=db_dir),
    )


def _persist_twice(tmp_path: Any, *, turn_index: int) -> tuple[Any, Any]:
    """同一 ctx（同一 session/turn）连续两次调用 _persist_memory，返回 (manager, ctx)。"""
    from bandi.core.agent_runner import _persist_memory

    long_reply = "结论：这是一个足够长的回复内容以触发长期记忆写入，" * 30
    ctx = _make_ctx(turn_index=turn_index, final_message=long_reply)
    manager = _make_manager(tmp_path)
    asyncio.run(
        _persist_memory(
            ctx,
            manager,
            session_id="sess-1",
            project_id="proj-A",
            employee_id="emp-1",
        )
    )
    asyncio.run(
        _persist_memory(
            ctx,
            manager,
            session_id="sess-1",
            project_id="proj-A",
            employee_id="emp-1",
        )
    )
    return manager, ctx


# ── 写入幂等 ────────────────────────────────────────────────────────────


def test_c4_long_term_same_turn_idempotent(tmp_path: Any) -> None:
    """契约：同一 (session, turn) 二次持久化，long_term 必须只落 1 条（REPLACE 覆盖）。"""
    manager, ctx = _persist_twice(tmp_path, turn_index=3)
    assert manager.long_term.count() == 1, "同 turn 重跑不得产生重复 long_term 条目"
    item = manager.long_term.get("turn:sess-1:3")
    assert item is not None, "稳定 memory_id 必须可定位（最终产物存在）"
    assert "项目代号" in item.content


def test_c4_episodic_same_turn_idempotent(tmp_path: Any) -> None:
    """契约：同 turn 二次持久化，episodic 经历只存 1 条（episode_id 稳定 REPLACE）。"""
    manager, ctx = _persist_twice(tmp_path, turn_index=4)
    ep = manager.episodic.get("ep:sess-1:4")
    assert ep is not None, "稳定 episode_id 必须可定位"
    assert "项目代号" in ep.summary
    # 二次写入后仍只有同 id 一条（EP-primary key REPLACE）
    rows = manager.episodic._conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE episode_id = ?", ("ep:sess-1:4",)
    ).fetchone()[0]
    assert rows == 1, "同 episode_id 二次 REPLACE 后不得重复行"


def test_c4_different_turn_not_merged(tmp_path: Any) -> None:
    """契约：不同 turn 各自独立条目（稳定键不误并同 turn 语义）。"""
    manager, _ = _persist_twice(tmp_path, turn_index=5)
    _persist_twice(tmp_path, turn_index=6)
    # 上面各造了独立 manager；此处重建 manager 单独验证两 turn 并存
    from bandi.core.agent_runner import _persist_memory

    ctx5 = _make_ctx(
        turn_index=5,
        final_message="结论：这是足够长的回复内容以触发长期记忆写入，" * 30,
        raw_input="记住：项目代号甲是 51",
    )
    ctx6 = _make_ctx(
        turn_index=6,
        final_message="结论：这是另一条足够长的回复内容以写入长期记忆，" * 30,
        raw_input="记住：项目代号乙是 62",
    )
    mgr = _make_manager(tmp_path)
    for c in (ctx5, ctx6):
        asyncio.run(
            _persist_memory(c, mgr, session_id="sess-2", project_id="proj-A", employee_id="emp-1")
        )
    assert mgr.long_term.count() == 2, "不同 turn 必须各自独立 1 条"
    assert mgr.long_term.get("turn:sess-2:5") is not None
    assert mgr.long_term.get("turn:sess-2:6") is not None
    assert mgr.episodic.get("ep:sess-2:5") is not None
    assert mgr.episodic.get("ep:sess-2:6") is not None


def test_c4_episodic_auto_id_when_none():
    """契约（破坏者）：显式不传 episode_id 仍能落库（生成本地 uuid——仅当调用方未给稳定 id）。"""
    from bandi.memory.episodic import Episode, EpisodicStore

    store = EpisodicStore(":memory:")
    ep = store.record(Episode(summary="不回传 id"))
    assert ep.episode_id, "record 必须兜底生成 id，不得因调用方未传而崩"


def test_b2_search_unified_dedupes_same_value_different_key(tmp_path: Any) -> None:
    from bandi.memory.long_term import MemoryItem

    manager = _make_manager(tmp_path)
    content = "项目 X 的部署配置位于 deploy/prod.yaml，端口 8080"
    manager.long_term.store(MemoryItem(item_id="k1", content=content, importance=0.6))
    manager.long_term.store(MemoryItem(item_id="k2", content=content, importance=0.5))
    hits = [
        h
        for h in manager.search_unified("部署配置", project_id=None, employee_id=None)
        if h.metadata.get("category") == "long_term"
    ]
    assert len(hits) == 1, "同 value 不同 key 在统一检索里必须去重为单条"


def test_b2_prefetch_no_duplicate_lines(tmp_path: Any) -> None:
    import asyncio

    from bandi.memory.long_term import MemoryItem
    from bandi.perception.prefetch import MemoryPrefetchProvider

    manager = _make_manager(tmp_path)
    content = "订单接口文档：api/order.py 的 create_order 返回 201"
    manager.long_term.store(MemoryItem(item_id="p1", content=content, importance=0.7))
    manager.long_term.store(MemoryItem(item_id="p2", content=content, importance=0.6))
    provider = MemoryPrefetchProvider(manager)
    payload = asyncio.run(provider("订单接口", "sess-9", [{"role": "user", "content": "订单接口"}]))
    focused = payload.get("focused_memory", "")
    lines = [ln for ln in focused.splitlines() if ln.strip()]
    assert lines, "focused_memory 必须有内容"
    assert len(set(lines)) == len(lines), "prefetch 注入不得出现重复行"


def _persist_company_session(tmp_path: Any, *, session_id: str, turn_index: int) -> Any:
    """以公司角色式 session_id 持久化一轮，返回 manager（long_term+episodic 真 store）。"""
    from bandi.core.agent_runner import _persist_memory

    ctx = _make_ctx(
        turn_index=turn_index, final_message="结论：这是足够长的回复内容以触发长期记忆写入，" * 30
    )
    manager = _make_manager(tmp_path)
    asyncio.run(
        _persist_memory(
            ctx,
            manager,
            session_id=session_id,
            project_id="proj-A",
            employee_id="emp-1",
        )
    )
    return manager


def test_c4_cross_session_same_prefix_no_collision(tmp_path: Any) -> None:
    """契约（破坏者）：两个公司角色会话共享 8 字符前缀（role-ses）且同 turn_index
    不得互相覆盖——long_term/episodic 各留各条（跨会话隔离）。
    回归：旧 [:8] 截断键 turn:role-ses:{n} 会被 REPLACE 互覆，此处必须 count==2。"""
    sess_a = "role-session::proj-A::研究员::成员甲"
    sess_b = "role-session::proj-B::运营::成员乙"
    mgr_a = _persist_company_session(tmp_path, session_id=sess_a, turn_index=2)
    mgr_b = _persist_company_session(tmp_path, session_id=sess_b, turn_index=2)

    assert mgr_a.long_term.get(f"turn:{sess_a}:2") is not None, "A 的长期记忆必须可定位"
    assert mgr_b.long_term.get(f"turn:{sess_b}:2") is not None, (
        "B 的长期记忆必须可定位（未被 A 覆写）"
    )
    assert mgr_b.long_term.get(f"turn:{sess_a}:2") is None, "A 的键不得在 B 库中以同前缀假性命中"
    assert mgr_b.episodic.get(f"ep:{sess_b}:2") is not None, "B 的经历并未因同前缀被并到 A"


def test_c4_append_memory_same_turn_replay_idempotent(tmp_path: Any) -> None:
    """契约（破坏者）：压缩纪要用 append_memory（SESSION 作用域）同 turn 重放
    不得重复叠加——同 entry 二次追加后快照只含该纪要一次。"""
    from bandi.company.store import WorkItemStore
    from bandi.core.agent_runner import _persist_memory
    from bandi.memory.global_store import GlobalMemoryStore
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.memory_manager import MemoryHierarchyManager
    from bandi.memory.session.store import SessionStore

    hierarchy = MemoryHierarchyManager(
        global_store=GlobalMemoryStore(str(tmp_path / "h_global.db")),
        markdown_memory=MarkdownMemoryStore(tmp_path),
        work_item_store=WorkItemStore(str(tmp_path / "h_work.db")),
        session_store=SessionStore(str(tmp_path / "sess.db")),
        store_dir=tmp_path,
    )
    manager = _make_manager(tmp_path)
    summary_msg = SimpleNamespace(
        content="[上下文摘要] 会话记忆要点：蓝鲸栖息地调查", tool_results=[]
    )
    ctx = _make_ctx(
        turn_index=2,
        final_message="结论：这是足够长的回复内容以触发长期记忆写入，" * 30,
        raw_input="继续排查",
    )
    ctx.messages = [summary_msg]
    for _ in range(2):
        asyncio.run(
            _persist_memory(
                ctx,
                manager,
                session_id="sess-1",
                project_id="proj-A",
                employee_id="emp-1",
                hierarchy=hierarchy,
            )
        )
    snapshot = hierarchy.load_memory(
        "session", session_id="sess-1", project_id="proj-A", employee_id="emp-1"
    )
    marker = "[压缩纪要 turn 2]"
    assert snapshot.count(marker) == 1, f"同 turn 重放不得重复叠加压缩纪要，快照={snapshot!r}"


# ==================== day124zj ====================


def _hierarchy(tmp_path: Any) -> Any:
    from bandi.company.store import WorkItemStore
    from bandi.memory.global_store import GlobalMemoryStore
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.memory_manager import MemoryHierarchyManager
    from bandi.memory.session.store import SessionStore

    return MemoryHierarchyManager(
        global_store=GlobalMemoryStore(tmp_path / "global.db"),
        markdown_memory=MarkdownMemoryStore(tmp_path / "store", project_root=tmp_path / "proj"),
        work_item_store=WorkItemStore(str(tmp_path / "company.db")),
        session_store=SessionStore(tmp_path / "sessions.db"),
    )


# 每块 ~8 tokens：`MARKER{i} 记忆内容`（tiktoken 中文 ≈1 字/token，estimate 已委托 count）。
# budget=20 只能装 2 块（2×8=16），5 块总 40 超限 → 部分保留可观测
_BLOCK = "记忆内容"  # 4 字 ≈ 4 tokens
_BLOCK_CHARS = len(f"## 项目记忆\n{_BLOCK}")  # 含 header


def test_oversized_project_scope_partially_retained(tmp_path: Any) -> None:
    """契约（J-1a/破坏者）：项目记忆含多个 `\\n\\n` 条目且总量超预算时应**部分保留**
    ——保留能放进预算的条目子集并带上 scope 头，而非把整个项目 scope 一次性丢弃。
    """
    h = _hierarchy(tmp_path)
    entries = "\n\n".join(f"MARKER{i} {_BLOCK}" for i in range(1, 6))
    h.save_memory("project", entries, project_id="proj1")

    context = h.collect_memory_context(
        project_id="proj1", session_id="s1", employee_id="emp1", max_tokens=20
    )
    assert "项目记忆" in context, "项目 scope 不得整块丢弃——部分保留必须带上 scope 头"
    kept = [f"MARKER{i}" for i in range(1, 6) if f"MARKER{i}" in context]
    assert 0 < len(kept) < 5, (
        f"预算内必须保留部分条目且非全部（旧整块丢弃=0；实际保留 {len(kept)}/5）"
    )


def test_employee_scope_partial_retention_above_session(tmp_path: Any) -> None:
    """契约（J-1b）：预算紧张时高优先级层（employee）的块部分保留优先于低优先级层
    （session）整条——employee 至少保留自身的部分内容。"""
    h = _hierarchy(tmp_path)
    emp_entries = "\n\n".join(f"EMP{i} {_BLOCK}" for i in range(1, 5))  # 4 块 ~32 tokens
    h.save_memory("employee", emp_entries, project_id="proj1", employee_id="emp1")
    h.save_memory(
        "session", "SESS1 会话内容", project_id="proj1", session_id="s1", employee_id="emp1"
    )

    context = h.collect_memory_context(
        project_id="proj1", session_id="s1", employee_id="emp1", max_tokens=20
    )
    assert "员工记忆" in context, "最高优先级 employee 必须保留（部分）"
    assert any(f"EMP{i}" in context for i in range(1, 5)), "employee 块级部分保留"


def test_budget_zero_returns_empty(tmp_path: Any) -> None:
    """契约（回归）：max_tokens<=0 必须返回 ""（最高优先级层也放不下）。"""
    h = _hierarchy(tmp_path)
    h.save_memory("global", "全局内容", allow_shared=True)
    assert h.collect_memory_context("proj1", "s1", "emp1", max_tokens=0) == ""


def test_blocks_rejoin_in_scope_order_preserving_headers(tmp_path: Any) -> None:
    """契约（J-1c）：背包保留的块按 global→project→session→employee 展示顺序重组
    scope 头保留且正文连续（子串断言成立）。"""
    h = _hierarchy(tmp_path)
    h.save_memory("global", "全局A", allow_shared=True)
    h.save_memory("project", "项目A", project_id="proj1")
    h.save_memory("session", "会话A", project_id="proj1", session_id="s1", employee_id="emp1")
    h.save_memory("employee", "员工A", project_id="proj1", employee_id="emp1")
    ctx = h.collect_memory_context("proj1", "s1", "emp1", max_tokens=4096)
    assert (
        ctx.index("全局记忆")
        < ctx.index("项目记忆")
        < ctx.index("会话记忆")
        < ctx.index("员工记忆")
    ), "展示顺序必须 global→project→session→employee"
    assert "全局A" in ctx and "项目A" in ctx and "会话A" in ctx and "员工A" in ctx


# ── 跨分类统一排序 ────────────────────────────────────────────────────────


def _mk_scores(path: Any) -> Any:
    """构造多分类命中：强相关 facts（tf 高 + importance 0.9）/ 弱相关 facts（0.3）/
    working 草稿（0.5）＋5 条无关文档。

    无关文档用于让微型库 bm25 的 IDF 有区分度（仅 2 条语料时 rank≈0，相关性
    无信息量）； 修复后 long_term 相关度取 FTS bm25（与 importance
    解耦），本契约验证统一 fusion 分数跨分类降序。"""
    from bandi.memory.long_term import LongTermMemory, MemoryItem
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.working import ScratchHit

    lt = LongTermMemory(str(path / "lt.db"))
    lt.store(
        MemoryItem(
            item_id="lt-high", content="蓝鲸 蓝鲸 迁徙路线 海洋哺乳动物知识库", importance=0.9
        )
    )
    lt.store(MemoryItem(item_id="lt-low", content="蓝鲸 稀有冷门 小知识", importance=0.3))
    for i in range(5):
        lt.store(
            MemoryItem(
                item_id=f"lt-noise-{i}", content=f"企鹅 南极 冰川 无关记录 {i}", importance=0.2
            )
        )

    class _Working:
        def search(self, q, limit=5, session_id=""):
            return [
                (ScratchHit("kv", "kw1", "蓝鲸工作草稿片段内容摘要", "2026-08-28"), 0.5),
            ]

    mgr = MemoryManager(long_term=lt, working=_Working())
    return mgr


def test_search_unified_orders_by_score_across_categories(tmp_path: Any) -> None:
    """契约（破坏者， 新口径）：search_unified 聚合后按统一 fusion
    分数跨分类降序——相关性（FTS bm25）与重要性各自入分；强相关高重要性事实第一
    working 草稿跨分类照常召回（不再硬编码旧「importance 双计权」顺序）。"""
    from bandi.memory.memory_provider import _fusion_score

    mgr = _mk_scores(tmp_path)
    out = mgr.search_unified("蓝鲸", session_id="s1")
    assert out, "必须有聚合命中"
    scores = [_fusion_score(h.metadata) for h in out]
    assert scores == sorted(scores, reverse=True), f"必须按统一分数降序（实际 {scores}）"
    assert out[0].key == "lt-high", (
        f"强相关 + 高重要性事实应第一（实际 {[(h.key, round(_fusion_score(h.metadata), 3)) for h in out]}）"
    )
    assert any("工作草稿" in (h.value or "") for h in out), "working 必须跨分类召回"


def test_search_unified_metadata_has_score_and_category(tmp_path: Any) -> None:
    """契约（J-2b）：每个统一出口命中必须携带统一 score 与来源 category 元数据
    消费方（MemoryContextSource/Prefetch）可据此做进一步取舍。"""
    from bandi.memory.long_term import LongTermMemory, MemoryItem
    from bandi.memory.memory_provider import MemoryManager

    lt = LongTermMemory(str(tmp_path / "lt2.db"))
    lt.store(MemoryItem(item_id="lt1", content="蓝鲸知识内容", importance=0.9))
    mgr = MemoryManager(long_term=lt)
    out = mgr.search_unified("蓝鲸", session_id="s1")
    assert out and out[0].metadata.get("category") == "long_term"
    assert out[0].metadata.get("score") is not None, "必须携带统一 score 供消费方排序/取舍"


def test_search_unified_applies_topk_limit(tmp_path: Any) -> None:
    """契约（J-2c）：统一排序后按 limit 收敛 top-k——跨分类的总命中数多于 limit"""
    from bandi.memory.long_term import LongTermMemory, MemoryItem
    from bandi.memory.memory_provider import MemoryManager

    lt = LongTermMemory(str(tmp_path / "lt3.db"))
    for i in range(6):
        lt.store(MemoryItem(item_id=f"lt{i}", content=f"蓝鲸知识点内容{i} 编号{i}", importance=0.5))
    mgr = MemoryManager(long_term=lt)
    out = mgr.search_unified("蓝鲸", session_id="s1", limit=3)
    assert len(out) <= 3, f"必须按 limit=3 收敛 top-k（实际 {len(out)}）"


def test_budget_field_level_mandatory_prefix_preserved(tmp_path: Any) -> None:
    """契约：块整体放不进预算但首行标识能放进时
    必须保留该块的**必留首行**（字段级），而非整块抹除——高价值块的语义身份
    drop 整块，连 `决策：` 这样的标识字段也一并消失。"""
    h = _hierarchy(tmp_path)
    # 员工层最高优先级——首个被处理；整块 ~17 tokens > budget=10，首行 ~5 tokens ≤ 10
    block = "决策：基线\n长正文说明此处压缩点细节每分钟节点记录超出预算的冗长叙述"
    h.save_memory("employee", block, project_id="proj1", employee_id="emp1")

    ctx = h.collect_memory_context(
        project_id="proj1", session_id="s1", employee_id="emp1", max_tokens=10
    )
    assert "决策" in ctx, "必留首行标识必须在超预算时保留"
    assert "长正文说明" not in ctx, "可省正文应在预算内被裁掉（不整块带入）"


def test_category_floor_promotes_excluded_category(tmp_path: Any) -> None:
    """契约：高 importance 分类占满 top-k 时，被挤出的分类
    必须保底 1 条进 top-k（替换掉高分分类的冗余最低分条目）。"""
    from bandi.memory.memory_provider import MemoryHit, _apply_category_floor

    hits = [
        MemoryHit(key=f"lt{i}", value=f"长期{i}", metadata={"category": "long_term", "score": 0.9})
        for i in range(4)
    ]
    hits.append(
        MemoryHit(key="wk1", value="工作草稿", metadata={"category": "working", "score": 0.2})
    )

    out = _apply_category_floor(hits, limit=3)

    assert len(out) == 3, f"limit 仍是硬上限（实际 {len(out)}）"
    cats = {h.metadata.get("category") for h in out}
    assert "working" in cats, "被挤出分类必须保底进 top-k"
    assert "long_term" in cats, "高分分类唯一代表不得被替换掉"
    # 保底替换的是 long_term 冗余条目（4 条里换 1 条），且保底后仍保留 2 条 long_term
    lt_count = sum(1 for h in out if h.metadata.get("category") == "long_term")
    assert lt_count == 2, f"只替换冗余条目，不得把高分分类唯一代表挤出（实际 {lt_count} 条）"


def test_category_floor_noop_when_no_exclusion(tmp_path: Any) -> None:
    """契约（J-3b 破坏者）：所有有命中分类都在 top-k 内时，保底不得改动任何条目
    （顺序与内容原样返回）。"""
    from bandi.memory.memory_provider import MemoryHit, _apply_category_floor

    hits = [
        MemoryHit(key="lt1", value="长期1", metadata={"category": "long_term", "score": 0.9}),
        MemoryHit(key="wk1", value="工作1", metadata={"category": "working", "score": 0.5}),
    ]

    out = _apply_category_floor(hits, limit=5)

    assert out == hits, "无挤出场景必须原样返回（顺序不变）"


def test_category_floor_limit_one_keeps_strongest(tmp_path: Any) -> None:
    """契约（J-3c 破坏者）：limit=1 时保底不适用（无槽位）——返回 score 最高者。"""
    from bandi.memory.memory_provider import MemoryHit, _apply_category_floor

    hits = [
        MemoryHit(key="lt1", value="长期1", metadata={"category": "long_term", "score": 0.9}),
        MemoryHit(key="wk1", value="工作1", metadata={"category": "working", "score": 0.8}),
    ]

    out = _apply_category_floor(hits, limit=1)

    assert len(out) == 1
    assert out[0].metadata.get("category") == "long_term", "limit=1 时保底让位给最高分"


def test_search_unified_category_floor_end_to_end(tmp_path: Any) -> None:
    """契约（J-3d，端到端）：search_unified 全链——long_term 高分占满 + working
    低分存在时，返回结果必须含 working 保底条目（生产主链路最终产物断言）。"""
    from bandi.memory.long_term import LongTermMemory, MemoryItem
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.working import ScratchpadStore

    lt = LongTermMemory(str(tmp_path / "lt_cat.db"))
    for i in range(5):
        lt.store(MemoryItem(item_id=f"lt{i}", content=f"蓝鲸知识内容{i} 编号{i}", importance=0.9))
    wk = ScratchpadStore(str(tmp_path / "wk_cat.db"))
    wk.set("turn:1", "蓝鲸工作草稿碎片", session_id="s1")
    mgr = MemoryManager(long_term=lt, working=wk)

    out = mgr.search_unified("蓝鲸", session_id="s1", limit=3)

    cats = {h.metadata.get("category") for h in out}
    assert len(out) == 3, f"limit 收敛不变（实际 {len(out)}）"
    assert "working" in cats, "端到端：working 分类必须被保底进 top-k"


# ==================== day124zk ====================


def r4xday124zk__store(tmp_path) -> MarkdownMemoryStore:
    return MarkdownMemoryStore(store_dir=str(tmp_path), project_root=str(tmp_path))


class TestK1SaveOversizeRoutesToEntry:
    def test_oversized_save_no_data_loss(self, tmp_path) -> None:
        store = r4xday124zk__store(tmp_path)
        big = "唯一锚点-" + "x" * (PROJECT_MEMORY_MAX_CHARS + 500)
        store.save_visible_text(big)
        visible = store.load_visible_text()
        assert "唯一锚点" in store.read_entry(store.list_entry_pointers()[0].entry_id), (
            "全文必须可经 entry 读回"
        )
        assert "唯一锚点-" in visible, "指针行必须含可发现锚点"
        assert len(visible) < PROJECT_MEMORY_MAX_CHARS, "MEMORY.md 可见部分保持精简"

    def test_small_save_inline_unchanged(self, tmp_path) -> None:
        store = r4xday124zk__store(tmp_path)
        store.save_visible_text("小段记忆")
        assert store.load_visible_text() == "小段记忆"
        assert store.list_entry_pointers() == []

    def test_oversized_save_structured_block_intact(self, tmp_path) -> None:
        from bandi.memory.markdown_memory import _STRUCTURED_BEGIN, _STRUCTURED_END

        store = r4xday124zk__store(tmp_path)
        store.save_visible_text(
            "可见A\n<!-- BANDI_STRUCTURED:BEGIN -->\n```yaml\nsecret: 1\n```\n<!-- BANDI_STRUCTURED:END -->"
        )
        store.save_visible_text("x" * (PROJECT_MEMORY_MAX_CHARS * 2))
        raw = store.memory_file_path.read_text(encoding="utf-8")
        assert _STRUCTURED_BEGIN in raw and _STRUCTURED_END in raw
        assert "secret: 1" in raw
        assert len(raw) <= PROJECT_MEMORY_MAX_CHARS + 200


class TestK2IndexCache:
    def test_search_reuses_cache_not_stale_after_save(self, tmp_path) -> None:
        store = r4xday124zk__store(tmp_path)
        store.save_entry("第一版", "灰度发布配置")
        assert store.search_entries("灰度")  # 建立缓存
        store.save_entry("第二版", "回滚预案已更新")
        hits = store.search_entries("回滚")
        assert hits and "回滚" in hits[0][0].hook, "保存后检索不得命中陈旧缓存"

    def test_cache_not_stale_after_external_delete(self, tmp_path) -> None:
        store = r4xday124zk__store(tmp_path)
        ptr = store.save_entry("将被删除", "临时内容")
        assert store.search_entries("临时内容")
        (store.entries_dir / f"{ptr.entry_id}.md").unlink()
        assert store.search_entries("临时内容") == [], "删除后不得从缓存返回已删条目"

    def test_cache_fresh_across_repeated_search(self, tmp_path) -> None:
        store = r4xday124zk__store(tmp_path)
        store.save_entry("稳定条目", "核对外键约束")
        first = store.search_entries("外键")
        second = store.search_entries("外键")
        assert [p.entry_id for p, _ in first] == [p.entry_id for p, _ in second]


class TestK3ContinuousRanking:
    def test_partial_higher_ratio_outranks_lower_ratio(self, tmp_path) -> None:
        store = r4xday124zk__store(tmp_path)
        store.save_entry("双词命中", "灰度 回滚 综合 记录")
        store.save_entry("单词命中", "灰度 单独 一条")
        hits = store.search_entries("灰度 回滚 备份")
        titles = [p.title for p, _ in hits]
        assert titles.index("双词命中") < titles.index("单词命中"), (
            "高占比部分命中必须连续排在低占比之前"
        )

    def test_full_match_remains_dominant(self, tmp_path) -> None:
        store = r4xday124zk__store(tmp_path)
        store.save_entry("全命中", "发布 回滚 灰度 预案 全齐")
        store.save_entry("部分命中", "只提 发布")
        hits = store.search_entries("发布 回滚 灰度 预案")
        assert hits[0][0].title == "全命中"


class TestK4MergeOversizeRoutesToEntry:
    def test_merge_oversize_routes_to_entry_no_data_loss(self, tmp_path) -> None:
        store = r4xday124zk__store(tmp_path)
        base = "前缀记忆-" + "y" * (PROJECT_MEMORY_MAX_CHARS - 200)
        store.save_visible_text(base)
        incoming = "关键结论-" + "z" * 1000
        store.merge_markdown(incoming)
        ptrs = store.list_entry_pointers()
        assert ptrs, "超预算合并必须路由到 entry"
        body = store.read_entry(ptrs[0].entry_id)
        assert body.startswith("前缀记忆-"), "合并全文头部必须入 entry 无丢失"
        assert "关键结论-" in body, "合并尾部内容也必须完整保留（非尾部截断丢数据）"
        assert "前缀记忆-" in store.load_visible_text(), "指针行保留可见锚点"

    def test_merge_dedupe_preserved(self, tmp_path) -> None:
        store = r4xday124zk__store(tmp_path)
        store.save_visible_text("已有内容")
        store.merge_markdown("已有内容")
        assert store.load_visible_text() == "已有内容"
        assert store.list_entry_pointers() == []


class TestK5SearchAllProjectMd:
    def test_search_all_injects_project_md_entries(self, tmp_path) -> None:
        """
        检索注入 project_md 分类（行为——注入面，而非 Protocol 声明存在）。"""
        from bandi.memory.memory_provider import MemoryManager

        manager = MemoryManager(markdown_memory=r4xday124zk__store(tmp_path))
        manager._markdown.save_entry("备份配置", "灰度 回滚 备份 预案")
        res = manager.search_all("灰度")
        assert "project_md" in res, "search_all 必须含 project_md 分类"
        assert res["project_md"], "已存 entries 必须能经 search_all 检索注入"
        # 标题经 metadata 随行透传
        assert "灰度 回滚 备份 预案" in res["project_md"][0].value, "注入命中必须携带实质正文"
        assert res["project_md"][0].metadata["title"] == "备份配置"

    def test_search_unified_injects_project_md_with_category(self, tmp_path) -> None:
        """
        候选分检索命中不被误去重。"""
        from bandi.memory.memory_provider import MemoryManager

        manager = MemoryManager(markdown_memory=r4xday124zk__store(tmp_path))
        manager._markdown.save_entry("发布手册", "灰度 回滚 发布 流程")
        manager._markdown.save_entry("备份配置", "灰度 备份 预案")
        hits = manager.search_unified("灰度 回滚")
        assert hits, "统一检索必须返回 project_md 命中"
        assert all(h.metadata.get("category") == "project_md" for h in hits), (
            "project_md 命中必须带分类透传"
        )
        titles = {h.metadata.get("title") for h in hits}
        assert "发布手册" in titles and "备份配置" in titles, (
            "不同内容的 project_md 命中都必须被注入（不得误去重）"
        )

    def test_search_unified_converges_to_limit(self, tmp_path) -> None:
        from bandi.memory.memory_provider import LongTermMemory, MemoryManager

        manager = MemoryManager(
            markdown_memory=r4xday124zk__store(tmp_path),
            long_term=LongTermMemory(db_path=str(tmp_path / "lt.sqlite")),
        )
        manager._markdown.save_entry("条目一", "灰度 回滚 备份 发布 全齐")
        manager._markdown.save_entry("条目二", "灰度 回滚 预案 手册")
        hits = manager.search_unified("灰度 回滚", limit=2)
        assert 1 <= len(hits) <= 2, "聚合结果必须收敛到 limit"

    def test_search_all_without_markdown_omits_category(self, tmp_path) -> None:
        from bandi.memory.memory_provider import MemoryManager

        manager = MemoryManager()
        assert "project_md" not in manager.search_all("灰度"), (
            "未注入 markdown 不得凭空出现 project_md 分类"
        )


# ==================== day124zl ====================


@pytest.fixture(autouse=True)
def _isolate_token_counter():
    TokenCounter._calibrations = {}
    TokenCounter._observations = []
    TokenCounter._active_model = ""
    yield
    TokenCounter._calibrations = {}
    TokenCounter._observations = []
    TokenCounter._active_model = ""


class TestL1FtsChinesePreTokenization:
    def test_chinese_bigram_search_hits(self) -> None:
        """中文 2-gram 预分词——以任一中文 bigram 检索可命中长句（旧 unicode61
        把整段 当一个 token，中文子串无法匹配）。"""
        from bandi.memory.long_term import LongTermMemory, MemoryItem

        mem = LongTermMemory()
        try:
            mem.store(MemoryItem(item_id="c1", content="灰度发布回滚预案演练"))
            hits = mem.search_fts("回滚", limit=5)
            assert [h.id for h in hits] == ["c1"], "中文单 bigram 检索必须命中"
        finally:
            mem.close()

    def test_chinese_multi_bigram_query(self) -> None:
        """中文多 bigram 查询可命中（全 gram AND 语义）。"""
        from bandi.memory.long_term import LongTermMemory, MemoryItem

        mem = LongTermMemory()
        try:
            mem.store(MemoryItem(item_id="c1", content="灰度发布回滚预案演练"))
            hits = mem.search_fts("灰度发布", limit=5)
            assert [h.id for h in hits] == ["c1"]
        finally:
            mem.close()

    def test_chinese_no_match_empty(self) -> None:
        """中文无匹配返回空（不回退到错误命中）。"""
        from bandi.memory.long_term import LongTermMemory, MemoryItem

        mem = LongTermMemory()
        try:
            mem.store(MemoryItem(item_id="c1", content="研发流程规范"))
            assert mem.search_fts("选品策略", limit=5) == []
        finally:
            mem.close()


class TestL2EstimateDelegatesToCount:
    def test_estimate_equals_count(self) -> None:
        """
        中文低估 9x，预算/成本判定失真）。"""
        text = "深度学习计划：构建个人多Agent数字员工组织"
        assert TokenCounter.estimate(text) == TokenCounter.count(text)

    def test_estimate_chinese_accurate_not_char_div4(self) -> None:
        text = "深度学习计划：构建个人多Agent数字员工组织，包含状态机与压缩管道"
        assert TokenCounter.estimate(text) > len(text) // 4


class TestL2BuilderCountBasedTruncation:
    def test_builder_truncates_to_token_budget(self) -> None:
        """
        （旧 `max_tokens*4` 字符口径对中文超分 4x，system prompt 撑爆）。"""
        from bandi.agent.prompt_harness.builder import PromptHarness

        long_cn = "项目记忆说明。" * 200
        max_tokens = 50
        out = PromptHarness._trim_project_instructions(long_cn, max_tokens)
        assert TokenCounter.count(out) <= max_tokens, "裁剪后 token 数必须落在预算内"

    def test_builder_keeps_short_instructions(self) -> None:
        from bandi.agent.prompt_harness.builder import PromptHarness

        short = "使用中文回复。"
        assert PromptHarness._trim_project_instructions(short, 500) == short


# ==================== day124zm ====================
# 功能：-m 契约测试——long_term prune 分区化（按 project/employee 分区独立配额）


def r6xday124zm__store(
    mem: LongTermMemory,
    item_id: str,
    importance: float,
    project_id: str | None = None,
    employee_id: str | None = None,
) -> None:
    mem.store(
        MemoryItem(item_id=item_id, content=item_id, importance=importance),
        project_id=project_id,
        employee_id=employee_id,
    )


class TestC5PrunePartitionQuota:
    def test_partition_isolation_single_project_not_ejected(self) -> None:
        """项目 A 大量记忆不得淘汰项目 B 的高价值记忆——每分区独立配额
        per_partition_max=2 时，B 分区的记录即使全局条数超标也保留。"""
        mem = LongTermMemory()
        try:
            for i in range(6):
                r6xday124zm__store(mem, f"a{i}", 0.3, project_id="projA")
            r6xday124zm__store(mem, "b_high", 0.9, project_id="projB")
            r6xday124zm__store(mem, "g1", 0.5)
            mem.prune_partitioned(max_items=100, per_partition_max=2)
            assert mem.get("b_high") is not None, "B 项目高价值记忆不得被 A 项目挤掉"
            assert mem.get("g1") is not None, "全局层不因 A 项目满额被误裁"
        finally:
            mem.close()

    def test_partition_trims_to_per_partition_cap(self) -> None:
        """单分区内超过 per_partition_max 时按重要性淘汰至配额上限。"""
        mem = LongTermMemory()
        try:
            r6xday124zm__store(mem, "a0", 0.1, project_id="projA")
            r6xday124zm__store(mem, "a1", 0.2, project_id="projA")
            r6xday124zm__store(mem, "a2", 0.3, project_id="projA")
            r6xday124zm__store(mem, "a3", 0.9, project_id="projA")
            mem.prune_partitioned(max_items=100, per_partition_max=2)
            assert mem.get("a3") is not None, "高重要性先保留"
            assert mem.get("a2") is not None, "第二高保留"
            assert mem.get("a0") is None, "最低重要性被淘汰"
            assert mem.get("a1") is None, "次低被淘汰"
        finally:
            mem.close()

    def test_global_backstop_trims_overall_cap(self) -> None:
        """分区裁剪后仍超全局 max_items 时，全局按重要性兜底裁剪。"""
        mem = LongTermMemory()
        try:
            for i in range(3):
                r6xday124zm__store(mem, f"a{i}", 0.2, project_id="projA")
                r6xday124zm__store(mem, f"b{i}", 0.2, project_id="projB")
            for k in range(4):
                r6xday124zm__store(mem, f"g{k}", 0.2)
            mem.prune_partitioned(max_items=5, per_partition_max=10)
            assert mem.count() <= 5, f"全局兜底后总数 {mem.count()} 必须 <= 5"
        finally:
            mem.close()

    def test_fts_rows_synced_on_partition_prune(self) -> None:
        mem = LongTermMemory()
        try:
            r6xday124zm__store(mem, "a0", 0.1, project_id="projA")
            r6xday124zm__store(mem, "a1", 0.2, project_id="projA")
            r6xday124zm__store(mem, "a2", 0.9, project_id="projA")
            mem.prune_partitioned(max_items=100, per_partition_max=1)
            rows = mem._conn.execute("SELECT COUNT(*) FROM long_term_fts").fetchone()[0]
            assert rows == 1, f"FTS 应剩 1 行，实际 {rows}"
            assert mem.search_fts("a0", limit=5) == []
        finally:
            mem.close()

    def test_global_layer_and_multiple_employees_separate_partitions(self) -> None:
        """全局层 (None,None) 与各 employee 分区彼此独立——配额各自生效。"""
        mem = LongTermMemory()
        try:
            r6xday124zm__store(mem, "e1_a", 0.2, project_id="p", employee_id="e1")
            r6xday124zm__store(mem, "e1_b", 0.9, project_id="p", employee_id="e1")
            r6xday124zm__store(mem, "e2_a", 0.2, project_id="p", employee_id="e2")
            r6xday124zm__store(mem, "e2_b", 0.9, project_id="p", employee_id="e2")
            mem.prune_partitioned(max_items=100, per_partition_max=1)
            assert mem.get("e1_b") is not None
            assert mem.get("e1_a") is None, "e1 分区各自裁剪到 1"
            assert mem.get("e2_b") is not None
            assert mem.get("e2_a") is None, "e2 分区各自裁剪到 1"
        finally:
            mem.close()

    def test_returns_audit_ids_of_pruned_items(self) -> None:
        """prune_partitioned 返回被裁剪条目 id 集合（审计记录需求）。"""
        mem = LongTermMemory()
        try:
            r6xday124zm__store(mem, "a0", 0.1, project_id="projA")
            r6xday124zm__store(mem, "a1", 0.2, project_id="projA")
            r6xday124zm__store(mem, "a2", 0.9, project_id="projA")
            pruned = mem.prune_partitioned(max_items=100, per_partition_max=1)
            assert pruned == {"a0", "a1"}, f"审计 id 应为被裁剪的 a0/a1，实际 {pruned}"
        finally:
            mem.close()

    def test_noop_when_under_all_quotas(self) -> None:
        """所有分区都在配额内时无任何裁剪（返回空 + 数据不变）。"""
        mem = LongTermMemory()
        try:
            r6xday124zm__store(mem, "a0", 0.5, project_id="projA")
            r6xday124zm__store(mem, "g0", 0.5)
            assert mem.prune_partitioned(max_items=100, per_partition_max=10) == set()
            assert mem.count() == 2
        finally:
            mem.close()

    def test_zero_partition_cap_empties_all_partitions(self) -> None:
        """per_partition_max=0 时各分区（含全局层分区）统一清空——
        配额对所有分区一致生效。"""
        mem = LongTermMemory()
        try:
            r6xday124zm__store(mem, "a0", 0.9, project_id="projA")
            r6xday124zm__store(mem, "g0", 0.9)
            mem.prune_partitioned(max_items=100, per_partition_max=0)
            assert mem.get("a0") is None, "配额 0 的 projA 分区应清空"
            assert mem.get("g0") is None, "全局层分区同样受 per_partition_max=0 约束"
        finally:
            mem.close()

    def test_standard_prune_still_global_backward_compat(self) -> None:
        """回归：既有全局 prune(max_items) 语义不变（向后兼容，不破坏旧调用方
        agent_runner.py:469）。"""
        mem = LongTermMemory()
        try:
            r6xday124zm__store(mem, "x0", 0.1)
            r6xday124zm__store(mem, "x1", 0.5)
            r6xday124zm__store(mem, "x2", 0.9)
            assert mem.prune(max_items=2) == 1
            assert mem.get("x2") is not None
            assert mem.get("x0") is None
        finally:
            mem.close()
