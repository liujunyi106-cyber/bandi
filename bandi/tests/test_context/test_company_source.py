from __future__ import annotations

import pytest

from bandi.context.sources.company_source import CompanyContextSource
from bandi.core.message import Message, MessageRole


class TestCompanyContextSource:
    @pytest.mark.asyncio
    async def test_no_runtime_returns_empty(self) -> None:
        src = CompanyContextSource()
        msg = Message(role=MessageRole.USER, content="test")
        items = await src.collect(msg, "s-1", budget=4096)
        assert items == []

    @pytest.mark.asyncio
    async def test_returns_empty_with_different_params(self) -> None:
        src = CompanyContextSource()
        msg = Message(role=MessageRole.USER, content="company mode")
        items = await src.collect(msg, "s-999", budget=0)
        assert items == []
