"""契约：EventBus 横切订阅缺依赖必须显式可见（防伪接线）。

原实现 `if obj is None: continue` 静默跳过：EventBus 照常收发事件，但计费/落库/
日志侧永不响应——「代码看着接线了、实际订阅未建立」，排障时零线索。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from bandi.core.dependencies import Dependencies
from bandi.core.event_bus import Event, EventBus, EventType, bootstrap_event_bus


class _Recorder:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def on_llm_response(self, event: object) -> None:
        self.seen.append("llm_response")

    async def on_tool_done(self, event: object) -> None:
        self.seen.append("tool_done")


class _AllHandlers:
    """提供全部 6 个横切处理器（用于「依赖齐备不得误报」的反向用例）。"""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def _mark(self, name: str) -> None:
        self.seen.append(name)

    async def on_llm_response(self, event: object) -> None:
        await self._mark("llm_response")

    async def on_tool_done(self, event: object) -> None:
        await self._mark("tool_done")

    async def on_state_change(self, event: object) -> None:
        await self._mark("state_change")

    async def on_terminated(self, event: object) -> None:
        await self._mark("terminated")

    async def on_error(self, event: object) -> None:
        await self._mark("error")

    async def on_degradation(self, event: object) -> None:
        await self._mark("degradation")


class TestBootstrapVisibility:
    @pytest.mark.asyncio
    async def test_missing_dependency_logs_warning(self, caplog) -> None:
        """破坏者视角：把 warning 删掉（退回静默 continue），本组必红。"""
        bus = EventBus()
        deps = Dependencies(bus=bus, config=_cfg())  # cost_tracker/session_store/logger 全 None
        with caplog.at_level(logging.WARNING, logger="bandi.core.event_bus"):
            await bootstrap_event_bus(bus, deps)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "依赖缺失必须产生告警（否则伪接线不可见，FM-100）"
        text = " ".join(r.getMessage() for r in warnings)
        assert "未建立" in text or "伪接线" in text, f"告警须点明未建立订阅：{text!r}"
        assert "LLM_RESPONSE" in text, (
            f"告警须指明缺失的是哪个事件（用事件名而非 .value——EventType 是 IntEnum）：{text!r}"
        )

    @pytest.mark.asyncio
    async def test_warning_not_emitted_when_all_present(self, caplog) -> None:
        """依赖齐备时不得误报（否则告警会被忽略，失去意义）。"""
        bus = EventBus()
        h = _AllHandlers()
        deps = Dependencies(
            bus=bus,
            config=_cfg(),
            cost_tracker=h,  # type: ignore[arg-type]
            session_store=h,  # type: ignore[arg-type]
            logger=h,  # type: ignore[arg-type]
        )
        with caplog.at_level(logging.WARNING, logger="bandi.core.event_bus"):
            await bootstrap_event_bus(bus, deps)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warnings, f"依赖齐备不应告警，实际：{[r.getMessage() for r in warnings]}"
        assert len(h.seen) == 0

    @pytest.mark.asyncio
    async def test_present_dependency_actually_subscribes(self) -> None:
        """反向：依赖在场时订阅必须真的建立（否则是「有对象但没接上」的另一种伪接线）。"""
        bus = EventBus()
        rec = _Recorder()
        deps = Dependencies(
            bus=bus,
            config=_cfg(),
            cost_tracker=rec,  # type: ignore[arg-type]
        )
        await bootstrap_event_bus(bus, deps)
        await bus.emit(Event(type=EventType.LLM_RESPONSE, data={}))
        await bus.emit(Event(type=EventType.TOOL_COMPLETED, data={}))
        assert rec.seen == ["llm_response", "tool_done"], (
            f"依赖在场时订阅未生效（另一种伪接线）：{rec.seen}"
        )

    @pytest.mark.asyncio
    async def test_missing_dependency_does_not_break_bus(self) -> None:
        """缺依赖只告警，**不得**让启动失败（EventBus 仍要能收事件给其它订阅者）。"""
        bus = EventBus()
        deps = Dependencies(bus=bus, config=_cfg())
        await bootstrap_event_bus(bus, deps)  # 不得抛
        received: list[Event] = []

        async def probe(event: object) -> None:
            received.append(event)  # type: ignore[arg-type]

        await bus.subscribe(EventType.LLM_RESPONSE, probe)
        await bus.emit(Event(type=EventType.LLM_RESPONSE, data={}))
        assert len(received) == 1, "缺依赖不得影响其它订阅者收事件"


def _cfg() -> Any:
    from bandi.core.config import BandiConfig

    return BandiConfig()


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent_on_empty_deps() -> None:
    """空依赖重复 bootstrap 不得抛（装配可能多次调用）。"""
    bus = EventBus()
    deps = Dependencies(bus=bus, config=_cfg())
    await bootstrap_event_bus(bus, deps)
    await bootstrap_event_bus(bus, deps)
    assert isinstance(asyncio.get_event_loop_policy(), asyncio.AbstractEventLoopPolicy)
