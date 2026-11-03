"""契约：~修复批次。

覆盖：三路破坏者发现的 6 真缺陷 + 11 建议项
交付文档收集/B1B5报告与修复清单-.md）。

生产调用路径：PermissionsPipeline / ToolExecutor / AuditReporter / TraceExplainer /
MemoryCompactor / ExecutionContext（live 口径/窗口/缓存）/ EventBus /
GlobalMemoryStore / repair_tool_arguments / subagent 装配 / google_stream。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import Message, MessageRole, ToolCall
from bandi.memory.global_store import GlobalMemoryStore
from bandi.memory.session.store import SessionStore
from bandi.observability.audit_reporter import AuditFilter, AuditReporter
from bandi.observability.trace_explainer import TraceExplainer
from bandi.tools.permissions_pipeline import PermissionsPipeline
from bandi.tools.registry import ToolRegistry

# 功能：部分用例断言「本地时间相对 UTC 的偏移」，在 UTC 机器上无从体现
requires_non_utc_local = pytest.mark.skipif(
    datetime.now().astimezone().utcoffset() == timedelta(0),
    reason="用例依赖非 UTC 的本地时区",
)


class _AutoApprove:
    async def ask_permission(self, tool_name: str, args: dict[str, Any], risk: str) -> bool:
        return True


def _pp(tmp_path: Any) -> PermissionsPipeline:
    return PermissionsPipeline(project_root=str(tmp_path), user_interaction=_AutoApprove())


def test_rf1_quoted_redirect_target_denied(tmp_path: Any) -> None:
    """契约：引号包裹的越界重定向目标必须提取并拒绝
    原正则排除引号致 `> "C:/越界"` 完整绕过沙箱落盘。"""
    pp = _pp(tmp_path)
    tc = ToolCall(id="b1", name="bash", arguments={"command": 'echo x > "C:/evil/x.txt"'})
    verdict = pp._check_shell_safety(tc)
    assert verdict.allow is False, "引号越界目标必须被沙箱拒绝"


def test_rf1_home_and_variable_targets_fail_closed(tmp_path: Any) -> None:
    """契约：~ / $HOME 展开为 home 绝对路径（workspace 外拒绝）
    其他变量/命令替换无法静态解析 → fail-closed。

    注：workspace 用 tmp_path/ws 与 home（隔离后 = tmp_path）分离——否则
    ~/evil.txt 落在 project_root 内被合法放行（Path.home 隔离适配）。"""
    pp = PermissionsPipeline(project_root=str(tmp_path / "ws"), user_interaction=_AutoApprove())
    for command in (
        "echo x > ~/evil.txt",
        "echo x > $HOME/evil.txt",
        'echo x > "$HOME/evil.txt"',
        "echo x > $UNKNOWN_DIR/evil.txt",
    ):
        verdict = pp._check_shell_safety(
            ToolCall(id="b2", name="bash", arguments={"command": command})
        )
        assert verdict.allow is False, f"必须拒绝: {command}"


def test_rf1_git_and_sed_variants_detected(tmp_path: Any) -> None:
    """契约：git 全局配置参数间隔变体与 sed --in-place 长选项同样
    提取目标（原 `git -c k=v config --global` / `sed --in-place` 漏报）。"""
    from bandi.tools.permissions_pipeline import extract_shell_write_targets

    git_targets = extract_shell_write_targets("git -c k=v config --global user.name x")
    assert any("gitconfig" in t for t in git_targets)
    sed_targets = extract_shell_write_targets("sed --in-place s/a/b/ /etc/passwd")
    assert sed_targets == ["/etc/passwd"]


def test_rf2_exec_tools_declare_execute() -> None:
    """契约：python_exec/browser 权限 action 必须为 execute
    原推导归 write 致 member 由 execute 拒绝松弛为审批后可执行任意代码。"""
    from bandi.tools.browser import BrowserTool
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.python_exec import PythonExecTool

    assert ToolExecutor._permission_action_for(PythonExecTool()) == "execute"
    assert ToolExecutor._permission_action_for(BrowserTool()) == "execute"


def test_rf3_tool_failure_rate_from_tool_messages(tmp_path: Any) -> None:
    """契约：失败率分母必须来自 TOOL 消息（生产每调用落一条）
    而非 assistant metadata tool_calls（生产 assistant 不带 → 恒 0 假绿）。"""
    store = SessionStore(str(tmp_path / "s.db"))
    sid = store.create_session(title="t")
    store.append_message(sid, Message(role=MessageRole.USER, content="hi"))
    store.append_message(sid, Message(role=MessageRole.ASSISTANT, content="call"))
    store.append_message(
        sid, Message(role=MessageRole.TOOL, content="ok", tool_call_id="c1", name="read")
    )
    store.append_message(
        sid,
        Message(
            role=MessageRole.TOOL,
            content="err",
            tool_call_id="c2",
            name="read",
            meta={"tool_error": True},
        ),
    )
    report = AuditReporter(store).generate(AuditFilter())
    assert report.summary["total_tool_calls"] == 2, "分母必须数 TOOL 消息（生产形态）"
    assert report.summary["tool_failure_rate"] == 0.5, (
        f"失败率必须真实驱动（实际 {report.summary['tool_failure_rate']}）"
    )


@pytest.mark.asyncio
async def test_rf4_verdict_visible_with_event_session(tmp_path: Any) -> None:
    """契约：事件 data 无 session_id 时
    形态）必须取 event.session_id 归桶——原落 _default 致 explain 不可见。"""
    store = SessionStore(str(tmp_path / "s.db"))
    bus = EventBus()
    explainer = TraceExplainer(bus, store)
    await explainer.start()
    await bus.emit(Event(type=EventType.VERDICT_ISSUED, data={"action": "pass"}, session_id="s1"))
    text = explainer.explain("s1")
    assert "验证裁决" in text, "生产形态裁决必须归入会话轨迹"


@pytest.mark.asyncio
async def test_rf5_system_excerpt_survives_truncation(tmp_path: Any) -> None:
    """契约：压缩区超 source 预算（生产规模）时，system 约束
    摘录必须仍进入摘要源——原追加尾部被保前缀截断必丢。"""
    from bandi.memory.compaction import MemoryCompactor

    captured: list[list[dict[str, Any]]] = []

    async def _llm(messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        captured.append(messages)
        from bandi.core.message import LLMResponse, TokenUsage

        return LLMResponse(
            content="## 目标\n- 完成",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
            finish_reason="stop",
        )

    msgs = [Message(role=MessageRole.SYSTEM, content="项目约束：必须使用 Python 3.11")]
    msgs.append(Message(role=MessageRole.USER, content="填充" * 15000))
    msgs.append(Message(role=MessageRole.ASSISTANT, content="ok"))
    compactor = MemoryCompactor()
    await compactor.compact(msgs, _llm)
    user_text = "\n".join(
        str(m.get("content", "")) for r in captured for m in r if m.get("role") == "user"
    )
    assert "[系统约束摘录]" in user_text, "生产规模截断下约束摘录必须保留"
    assert "必须使用 Python 3.11" in user_text


def test_rf6_live_token_count_includes_arguments() -> None:
    """契约：live_token_count 必须计 tool_calls.arguments
    原只计 content，大参数（写文件类）被低估致压缩/超窗漏判。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.llm.mock import MockProvider

    provider = MockProvider()
    provider.count_tokens = lambda text: max(1, len(text or "") // 4) if text else 0
    big_args = {"data": "x" * 4000}
    ctx = ExecutionContext(
        messages=[
            Message(
                role=MessageRole.ASSISTANT,
                content="ok",
                tool_calls=[ToolCall(id="c1", name="write", arguments=big_args)],
            )
        ],
        provider=provider,
        config=AgentConfig(),
        bus=EventBus(),
    )
    content_only = sum(provider.count_tokens(m.content or "") for m in ctx.messages)
    assert ctx.live_token_count() > content_only + 500, (
        "arguments 必须计入 live 口径（4000 字符 ≈ 1000 token）"
    )


def test_rf7_output_path_sensitive_and_media_guard(tmp_path: Any) -> None:
    """契约：output_path 参数名进入敏感校验面；生成落盘
    （save_generation_output）覆盖 .env 必须拒绝。"""
    pp = _pp(tmp_path)
    tc = ToolCall(id="s1", name="image_generate", arguments={"output_path": ".env"})
    assert pp._check_sensitive_path(tc, None).allow is False, "output_path 参数必须进入敏感校验面"


@pytest.mark.asyncio
async def test_rf7_media_save_rejects_env(tmp_path: Any) -> None:
    from bandi.tools.media.base import GenerationOutput, save_generation_output

    out = GenerationOutput(success=True, kind="image", data=b"OVERWRITE")
    ok, reason, _ = await save_generation_output(
        out, output_path=".env", project_root=str(tmp_path)
    )
    assert ok is False, f"生成落盘不得覆盖 .env（reason={reason}）"


def test_rf8_executor_fallback_pipeline_gets_project_root(tmp_path: Any) -> None:
    """契约：ToolExecutor fallback 构造的权限管道必须继承
    project_root（原裸构造致沙箱基准=全局 workspace）。"""
    from bandi.tools.executor import ToolExecutor

    ex = ToolExecutor(registry=ToolRegistry(), project_root=str(tmp_path))
    assert ex._permissions_pipeline._project_root == str(tmp_path)


@pytest.mark.asyncio
async def test_rf9_recovery_syncs_seen_window(tmp_path: Any) -> None:
    """契约：enable_persistence 恢复的 event_id 必须同步进
    滑动窗口 deque（原只 add set，窗口逐出永不可达）。"""
    path = tmp_path / "events.jsonl"
    bus1 = EventBus(persist_path=path)
    bus1.enable_persistence()
    try:
        await bus1.emit(Event(type=EventType.TOOL_COMPLETED, event_id="e0"))
    finally:
        bus1.disable_persistence()
    bus2 = EventBus(persist_path=path)
    bus2.enable_persistence()
    try:
        assert "e0" in bus2._seen_event_ids
        assert "e0" in bus2._seen_order, "恢复 id 必须同步进滑动窗口"
    finally:
        bus2.disable_persistence()


@requires_non_utc_local
def test_rf10_search_normalizes_mixed_timestamps(tmp_path: Any) -> None:
    """契约：旧本地 naive 与新 UTC ISO 混排时按真实时间
    DESC（字符串序会颠倒：本地 10:00 = UTC 02:00 应排 UTC 04:00 之后）。"""
    store = GlobalMemoryStore(tmp_path / "g.db")
    try:
        store.put_global_memory("k_old", "旧条目")
        store.put_global_memory("k_new", "新条目")
        store._conn.execute(
            "UPDATE global_memory SET updated_at = '2026-09-12T10:00:00' WHERE key = 'k_old'"
        )
        store._conn.execute(
            "UPDATE global_memory SET updated_at = '2026-09-12T04:00:00+00:00' WHERE key = 'k_new'"
        )
        store._conn.commit()
        keys = [r["key"] for r in store.search("条目")]
        assert keys[0] == "k_new", f"UTC 04:00 必须排在本地 10:00 之前（实际 {keys}）"
    finally:
        store.close()


def test_rf11_empty_arguments_actionable_error() -> None:
    """契约：空串 arguments 不得静默降级 {}
    参数执行）——repair 返回 None，解析入口转可行动错误。"""
    from bandi.core.message import repair_tool_arguments
    from bandi.llm.adapters.openai_compat import _parse_tool_calls

    assert repair_tool_arguments("") is None
    calls = _parse_tool_calls(
        {"tool_calls": [{"id": "c1", "function": {"name": "write", "arguments": ""}}]}
    )
    assert "_parse_error" in calls[0].arguments, "空串必须转可行动错误而非空参执行"


def test_rf12_maintenance_mode_dead_code_removed() -> None:
    """契约：零消费死函数 _is_maintenance_mode 必须删除
    states.on_compressing 的 compressible/effective 口径）。"""
    import bandi.agent.deps as deps_mod

    assert not hasattr(deps_mod, "_is_maintenance_mode")


def test_rf13_small_window_effective_not_degenerate() -> None:
    """契约：reserve >= window 时有效窗口不得退化为 1
    （原 max(1, window-reserve) 全链 PROMPT_TOO_LONG）。"""
    from bandi.agent.base import ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.llm.mock import MockProvider

    ctx = ExecutionContext(
        messages=[],
        provider=MockProvider(),
        config=AgentConfig(context_window=1000, reserved_output_tokens=2000),
        bus=EventBus(),
    )
    assert ctx.effective_context_window == 500, (
        f"reserve 必须按窗口 50% 钳制（实际 {ctx.effective_context_window}）"
    )


def test_rf14_encode_cache_hit() -> None:
    """契约：同文本重复计数必须命中编码缓存
    重复编码，单轮多口径 30-54ms/次）。"""
    from bandi.utils.token_counter import TokenCounter

    TokenCounter._encode_cache.clear()
    text = "重复编码的文本内容 sample text for cache" * 3
    TokenCounter.count(text)
    assert text in TokenCounter._encode_cache, "编码结果必须进入缓存"


@pytest.mark.asyncio
async def test_rf16_google_stream_usage_merge() -> None:
    """契约：流式 usageMetadata 增量帧缺字段不得清零既有值
    （原整体重建——中间帧缺 promptTokenCount 会把 prompt_tokens 归 0）。"""
    from bandi.llm.adapters.google import google_stream

    frames = [
        b'data: {"usageMetadata": {"promptTokenCount": 100, "totalTokenCount": 110}, '
        b'"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}\n\n',
        b'data: {"usageMetadata": {"candidatesTokenCount": 10, "totalTokenCount": 110}, '
        b'"candidates": [{"content": {"parts": []}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            for f in frames:
                yield f

    class _Ctx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a: Any) -> bool:
            return False

    class _Client:
        def stream(self, *a: Any, **k: Any) -> _Ctx:
            return _Ctx()

    events = [e async for e in google_stream(_Client(), "k", "gemini-2.0-flash", [])]
    done = events[-1]
    assert done.usage is not None and done.usage.prompt_tokens == 100, (
        f"缺字段帧不得清零 prompt_tokens（实际 {getattr(done.usage, 'prompt_tokens', None)}）"
    )


def test_rf18_contextvar_reset_prevents_leak() -> None:
    """契约：set_event_context 后必须 reset_event_context 恢复
    泄漏会让后续 emit 被错误填充 session_id（c1 契约原误用 set(token=...)
    致全量顺序污染 trace 归桶）。"""
    import asyncio

    from bandi.core.event_bus import reset_event_context, set_event_context

    bus = EventBus()
    token = set_event_context(session_id="s-leak")
    ev1 = Event(type=EventType.TOOL_COMPLETED, data={})
    ev2 = Event(type=EventType.TOOL_COMPLETED, data={})

    async def run_first() -> None:
        await bus.emit(ev1)

    async def run_second() -> None:
        await bus.emit(ev2)

    asyncio.run(run_first())
    reset_event_context(token)
    asyncio.run(run_second())
    assert ev1.session_id == "s-leak", "set 范围内事件必须填充"
    assert not ev2.session_id, "reset 后事件不得再被填充（防跨会话污染）"


def test_rf17_cut_summary_reserves_appendix() -> None:
    """契约：_cut_summary 必须为尾部附录预留预算
    不含附录，极端输入总产物超 max_summary_chars）。"""
    from bandi.memory.compaction import MemoryCompactor

    compactor = MemoryCompactor()
    result = compactor._cut_summary("x" * 5000, reserve_chars=300)
    assert len(result) <= 1700, f"摘要截断必须计入附录预留（实际 {len(result)}，预算 2000-300）"
