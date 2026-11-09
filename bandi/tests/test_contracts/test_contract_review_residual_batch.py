"""

A. 压缩截断 token 化——触发已 token 驱动；输入/输出截断若仍按字符，超长中文会
   直接打穿 token 预算（1 字 ≈ 1.6-2 token）。契约钉：默认输出截断按 token
   预算（1500）而非 2000 字符；超长源文本按 token 预算截断；prompt 分项计量
   必须含工具 schema 独立分项。
B. 摘要字段级保真——实体级三检之外，已完成项/下一步动作各自独立判降级
   防止"只保实体、丢弃进度"的隐性信息丢失。
C. 统一融合分——跨源排序在 plus 默认「按分类 score」基础上提供 fusion 模式
   新鲜近的事实按 (relevance, importance, recency, recall) 融合排序，default
   经典路径不受影响。
D. 去重稳定化——同一事实仅空白/换行形态不同时只注入一条（旧 strip 去重对
   换行漂移失效）。

"""

from datetime import UTC, datetime, timedelta

import pytest

from bandi.memory.compaction import MemoryCompactor
from bandi.memory.compression_pipeline import SUMMARY_MESSAGE_PREFIX
from bandi.memory.fidelity import check_fidelity
from bandi.memory.memory_provider import MemoryManager
from bandi.utils.token_counter import TokenCounter

# ── A. 压缩 token 预算 ──────────────────────────────────────────────


class _SummaryCapture:
    """功能：捕获发给 LLM 的摘要请求，回放预设响应。"""

    def __init__(self, response_text: str):
        self.requests: list[list[dict]] = []
        self._response_text = response_text

    async def __call__(self, messages: list[dict]):
        from bandi.core.message import LLMResponse, TokenUsage

        self.requests.append(messages)
        return LLMResponse(
            content=self._response_text,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )


def _messages_with_payload(payload: str, count: int = 3) -> list:
    from bandi.core.message import Message, MessageRole

    msgs = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
        Message(role=MessageRole.USER, content=payload),
    ]
    for i in range(count):
        msgs.append(
            Message(role=MessageRole.USER, content=f"第{i}轮分析：logs/run_{i}.py ValueError")
        )
        msgs.append(Message(role=MessageRole.ASSISTANT, content=f"已定位第{i}个问题"))
    return msgs


def _summary_body(messages: list) -> str:
    for m in messages:
        if "[上下文摘要]" in (getattr(m, "content", "") or ""):
            return (m.content or "").replace(SUMMARY_MESSAGE_PREFIX, "")
    raise AssertionError("产物必须包含上下文摘要消息")


@pytest.mark.asyncio
async def test_default_compactor_truncates_overlong_summary_to_token_budget() -> None:
    """契约（残留A 破坏者）：LLM 返回超长中文摘要时，输出截断必须按 token 预算
    （1500）而非字符兜底（2000 字 ≈ 3000+ token 会打穿预算）。"""
    capture = _SummaryCapture("很长的中文摘要" * 1500)
    compactor = MemoryCompactor(target_token_budget=10)
    result, success, _ = await compactor.compact(
        _messages_with_payload("请压缩以下内容：" * 100), capture
    )
    assert success is True

    body = _summary_body(result)
    assert body, "截断后摘要不得为空（前缀保留）"
    assert TokenCounter.count(body) <= 1500, "默认输出截断必须由 token 预算（1500）约束"


@pytest.mark.asyncio
async def test_overlong_source_text_truncated_by_token_budget() -> None:
    """契约（残留A 破坏者）：超长源文本进入提示时必须按 token 预算截断（上限
    32000）——否则超长帖子直接撑爆上下文（input 侧唯一防线）。"""
    capture = _SummaryCapture("## 目标\n- 完成摘要\n\n## 进度\n- 完成")
    compactor = MemoryCompactor(target_token_budget=10)
    result, success, _ = await compactor.compact(
        _messages_with_payload("非常长的源文本内容" * 6000), capture
    )
    assert success is True
    assert capture.requests, "前置：必须真实触发 LLM 摘要请求"

    for request in capture.requests:
        for m in request:
            content = str(m.get("content", "") or "")
            if "请压缩以下对话内容" in content:
                body = content.split("\n\n", 1)[-1] if "\n\n" in content else content
                # B1b 1.5：摘要指令后置于尾部 user（前缀缓存）——源文本段为
                # 「指令分隔符」之前的正文，预算断言只针对它。
                src = body.split("\n\n你是一个对话摘要工具", 1)[0]
                assert TokenCounter.count(src) <= 32000 + 200, (
                    "输入源片段必须按 token 预算截断（上限 32000），超长帖不得整段进请求"
                )


def test_prompt_breakdown_counts_tools_schema() -> None:
    """契约（残留A）：prompt 分项计量必须含工具 schema 独立分项——审计只能看到
    role 分项时工具定义占用的 tokens 不可见。"""
    from bandi.agent.states import compute_prompt_breakdown

    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": "检索长期记忆，支持中文关键词匹配与重要性排序返回",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]
    messages = [{"role": "system", "content": "你是 Bandi"}, {"role": "user", "content": "你好"}]
    out = compute_prompt_breakdown(messages, tools)
    assert "tools" in out, "非空 schema 时 tools 独立分项必须存在"
    assert out["tools"] > 0, "非空工具 schema 必须计为正 tokens"
    out_empty = compute_prompt_breakdown(messages, None)
    assert "tools" not in out_empty, "无工具时不得引入 tools 键（保持 4 role 契约）"


# ── B. 摘要字段级保真 ──────────────────────────────────────────────


def test_summary_dropping_completion_marker_degraded() -> None:
    """契约（残留B 破坏者）：源含「已完成」标记而摘要全丢→ completed_degraded
    必须为 True（续作无法区分完成了什么）。"""
    report = check_fidelity("已完成领域建模，正在调研可观测性组件", "调研可观测性组件相关候选")
    assert report.completed_degraded is True


def test_summary_dropping_next_action_marker_degraded() -> None:
    """契约（残留B 破坏者）：源含「下一步」标记而摘要删句→ next_action_degraded
    必须为 True（续作无法恢复后续动作）。"""
    report = check_fidelity("已完成领域建模。下一步实现查询性能优化", "已完成领域建模")
    assert report.completed_degraded is False
    assert report.next_action_degraded is True


def test_faithful_summary_keeps_fields_clean() -> None:
    """契约（残留B 正常面）：源的关键字段完整保留→ 新增降级字段全 False。"""
    src = "已完成领域建模。下一步实现查询性能优化。日志见 logs/a.py ValueERror"
    summary = src
    report = check_fidelity(src, summary)
    assert report.completed_degraded is False
    assert report.next_action_degraded is False
    assert report.status_degraded is False
    assert report.degraded is False


# ── C. 统一融合分 ──────────────────────────────────────────────────


def _fusion_fixture(tmp_path) -> tuple[MemoryManager, str, str]:
    from bandi.memory.long_term import LongTermMemory

    class _Row:
        @property
        def importance(self) -> float:
            return self._imp

        @property
        def timestamp(self) -> str:
            return self._ts

    now = datetime.now(UTC)

    class _FakeLT(LongTermMemory):
        def __init__(self) -> None:
            self._rows = [
                _row("lt-stale", "蓝鲸老记忆但重要性高", 0.8, now - timedelta(days=180)),
                _row("lt-fresh", "蓝鲸新近记忆也可用", 0.7, now),
            ]

        def search_fts_ranked(self, query, limit=5, project_id=None, employee_id=None):
            return self._rows

    def _row(rid: str, content: str, imp: float, ts: datetime):
        r = _Row()
        r._imp = imp
        r._ts = ts.isoformat()
        r.id = rid
        r.content = content
        return r

    mgr = MemoryManager(long_term=_FakeLT())
    return mgr, "lt-stale", "lt-fresh"


def test_fusion_scoring_prefers_fresh_over_stale_high_importance(tmp_path) -> None:
    """契约（残留C）：fusion 模式按融合分排序，新鲜近事实可压过老化高重要性
    事实；default（起 = fusion）与显式 fusion 一致，classified 仍可显式选用。"""
    mgr, stale_id, fresh_id = _fusion_fixture(tmp_path)

    fused = mgr.search_unified("蓝鲸", scoring="fusion")
    assert fused[0].key == fresh_id, "fusion 模式必须让新近事实优先（融合分排序）"

    classified = mgr.search_unified("蓝鲸", scoring="classified")
    assert classified[0].key == stale_id, "显式 classified 模式保持 score（importance）降序"


def test_fusion_scoring_falls_back_safely_without_metadata(tmp_path) -> None:
    """契约（残留C 破坏者）：fusion 遇缺时间/重要性字段不得抛错，正常返回。"""
    from bandi.memory.long_term import LongTermMemory

    class _Row:
        id = "lt-x"
        content = "蓝鲸盲知识"
        importance = 0.5
        timestamp = ""

    class _BareLT(LongTermMemory):
        def search_fts_ranked(self, query, limit=5, project_id=None, employee_id=None):
            return [_Row()]

    mgr = MemoryManager(long_term=_BareLT())
    out = mgr.search_unified("蓝鲸", scoring="fusion")
    assert out and out[0].key == "lt-x"
    assert out[0].value == "蓝鲸盲知识"


# ── D. 去重稳定化 ──────────────────────────────────────────────────


def test_whitespace_variant_duplicates_injected_once(tmp_path) -> None:
    """契约（残留D 破坏者）：同一事实仅空白/换行形态不同（跨分类）只注入一条
    ——旧 strip 去重对换行漂移失败，会把同实质记忆注入两次。"""
    from bandi.memory.long_term import LongTermMemory, MemoryItem
    from bandi.memory.working import ScratchHit

    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(MemoryItem(item_id="lt-a", content="threading 管理并发共享状态", importance=0.5))

    class _Working:
        def search(self, q, limit=5, session_id=""):
            # 与 lt 除空白/换行外字符序列完全一致——折叠空白后必须同键
            return [(ScratchHit("kv", "kw1", "threading\n管理并发共享状态", "2026-08-28"), 0.5)]

    mgr = MemoryManager(long_term=lt, working=_Working())
    out = mgr.search_unified("并发")
    values = [getattr(x, "value", "") for x in out]
    matching = [v for v in values if "并发" in v]

    assert len(matching) == 1, "空白/换行形态不同的同实质记忆必须只注入一条"
