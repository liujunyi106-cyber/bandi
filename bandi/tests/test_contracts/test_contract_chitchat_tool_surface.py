"""契约：**纯闲聊**轮不注入完整工具面（能力询问见 capability_honesty 契约）。

台账（`sess-93a6c77d`，3 轮）：闲聊轮 tool_schema=5524 tokens，占单轮 input
的 61%，而 history 仅 16 tokens——对话内容几乎不占预算，工具面却是最大头。
3 轮累计 33.1k tokens 里，工具面占约 50%。

本契约钉两件事：
1. 闲聊判定准确——带动作意图/业务信号的判否（不误杀业务轮）
2. 闲聊轮工具面收缩到最小集，且**模型仍保有按需扩面入口**（`tool_search`）

**能力询问（「你能做什么」「有什么功能」）不属于闲聊**：裁掉工具面会让模型只能
看见挑剩的几个工具，从而如实却错误地回答「暂不支持写文件/图表/团队」——
对用户谎报能力。判定见 `test_contract_capability_honesty.py`。"""

from __future__ import annotations

import pytest

from bandi.tools.selection import (
    MAX_TOOLS,
    is_chitchat_query,
    select_relevant_schemas,
)


def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": f"{name} 描述"}}


_SCHEMAS = [
    _schema(n) for n in ("read", "write", "bash", "web_search", "team_create", "tool_search")
]


class TestChitchatIntentGate:
    @pytest.mark.parametrize(
        "query",
        [
            "你是谁",
            "你叫什么",
            "自我介绍",
            "你好",
            "早上好",
        ],
    )
    def test_chitchat_detected(self, query: str) -> None:
        assert is_chitchat_query(query), f"{query!r} 应判为闲聊"

    @pytest.mark.parametrize(
        "query",
        [
            "帮我搜索一下竞品资料",
            "读一下 src/a.py 的报错",
            "生成一份季度报告",
            "你好，帮我看下这个 bug",
            "创建明天的任务",
            "你好呀，你能帮我做什么？顺便查一下最近的 AI 新闻",
            "把这 3000 字的会议记录整理成结构化纪要并提炼待办",
        ],
    )
    def test_business_query_not_chitchat(self, query: str) -> None:
        assert not is_chitchat_query(query), f"{query!r} 不应判为闲聊（误杀业务轮）"


class TestChitchatToolSurface:
    def test_chitchat_gets_minimal_surface_with_search_entry(self) -> None:
        picked = select_relevant_schemas(_SCHEMAS, "你好呀")
        names = [s["function"]["name"] for s in picked]
        assert names == ["tool_search"], f"闲聊轮只应留按需扩面入口，实得 {names}"

    def test_chitchat_surface_shrinks_dramatically(self) -> None:
        """破坏者视角：必须真的变小，而不是换个方式送全量。"""
        big = [_schema(f"tool_{i}") for i in range(100)] + _SCHEMAS
        chitchat = select_relevant_schemas(big, "你好呀")
        business = select_relevant_schemas(big, "帮我搜索一下竞品资讯")
        assert len(chitchat) < len(business)
        assert len(chitchat) <= 2

    def test_business_query_keeps_normal_trimming(self) -> None:
        """业务轮行为不变：仍走关键词裁剪，不是零工具。"""
        big = [_schema(f"tool_{i}") for i in range(100)] + _SCHEMAS
        picked = select_relevant_schemas(big, "帮我搜索一下竞品资料")
        assert len(picked) > 1, "业务轮不应被削成闲聊面"
        assert len(picked) <= max(MAX_TOOLS, 17)

    def test_empty_toolset_does_not_crash(self) -> None:
        assert select_relevant_schemas([], "你好") == []

    def test_accepts_message_object_not_only_str(self) -> None:
        """破坏者视角：perception 传进来的是 Message 对象，不是 str。

        曾经 `str(Message)` 走 repr → 意图门永远判否 → 首轮把全量工具面
        （≈6100 tokens）以文本再送一遍，与 `tools` 参数通道重复。
        """
        from bandi.core.message import Message, MessageRole

        msg = Message(role=MessageRole.USER, content="你好呀")
        assert is_chitchat_query(msg), "Message 对象必须按其 content 判定"
        picked = select_relevant_schemas(_SCHEMAS, msg)
        assert [s["function"]["name"] for s in picked] == ["tool_search"]

    def test_business_message_object_not_chitchat(self) -> None:
        from bandi.core.message import Message, MessageRole

        msg = Message(role=MessageRole.USER, content="帮我搜索一下竞品资料")
        assert not is_chitchat_query(msg)
