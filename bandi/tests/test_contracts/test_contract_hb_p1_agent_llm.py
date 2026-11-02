# 功能：契约——agent+llm 子批 7 项修复（第 4 轮审计）
# 钉行为（不钉实现）：
# （放大上限重试）与耗尽标记必须对 "max_tokens" 同样生效。
# 后续合法文本必须保留（"结束"不丢）。
# 同会话确定性（跨进程可重算）保持。
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bandi.agent.base import ExecutionContext
from bandi.agent.states import _TRUNCATION_EXHAUSTED_NOTE, on_thinking
from bandi.core.config import AgentConfig, BandiConfig
from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall
from bandi.llm.streaming import StreamEvent, StreamEventType
from bandi.tools.base import BaseTool, RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry

# ── Google tool_call_id ─────────────────────────────────────────────


def _google_payload(name: str, times: int = 1) -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"functionCall": {"name": name, "args": {"path": "x"}}}
                        for _ in range(times)
                    ]
                },
                "finishReason": "STOP",
            }
        ],
    }


class TestGoogleToolIds:
    def test_tool_ids_unique_within_response(self) -> None:
        from bandi.llm.adapters.google import _parse_google_response

        calls = _parse_google_response(_google_payload("read", times=3)).tool_calls
        ids = [c.id for c in calls]
        assert len(set(ids)) == 3, f"同响应内 id 必须唯一: {ids}"
        assert all(i.startswith("fc_read") for i in ids)

    def test_tool_ids_not_reused_across_responses(self) -> None:
        """跨响应（两轮对话）同名工具不得复用同一 id——成本归因/历史关联按 id 索引。"""
        from bandi.llm.adapters.google import _parse_google_response

        first = _parse_google_response(_google_payload("read")).tool_calls[0].id
        second = _parse_google_response(_google_payload("read")).tool_calls[0].id
        assert first != second, f"跨响应复用同一 id: {first!r}"


# ── 截断语义归一 ────────────────────────────────────────────────────


class _Bus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class _KwProvider:
    model_name = "stub"
    protocol = "openai"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.kwargs_seen: list[dict] = []

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    async def complete(self, messages: list[dict], tools: Any = None, **kwargs: Any) -> LLMResponse:
        self.kwargs_seen.append(dict(kwargs))
        if not self._responses:
            return LLMResponse(content="done")
        return self._responses.pop(0)


def _make_ctx(provider: _KwProvider) -> ExecutionContext:
    ctx = ExecutionContext(
        messages=[
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="hello"),
        ],
        provider=provider,  # type: ignore[arg-type]
        config=AgentConfig(context_window=128_000, compression_threshold=0.85),
        bus=_Bus(),
    )
    ctx.meta["bandi_config"] = BandiConfig()
    return ctx


class TestTruncationSemantics:
    @pytest.mark.asyncio
    async def test_max_tokens_triggers_recovery(self) -> None:
        """Anthropic/Google 风格 finish_reason=max_tokens 必须触发截断恢复（旧仅认 length）。"""
        provider = _KwProvider(
            [
                LLMResponse(content="part-1", finish_reason="max_tokens"),
                LLMResponse(content="full answer"),
            ]
        )
        ctx = _make_ctx(provider)
        await on_thinking(ctx)
        assert len(provider.kwargs_seen) == 2, "max_tokens 截断必须触发恢复调用"
        assert provider.kwargs_seen[1].get("max_tokens") == 32768, (
            f"恢复请求必须提升输出上限，实际 {provider.kwargs_seen[1].get('max_tokens')}"
        )

    @pytest.mark.asyncio
    async def test_max_tokens_exhaustion_marks_incomplete(self) -> None:
        """max_tokens 风格恢复耗尽同样必须显式标记不完整（不静默当完整回答）。"""
        provider = _KwProvider(
            [
                LLMResponse(content="p1", finish_reason="max_tokens"),
                LLMResponse(content="p2", finish_reason="max_tokens"),
                LLMResponse(content="p3", finish_reason="max_tokens"),
            ]
        )
        ctx = _make_ctx(provider)
        await on_thinking(ctx)
        assert ctx.meta.get("truncation_exhausted") is True, "耗尽必须显式标记"
        assert _TRUNCATION_EXHAUSTED_NOTE in (ctx.current_response.content or ""), (
            "耗尽内容必须带不完整标记"
        )


# ── 流式 PII 边界不丢字 ─────────────────────────────────────────────


class _PiiStub:
    def sanitize(self, text: str) -> str:
        return text.replace("13800138000", "[PHONE]")


class _SplitStreamProvider:
    model_name = "stub"
    protocol = "openai"

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = deltas

    async def stream(self, messages: list[dict], **kwargs: Any) -> Any:
        for d in self._deltas:
            yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta=d)
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")


class TestStreamPiiBoundary:
    def test_filtered_delta_tail_not_dropped(self) -> None:
        """悬空前缀被 sanitize 改写时，delta 中剩余文本必须保留（"结束"不得丢）。"""
        from bandi.agent.states import _stream_thinking

        ctx = ExecutionContext(messages=[], provider=object(), config=AgentConfig(), bus=EventBus())
        ctx.pii_detector = _PiiStub()
        emitted: list[str] = []

        async def _on(ev: Event) -> None:
            if ev.type == EventType.LLM_STREAM_EVENT and ev.data.get("type") == "content_delta":
                emitted.append(ev.data.get("content", ""))

        asyncio.run(ctx.bus.subscribe(EventType.LLM_STREAM_EVENT, _on))
        provider = _SplitStreamProvider(["电话 1380013", "8000 结束"])
        resp = asyncio.run(_stream_thinking(ctx, [], provider=provider))
        blob = "".join(emitted) + (resp.content or "")
        assert "结束" in blob, f"sanitize 改写前缀后剩余文本被整段丢弃（实际 {blob!r}）"
        assert "13800138000" not in blob, "完整手机号不得泄漏"


class TestApplyPatchPartialFailure:
    @pytest.fixture(autouse=True)
    def _sandbox(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)

    @pytest.mark.asyncio
    async def test_partial_hunk_failure_raises_and_keeps_file(self, tmp_path: Path) -> None:
        from bandi.tools.apply_patch import ApplyPatchTool
        from bandi.tools.errors import NonRetryableToolError

        target = tmp_path / "t.txt"
        original = "a\nb\nc\n"
        target.write_text(original, encoding="utf-8")
        patch = (
            "--- a/t.txt\n+++ b/t.txt\n"
            "@@ -1,2 +1,2 @@\n a\n-b\n+b1\n"
            "@@ -9,2 +9,2 @@\n-不存在\n+xxx\n"
        )
        tool = ApplyPatchTool()
        with pytest.raises(NonRetryableToolError) as exc_info:
            await tool.execute({"patch": patch})
        assert "failed" in str(exc_info.value).lower(), f"必须显式报失败: {exc_info.value}"
        assert target.read_text(encoding="utf-8") == original, "部分失败不得半应用写盘"

    @pytest.mark.asyncio
    async def test_all_hunks_ok_still_succeeds(self, tmp_path: Path) -> None:
        from bandi.tools.apply_patch import ApplyPatchTool

        target = tmp_path / "t2.txt"
        target.write_text("a\nb\n", encoding="utf-8")
        tool = ApplyPatchTool()
        result = await tool.execute(
            {"patch": "--- a/t2.txt\n+++ b/t2.txt\n@@ -1,2 +1,2 @@\n a\n-b\n+b1\n"}
        )
        assert "applied" in result
        assert target.read_text(encoding="utf-8") == "a\nb1\n"


# ── 会话租约续期 ────────────────────────────────────────────────────


class _LeaseStore:
    """最小会话存储面（get_session / CAS / 全域更新）。"""

    def __init__(self, metadata: dict | None = None) -> None:
        self._meta = dict(metadata or {})

    def get_session(self, session_id: str) -> dict:
        return {"session_id": session_id, "metadata": dict(self._meta)}

    def update_session_if_metadata(self, session_id: str, metadata: dict, expected: dict) -> bool:
        if dict(self._meta) != dict(expected):
            return False
        self._meta = dict(metadata)
        return True

    def update_session(self, session_id: str, metadata: str | None = None, **kw: Any) -> None:
        if metadata is not None:
            self._meta = json.loads(metadata)


class TestSessionLeaseRenewal:
    def test_renew_extends_owner_lease(self) -> None:
        from bandi.core.agent_runner import (
            _INSTANCE_ID,
            _acquire_session_slot,
            _release_session_slot,
            _renew_session_lease,
        )

        store = _LeaseStore()
        assert _acquire_session_slot("s1", store=store) is True
        try:
            # 模拟租约已消耗大半（5 秒后到期）
            meta = store.get_session("s1")["metadata"]
            soon = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
            meta["execution_claim"]["lease_expires_at"] = soon
            store._meta = meta
            assert _renew_session_lease("s1", store=store) is True, "owner 续期必须成功"
            renewed = store.get_session("s1")["metadata"]["execution_claim"]
            expires = datetime.fromisoformat(str(renewed["lease_expires_at"]))
            assert expires > datetime.now(UTC) + timedelta(seconds=60), (
                f"续期后过期时间必须前移，实际 {expires.isoformat()}"
            )
        finally:
            _release_session_slot("s1", store=store)
        # 释放后他实例可认领（对比：未续期的旧租约场景不在本用例）
        assert _acquire_session_slot("s1", store=store, instance_id="other") is True
        _release_session_slot("s1", store=store, instance_id="other")
        _ = _INSTANCE_ID

    def test_renew_rejects_non_owner(self) -> None:
        from bandi.core.agent_runner import _renew_session_lease

        owner_meta = {
            "execution_claim": {
                "instance_id": "someone-else",
                "lease_expires_at": (datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
            }
        }
        store = _LeaseStore(owner_meta)
        assert _renew_session_lease("s1", store=store) is False, "不得劫持他实例租约"

    def test_renew_without_lease_returns_false(self) -> None:
        from bandi.core.agent_runner import _renew_session_lease

        store = _LeaseStore()
        assert _renew_session_lease("s1", store=store) is False


# ── CostTracker 预算就地更新 ────────────────────────────────────────


class TestSharedCostTrackerBudgetUpdate:
    def test_budget_change_keeps_accumulator(self) -> None:
        from bandi.llm.cost_tracker import (
            get_shared_cost_tracker,
            reset_shared_cost_tracker,
        )
        from bandi.llm.model_catalog import ModelCatalog

        reset_shared_cost_tracker()
        try:
            t1 = get_shared_cost_tracker(ModelCatalog(), budget_limit=100.0)
            t1.record(
                session_id="s-1",
                provider="bandi",
                model="deepseek-v4-flash",
                prompt_tokens=1200,
                completion_tokens=300,
            )
            spent = t1.daily_total
            assert spent > 0
            t2 = get_shared_cost_tracker(ModelCatalog(), budget_limit=200.0)
            assert t2 is t1, "budget_limit 变化不得重建轨道（否则当日累计清零）"
            assert t2.daily_total == pytest.approx(spent), (
                f"当日累计必须保留，{t2.daily_total} != {spent}"
            )
        finally:
            reset_shared_cost_tracker()


# ── 幂等键会话维度 ──────────────────────────────────────────────────


class _ResumeProbeTool(BaseTool):
    name = "resume_probe"
    description = "跨进程恢复探针"
    category = ToolCategory.UTIL
    retry_policy = RetryPolicy(retryable=False, cross_process_resume=True)

    async def execute(self, params: dict, context: ToolPermissionContext | None = None) -> str:  # type: ignore[override]
        return "ok"


def _executor() -> ToolExecutor:
    reg = ToolRegistry()
    tool = _ResumeProbeTool()
    reg.register_instance(tool)
    return ToolExecutor(registry=reg, project_root="/proj/x")


class TestIdempotencyKeySessionScope:
    def test_cross_session_same_args_distinct_keys(self) -> None:
        """跨会话同参调用必须不同 key——否则第二会话被判「重放」假成功。"""
        tool = _ResumeProbeTool()
        tc = ToolCall(id="c1", name="resume_probe", arguments={"op": "deploy"})
        ex1 = _executor()
        ex1.bind_session_meta({"session_id": "session-a"})
        ex2 = _executor()
        ex2.bind_session_meta({"session_id": "session-b"})
        key_a = ex1._idempotency_key_for(tool, tc)
        key_b = ex2._idempotency_key_for(tool, tc)
        assert key_a != key_b, f"跨会话同参必须区分: {key_a!r}"

    def test_same_session_deterministic_across_instances(self) -> None:
        """同会话同参跨进程必须确定性同 key（恢复命中语义保持）。"""
        tool = _ResumeProbeTool()
        tc = ToolCall(id="c1", name="resume_probe", arguments={"op": "deploy"})
        ex1 = _executor()
        ex1.bind_session_meta({"session_id": "session-a"})
        ex2 = _executor()
        ex2.bind_session_meta({"session_id": "session-a"})
        assert ex1._idempotency_key_for(tool, tc) == ex2._idempotency_key_for(tool, tc)
