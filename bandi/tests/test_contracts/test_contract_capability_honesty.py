"""契约：能力询问不得被闲聊裁剪剪掉工具面。

缺陷：`_CHITCHAT_PHRASES` 把「你能做什么/会做什么/有什么功能」与「你好/你是谁」
混为一谈，两者都走最小工具面。能力询问时模型只看得见 `tool_search`，于是
（正确地）报告自己看见的那几个工具，对用户谎报产品能力——

    「暂不支持的：写入/创建文件、数据处理、图表生成、团队组建」

而这些工具全部存在
chart_generator 8 次）。数据集 rubric `capability-honesty-v1` 直接判负。
"""

from __future__ import annotations

import pytest

from bandi.tools.selection import (
    _CHITCHAT_TOOL_NAMES,
    is_capability_query,
    is_chitchat_query,
    select_relevant_schemas,
)


def _schema(name: str) -> dict:
    return {"name": name, "description": f"{name} 工具", "parameters": {}}


FULL = [
    _schema(n)
    for n in (
        "tool_search",
        "read",
        "write",
        "bash",
        "excel_generate",
        "chart_generator",
        "team_create",
        "memory_assert",
        "web_search",
    )
]


class TestCapabilityQueryIgnoresMaxTools:
    """真实注册表有 100+ 工具，必须在相关度裁剪**之前**返回全量。"""

    def _big(self) -> list[dict]:
        return [_schema(f"tool_{i}") for i in range(120)] + FULL

    @pytest.mark.parametrize("q", ["你能帮我做什么？", "有什么功能", "你会做什么"])
    def test_full_surface_despite_over_limit(self, q: str) -> None:
        got = {s["name"] for s in select_relevant_schemas(self._big(), q)}
        assert got >= {s["name"] for s in FULL}, "能力询问被相关度裁剪了"
        for tool in ("write", "excel_generate", "chart_generator", "team_create"):
            assert tool in got

    def test_business_query_still_trimmed(self) -> None:
        """破坏者视角：只给能力询问放行，业务轮的裁剪不得被一并放开。"""
        picked = select_relevant_schemas(self._big(), "帮我搜索一下竞品资讯")
        assert len(picked) < len(self._big())


class TestClassification:
    @pytest.mark.parametrize(
        "q", ["你能帮我做什么？", "你能做什么", "有什么功能", "会做什么", "支持什么"]
    )
    def test_capability_queries_recognized(self, q: str) -> None:
        assert is_capability_query(q)

    @pytest.mark.parametrize("q", ["你好呀", "你好", "你是谁", "介绍一下你自己"])
    def test_pure_chitchat_not_capability(self, q: str) -> None:
        assert not is_capability_query(q)

    @pytest.mark.parametrize("q", ["你好呀", "你是谁", "介绍一下你自己"])
    def test_pure_chitchat_still_trimmed(self, q: str) -> None:
        """破坏者视角：真闲聊的裁剪是 token 优化，不能因为这次修复就丢掉。"""
        assert is_chitchat_query(q)

    def test_business_query_untouched(self) -> None:
        q = "帮我查一下订单服务的依赖"
        assert not is_chitchat_query(q)
        assert not is_capability_query(q)


class TestSurfaceNotMisreported:
    @pytest.mark.parametrize("q", ["你好呀，你能帮我做什么？", "你能做什么", "有什么功能"])
    def test_capability_query_gets_full_surface(self, q: str) -> None:
        """核心回归：能力询问必须看到全部工具，否则只能谎报。"""
        got = {s["name"] for s in select_relevant_schemas(FULL, q)}
        for tool in ("write", "excel_generate", "chart_generator", "team_create"):
            assert tool in got, f"能力询问时 {tool} 不可见 → 模型会谎报不支持"

    def test_chitchat_still_minimal(self) -> None:
        got = {s["name"] for s in select_relevant_schemas(FULL, "你好呀")}
        assert got <= _CHITCHAT_TOOL_NAMES | {"read"}, f"闲聊轮应保持精简，实得 {got}"

    def test_capability_query_does_not_become_chitchat(self) -> None:
        assert not is_chitchat_query("你能帮我做什么？")
