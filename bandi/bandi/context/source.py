# 功能：RuntimeContextSource Protocol + ContextSourceRegistry

from __future__ import annotations

from typing import Protocol

from bandi.context.item import ContextItem
from bandi.core.message import Message


class RuntimeContextSource(Protocol):
    async def collect(
        self,
        message: Message,
        session_id: str,
        budget: int = 4096,
        intent_hint: str | None = None,
    ) -> list[ContextItem]: ...


class ContextSourceRegistry:
    def __init__(self) -> None:
        self._sources: dict[str, RuntimeContextSource] = {}

    def register(self, name: str, source: RuntimeContextSource) -> None:
        self._sources[name] = source

    def get(self, name: str) -> RuntimeContextSource | None:
        return self._sources.get(name)

    def all(self) -> dict[str, RuntimeContextSource]:
        return dict(self._sources)

    async def collect_all(
        self,
        message: Message,
        session_id: str,
        budget: int = 4096,
        intent_hint: str | None = None,
    ) -> list[ContextItem]:
        items: list[ContextItem] = []
        for name, src in self._sources.items():
            collected = await src.collect(
                message,
                session_id,
                budget // max(len(self._sources), 1),
                intent_hint=intent_hint,
            )
            for c in collected:
                if c.metadata is None:
                    c.metadata = {}
                c.metadata.setdefault("source_name", name)
            items.extend(collected)
        return items
