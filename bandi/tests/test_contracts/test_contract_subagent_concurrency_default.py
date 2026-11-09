# 功能：契约——子代理 fan-out 默认并行上限（平台限流；显式 0 仍可全并发）。
# 钉行为：SubAgentTool 未显式传 max_concurrent 时使用构造注入的平台默认值；
# 显式传 0 = 不限（模型逐调用可控）；显式正数优先。
from __future__ import annotations

import asyncio
from typing import Any

from bandi.agent.subagent import SubagentResult
from bandi.agent.subagent_tool import SubAgentTool


class _SpyManager:
    seen: dict[str, Any] = {}

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def spawn_many(self, tasks, config, **kwargs):
        type(self).seen["max_concurrent"] = kwargs.get("max_concurrent")
        return [SubagentResult(success=True, summary="ok") for _ in tasks]

    def cancel_all(self) -> None:
        pass

    def close(self) -> None:
        pass


def _run(tool: SubAgentTool, params: dict[str, Any]) -> None:
    from bandi.agent import subagent_tool as st_mod

    original = st_mod.SubagentManager
    st_mod.SubagentManager = _SpyManager  # type: ignore[assignment]
    try:
        asyncio.run(tool.execute(params, None))
    finally:
        st_mod.SubagentManager = original  # type: ignore[assignment]


def test_default_concurrency_applied_when_omitted() -> None:
    _SpyManager.seen.clear()
    tool = SubAgentTool(provider=object(), default_max_concurrency=3)

    _run(tool, {"tasks": ["a", "b", "c"]})

    assert _SpyManager.seen["max_concurrent"] == 3


def test_explicit_zero_disables_limit() -> None:
    _SpyManager.seen.clear()
    tool = SubAgentTool(provider=object(), default_max_concurrency=3)

    _run(tool, {"tasks": ["a", "b"], "max_concurrent": 0})

    assert _SpyManager.seen["max_concurrent"] == 0


def test_explicit_value_wins() -> None:
    _SpyManager.seen.clear()
    tool = SubAgentTool(provider=object(), default_max_concurrency=3)

    _run(tool, {"tasks": ["a", "b"], "max_concurrent": 7})

    assert _SpyManager.seen["max_concurrent"] == 7
